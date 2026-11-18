"""Voice mock + ledger + decision tests. In-memory Mongo (mongomock) runs the real repo code."""
from datetime import timedelta

import mongomock
import pytest
from fastapi.testclient import TestClient

from agent import ledger
from agent.app import app
from agent.decision import choose_supplier, memory_impact, replay_without_memory
from agent.fixtures import SCENARIOS, post_call_payload
from agent.voice import MockVoiceProvider, parse_post_call_payload
from shared.db import get_db
from shared.interfaces import CallOutcome
from shared.models import Memory, Order, OrderStatus, PriceQuote, Source, now

A, B = "maple", "harbour"
BRIEF = {"shop_name": "Sean's Bakery", "quantity": 40, "unit": "bag", "item": "flour", "price": 3.2}


@pytest.fixture
def db():
    d = mongomock.MongoClient(tz_aware=True)["t"]
    d.orders.create_index([("tenant_id", 1), ("idempotency_key", 1)], unique=True)
    return d


@pytest.fixture
def env(db):
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    # mock delivers by hand (delay=None) through the real HTTP endpoint
    prov = MockVoiceProvider(delay=None, post=lambda p: client.post("/voice/post-call", json=p))
    yield db, ledger.Repos(db), prov, client
    app.dependency_overrides.clear()


def planned(repos, tenant=A, key="k1"):
    return ledger.create_order(tenant, Order(tenant_id=tenant, idempotency_key=key), repos)


def run_call(repos, prov, scenario, tenant=A, key="k1"):
    prov.scenario = scenario
    o = planned(repos, tenant, key)
    cid = ledger.start_call(tenant, o.id, dict(BRIEF), repos, provider=prov)
    prov.deliver(cid)
    return repos.orders.get(tenant, o.id), cid


@pytest.mark.parametrize("scenario,status,needle", [
    ("confirmed", OrderStatus.confirmed, None),
    ("ended_without_number", OrderStatus.needs_owner, "without_confirmation_number"),
    ("supplier_asks_question", OrderStatus.needs_owner, "supplier_asked_question"),
    ("no_answer", OrderStatus.needs_owner, "no_answer"),
])
def test_scenarios_end_to_end(env, scenario, status, needle):
    _, repos, prov, _ = env
    order, cid = run_call(repos, prov, scenario)
    assert order.status == status
    assert repos.calls.get_by_conversation(A, cid).provider == "mock"
    if needle:
        assert needle in order.needs_owner_reason and order.confirmation_number is None
    else:
        assert order.confirmation_number == "MB-48213"


def test_confirmed_requires_number_in_transcript(env):
    _, repos, prov, _ = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    forged = CallOutcome(status="confirmed", confirmation_number="ZZ-999",
                         transcript=post_call_payload("ended_without_number", cid, BRIEF)["data"]["transcript"])
    assert ledger.apply_outcome(A, cid, forged, repos).status == OrderStatus.needs_owner


def test_supplier_must_say_it_agent_turns_do_not_count():
    p = post_call_payload("no_answer", "c", BRIEF)
    p["data"]["transcript"][0]["message"] += " Confirmation number is AI-123."
    assert parse_post_call_payload(p).status == "needs_owner"


def test_idempotency(env):
    db, repos, prov, _ = env
    assert planned(repos).id == planned(repos).id and db.orders.count_documents({}) == 1
    o = planned(repos)
    c1 = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    assert ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov) == c1
    assert len(prov.pending) == 1  # second start placed no second call
    payload = prov.pending[c1]
    prov.deliver(c1)
    r = env[3].post("/voice/post-call", json=payload)  # duplicate webhook
    assert r.status_code == 200 and db.calls.count_documents({}) == 1 and db.audit_events.count_documents({}) == 1
    assert repos.orders.get(A, o.id).status == OrderStatus.confirmed


