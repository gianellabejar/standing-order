"""Sessions, context pack, governed memory, memory-driven call brief. Offline: mongomock + stub LLM + fake embeddings."""
import math
import zlib
from datetime import timedelta

import mongomock
import pytest

from agent import ledger
from agent.decision import choose_supplier
from agent.memory.brief import build_call_brief
from agent.memory.context_pack import build_context_pack
from agent.memory.embed import count_tokens
from agent.memory.governance import Governance
from agent.sessions.manager import SessionManager
from agent.sessions.summarize import Candidate, CloseResult
from agent.voice import MockVoiceProvider
from shared.db import Memories
from shared.interfaces import Decision
from shared.models import Order, OrderStatus, PriceQuote, Source, Tenant, now

A, B = "maple", "harbour"
SRC = Source(type="sms", ref="SM1")


class FakeEmbedder:
    """Deterministic bag-of-words hash vectors (dim 64): shared words give similar vectors."""
    def embed(self, texts, input_type="document"):
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in t.lower().split():
                v[zlib.crc32(w.encode()) % 64] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class LocalVectorMemories(Memories):
    """mongomock has no $vectorSearch: same tenant/status/expiry filter (self._live), cosine in Python."""
    def search_scored(self, tenant_id, embedding, k=5):
        rows = [d for d in self._c.find(self._live(tenant_id)) if d.get("embedding")]
        sc = [(self.model(**d), sum(a * b for a, b in zip(embedding, d["embedding"]))) for d in rows]
        return sorted(sc, key=lambda p: p[1], reverse=True)[:k]


@pytest.fixture
def repos():
    db = mongomock.MongoClient(tz_aware=True)["t"]
    r = ledger.Repos(db)
    r.memories = LocalVectorMemories(db)
    r.tenants.insert(A, Tenant(name="Maple Bakery", profile="Small bakery in Toronto."))
    r.tenants.insert(B, Tenant(name="Harbour Cafe", profile="Cafe by the harbour."))
    return r


@pytest.fixture
def gov(repos):
    return Governance(repos, FakeEmbedder())


def seed(gov, tenant=A, supplier="supplier_b", discount=8):
    return gov.add(tenant, "terms", f"Supplier B gives Maple Bakery {discount}% off list.", SRC,
                   {"supplier": supplier, "key": "discount_pct", "value": discount}, seeded=True)


# ---------- sessions ----------
def stub_llm(result):
    calls = []
    def f(convo):
        calls.append(convo)
        return result, {"prompt": 120, "completion": 40}
    f.calls = calls
    return f


def test_new_session_after_30_min_silence_has_new_thread_and_never_loads_old_transcript(repos):
    seen = []
    mgr = SessionManager(repos, llm=stub_llm(CloseResult(summary="Ordered flour.")),
                         messages_for=lambda t: seen.append(t) or [{"role": "owner", "text": "40 bags", "source": {"type": "sms", "ref": "SM9"}}])
    t0 = now()
    s1 = mgr.current(A, t0)
    assert mgr.current(A, t0 + timedelta(minutes=29)).id == s1.id            # still the same session
    s2 = mgr.current(A, t0 + timedelta(minutes=29) + timedelta(minutes=31))  # silent 31 min
    assert s2.id != s1.id and s2.thread_id != s1.thread_id
    assert repos.sessions.get(A, s1.id).closed_at is not None
    assert seen == [s1.thread_id]                                            # only the closing session was read
    assert repos.sessions.get(A, s1.id).summary == "Ordered flour."


def test_task_completion_closes_and_close_is_idempotent(repos):
    llm = stub_llm(CloseResult(summary="done"))
    mgr = SessionManager(repos, llm=llm, messages_for=lambda t: [{"role": "owner", "text": "hi", "source": {"type": "sms", "ref": "S"}}])
    s = mgr.current(A)
    assert mgr.complete_task(A, s.id) == "done" and mgr.close(A, s.id) == "done"
    assert len(llm.calls) == 1


