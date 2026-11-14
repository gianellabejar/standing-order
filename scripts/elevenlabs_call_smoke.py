"""Places ONE outbound ElevenLabs call to SUPPLIER1_PHONE (a teammate's verified phone only).
Dry run by default; pass --yes to dial. Run: python scripts/elevenlabs_call_smoke.py --yes"""
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api.elevenlabs.io/v1/convai"  # confirm data-residency host for your account
DONE = {"done", "failed"}  # terminal statuses; exact enum unverified, see timeout fallback below
TIMEOUT_S = 300


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def main():
    env = {k: os.environ.get(k) for k in ("ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID",
                                          "ELEVENLABS_PHONE_NUMBER_ID", "SUPPLIER1_PHONE")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        fail(f"missing env: {', '.join(missing)}")
    body = {
        "agent_id": env["ELEVENLABS_AGENT_ID"],
        "agent_phone_number_id": env["ELEVENLABS_PHONE_NUMBER_ID"],
        "to_number": env["SUPPLIER1_PHONE"],
        "conversation_initiation_client_data": {"dynamic_variables": {
            "shop_name": "Maple Bakery", "supplier_contact": "Dave", "item": "flour", "quantity": 1,
            "unit": "bag", "price_to_confirm": 3.20, "po_number": "SMOKE-0001", "delivery_day": "Friday"}},
    }
    if "--yes" not in sys.argv:
        print("dry run, would POST", f"{BASE}/twilio/outbound-call", "to", env["SUPPLIER1_PHONE"])
        print("re-run with --yes to place the call")
        return
    h = {"xi-api-key": env["ELEVENLABS_API_KEY"]}
    r = httpx.post(f"{BASE}/twilio/outbound-call", headers=h, json=body, timeout=30)
    if r.status_code != 200:
        fail(f"outbound-call {r.status_code}: {r.text[:300]}")
    res = r.json()
    print("response:", res)
    cid = res.get("conversation_id")
    if not res.get("success", True) or not cid:
        fail(f"call not started: {res}")
    print("conversation_id:", cid)

    deadline, status = time.time() + TIMEOUT_S, None
    while time.time() < deadline:
        time.sleep(5)
        c = httpx.get(f"{BASE}/conversations/{cid}", headers=h, timeout=30).json()
        if c.get("status") != status:
            status = c.get("status")
            print("status:", status)
        if status in DONE:
            break
    else:
        fail(f"call did not reach a terminal status in {TIMEOUT_S}s (last status {status!r})")
    for t in c.get("transcript") or []:
        print(f"{t.get('role')}: {t.get('message')}")
    if status == "done" and c.get("transcript"):
        print(f"PASS: call completed with {len(c['transcript'])} transcript turns")
    else:
        fail(f"status={status!r}, transcript turns={len(c.get('transcript') or [])}")


main()
