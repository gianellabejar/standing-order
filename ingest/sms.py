"""POST /sms/inbound (see docs/contracts.md). Real Twilio and scripts/sms_sim.py hit the same code.

Flow: validate signature -> map From to a tenant -> dedupe on MessageSid -> hand the text to the
agent (shared.interfaces.handle_owner_text) -> reply with TwiML.
"""
import logging
import os
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request, Response
from pymongo.errors import DuplicateKeyError

from shared import interfaces
from shared.db import AuditEvents, Tenants
from shared.models import AuditEvent

log = logging.getLogger("ingest.sms")
router = APIRouter()
PATH = "/sms/inbound"
MAX_SMS = 1500  # Twilio's per-message body limit is 1600 chars; leave headroom


def twiml(message: Optional[str] = None) -> Response:
    body = f"<Message>{escape(message[:MAX_SMS])}</Message>" if message else ""
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
                    media_type="text/xml")


def public_url(request: Request) -> str:
    """The URL Twilio signed. Behind a tunnel or proxy, request.url is the internal one, so prefer
    PUBLIC_BASE_URL. Keep the query string exactly as received."""
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        return str(request.url)
    q = request.url.query
    return f"{base}{request.url.path}" + (f"?{q}" if q else "")


def check_signature(request: Request, params: dict) -> None:
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token:  # local dev without Twilio: nothing to validate against
        return
    from twilio.request_validator import RequestValidator
    sig = request.headers.get("X-Twilio-Signature", "")
    if not RequestValidator(token).validate(public_url(request), params, sig):
        raise HTTPException(status_code=403, detail="invalid Twilio signature")


def default_reply(text: str) -> str:
    return "Order Desk got your message. The agent is not connected yet, so nothing was ordered."


@router.post(PATH)
async def sms_inbound(request: Request) -> Response:
    params = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    check_signature(request, params)

    sender, text, sid = params.get("From", ""), params.get("Body", "").strip(), params.get("MessageSid", "")
    tenant = Tenants().resolve_by_phone(sender)
    if tenant is None or not sid:
        log.info("sms from unknown sender or without MessageSid ignored")
        return twiml()  # unknown sender: no reply, no data written

    try:  # the audit _id is the MessageSid, so a Twilio retry hits a duplicate key and stops here
        AuditEvents().insert(tenant.id, AuditEvent(id=f"sms:{sid}", tenant_id=tenant.id, actor="owner",
                                                   event="sms.inbound", ids=[sid],
                                                   detail={"from": sender, "body": text}))
    except DuplicateKeyError:
        log.info("duplicate MessageSid %s ignored", sid)
        return twiml()

    try:
        reply = interfaces.handle_owner_text(tenant.id, text, sid)
    except NotImplementedError:
        reply = default_reply(text)
    return twiml(reply)


def send_sms(tenant_id: str, to: str, body: str) -> Optional[str]:
    """Out-of-band message (approval request, order outcome). Always logged to audit_events so the
    simulator and dashboard can show it. Sent through Twilio only if credentials are set and
    SMS_DRY_RUN is not 1. Returns the Twilio message SID, or None when not sent."""
    sid = None
    acct, token, from_ = (os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                                                       "TWILIO_PHONE_NUMBER"))
    if acct and token and from_ and os.environ.get("SMS_DRY_RUN") != "1":
        from twilio.rest import Client
        sid = Client(acct, token).messages.create(to=to, from_=from_, body=body[:MAX_SMS]).sid
    AuditEvents().insert(tenant_id, AuditEvent(tenant_id=tenant_id, actor="agent", event="sms.outbound",
                                               ids=[sid] if sid else [],
                                               detail={"to": to, "body": body, "sent": bool(sid)}))
    log.info("sms.outbound to tenant %s (%s)", tenant_id, "sent" if sid else "dry run")
    return sid