def test_empty_session_makes_no_llm_call(repos):
    llm = stub_llm(CloseResult(summary="x"))
    mgr = SessionManager(repos, llm=llm)
    s = mgr.current(A)
    assert mgr.close(A, s.id) == "" and llm.calls == []


def test_summary_is_clamped_and_only_sourced_candidates_become_memories(repos, gov):
    long = "word " * 400
    res = CloseResult(summary=long, candidates=[
        Candidate(text="Supplier B ships Tuesdays.", kind="supplier_pref", structured={"supplier": "supplier_b", "key": "delivery_day", "value": "Tue"}, source_ref="m0"),
        Candidate(text="Invented fact with a bogus source.", source_ref="m99"),
        Candidate(text="Invented fact with no source."),
    ])
    mgr = SessionManager(repos, llm=stub_llm(res), embedder=FakeEmbedder(),
                         messages_for=lambda t: [{"role": "owner", "text": "B ships Tuesdays", "source": {"type": "sms", "ref": "SM7"}}])
    s = mgr.current(A)
    summary = mgr.close(A, s.id)
    assert count_tokens(summary) <= 150
    mems = repos.memories.list_active(A)
    assert [m.text for m in mems] == ["Supplier B ships Tuesdays."]
    assert (mems[0].source.type, mems[0].source.ref) == ("sms", "SM7") and mems[0].embedding
    assert repos.sessions.get(A, s.id).tokens == {"prompt": 120, "completion": 40}
    assert any(e.event == "memory.add" for e in repos.audit.list(A))


# ---------- context pack ----------
def test_context_pack_top5_filters_and_logs_tokens(repos, gov, caplog):
    for i in range(7):
        gov.add(A, "note", f"flour fact number {i} about suppliers", SRC)
    dead = gov.add(A, "note", "flour fact EXPIRED", SRC); gov.expire(A, dead)
    s = SessionManager(repos).current(A)
    with caplog.at_level("INFO", logger="context_pack"):
        pack = build_context_pack(A, s, repos, "flour facts", FakeEmbedder())
    assert len(pack.memory_ids) == 5 and dead not in pack.memory_ids
    assert "Small bakery in Toronto." in pack.text and "EXPIRED" not in pack.text
    assert repos.sessions.get(A, s.id).context_tokens == pack.tokens == count_tokens(pack.text)
    assert "context pack tenant=maple" in caplog.text and f"tokens={pack.tokens}/1500" in caplog.text


def test_context_pack_cap_drops_lowest_scoring_memories_first(repos, gov):
    best = gov.add(A, "note", "flour flour flour price", SRC)
    mid = gov.add(A, "note", "flour delivery tuesday morning only for bakery", SRC)
    worst = gov.add(A, "note", "unrelated parking permit renewal reminder text", SRC)
    s = SessionManager(repos).current(A)
    full = build_context_pack(A, s, repos, "flour price", FakeEmbedder())
    cap = full.tokens - 1
    tight = build_context_pack(A, s, repos, "flour price", FakeEmbedder(), max_tokens=cap)
    assert tight.tokens <= cap and tight.dropped_memory_ids == [worst] and best in tight.memory_ids and mid in tight.memory_ids


def test_context_pack_hard_cap_even_with_huge_summary(repos):
    mgr = SessionManager(repos, llm=stub_llm(CloseResult(summary="long " * 100)),
                         messages_for=lambda t: [{"role": "owner", "text": "x", "source": {"type": "sms", "ref": "S"}}])
    mgr.close(A, mgr.current(A).id)
    s = mgr.current(A)
    pack = build_context_pack(A, s, repos, "", None, max_tokens=60)
    assert pack.tokens <= 60


def test_context_pack_never_leaks_across_tenants(repos, gov):
    seed(gov, A)
    gov.add(B, "note", "Harbour only fact", SRC)
    sb = SessionManager(repos).current(B)
    pack = build_context_pack(B, sb, repos, "Supplier B discount off list", FakeEmbedder())
    assert "Maple" not in pack.text and "8%" not in pack.text and "Harbour only fact" in pack.text


