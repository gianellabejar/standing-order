"""Seed a clean demo state. ALL DATA IS SYNTHETIC; every memory is marked seeded=true.

  python -m scripts.seed.seed            # seed an empty database (refuses if already seeded)
  python -m scripts.seed.seed --reset    # drop the app database, then seed  (= make reset)
  python -m scripts.seed.seed --sync-contacts  # copy owner phones/emails from .env into tenants, no wipe

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
from shared.suppliers import env_first  # noqa: E402
from shared.models import (AuditEvent, Document, Memory, PriceQuote, Source, Tenant, User)  # noqa: E402

SEED = ROOT / "data" / "seed"
SEED_SOURCE = Source(type="session", ref="seed:data/seed/memories.json")


def load(name: str):
    return json.loads((SEED / name).read_text(encoding="utf-8"))


def contacts(t: dict) -> tuple[str, str]:
    phone = env_first(t["phone_env"], t.get("phone_env_legacy")) or t["phone_default"]
    email = (env_first(t["email_env"], t.get("email_env_legacy")) or t["email_default"]).strip().lower()
    return phone, email


def sync_contacts(db) -> None:
    for t in load("tenants.json"):
        phone, email = contacts(t)
        changed = dbm.Tenants(db).set_contacts(t["id"], [phone], [email])
        print(f"{t['id']}: {'updated' if changed else 'already up to date'}"
              f" (phone ...{phone[-4:]}, email {email.split('@')[0][:3]}***@{email.split('@')[-1]})")


def seed_users(db, tenants: list[dict]) -> int:
    """One dashboard login per seeded tenant: the owner email, password from DASHBOARD_SEED_PASSWORD.
    No password set = no seeded logins (the repo is public, so there is no default password)."""
    password = os.environ.get("DASHBOARD_SEED_PASSWORD", "")
    if not password:
        print("DASHBOARD_SEED_PASSWORD not set: no seeded dashboard logins (register one instead)")
        return 0
    from ui.auth import hash_password
    password_hash = hash_password(password)  # bcrypt is deliberately slow: hash once, same password for both
    users = dbm.Users(db)
    for t in tenants:
        users.insert(t["id"], User(id=f"user_{t['id']}", tenant_id=t["id"], email=contacts(t)[1],
                                   password_hash=password_hash))
    return len(tenants)


def seed(db) -> dict:
    counts = {}
    tenants = load("tenants.json")
    if dbm.Tenants(db).get(tenants[0]["id"]) is not None:
        raise SystemExit("already seeded; use --reset (make reset) to wipe and reseed")

    for t in tenants:
        phone, email = contacts(t)
        dbm.Tenants(db).insert(t["id"], Tenant(name=t["name"], phones=[phone], emails=[email],
                                               prefs=t["prefs"], profile=t["profile"], seeded=True))
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
        tid, documents, quotes = t["id"], [], []
        for supplier_id, (kind, path, page) in docs.items():
            doc_id = f"doc_{tid}_{supplier_id}_{kind}"
            documents.append(Document(id=doc_id, tenant_id=tid, kind=kind,
                                      ref=str(path.relative_to(ROOT).as_posix()),
                                      sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            quotes += [PriceQuote(id=f"q_{tid}_{supplier_id}_{sku}", tenant_id=tid, supplier_id=supplier_id,
                                  sku=sku, unit=cat["skus"][sku]["unit"], price=price, valid_until=valid_until,
                                  source={"document_id": doc_id, "page": page, "seeded": True})
                       for sku, price in cat[supplier_id].items()]
        n_docs += len(dbm.Documents(db).insert_many(tid, documents))
        n_quotes += len(dbm.PriceQuotes(db).insert_many(tid, quotes))
    counts["documents"], counts["price_quotes"] = n_docs, n_quotes

    mems = load("memories.json")
    for t in tenants:
        dbm.Memories(db).insert_many(t["id"], [
            Memory(id=m["id"], tenant_id=t["id"], kind=m["kind"], text=m["text"], structured=m["structured"],
                   source=SEED_SOURCE, seeded=True) for m in mems if m["tenant"] == t["id"]])
    counts["memories"] = len(mems)

    # Maple's demo orders come from the real decision + ledger code with a MOCK call (see demo_orders.py).
    # ord_seed_maple_prev (40 bags of flour) is what "same as last time" resolves to. Harbour has none.
    from scripts.seed import demo_orders
    counts.update(demo_orders.write(db, demo_orders.build(db)))

    counts["users"] = seed_users(db, tenants)

    for t in tenants:
        dbm.AuditEvents(db).insert(t["id"], AuditEvent(tenant_id=t["id"], actor="code", event="seed",
                                                       detail={"counts": counts, "synthetic": True}))
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reset", action="store_true", help="drop the app database first")
    ap.add_argument("--sync-contacts", action="store_true", help="only update owner phones/emails from .env")
    args = ap.parse_args()
    if not os.environ.get("MONGODB_URI"):
        raise SystemExit("MONGODB_URI is not set (copy .env.example to .env)")
    t0 = time.perf_counter()
    db = dbm._db()
    if args.sync_contacts:
        sync_contacts(db)
        return
    if args.reset:
        print("dropped", dbm.drop_app_database(db))
    dbm.ensure_indexes(db)
    counts = seed(db)
    print(f"seeded {db.name}: {counts} in {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    main()