def test_webhook_for_other_tenants_call_is_rejected(env):
    _, repos, prov, client = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    p = prov.pending[cid]
    p["data"]["conversation_initiation_client_data"]["dynamic_variables"]["tenant_id"] = B
    assert client.post("/voice/post-call", json=p).status_code == 404
    assert repos.orders.get(A, o.id).status == OrderStatus.calling


def test_every_mock_scenario_has_fixture():
    assert set(SCENARIOS) == {"confirmed", "ended_without_number", "supplier_asks_question", "no_answer"}


# ---- decision, receipts, replay ----
def quote(sup, sku, price, days=7, tenant=A):
    return PriceQuote(tenant_id=tenant, supplier_id=sup, sku=sku, unit="bag", price=price,
                      valid_until=now() + timedelta(days=days), source={"document_id": "d"})


def seed(repos):
    q = {k: repos.quotes.insert(A, v) for k, v in {"a": quote("A", "FLOUR", 3.45), "b": quote("B", "FLOUR", 3.48),
                                                  "ao": quote("A", "OATS", 2.60), "bo": quote("B", "OATS", 2.90)}.items()}
    m = repos.memories.insert(A, Memory(tenant_id=A, kind="terms", text="Gianella's Ingredients gives Maple 8% off list",
                                        structured={"supplier": "B", "key": "discount_pct", "value": 8},
                                        source=Source(type="sms", ref="seed"), seeded=True))
    return q, m


def test_memory_changes_decision_and_replay(db):
    repos = ledger.Repos(db)
    q, m = seed(repos)
    quotes, mems = repos.quotes.list(A), repos.memories.list_active(A)
    items = [{"sku": "FLOUR", "qty": 40}]
    with_m, without = choose_supplier(items, quotes, mems), choose_supplier(items, quotes, [])
    assert (with_m.supplier_id, with_m.unit_price, with_m.memories_used) == ("B", 3.20, [m])
    assert (without.supplier_id, without.unit_price, without.memories_used) == ("A", 3.45, [])
    o = ledger.create_order(A, Order(tenant_id=A, idempotency_key="f", items=items, supplier_id="B",
                                     memories_used=[m], quotes_used=[q["a"], q["b"]]), repos)
    r_with, r_without = replay_without_memory(A, o.id, repos)
    assert (r_with.supplier_id, r_without.supplier_id) == ("B", "A")
    assert repos.orders.get(A, o.id).memories_used == [m]  # replay does not alter the receipt


def test_expired_quote_ignored_and_memory_impact_counts(db):
    repos = ledger.Repos(db)
    q, m = seed(repos)
    old = repos.quotes.insert(A, quote("A", "FLOUR", 1.00, days=-1))  # expired: must not win
    flour = [{"sku": "FLOUR", "qty": 40}]
    assert choose_supplier(flour, repos.quotes.list(A), []).supplier_id == "A"
    ledger.create_order(A, Order(tenant_id=A, idempotency_key="1", items=flour, memories_used=[m],
                                 quotes_used=[q["a"], q["b"], old]), repos)
    oats = [{"sku": "OATS", "qty": 10}]  # A is cheaper even with B's 8%: memory does not change it
    ledger.create_order(A, Order(tenant_id=A, idempotency_key="2", items=oats, memories_used=[m],
                                 quotes_used=[q["ao"], q["bo"]]), repos)
    assert memory_impact(A, repos) == (1, 2)
    assert memory_impact(B, repos) == (0, 0)


# ---- ElevenLabs provider (no network) ----
def test_elevenlabs_refuses_numbers_outside_allowlist(monkeypatch):
    from agent.voice import ElevenLabsVoiceProvider
    monkeypatch.setenv("TEST_SUPPLIER_PHONE", "+15550000001")
    monkeypatch.delenv("SUPPLIER_B_PHONE", raising=False)
    with pytest.raises(ValueError):
        ElevenLabsVoiceProvider().place_call(Order(tenant_id=A, idempotency_key="x"), {"to_number": "+15559999999"})


