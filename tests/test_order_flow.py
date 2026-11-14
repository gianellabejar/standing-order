"""The order flow end to end on the real seed data (mongomock), with a scripted parser (stands in for the one LLM
call), the mock voice provider and a recording SMS sender. No network."""
import mongomock
import pytest
from fastapi.testclient import TestClient

from agent import ledger
from agent.app import app
from agent.decision import choose_supplier, replay_without_memory
from agent.orders import flow as F
from agent.orders.outcome import CallNotes
from agent.orders.parse import Correction, Item, ParsedText
from agent.sessions.manager import SessionManager
from agent.voice import MockVoiceProvider
from scripts.seed import seed as seedmod

SEEDED_ORDERS = ["ord_seed_maple_oats", "ord_seed_maple_prev"]  # scripts/seed/demo_orders.py
from shared.db import get_db
from shared.models import OrderStatus

M, H = "maple", "harbour"
FLOUR, OATS = "FLR-AP-2KG", "OAT-RL-1KG"
USAGE = {"prompt": 40, "completion": 20}

SCRIPT = {  # owner text -> what the LLM call would return (stand-in)
    "Need 40 bags of flour for Friday, same as last time.": ParsedText(
        intent="order", items=[Item(name="flour", qty=40)], same_as_last_time=True, deadline="Friday"),
    "Same as last time": ParsedText(intent="order", same_as_last_time=True),
    "Oats from Supplier B are $2.80 now": ParsedText(intent="price_correction",   # seen from the real model
                                                     correction=Correction(sku=OATS, supplier="Supplier B", new_price=2.80)),
    "Oats from B are $2.80 now": ParsedText(intent="price_correction",
                                            correction=Correction(sku=OATS, supplier="B", new_price=2.80)),
    "Add 10 bags of oats to Friday's order": ParsedText(intent="add_to_order", items=[Item(name="oats", sku=OATS, qty=10)]),
    "What did we pay for flour last time?": ParsedText(intent="question", question_sku=FLOUR),
    "Forget that B gives us a discount": ParsedText(intent="forget", forget_target="that B gives us a discount"),
    "Need 5 bags of quinoa": ParsedText(intent="order", items=[Item(name="quinoa", sku=None, qty=5)]),
    "Need 5 bags of quinoa please": ParsedText(intent="other", items=[Item(name="quinoa", qty=5)]),  # seen from the real model
    "hello": ParsedText(intent="other"),
}


def scripted(text, context, skus):
    return SCRIPT[text], USAGE


@pytest.fixture
def world(monkeypatch):
    db = mongomock.MongoClient(tz_aware=True)["t"]
    seedmod.seed(db)
    repos = ledger.Repos(db)
    prov = MockVoiceProvider(delay=None, post=lambda p: None)
    flow = F.OrderFlow(repos, SessionManager(repos), parser=scripted, provider=prov)
    sent = []
    monkeypatch.setattr("ingest.sms.send_sms", lambda tenant, to, body: sent.append((tenant, to, body)))
    monkeypatch.setattr(F, "llm_notes", lambda brief, transcript: (CallNotes(), USAGE))
    return flow, repos, prov, sent, db


def say(flow, text, tenant=M, sid="SM1"):
    return flow.handle(tenant, text, sid)


def test_order_text_is_priced_from_memory_and_needs_approval(world):
    flow, r, prov, sent, db = world
    reply = say(flow, "Need 40 bags of flour for Friday, same as last time.")
    assert "Supplier B" in reply and "$3.20" in reply and "$128.00" in reply and "8%" in reply and "Reply YES" in reply
    o = r.orders.awaiting_approval(M)
    assert o.status == OrderStatus.planned and o.supplier_id == "supplier_b" and (o.unit_price, o.total) == (3.20, 128.0)
    mem_keys = {r.memories.get(M, i).structured["key"] for i in o.memories_used}
    assert {"discount_pct", "alias:flour", "approval_threshold"} <= mem_keys            # the receipt
    assert {r.quotes.get(M, q).supplier_id for q in o.quotes_used} == {"supplier_a", "supplier_b"}  # all considered
    assert o.tokens["prompt"] == 40 and o.tokens["context_pack_est"] > 0 and o.session_id
    with_m, without = replay_without_memory(M, o.id, r)
    assert (with_m.supplier_id, without.supplier_id, without.unit_price) == ("supplier_b", "supplier_a", 3.45)
    assert prov.pending == {}                                                          # no call before YES


