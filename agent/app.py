import logging

from fastapi import Depends, FastAPI, HTTPException, Request

from agent import ledger
from agent.voice import get_provider
from shared.db import get_db

app = FastAPI(title="Order Desk Agent")
log = logging.getLogger("voice")


@app.get("/health")
def health():
    return {"ok": True, "voice_provider": get_provider().name}


@app.post("/voice/post-call")
async def post_call(request: Request, db=Depends(get_db)):
    # TODO(real provider): verify the ElevenLabs HMAC signature before trusting the payload.
    payload = await request.json()
    provider = get_provider()
    try:
        outcome = provider.parse_post_call(payload)
    except NotImplementedError as e:
        raise HTTPException(501, str(e))
    data = payload.get("data") or {}
    dv = (data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {}
    tenant_id, cid = dv.get("tenant_id"), data.get("conversation_id")
    if not tenant_id or not cid:
        raise HTTPException(400, "payload lacks tenant_id dynamic variable or conversation_id")
    try:
        order = ledger.apply_outcome(tenant_id, cid, outcome, ledger.Repos(db))
    except LookupError as e:
        raise HTTPException(404, str(e))
    log.warning("provider=%s order=%s status=%s", provider.name, order.id, order.status.value)
    return {"provider": provider.name, "order_id": order.id, "status": order.status.value}
