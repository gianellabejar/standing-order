"""ONE real call through the real path: create an order in Atlas, place the call with the ElevenLabs provider,
then wait for the post-call WEBHOOK (received by the deployed app) to update the order. Dry run by default.
Run: python scripts/real_order_call.py --yes   (the deployed app must run VOICE_PROVIDER=elevenlabs)"""
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent import ledger  # noqa: E402
from agent.voice import ElevenLabsVoiceProvider  # noqa: E402
from shared.models import Order, OrderStatus  # noqa: E402

TENANT, WAIT_S = "maple", 300
H = {"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")}


def main():
    cfg = httpx.get(f"https://api.elevenlabs.io/v1/convai/agents/{os.environ['ELEVENLABS_AGENT_ID']}", headers=H, timeout=30).json()["conversation_config"]
    tools = cfg["agent"]["prompt"]["built_in_tools"]
    ok = "transcri" in cfg["agent"]["first_message"].lower() and tools["end_call"] and tools["voicemail_detection"]
    health = httpx.get(os.environ["PUBLIC_BASE_URL"].rstrip("/") + "/health", timeout=30).json()
    print("agent config ok:", bool(ok), "| deployed voice provider:", health.get("voice_provider"))
    if not ok or health.get("voice_provider") != "elevenlabs":
        sys.exit("FAIL: agent needs transcription notice + end_call + voicemail_detection, and the deployed app VOICE_PROVIDER=elevenlabs")
    if "--yes" not in sys.argv:
        sys.exit("dry run OK; re-run with --yes to dial")
    repos = ledger.Repos()
    order = ledger.create_order(TENANT, Order(tenant_id=TENANT, idempotency_key=f"real-call-{int(time.time())}",
                                              items=[{"sku": "FLOUR", "qty": 1}], supplier_id="supplier_b"), repos)
    brief = {"shop_name": "Maple Bakery", "contact_name": "Dave", "item": "all-purpose flour", "quantity": 1,
             "unit": "bag", "price": 3.20, "po_number": "TEST-0002"}
    cid = ledger.start_call(TENANT, order.id, brief, repos, provider=ElevenLabsVoiceProvider())
    print("order:", order.id, "conversation_id:", cid)
    deadline = time.time() + WAIT_S
    while time.time() < deadline:
        time.sleep(5)
        o = repos.orders.get(TENANT, order.id)
        if o.status != OrderStatus.calling:
            break
    else:
        sys.exit(f"no webhook-driven update in {WAIT_S}s; order still 'calling' (conversation {cid})")
    call = repos.calls.get_by_conversation(TENANT, cid)
    print("order status:", o.status.value, "| confirmation_number:", o.confirmation_number, "| reason:", o.needs_owner_reason)
    print("raw webhook stored:", call.raw_post_call is not None, "| envelope keys:", sorted(call.raw_post_call or {}))
    for t in call.transcript:
        print(f"  {t.get('role')}: {t.get('message')}")


main()
