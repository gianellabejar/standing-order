"""SMS simulator: posts the same form fields Twilio does to /sms/inbound, so the demo works without a
carrier. If TWILIO_AUTH_TOKEN is set it signs the request, so it takes the same validation path.

  python scripts/sms_sim.py --list                       # the prepared owner texts
  python scripts/sms_sim.py --text 1                     # send prepared text #1 (from its tenant)
  python scripts/sms_sim.py --tenant harbour "Need oats" # free text as Cerebral Cafe
  python scripts/sms_sim.py --tenant maple               # interactive: type messages, Ctrl+C to quit
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
PATH = "/sms/inbound"


def load(name: str):
    return json.loads((ROOT / "data" / "seed" / name).read_text(encoding="utf-8"))


def tenant_phone(tenant_id: str) -> str:
    t = next((t for t in load("tenants.json") if t["id"] == tenant_id), None)
    if t is None:
        raise SystemExit(f"unknown tenant {tenant_id!r}")
    return os.environ.get(t["phone_env"]) or t["phone_default"]


def payload(sender: str, body: str, sid: str | None = None) -> dict:
    sid = sid or "SM" + uuid.uuid4().hex  # same shape as a Twilio MessageSid: SM + 32 hex
    return {"MessageSid": sid, "SmsSid": sid, "SmsMessageSid": sid,
            "AccountSid": os.environ.get("TWILIO_ACCOUNT_SID") or "AC" + "0" * 32,
            "From": sender, "To": os.environ.get("TWILIO_PHONE_NUMBER") or "+15550100000",
            "Body": body, "NumMedia": "0", "NumSegments": "1", "ApiVersion": "2010-04-01"}


def post(base: str, params: dict) -> tuple[int, str]:
    url = base.rstrip("/") + PATH
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if token:  # sign with the URL the server validates against (PUBLIC_BASE_URL if set)
        from twilio.request_validator import RequestValidator
        signed_url = (os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") + PATH) if os.environ.get(
            "PUBLIC_BASE_URL") else url
        headers["X-Twilio-Signature"] = RequestValidator(token).compute_signature(signed_url, params)
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def reply_text(twiml: str) -> str:
    try:
        msgs = [m.text or "" for m in ET.fromstring(twiml).iter("Message")]
    except ET.ParseError:
        return twiml
    return "\n".join(msgs) if msgs else "(no reply)"


def send(base: str, sender: str, body: str, sid: str | None = None) -> str:
    params = payload(sender, body, sid)
    status, text = post(base, params)
    print(f"> {sender}: {body}\n< [{status}] {reply_text(text) if status == 200 else text}")
    return params["MessageSid"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Post Twilio-shaped SMS to /sms/inbound")
    ap.add_argument("body", nargs="?", help="free text to send")
    ap.add_argument("--url", default=os.environ.get("SIM_BASE_URL", "http://localhost:8000"))
    ap.add_argument("--tenant", default="maple", help="tenant whose owner phone sends the text")
    ap.add_argument("--from", dest="sender", help="explicit sender number (overrides --tenant)")
    ap.add_argument("--text", type=int, help="send prepared owner text #N (uses its tenant)")
    ap.add_argument("--list", action="store_true", help="list prepared owner texts")
    ap.add_argument("--repeat", action="store_true", help="resend with the same MessageSid (retry test)")
    a = ap.parse_args()

    texts = load("owner_texts.json")
    if a.list:
        for t in texts:
            print(f"{t['n']}. [{t['tenant']}] {t['text']}   ({t['beat']})")
        return
    if a.text is not None:
        t = next((t for t in texts if t["n"] == a.text), None) or sys.exit(f"no owner text #{a.text}")
        sender, body = a.sender or tenant_phone(t["tenant"]), t["text"]
    else:
        sender, body = a.sender or tenant_phone(a.tenant), a.body

    if body is None:  # interactive
        print(f"Texting {a.url}{PATH} as {sender}. Ctrl+C to quit.")
        try:
            while True:
                line = input("> ").strip()
                if line:
                    send(a.url, sender, line)
        except (KeyboardInterrupt, EOFError):
            return
    sid = send(a.url, sender, body)
    if a.repeat:
        send(a.url, sender, body, sid)


if __name__ == "__main__":
    main()
