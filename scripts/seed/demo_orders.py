"""Seeded demo orders, produced by the REAL decision and ledger code, not typed in.

For each scripted owner request: choose_supplier (with the seeded quotes and memories) -> create_order ->
place_with_memory (call brief from memory, PO number from the PO-format memory) -> MOCK call (the
provider is "mock": no real call is placed; the transcript is the mock fixture) -> apply_outcome
(confirmed only if the confirmation number is in the supplier's words).

It runs on an in-memory copy (mongomock) and the resulting records are written to Atlas in a few batched
inserts, so `make reset` stays fast. Every order is marked seeded=true and disclosed.
"""
from datetime import timedelta

import mongomock

from agent import ledger
from agent.decision import choose_supplier
from agent.memory.brief import build_call_brief
from agent.voice import MockVoiceProvider
from shared import db as dbm
from shared.models import AuditEvent, Call, Order, now
from shared.suppliers import product_name

TENANT = "maple"
# (order id, sku, qty, the owner's text, delivery, days ago, mock call scenario or None = awaiting approval)
REQUESTS = [
    ("ord_seed_maple_prev", "FLR-AP-2KG", 40, "Need 40 bags of flour for Friday.", "Friday", 7, "confirmed"),
    ("ord_seed_maple_oats", "OAT-RL-1KG", 10, "10 bags of oats too please, Tuesday is fine.", "Tuesday", 2,
     "ended_without_number"),
]


def _memory_copy(atlas_db) -> mongomock.Database:
    mem = mongomock.MongoClient(tz_aware=True)["demo"]
    for coll in ("tenants", "memories", "price_quotes"):
        docs = list(atlas_db[coll].find({"_id": TENANT} if coll == "tenants" else {"tenant_id": TENANT}))
        if docs:
            mem[coll].insert_many(docs)
    mem.orders.create_index([("tenant_id", 1), ("idempotency_key", 1)], unique=True)
    return mem


def build(atlas_db) -> dict[str, list]:
    """Run the pipeline in memory; return the new records as models, per collection."""
    mem = _memory_copy(atlas_db)
    repos = ledger.Repos(mem)
    for oid, sku, qty, text, delivery, days_ago, scenario in REQUESTS:
        items = [{"sku": sku, "qty": qty, "unit": "bag", "name": product_name(sku, short=True)}]
        quotes = repos.quotes.list_valid(TENANT, sku=sku)          # every quote considered
        decision = choose_supplier(items, quotes, repos.memories.list_active(TENANT))
        order = Order(id=oid, tenant_id=TENANT, idempotency_key=f"seed-{oid}", items=items, seeded=True,
                      supplier_id=decision.supplier_id, memories_used=decision.memories_used,
                      quotes_used=[q.id for q in quotes], request_text=text, delivery=delivery,
                      unit_price=decision.unit_price, total=round(decision.unit_price * qty, 2),
                      created_at=now() - timedelta(days=days_ago, hours=3))
        ledger.create_order(TENANT, order, repos)
        if scenario is None:  # awaiting the owner's YES: no call yet, so no PO number yet
            channel = build_call_brief(TENANT, order, decision, repos)["channel"]
            repos.orders.set_status(TENANT, oid, order.status, channel=channel)
            continue
        provider = MockVoiceProvider(scenario=scenario, delay=None, post=lambda payload: None)
        call_id = ledger.place_with_memory(TENANT, oid, decision, repos, provider=provider)
        repos.orders.set_status(TENANT, oid, repos.orders.get(TENANT, oid).status, channel="phone")
        payload = provider.pending.pop(call_id)
        ledger.apply_outcome(TENANT, call_id, provider.parse_post_call(payload), repos, raw=payload)

    return {"orders": [Order(**d) for d in mem.orders.find({"tenant_id": TENANT})],
            "calls": [Call(**d) for d in mem.calls.find({"tenant_id": TENANT})],
            "audit_events": [AuditEvent(**d) for d in mem.audit_events.find({"tenant_id": TENANT})]}


def write(atlas_db, records: dict[str, list]) -> dict[str, int]:
    repos = {"orders": dbm.Orders(atlas_db), "calls": dbm.Calls(atlas_db), "audit_events": dbm.AuditEvents(atlas_db)}
    return {k: len(repos[k].insert_many(TENANT, v)) for k, v in records.items()}
