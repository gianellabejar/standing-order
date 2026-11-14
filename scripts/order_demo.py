"""Runs owner texts through the REAL order flow (real DeepSeek parse, real Atlas, real memory) and prints each reply.

  python scripts/order_demo.py --provider mock            # SIMULATED call, in-process, db orderdesk_test (reset + seed)
  python scripts/order_demo.py --provider elevenlabs --yes  # REAL call to SUPPLIER1_PHONE, db from .env, NO reset

The mock never places a call. The real run needs the deployed app to include the flow (the webhook is handled there)."""
import argparse
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEXTS = ["Need 40 bags of flour for Friday, same as last time.", "Oats from B are $2.80 now",
         "Add 10 bags of oats to Friday's order", "YES"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["mock", "elevenlabs"], required=True)
    ap.add_argument("--yes", action="store_true", help="required to place a REAL call")
    ap.add_argument("--texts", nargs="*", help="override the owner texts (default: the order beats)")
    a = ap.parse_args()
    if a.provider == "elevenlabs" and not a.yes:
        sys.exit("dry run: re-run with --yes to place a REAL call to SUPPLIER1_PHONE")
    if a.provider == "mock":
        os.environ.update(MONGODB_DB="orderdesk_test", SMS_DRY_RUN="1", VOICE_PROVIDER="mock")

    from agent import ledger, voice
    from agent.decision import replay_without_memory
    from agent.orders import flow as F
    from shared import db as dbm
    from scripts.seed import seed as seedmod

    db = dbm.get_db()
    if a.provider == "mock":
        print("== MOCK provider: calls are SIMULATED, no real call is placed. db:", dbm.drop_app_database(db))
        dbm.ensure_indexes(db)
        seedmod.seed(db)
        from fastapi.testclient import TestClient
        from agent.app import app
        client = TestClient(app)
        prov = voice.MockVoiceProvider(delay=None, post=lambda p: client.post("/voice/post-call", json=p))
        voice._provider = prov
    else:
        print("== REAL call via ElevenLabs to SUPPLIER1_PHONE. db:", db.name)
        prov = voice.ElevenLabsVoiceProvider()
        voice._provider = prov
    repos = ledger.Repos(db)
    flow = F.for_app(repos)
    flow.provider = prov

    for i, text in enumerate(a.texts or TEXTS, 1):
        t0 = time.time()
        reply = flow.handle("maple", text, f"SMdemo{int(time.time())}{i}")
        print(f"\nOWNER: {text}\nAGENT: {reply}   ({time.time() - t0:.1f}s)")
        if a.provider == "mock":
            for cid in list(prov.pending):
                prov.deliver(cid)  # the simulated post-call webhook, through the real /voice/post-call endpoint

    if a.provider == "elevenlabs":
        placed = [o for o in repos.orders.list("maple") if o.status.value == "calling"]
        deadline = time.time() + 300
        while placed and time.time() < deadline:
            time.sleep(5)
            placed = [o for o in repos.orders.list("maple") if o.status.value == "calling"]
        print("\nstill calling after 300s:" if placed else "\ncall finished.", [o.id for o in placed])

    print("\n== ledger")
    for o in repos.orders.list("maple"):
        if o.id == "ord_seed_maple_prev":
            continue
        print(f"order {o.id[:8]} status={o.status.value} supplier={o.supplier_id} total={o.total} "
              f"confirmation={o.confirmation_number} po={o.po_number} tokens={o.tokens}")
        print(f"  memories_used={len(o.memories_used)} quotes_used={len(o.quotes_used)} reason={o.needs_owner_reason}")
        if o.memories_used and o.quotes_used:
            w, wo = replay_without_memory("maple", o.id, repos)
            print(f"  REPLAY with memory: {w.supplier_id} @ {w.unit_price} | without: {wo.supplier_id} @ {wo.unit_price}")
    out = [e for e in repos.audit.list("maple") if e.event == "sms.outbound"]
    for e in out[-2:]:
        print("owner SMS:", e.detail.get("body"), "| sent" if e.detail.get("sent") else "| dry run (not sent)")


main()
