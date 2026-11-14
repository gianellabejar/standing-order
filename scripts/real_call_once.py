"""Places ONE real ElevenLabs call to SUPPLIER1_PHONE, saves the fetched conversation to
fixtures/real_post_call.json and runs the confirmation rule on it. Aborts unless the agent's first
message discloses transcription. Run: python scripts/real_call_once.py --yes"""
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.voice import ElevenLabsVoiceProvider, parse_post_call_payload  # noqa: E402
from shared.models import Order  # noqa: E402

BASE, H = "https://api.elevenlabs.io/v1/convai", {"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")}
TIMEOUT_S = 240


def main():
    cfg = httpx.get(f"{BASE}/agents/{os.environ['ELEVENLABS_AGENT_ID']}", headers=H, timeout=30).json()
    first = cfg["conversation_config"]["agent"]["first_message"]
    print("first_message:", first)
    if "transcri" not in first.lower() or "ai assistant" not in first.lower():
        sys.exit("FAIL: first message must say it is an AI assistant and that the call is transcribed")
    if "--yes" not in sys.argv:
        sys.exit("dry run: agent config OK; re-run with --yes to dial")
    brief = {"shop_name": "Maple Bakery", "contact_name": "Dave", "item": "all-purpose flour", "quantity": 1,
             "unit": "bag", "price": 3.20, "po_number": "TEST-0001"}
    cid = ElevenLabsVoiceProvider().place_call(Order(tenant_id="maple", idempotency_key="real-call-1"), brief)
    print("conversation_id:", cid)
    deadline, status = time.time() + TIMEOUT_S, None
    while time.time() < deadline:
        time.sleep(5)
        c = httpx.get(f"{BASE}/conversations/{cid}", headers=H, timeout=30).json()
        if c.get("status") != status:
            status = c.get("status")
            print("status:", status)
        if status in ("done", "failed"):
            break
    else:
        sys.exit(f"FAIL: no terminal status in {TIMEOUT_S}s (last {status!r}); conversation {cid} may still be running")
    with open("fixtures/real_post_call.json", "w") as f:
        json.dump(c, f, indent=2)
    for t in c.get("transcript") or []:
        print(f"  {t.get('role')}: {t.get('message')}")
    out = parse_post_call_payload(c)
    print("outcome:", out.model_dump(exclude={"transcript"}))


main()
