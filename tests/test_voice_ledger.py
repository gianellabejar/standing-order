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
BRIEF = {"shop_name": "Maple Bakery", "quantity": 40, "unit": "bag", "item": "flour", "price": 3.2}


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
    m = repos.memories.insert(A, Memory(tenant_id=A, kind="terms", text="Supplier B gives Maple 8% off list",
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