def test_yes_places_the_call_with_the_memory_brief_and_confirmed_only_with_a_number(world):
    flow, r, prov, sent, db = world
    say(flow, "Need 40 bags of flour for Friday, same as last time.")
    reply = say(flow, "YES", sid="SM2")
    assert reply.startswith("Calling Supplier B")
    new = [x for x in r.orders.list(M) if x.status == OrderStatus.calling][0]
    brief = r.calls.get_by_conversation(M, new.call_id).brief
    assert (brief["contact_name"], brief["po_number"], brief["quantity"], brief["price"], brief["item"]) == \
        ("Dave", "MB-0003", 40, 3.2, "flour")  # MB-0001/0002 are the seeded orders
    assert sent == []                                                                  # nothing texted until the call ends
    from agent.fixtures import post_call_payload
    out = prov.parse_post_call(post_call_payload("confirmed", new.call_id, brief))   # what the webhook would deliver
    ledger.apply_outcome(M, new.call_id, out, r)
    body = F.notify_outcome(r, M, new.id, send=lambda t, to, b: sent.append((t, to, b)))
    done = r.orders.get(M, new.id)
    assert done.status == OrderStatus.confirmed and done.confirmation_number == "MB-48213"
    assert "Confirmation number MB-48213" in body and "PO MB-0003" in body and "$3.20" in body
    assert sent and sent[0][0] == M and sent[0][1] == r.tenants.get(M).phones[0]


def test_price_correction_supersedes_the_quote_and_the_add_uses_the_new_price(world):
    flow, r, prov, sent, db = world
    say(flow, "Need 40 bags of flour for Friday, same as last time.")
    reply = say(flow, "Oats from B are $2.80 now", sid="SM2")
    assert "$2.80" in reply and "was $3.10" in reply
    old = r.quotes.get(M, f"q_{M}_supplier_b_{OATS}")
    new = [q for q in r.quotes.list_valid(M, OATS) if q.supplier_id == "supplier_b"][0]
    assert old.status == "superseded" and new.price == 2.80 and new.supersedes == old.id   # history kept
    assert old.superseded_by == new.id                                                     # linked both ways
    assert any(e.event == "quote.supersede" and e.ids == [old.id, new.id] for e in r.audit.list(M))
    reply = say(flow, "Add 10 bags of oats to Friday's order", sid="SM3")
    pending = r.orders.awaiting_approval(M)
    assert {(i["sku"], i["qty"]) for i in pending.items} == {(FLOUR, 40), (OATS, 10)} and "oats" in reply
    assert new.id in pending.quotes_used and old.id not in pending.quotes_used        # priced from the corrected quote
    assert len([o for o in r.orders.list(M) if o.status == OrderStatus.planned]) == 1  # merged, not a second order


def test_question_and_unclear_requests_create_no_order(world):
    flow, r, *_ = world
    reply = say(flow, "What did we pay for flour last time?")
    assert "MB-48213" in reply and "$3.20" in reply          # the seeded prior order has its number and price
    r.orders.update_fields(M, "ord_seed_maple_prev", unit_price=None)
    assert "wasn't recorded" in say(flow, "What did we pay for flour last time?", sid="SM2")
    assert "don't have a current price for 'quinoa'" in say(flow, "Need 5 bags of quinoa", sid="SM3")
    assert "don't have a current price for 'quinoa'" in say(flow, "Need 5 bags of quinoa please", sid="SM5")
    assert "place orders" in say(flow, "hello", sid="SM4")
    assert sorted(o.id for o in r.orders.list(M)) == SEEDED_ORDERS


def test_forgetting_the_discount_changes_the_next_decision_and_leaves_a_tombstone(world):
    flow, r, prov, sent, db = world
    assert "Supplier B" in say(flow, "Need 40 bags of flour for Friday, same as last time.")
    say(flow, "NO", sid="SM2")                                                          # owner declines the first one
    assert r.orders.awaiting_approval(M) is None
    assert say(flow, "Forget that B gives us a discount", sid="SM3") .startswith("Forgotten")
    gone = r.memories.get(M, "mem_maple_b_discount")
    assert gone.text == "" and gone.status.value == "retracted"
    assert any(e.event == "memory.forget" and e.tombstone == {"memory_id": "mem_maple_b_discount"} for e in r.audit.list(M))
    reply = say(flow, "Same as last time", sid="SM4")
    assert "Supplier A" in reply and "$3.45" in reply and "$138.00" in reply            # without the memory A wins
    reply = say(flow, "YES", sid="SM5")
    a = [o for o in r.orders.list(M) if o.supplier_id == "supplier_a"][0]
    assert a.status == OrderStatus.needs_owner and "email ordering is not built" in a.needs_owner_reason
    assert "by phone yet" in reply and prov.pending == {}                               # A takes email: no call placed


def test_yes_without_a_pending_order_does_not_place_anything(world):
    flow, r, prov, *_ = world
    assert "place orders" in (flow.handle(M, "hello", "SM1") or "")
    SCRIPT["YES"] = ParsedText(intent="other")
    try:
        say(flow, "YES", sid="SM2")
    finally:
        del SCRIPT["YES"]
    assert prov.pending == {} and sorted(o.id for o in r.orders.list(M)) == SEEDED_ORDERS


