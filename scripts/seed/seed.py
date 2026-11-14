"""Seed a clean demo state. ALL DATA IS SYNTHETIC; every memory is marked seeded=true.

  python -m scripts.seed.seed            # seed an empty database (refuses if already seeded)
  python -m scripts.seed.seed --reset    # drop the app database, then seed  (= make reset)

Everything is written through the tenant-scoped repositories in shared/db.py.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from data.seed import make_docs  # noqa: E402
from shared import db as dbm  # noqa: E402
from shared.models import (AuditEvent, Document, Memory, Order, OrderStatus, PriceQuote, Source,  # noqa: E402
                           Tenant, now)

SEED = ROOT / "data" / "seed"
SEED_SOURCE = Source(type="session", ref="seed:data/seed/memories.json")


def load(name: str):
    return json.loads((SEED / name).read_text(encoding="utf-8"))


def seed(db) -> dict:
    counts = {}
    tenants = load("tenants.json")
    if dbm.Tenants(db).get(tenants[0]["id"]) is not None:
        raise SystemExit("already seeded; use --reset (make reset) to wipe and reseed")

    for t in tenants:
        phone = os.environ.get(t["phone_env"]) or t["phone_default"]
        dbm.Tenants(db).insert(t["id"], Tenant(name=t["name"], phones=[phone], prefs=t["prefs"],
                                               profile=t["profile"]))
    counts["tenants"] = len(tenants)

    # Documents + quotes. Each tenant gets its own copy: the documents are the same synthetic files,
    # but quotes are tenant records (a quote forwarded by one shop is not visible to another).
    issued = datetime.now(timezone.utc).date()
    paths = make_docs.generate(issued)
    cat = make_docs.load()
    valid_until = datetime.combine(issued + timedelta(days=cat["valid_days"]), datetime.max.time(),
                                   tzinfo=timezone.utc)
    docs = {"supplier_b": ("pdf", paths["pdf"], 1), "supplier_a": ("email", paths["eml"], None)}
    n_docs = n_quotes = 0
    for t in tenants:
        tid = t["id"]
        for supplier_id, (kind, path, page) in docs.items():
            doc_id = f"doc_{tid}_{supplier_id}_{kind}"
            dbm.Documents(db).insert(tid, Document(id=doc_id, tenant_id=tid, kind=kind,
                                                   ref=str(path.relative_to(ROOT).as_posix()),
                                                   sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            n_docs += 1
            for sku, price in cat[supplier_id].items():
                dbm.PriceQuotes(db).insert(tid, PriceQuote(
                    id=f"q_{tid}_{supplier_id}_{sku}", tenant_id=tid, supplier_id=supplier_id, sku=sku,
                    unit=cat["skus"][sku]["unit"], price=price, valid_until=valid_until,
                    source={"document_id": doc_id, "page": page, "seeded": True}))
                n_quotes += 1
    counts["documents"], counts["price_quotes"] = n_docs, n_quotes

    mems = load("memories.json")
    for m in mems:
        dbm.Memories(db).insert(m["tenant"], Memory(id=m["id"], tenant_id=m["tenant"], kind=m["kind"],
                                                    text=m["text"], structured=m["structured"],
                                                    source=SEED_SOURCE, seeded=True))
    counts["memories"] = len(mems)

    # One prior Maple order so "same as last time" has something to resolve. Harbour has none.
    dbm.Orders(db).insert("maple", Order(
        id="ord_seed_maple_prev", tenant_id="maple", status=OrderStatus.confirmed,
        idempotency_key="seed-maple-prev", supplier_id="supplier_b",
        items=[{"sku": "FLR-AP-2KG", "qty": 40, "unit": "bag"}], confirmation_number="SEED-0001",
        created_at=now() - timedelta(days=7)))
    counts["orders"] = 1

    for t in tenants:
        dbm.AuditEvents(db).insert(t["id"], AuditEvent(tenant_id=t["id"], actor="code", event="seed",
                                                       detail={"counts": counts, "synthetic": True}))
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reset", action="store_true", help="drop the app database first")
    args = ap.parse_args()
    if not os.environ.get("MONGODB_URI"):
        raise SystemExit("MONGODB_URI is not set (copy .env.example to .env)")
    t0 = time.perf_counter()
    db = dbm._db()
    if args.reset:
        print("dropped", dbm.drop_app_database(db))
    dbm.ensure_indexes(db)
    counts = seed(db)
    print(f"seeded {db.name}: {counts} in {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    main()