# ---------- governance ----------
def test_correction_supersedes_keeps_history_and_changes_the_decision(repos, gov):
    old = seed(gov, discount=8)
    qs = [PriceQuote(tenant_id=A, supplier_id=s, sku="FLR", unit="bag", price=p, valid_until=now() + timedelta(days=3), source={"document_id": "d"})
          for s, p in (("supplier_a", 3.45), ("supplier_b", 3.48))]
    for q in qs: repos.quotes.insert(A, q)
    items = [{"sku": "FLR", "qty": 40}]
    assert choose_supplier(items, repos.quotes.list(A), repos.memories.list_active(A)).supplier_id == "supplier_b"
    new = gov.correct(A, old, "Supplier B no longer gives a discount.", Source(type="sms", ref="SM2"),
                      {"supplier": "supplier_b", "key": "discount_pct", "value": 0})
    assert repos.memories.get(A, old).status.value == "superseded" and repos.memories.get(A, new).supersedes == old
    assert [m.id for m in gov.history(A, new)] == [new, old]
    assert choose_supplier(items, repos.quotes.list(A), repos.memories.list_active(A)).supplier_id == "supplier_a"
    assert any(e.event == "memory.supersede" and e.ids == [old, new] for e in repos.audit.list(A))
    with pytest.raises(ValueError):
        gov.correct(A, old, "again", Source(type="sms", ref="SM3"))   # superseded memories cannot be corrected again


def test_add_requires_a_source(gov):
    with pytest.raises(ValueError):
        gov.add(A, "note", "no source", None)


def test_forget_deletes_content_keeps_tombstone_and_sets_purge(repos, gov):
    mid = seed(gov)
    gov.forget(A, mid)
    m = repos.memories.get(A, mid)
    assert m.text == "" and m.structured == {} and m.embedding is None and m.status.value == "retracted"
    assert m.purge_at is not None and m.purge_at > now() and repos.memories.list_active(A) == []
    tomb = [e for e in repos.audit.list(A) if e.event == "memory.forget"]
    assert tomb and tomb[0].ids == [mid] and tomb[0].tombstone == {"memory_id": mid}


def test_expire_excludes_from_retrieval_and_can_schedule_purge(repos, gov):
    mid = seed(gov)
    gov.expire(A, mid, purge_after=timedelta(days=3))
    m = repos.memories.get(A, mid)
    assert repos.memories.list_active(A) == [] and m.expires_at <= now() and m.purge_at > now()


def test_tenant_b_cannot_correct_expire_forget_or_read_history_of_a_memory(repos, gov):
    mid = seed(gov, A)
    for fn in (lambda: gov.correct(B, mid, "x", SRC), lambda: gov.expire(B, mid), lambda: gov.forget(B, mid),
               lambda: gov.history(B, mid)):
        with pytest.raises(LookupError):
            fn()
    m = repos.memories.get(A, mid)
    assert m.status.value == "active" and m.text and repos.audit.list(B) == []


# ---------- call brief ----------
def memories_for_b(gov):
    ids = [gov.add(A, "supplier_pref", t, SRC, {"supplier": "supplier_b", "key": k, "value": v}, seeded=True)
           for t, k, v in (("Supplier B takes orders by phone.", "order_channel", "phone"),
                           ("When calling Supplier B, ask for Dave.", "contact_name", "Dave"),
                           ("Supplier B wants PO numbers like MB-0000.", "po_format", "MB-{:04d}"))]
    return ids


def test_brief_uses_memory_for_contact_po_and_channel(repos, gov):
    ids = memories_for_b(gov)
    o = ledger.create_order(A, Order(tenant_id=A, idempotency_key="k", items=[{"sku": "FLR", "name": "all-purpose flour", "qty": 40}]), repos)
    b = build_call_brief(A, o, Decision(supplier_id="supplier_b", unit_price=3.2), repos)
    assert (b["contact_name"], b["po_number"], b["channel"], b["item"], b["quantity"], b["price"], b["shop_name"]) == \
        ("Dave", "MB-0001", "phone", "all-purpose flour", 40, 3.2, "Maple Bakery")
    assert sorted(b["memories_used"]) == sorted(ids)


