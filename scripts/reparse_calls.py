"""Re-read stored supplier calls with the current confirmation-number parser and fix orders that were
recorded as needing the owner although the supplier gave a confirmation number.

  python scripts/reparse_calls.py            # dry run: show what would change
  python scripts/reparse_calls.py --apply    # update the calls and orders, write an audit event each

Same rule as a live call: an order becomes confirmed only if the number is in the supplier's own words.
Only orders still in needs_owner with no confirmation number are touched. Nobody is texted.
"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from agent.voice import parse_post_call_payload, supplier_said  # noqa: E402
from shared import db as dbm  # noqa: E402
from shared.models import AuditEvent, OrderStatus  # noqa: E402


def reparse(db=None, apply: bool = False) -> list[dict]:
    db = db if db is not None else dbm._db()
    changes = []
    for tenant_id in db.tenants.distinct("_id"):  # admin script: every tenant, then tenant-scoped repos only
        calls, orders, audit = dbm.Calls(db), dbm.Orders(db), dbm.AuditEvents(db)
        for call in calls.list(tenant_id, limit=10000, **{"outcome.status": "needs_owner"}):
            order = orders.get(tenant_id, call.order_id)
            if order is None or order.status != OrderStatus.needs_owner or order.confirmation_number:
                continue
            payload = call.raw_post_call or {"type": "post_call_transcription", "data": {"transcript": call.transcript}}
            out = parse_post_call_payload(payload)
            if out.status != "confirmed" or not supplier_said(out.transcript, out.confirmation_number):
                continue
            changes.append({"tenant": tenant_id, "order": order.id, "po": order.po_number, "call": call.conversation_id,
                            "number": out.confirmation_number, "was": order.needs_owner_reason})
            if apply:
                calls.record_outcome(tenant_id, call.id, out.transcript, out.model_dump(exclude={"transcript"}),
                                     call.raw_post_call)  # keep the stored webhook exactly as received
                orders.set_status(tenant_id, order.id, OrderStatus.confirmed,
                                  confirmation_number=out.confirmation_number, needs_owner_reason=None)
                audit.insert(tenant_id, AuditEvent(tenant_id=tenant_id, actor="code", event="order.confirmed",
                                                   ids=[order.id, call.id],
                                                   detail={"reparsed": True, "previous_reason": order.needs_owner_reason}))
    return changes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    a = ap.parse_args()
    changes = reparse(apply=a.apply)
    for c in changes:
        print(f"{'fixed' if a.apply else 'would fix'}: {c['tenant']} order {c['po'] or c['order']} -> confirmed, "
              f"number {c['number']} (was: {c['was']})")
    print(f"{len(changes)} order(s) {'updated' if a.apply else 'to update; run with --apply'}")


if __name__ == "__main__":
    main()