def test_webhook_with_bad_signature_is_rejected(db, monkeypatch):
    import agent.voice as v
    monkeypatch.setattr(v, "_provider", v.ElevenLabsVoiceProvider())
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", "s")
    app.dependency_overrides[get_db] = lambda: db
    try:
        r = TestClient(app).post("/voice/post-call", json={}, headers={"elevenlabs-signature": "t=1,v0=bad"})
        assert r.status_code == 401
        assert TestClient(app).post("/voice/post-call", json={}).status_code == 401  # no header
    finally:
        app.dependency_overrides.clear()


def test_parser_accepts_get_conversation_shape():
    p = post_call_payload("confirmed", "c", BRIEF)["data"]  # GET response has these fields at top level
    assert parse_post_call_payload(p).confirmation_number == "MB-48213"


def test_real_no_answer_response_maps_to_needs_owner():
    """fixtures/real_no_answer.json: a REAL GET /conversations response (phone numbers redacted)."""
    import json
    out = parse_post_call_payload(json.load(open("fixtures/real_no_answer.json")))
    assert (out.status, out.confirmation_number) == ("needs_owner", None) and "no_answer" in out.needs_owner_reason


def test_inline_delivery_posts_after_call_record_exists(env, monkeypatch):
    """Serverless mode: no Timer thread; the payload is delivered inside start_call, after the call is saved."""
    db, repos, _, client = env
    monkeypatch.setenv("MOCK_VOICE_DELIVERY", "inline")
    prov = MockVoiceProvider(delay=1.0, post=lambda p: client.post("/voice/post-call", json=p))
    o = planned(repos)
    ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    assert repos.orders.get(A, o.id).status == OrderStatus.confirmed and prov.pending == {}


def test_combined_app_serves_sms_and_voice_routes(monkeypatch):
    import app as root
    from shared import db as dbmod
    mem = mongomock.MongoClient(tz_aware=True)["t"]
    monkeypatch.setattr(dbmod, "_db", lambda: mem)  # no Atlas needed: covers Depends(get_db) and direct repo use
    c = TestClient(root.app)
    assert c.get("/healthz").status_code == 200
    assert c.post("/sms/inbound", data={}).status_code != 404   # exists (rejects the empty request)
    assert c.post("/voice/post-call", json={}).status_code != 404


def test_real_voicemail_call_is_needs_owner_with_no_number(env):
    """fixtures/real_post_call.json: a REAL call (redacted) that reached a carrier voicemail."""
    import json
    real = json.load(open("fixtures/real_post_call.json"))
    out = parse_post_call_payload(real)
    assert real["status"] == "done" and len(real["transcript"]) == 30
    assert (out.status, out.confirmation_number) == ("needs_owner", None) and "voicemail" in out.needs_owner_reason
    # through the ledger: the same transcript can never confirm an order
    _, repos, prov, _ = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    assert ledger.apply_outcome(A, cid, out, repos).status == OrderStatus.needs_owner
    assert find_number(real["transcript"]) is None


def test_real_dynamic_variables_are_echoed_back():
    import json
    dv = json.load(open("fixtures/real_post_call.json"))["conversation_initiation_client_data"]["dynamic_variables"]
    assert dv["shop_name"] == "Maple Bakery" and dv["po_number"] == "TEST-0001"  # the real call, made before the rename and dv["quantity"] == 1


def find_number(transcript):
    from agent.voice import find_confirmation_number
    return find_confirmation_number(transcript)


def test_raw_post_call_payload_is_stored_on_the_call(env):
    _, repos, prov, _ = env
    order, cid = run_call(repos, prov, "confirmed")
    raw = repos.calls.get_by_conversation(A, cid).raw_post_call
    assert raw["type"] == "post_call_transcription" and raw["data"]["conversation_id"] == cid