def test_place_with_memory_calls_with_the_brief_and_records_receipt(repos, gov):
    ids = memories_for_b(gov)
    o = ledger.create_order(A, Order(tenant_id=A, idempotency_key="k", items=[{"sku": "FLR", "qty": 40}]), repos)
    prov = MockVoiceProvider(delay=None, post=lambda p: None)
    cid = ledger.place_with_memory(A, o.id, Decision(supplier_id="supplier_b", unit_price=3.2), repos, provider=prov)
    got = repos.orders.get(A, o.id)
    brief = repos.calls.get_by_conversation(A, cid).brief
    assert got.status == OrderStatus.calling and got.po_number == "MB-0001" and sorted(got.memories_used) == sorted(ids)
    assert brief["contact_name"] == "Dave" and brief["po_number"] == "MB-0001" and brief["order_id"] == o.id
    assert "channel" not in brief and "memories_used" not in brief
    o2 = ledger.create_order(A, Order(tenant_id=A, idempotency_key="k2", items=[{"sku": "FLR", "qty": 1}]), repos)
    assert build_call_brief(A, o2, Decision(supplier_id="supplier_b", unit_price=3.2), repos)["po_number"] == "MB-0002"


def test_supplier_without_phone_memory_is_not_called(repos, gov):
    o = ledger.create_order(A, Order(tenant_id=A, idempotency_key="k", items=[{"sku": "FLR", "qty": 1}]), repos)
    prov = MockVoiceProvider(delay=None, post=lambda p: None)
    assert ledger.place_with_memory(A, o.id, Decision(supplier_id="supplier_a", unit_price=3.45), repos, provider=prov) is None
    got = repos.orders.get(A, o.id)
    assert got.status == OrderStatus.needs_owner and "phone" in got.needs_owner_reason and prov.pending == {}


def test_memories_without_a_vector_still_reach_the_pack(repos, gov):
    """If embedding was rate-limited when a memory was written it has no vector; Vector Search cannot see it,
    so the pack tops up with such memories instead of silently losing them."""
    vec = gov.add(A, "note", "flour price memory with a vector", SRC)
    no_vec = repos.memories.insert(A, __import__("shared.models", fromlist=["Memory"]).Memory(
        tenant_id=A, kind="note", text="memory saved while embedding failed", source=SRC))
    s = SessionManager(repos).current(A)
    pack = build_context_pack(A, s, repos, "flour price", FakeEmbedder())
    assert set(pack.memory_ids) == {vec, no_vec}


def test_embedder_retries_on_429(monkeypatch):
    import httpx
    from agent.memory import embed as E
    calls = []
    class R:
        def __init__(s, code): s.status_code, s.headers = code, {"retry-after": "0"}
        def raise_for_status(s):
            if s.status_code >= 400: raise httpx.HTTPStatusError("x", request=None, response=s)
        def json(s): return {"data": [{"index": 0, "embedding": [0.5]}]}
    monkeypatch.setattr(E.httpx, "post", lambda *a, **k: calls.append(1) or R(429 if len(calls) < 3 else 200))
    assert E.VoyageEmbedder("k", retries=3).embed(["x"]) == [[0.5]] and len(calls) == 3


def test_candidate_sourced_from_the_agents_own_message_is_rejected(repos):
    msgs = [{"role": "owner", "text": "Need flour", "source": {"type": "sms", "ref": "SM1"}},
            {"role": "agent", "text": "B is $3.20 with your discount", "source": {"type": "session", "ref": "t#1"}}]
    res = CloseResult(summary="s", candidates=[Candidate(text="Flour costs $3.20 from B.", source_ref="[m1]"),
                                               Candidate(text="Owner needs flour regularly.", source_ref="[m0]")])
    mgr = SessionManager(repos, llm=stub_llm(res), messages_for=lambda t: msgs)
    mgr.close(A, mgr.current(A).id)
    assert [m.text for m in repos.memories.list_active(A)] == ["Owner needs flour regularly."]
