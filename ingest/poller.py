"""IMAP poller for the throwaway inbox.

  python -m ingest.poller            # poll every 45 s (run on a laptop during the demo)
  python -m ingest.poller --once     # one pass, print the summary
  POST/GET /ingest/poll              # one pass over HTTP (Vercel cron or a dashboard button)

The mailbox is opened read-only and messages are fetched with BODY.PEEK, so nothing is marked read.
A UID watermark in `ingest_state` records how far we got. On the very first run the watermark is set
to the newest existing message, so mail that was already in the inbox is skipped, not ingested.
Re-delivered bytes are also caught by the documents (tenant_id, sha256) unique index.
"""
import argparse
import imaplib
import logging
import os
import time
from typing import Callable, Optional

from ingest import email_ingest
from shared import db as dbm

log = logging.getLogger("ingest.poller")
MAILBOX = "INBOX"
MAX_PER_POLL = 20  # bound one pass so an HTTP-triggered poll stays short


def connect() -> imaplib.IMAP4:
    m = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], timeout=30)
    m.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
    return m


def _uids(m: imaplib.IMAP4, criteria: str) -> list[int]:
    typ, data = m.uid("SEARCH", None, criteria)
    return sorted(int(x) for x in (data[0] or b"").split()) if typ == "OK" else []


def poll_once(db=None, connect_fn: Callable[[], imaplib.IMAP4] = connect) -> dict:
    state_repo = dbm.IngestState(db)
    key = f"{os.environ.get('IMAP_USER', 'inbox').lower()}/{MAILBOX}"
    m = connect_fn()
    try:
        typ, _ = m.select(MAILBOX, readonly=True)
        if typ != "OK":
            raise RuntimeError(f"cannot select {MAILBOX}")
        uidvalidity = int((m.untagged_responses.get("UIDVALIDITY") or [b"0"])[0])
        state = state_repo.get(key)
        if state.get("uidvalidity") != uidvalidity:  # first run, or the mailbox was rebuilt
            existing = _uids(m, "ALL")
            state_repo.set(key, uidvalidity=uidvalidity, last_uid=max(existing, default=0))
            log.info("initialised watermark; skipped %d existing messages", len(existing))
            return {"initialized": True, "skipped_existing": len(existing), "processed": []}

        last = state.get("last_uid", 0)
        new = [u for u in _uids(m, f"UID {last + 1}:*") if u > last][:MAX_PER_POLL]
        processed = []
        for uid in new:
            processed.append(_process(m, uid, uidvalidity, db))
            state_repo.set(key, last_uid=uid)  # advance per message; a failed one is kept as a failed document
        return {"initialized": False, "processed": processed}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def _process(m: imaplib.IMAP4, uid: int, uidvalidity: int, db) -> dict:
    typ, data = m.uid("FETCH", str(uid), "(BODY.PEEK[])")
    raw = next((d[1] for d in data if isinstance(d, tuple)), None) if typ == "OK" else None
    if not raw:
        return {"uid": uid, "result": "fetch_failed"}
    msg = email_ingest.parse(raw)
    tenant = email_ingest.route_tenant(msg, db)
    if tenant is None:  # unknown forwarder: store nothing (no tenant to store it under)
        log.info("uid %s from an unknown sender ignored", uid)
        return {"uid": uid, "result": "unknown_sender"}
    try:
        r = email_ingest.ingest_email(tenant.id, raw, ref=f"imap:{uidvalidity}:{uid}", db=db)
    except Exception as e:
        log.exception("uid %s failed", uid)
        return {"uid": uid, "tenant": tenant.id, "result": "failed", "error": type(e).__name__}
    return {"uid": uid, "tenant": tenant.id, "result": "duplicate" if r.duplicate else "ingested",
            "documents": [r.document.id] + [c.document.id for c in r.children],
            "statuses": [r.document.status] + [c.document.status for c in r.children],
            "quotes": len(r.all_quotes)}


def describe(summary: dict) -> list[str]:
    """One readable line per poll, plus one per processed email."""
    if summary.get("initialized"):
        return [f"started fresh: skipped {summary['skipped_existing']} email(s) already in the inbox; "
                "forward new ones now"]
    if not summary["processed"]:
        return ["no new mail"]
    lines = []
    for p in summary["processed"]:
        if p["result"] == "ingested":
            lines.append(f"email #{p['uid']} -> {p['tenant']}: {p['quotes']} quote(s); "
                         f"documents {', '.join(f'{d} ({s})' for d, s in zip(p['documents'], p['statuses']))}")
        else:
            lines.append(f"email #{p['uid']}: {p['result']}" + (f" ({p['error']})" if p.get("error") else ""))
    return lines


def preflight(db=None) -> None:
    """Fail fast with a clear message instead of a stack trace on every poll."""
    missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "MONGODB_URI") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing in .env: {', '.join(missing)}")
    d = db if db is not None else dbm._db()
    d.command("ping")
    routes = {t["_id"]: t.get("emails", []) for t in d.tenants.find({}, {"emails": 1})}
    if not any(routes.values()):
        raise SystemExit("no tenant has an owner email; run: python -m scripts.seed.seed --sync-contacts")
    connect().logout()
    print(f"inbox {os.environ['IMAP_USER']} ok | database {d.name} ok | routing: "
          + ", ".join(f"{tid} <- {len(e)} owner email(s)" for tid, e in routes.items()), flush=True)


def main(argv: Optional[list[str]] = None) -> None:
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="Poll the supplier inbox")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=45, help="seconds between polls (30 to 60)")
    ap.add_argument("-v", "--verbose", action="store_true", help="also show ingest and LLM logs")
    a = ap.parse_args(argv)
    if a.verbose:
        logging.getLogger("ingest").setLevel(logging.INFO)
    interval = max(30, min(60, a.interval))
    preflight()
    if not a.once:
        print(f"polling every {interval} s, Ctrl+C to stop", flush=True)
    try:
        while True:
            stamp = time.strftime("%H:%M:%S")
            try:
                for line in describe(poll_once()):
                    print(f"{stamp} {line}", flush=True)
            except Exception as e:  # keep the loop alive through a network blip
                print(f"{stamp} poll failed: {type(e).__name__}: {e}", flush=True)
            if a.once:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        print("stopped", flush=True)


if __name__ == "__main__":
    main()
