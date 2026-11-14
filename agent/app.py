import json
import logging

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

from agent import ledger
from agent.voice import get_provider
from shared.db import get_db

router = APIRouter()
log = logging.getLogger("voice")


@router.get("/health")
def health():
    return {"ok": True, "voice_provider": get_provider().name}


@router.post("/voice/post-call")
async def post_call(request: Request, db=Depends(get_db)):
    provider = get_provider()
    raw = await request.body()
    if hasattr(provider, "verify_webhook"):  # real provider only; the mock posts to itself unsigned
        try:
            provider.verify_webhook(raw, request.headers.get("elevenlabs-signature"))
        except Exception as e:
            raise HTTPException(401, f"webhook signature check failed: {type(e).__name__}")
    try:
        payload = json.loads(raw)
    except ValueError:
        log.warning("post-call webhook rejected: body is not JSON (%d bytes)", len(raw))
        raise HTTPException(400, "body is not JSON")
    d = payload.get("data") or {}
    log.warning("post-call webhook received: provider=%s type=%s conversation_id=%s status=%s turns=%s",
                provider.name, payload.get("type"), d.get("conversation_id"), d.get("status"),
                len(d.get("transcript") or []))
    try:
        outcome = provider.parse_post_call(payload)
    except NotImplementedError as e:
        raise HTTPException(501, str(e))
    data = payload.get("data") or {}
    dv = (data.get("conversation_initiation_client_data") or {}).get("dynamic_variables") or {}
    tenant_id, cid = dv.get("tenant_id"), data.get("conversation_id")
    if not tenant_id or not cid:
        log.warning("post-call webhook rejected: no tenant_id dynamic variable or conversation_id (keys=%s)",
                    sorted(dv)[:12])
        raise HTTPException(400, "payload lacks tenant_id dynamic variable or conversation_id")
    try:
        order = ledger.apply_outcome(tenant_id, cid, outcome, ledger.Repos(db), raw=payload)
    except LookupError as e:
        log.warning("post-call webhook rejected: %s (conversation_id=%s)", e, cid)
        raise HTTPException(404, str(e))
    log.warning("provider=%s order=%s status=%s", provider.name, order.id, order.status.value)
    return {"provider": provider.name, "order_id": order.id, "status": order.status.value}


app = FastAPI(title="Order Desk Agent")
app.include_router(router)