def test_real_answered_call_terse_number_is_confirmed(env):
    """fixtures/real_supplier_answered.json: a REAL call (redacted) where a person answered "Yes, it's 1234"
    after the agent asked for the confirmation number. The call itself ended in a platform error (status failed)."""
    import json
    real = json.load(open("fixtures/real_supplier_answered.json"))
    out = parse_post_call_payload(real)
    assert real["status"] == "failed" and (out.status, out.confirmation_number) == ("confirmed", "1234")
    _, repos, prov, _ = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    assert ledger.apply_outcome(A, cid, out, repos).confirmation_number == "1234"


def test_number_only_counts_after_agent_asks_and_from_supplier():
    from agent.voice import find_confirmation_number as f
    T = lambda r, m: {"role": r, "message": m}
    assert f([T("agent", "Delivery date?"), T("user", "Tomorrow, 2 pm")]) is None            # not asked
    assert f([T("agent", "Your confirmation number?"), T("user", "Let me check")]) is None    # no digits
    assert f([T("agent", "Your confirmation number?"), T("user", "It is MB-48213")]) == "MB-48213"
    assert f([T("agent", "My confirmation number is AI-123")]) is None                        # agent turn


def test_real_webhook_voicemail_through_the_endpoint(env):
    """fixtures/real_webhook_voicemail.json: a REAL webhook body (redacted), replayed through /voice/post-call
    with the mock provider (no signature check in mock mode; the real signature path ran on Vercel)."""
    import json
    real = json.load(open("fixtures/real_webhook_voicemail.json"))
    _, repos, prov, client = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    real["data"]["conversation_id"] = cid
    dv = real["data"]["conversation_initiation_client_data"]["dynamic_variables"]
    dv["tenant_id"], dv["order_id"] = A, o.id
    r = client.post("/voice/post-call", json=real)
    assert r.status_code == 200 and r.json()["status"] == "needs_owner"
    got = repos.orders.get(A, o.id)
    assert "voicemail" in got.needs_owner_reason and got.confirmation_number is None
    assert repos.calls.get_by_conversation(A, cid).raw_post_call["type"] == "post_call_transcription"


def test_real_webhook_with_confirmation_number_through_the_endpoint(env):
    """fixtures/real_webhook_confirmed.json: a REAL webhook (redacted), a person gave "The number is 1234"."""
    import json
    real = json.load(open("fixtures/real_webhook_confirmed.json"))
    assert real["data"]["status"] == "done" and real["type"] == "post_call_transcription"
    _, repos, prov, client = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    real["data"]["conversation_id"] = cid
    dv = real["data"]["conversation_initiation_client_data"]["dynamic_variables"]
    dv["tenant_id"], dv["order_id"] = A, o.id
    r = client.post("/voice/post-call", json=real)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    assert repos.orders.get(A, o.id).confirmation_number == "1234"


# --- confirmation numbers in REAL call transcripts (fixtures/real_transcripts_confirmation.json) -------------

def _real_calls():
    import json
    return json.load(open("fixtures/real_transcripts_confirmation.json", encoding="utf-8"))["calls"]


def _webhook(transcript):
    return {"type": "post_call_transcription", "data": {"status": "done", "transcript": transcript}}


@pytest.mark.parametrize("call", _real_calls(), ids=lambda c: c["expected_confirmation_number"])
def test_real_calls_confirmation_number_is_found(call):
    out = parse_post_call_payload(_webhook(call["transcript"]))
    assert (out.status, out.confirmation_number) == ("confirmed", call["expected_confirmation_number"]), call["case"]


@pytest.mark.parametrize("call", _real_calls(), ids=lambda c: c["expected_confirmation_number"])
def test_real_calls_confirm_the_order_in_the_ledger(env, call):
    _, repos, prov, _ = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    done = ledger.apply_outcome(A, cid, parse_post_call_payload(_webhook(call["transcript"])), repos)
    assert done.status == OrderStatus.confirmed and done.confirmation_number == call["expected_confirmation_number"]