def test_second_business_sees_nothing_of_the_first(world):
    flow, r, prov, sent, db = world
    say(flow, "Need 40 bags of flour for Friday, same as last time.")
    reply = say(flow, "Same as last time", tenant=H, sid="SMH")
    assert "What would you like" in reply and "Maple" not in reply and "Supplier B" not in reply and "$3.20" not in reply
    assert r.orders.list(H) == [] and r.orders.awaiting_approval(H) is None
    assert all(m.tenant_id == H and "Maple" not in m.text for m in r.memories.list_active(H))


def test_task_done_closes_the_session_on_next_activity(world):
    flow, r, prov, sent, db = world
    say(flow, "Need 40 bags of flour for Friday, same as last time.")
    o = r.orders.awaiting_approval(M)
    s1 = r.sessions.get(M, o.session_id)
    r.orders.set_status(M, o.id, OrderStatus.confirmed, confirmation_number="X-1", awaiting_approval=False)
    F.notify_outcome(r, M, o.id, send=lambda *a: None)
    assert r.sessions.get(M, s1.id).task_done is True
    s2 = flow.sessions.current(M)
    assert s2.id != s1.id and s2.thread_id != s1.thread_id and r.sessions.get(M, s1.id).closed_at is not None


def test_voicemail_outcome_texts_the_owner_and_spends_no_llm_tokens(world, monkeypatch):
    flow, r, prov, sent, db = world
    calls = []
    monkeypatch.setattr(F, "llm_notes", lambda b, t: calls.append(1) or (CallNotes(), USAGE))
    say(flow, "Need 40 bags of flour for Friday, same as last time.")
    say(flow, "YES", sid="SM2")
    new = [x for x in r.orders.list(M) if x.status == OrderStatus.calling][0]
    from agent.fixtures import post_call_payload
    out = prov.parse_post_call(post_call_payload("no_answer", new.call_id, {}))
    ledger.apply_outcome(M, new.call_id, out, r)
    body = F.notify_outcome(r, M, new.id, send=lambda t, to, b: sent.append(b))
    assert "needs you" in body and "no_answer" in body and calls == []


def test_changed_terms_from_the_call_are_passed_to_the_owner_but_do_not_decide_confirmation(world, monkeypatch):
    flow, r, prov, sent, db = world
    monkeypatch.setattr(F, "llm_notes", lambda b, t: (CallNotes(changed_terms=["price is now 3.50"], unclear=[]), USAGE))
    say(flow, "Need 40 bags of flour for Friday, same as last time.")
    say(flow, "YES", sid="SM2")
    new = [x for x in r.orders.list(M) if x.status == OrderStatus.calling][0]
    from agent.fixtures import post_call_payload
    out = prov.parse_post_call(post_call_payload("ended_without_number", new.call_id, {}))
    ledger.apply_outcome(M, new.call_id, out, r)
    body = F.notify_outcome(r, M, new.id, send=lambda t, to, b: sent.append(b))
    assert r.orders.get(M, new.id).status == OrderStatus.needs_owner                   # no number: still not confirmed
    assert "The supplier said: price is now 3.50." in body
    assert r.calls.get_by_conversation(M, new.call_id).notes["changed_terms"] == ["price is now 3.50"]


def test_webhook_notifies_the_owner_once_even_if_delivered_twice(world, monkeypatch):
    flow, r, prov, sent, db = world
    app.dependency_overrides[get_db] = lambda: db
    try:
        say(flow, "Need 40 bags of flour for Friday, same as last time.")
        say(flow, "YES", sid="SM2")
        new = [x for x in r.orders.list(M) if x.status == OrderStatus.calling][0]
        payload = prov.pending[new.call_id]
        c = TestClient(app)
        assert c.post("/voice/post-call", json=payload).status_code == 200
        assert c.post("/voice/post-call", json=payload).status_code == 200            # duplicate delivery
        assert r.orders.get(M, new.id).status == OrderStatus.confirmed
        assert len([s for s in sent if s[0] == M]) == 1
    finally:
        app.dependency_overrides.clear()


def test_supplier_named_in_any_spelling_matches(world):
    flow, r, *_ = world
    assert "$2.80" in say(flow, "Oats from Supplier B are $2.80 now")


def test_multi_item_order_has_an_unambiguous_brief_and_no_misleading_unit_price(world):
    flow, r, prov, sent, db = world
    say(flow, "Need 40 bags of flour for Friday, same as last time.")
    reply = say(flow, "Add 10 bags of oats to Friday's order", sid="SM2")
    assert "each" not in reply and "$" in reply and "flour" in reply and "oats" in reply
    say(flow, "YES", sid="SM3")
    new = [x for x in r.orders.list(M) if x.status == OrderStatus.calling][0]
    b = r.calls.get_by_conversation(M, new.call_id).brief
    assert "40 bags of flour" in b["quantity"] and "10 bags of oats" in b["quantity"] and "flour and oats" in b["item"]
    assert "in total" in b["price"] and str(int(new.total)) in b["price"]