def test_spoken_number_through_the_endpoint_and_the_owner_text(env):
    from agent.orders.flow import notify_outcome
    spoken = next(c for c in _real_calls() if c["expected_confirmation_number"] == "1234")
    _, repos, prov, client = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    body = _webhook(spoken["transcript"])
    body["data"].update(conversation_id=cid, conversation_initiation_client_data={"dynamic_variables": {"tenant_id": A}})
    r = client.post("/voice/post-call", json=body)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    sent = []
    text = notify_outcome(repos, A, o.id, send=lambda tid, to, b: sent.append(b), notes_llm=lambda brief, t: None)
    assert "Confirmation number 1234" in (text or sent[-1]) and "needs you" not in (text or sent[-1])


def test_spoken_digits_and_what_does_not_count():
    from agent.voice import find_confirmation_number as f, spoken_digits
    T = lambda r, m: {"role": r, "message": m}
    assert spoken_digits("one, two, three, four.") == "1234." and spoken_digits("one eight eight seven six") == "18876"
    assert spoken_digits("Oh, one two three") == "Oh, 123"            # a leading "oh" is a word, not a zero
    assert spoken_digits("I need two bags, one or two more") == "I need two bags, one or two more"
    ask = T("agent", "Could you please provide your order confirmation number?")
    assert f([ask, T("user", "Uh, uh-"), T("agent", "[Friendly]..."), T("user", "18876.")]) == "18876"  # filler kept the question open
    assert f([ask, T("user", "It arrives on the 18th")]) is None       # an ordinal date is not a number
    assert f([T("agent", "The purchase order number is M B zero zero zero five."),
              T("user", "Yes, I can confirm.")]) is None               # the agent's PO number never counts
    assert f([ask, T("user", "PO 5521, and the confirmation is 77310"),
              T("agent", "I have that as seven seven three one zero.")]) == "77310"


def test_ledger_guard_still_rejects_a_number_the_supplier_never_said(env):
    _, repos, prov, _ = env
    o = planned(repos)
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    said = [{"role": "agent", "message": "Your confirmation number?"}, {"role": "user", "message": "one, two, three, four"}]
    forged = CallOutcome(status="confirmed", confirmation_number="9999", transcript=said)
    assert ledger.apply_outcome(A, cid, forged, repos).status == OrderStatus.needs_owner


def test_reparse_fixes_stored_calls_once_and_only_with_the_suppliers_number(env, db):
    import sys
    sys.path.insert(0, "scripts")
    from reparse_calls import reparse
    _, repos, prov, _ = env
    db.tenants.insert_one({"_id": A, "name": "Test shop"})
    spoken = next(c for c in _real_calls() if c["expected_confirmation_number"] == "1234")
    o = planned(repos, key="k-spoken")
    cid = ledger.start_call(A, o.id, dict(BRIEF), repos, provider=prov)
    stale = CallOutcome(status="needs_owner", transcript=spoken["transcript"],          # what the old parser recorded
                        needs_owner_reason="call_ended_without_confirmation_number")
    raw = _webhook(spoken["transcript"])
    ledger.apply_outcome(A, cid, stale, repos, raw=raw)
    o2 = planned(repos, key="k-none")
    cid2 = ledger.start_call(A, o2.id, dict(BRIEF), repos, provider=prov)
    nothing = post_call_payload("ended_without_number", cid2, BRIEF)
    ledger.apply_outcome(A, cid2, parse_post_call_payload(nothing), repos, raw=nothing)

    assert [c["number"] for c in reparse(db)] == ["1234"] and repos.orders.get(A, o.id).status == OrderStatus.needs_owner
    assert [c["number"] for c in reparse(db, apply=True)] == ["1234"]
    fixed = repos.orders.get(A, o.id)
    assert fixed.status == OrderStatus.confirmed and fixed.confirmation_number == "1234" and fixed.needs_owner_reason is None
    assert repos.calls.get_by_conversation(A, cid).raw_post_call == raw                 # stored webhook kept
    assert repos.orders.get(A, o2.id).status == OrderStatus.needs_owner                  # no number: untouched
    assert reparse(db, apply=True) == []                                                 # idempotent
