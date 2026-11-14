"""Voice providers. VOICE_PROVIDER=mock (default) simulates a call; it is NEVER a real call."""
import logging
import os
import re
import threading
import uuid
from typing import Callable, Optional

import httpx

from agent.fixtures import SCENARIOS, post_call_payload
from shared.interfaces import CallOutcome
from shared.models import Order

log = logging.getLogger("voice")
CONF_RE = re.compile(r"confirmation\s+(?:number|code|#)?\s*(?:is|:)?\s*([A-Za-z0-9][A-Za-z0-9-]{2,})", re.I)


VOICEMAIL_RE = re.compile(r"record your message|leave (?:a|your) message|after the (?:beep|tone)|voicemail", re.I)


TOKEN_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]*\d[A-Za-z0-9-]*\b")


def find_confirmation_number(transcript: list[dict]) -> Optional[str]:
    """Only the supplier's (`user`) turns count, and a number must contain a digit. Two accepted forms:
    the supplier says "confirmation number is X", or answers (tersely, "it's 1234") right after the agent
    asked for the confirmation number."""
    asked = False
    for turn in transcript:
        msg = turn.get("message") or ""
        if turn.get("role") != "user":
            asked = "confirmation" in msg.lower() if msg else asked
            continue
        for m in CONF_RE.finditer(msg):
            if any(ch.isdigit() for ch in m.group(1)):
                return m.group(1)
        if asked:
            tok = next((t for t in TOKEN_RE.findall(msg) if len(t) >= 3), None)
            if tok:
                return tok
        asked = False
    return None


def parse_post_call_payload(payload: dict) -> CallOutcome:
    """Plain code, no LLM. Accepts the webhook envelope ({type, data}) or a GET /conversations/{id} response."""
    if "data" not in payload and "transcript" in payload:  # GET /conversations/{id} response shape
        payload = {"type": "post_call_transcription", "data": payload}
    if payload.get("type") != "post_call_transcription":
        return CallOutcome(status="needs_owner", transcript=[],
                           needs_owner_reason=f"unsupported webhook type {payload.get('type')!r}")
    data = payload.get("data") or {}
    transcript = data.get("transcript") or []
    number = find_confirmation_number(transcript)
    if number:
        return CallOutcome(status="confirmed", transcript=transcript, confirmation_number=number)
    call = ((data.get("metadata") or {}).get("phone_call") or {})
    if call.get("call_result") == "no_answer":  # seen in a real response (fixtures/real_no_answer.json)
        return CallOutcome(status="needs_owner", transcript=transcript, needs_owner_reason="no_answer: supplier did not pick up")
    supplier = [t for t in transcript if t.get("role") == "user" and (t.get("message") or "").strip("… .")]
    if any(VOICEMAIL_RE.search(t["message"]) for t in supplier):  # seen in a real call: carrier voicemail answered
        return CallOutcome(status="needs_owner", transcript=transcript,
                           needs_owner_reason="voicemail: no person took the order")
    if data.get("status") == "failed":
        return CallOutcome(status="failed", transcript=transcript, needs_owner_reason="call failed")
    if not supplier:
        reason = "no_answer: nobody spoke on the supplier side"
    elif "?" in supplier[-1]["message"]:
        reason = "supplier_asked_question: " + supplier[-1]["message"]
    else:
        reason = "call_ended_without_confirmation_number"
    return CallOutcome(status="needs_owner", transcript=transcript, needs_owner_reason=reason)


def post_over_http(payload: dict) -> None:
    url = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/") + "/voice/post-call"
    httpx.post(url, json=payload, headers={"X-Voice-Provider": "mock"}, timeout=10)


class MockVoiceProvider:
    """Simulated call. Posts a canned payload to our own /voice/post-call after `delay` seconds."""
    name = "mock"

    def __init__(self, scenario: Optional[str] = None, delay: Optional[float] = 2.0,
                 post: Callable[[dict], None] = post_over_http):
        self.scenario = scenario or os.environ.get("MOCK_VOICE_SCENARIO", "confirmed")
        if self.scenario not in SCENARIOS:
            raise ValueError(f"unknown mock scenario {self.scenario!r}; use one of {SCENARIOS}")
        self.delay, self.post, self.pending = delay, post, {}

    def place_call(self, order: Order, context: dict) -> str:
        call_id = "mock_" + uuid.uuid4().hex[:12]
        self.pending[call_id] = post_call_payload(self.scenario, call_id, context)
        log.warning("provider=mock SIMULATED call %s for order %s (scenario=%s); no real call is placed",
                    call_id, order.id, self.scenario)
        return call_id

    def after_start(self, call_id: str) -> None:
        """Called by the ledger once the call record is saved. delay=None: tests deliver by hand.
        MOCK_VOICE_DELIVERY=inline (the default on Vercel, where a Timer thread would be frozen after the
        response) posts the payload right away, before start_call returns."""
        if self.delay is None:
            return
        if os.environ.get("MOCK_VOICE_DELIVERY") == "inline" or os.environ.get("VERCEL"):
            self.deliver(call_id)
        else:
            threading.Timer(self.delay, self.deliver, [call_id]).start()

    def deliver(self, call_id: str) -> None:
        self.post(self.pending.pop(call_id))

    def parse_post_call(self, payload: dict) -> CallOutcome:
        return parse_post_call_payload(payload)


class ElevenLabsVoiceProvider:
    """Outbound call through ElevenLabs Agents + the Twilio number imported there.
    Docs: POST /v1/convai/twilio/outbound-call with conversation_initiation_client_data.dynamic_variables."""
    name = "elevenlabs"
    BASE = "https://api.elevenlabs.io/v1/convai"  # confirm data-residency host for the account

    def _allowed_numbers(self) -> set:
        return {n for n in (os.environ.get("TEST_SUPPLIER_PHONE"), os.environ.get("SUPPLIER_B_PHONE")) if n}

    def place_call(self, order: Order, context: dict) -> str:
        to = context.get("to_number") or os.environ.get("TEST_SUPPLIER_PHONE")
        if to not in self._allowed_numbers():  # only ever call a teammate's verified phone
            raise ValueError("refusing to call a number that is not TEST_SUPPLIER_PHONE or SUPPLIER_B_PHONE")
        variables = {k: v for k, v in context.items()
                     if k != "to_number" and (v is None or isinstance(v, (str, int, float, bool)))}
        r = httpx.post(f"{self.BASE}/twilio/outbound-call", headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                       json={"agent_id": os.environ["ELEVENLABS_AGENT_ID"],
                             "agent_phone_number_id": os.environ["ELEVENLABS_PHONE_NUMBER_ID"],
                             "to_number": to,
                             "conversation_initiation_client_data": {"dynamic_variables": variables}}, timeout=30)
        body = r.json() if r.content else {}
        if r.status_code != 200 or not body.get("success", True) or not body.get("conversation_id"):
            raise RuntimeError(f"outbound-call failed ({r.status_code}): {str(body)[:300]}")
        log.warning("provider=elevenlabs REAL call placed, conversation_id=%s", body["conversation_id"])
        return body["conversation_id"]

    def verify_webhook(self, raw: bytes, sig_header: Optional[str]) -> None:
        """HMAC check via the ElevenLabs SDK. Raises on a missing secret, missing header or bad signature."""
        from elevenlabs.client import ElevenLabs
        secret = os.environ.get("ELEVENLABS_WEBHOOK_SECRET")
        if not secret or not sig_header:
            raise PermissionError("webhook secret or signature header missing")
        ElevenLabs(api_key="unused").webhooks.construct_event(raw.decode(), sig_header, secret)

    def parse_post_call(self, payload: dict) -> CallOutcome:
        return parse_post_call_payload(payload)


_provider = None


def get_provider():
    global _provider
    if _provider is None:
        kind = os.environ.get("VOICE_PROVIDER") or "mock"
        if kind not in ("mock", "elevenlabs"):
            raise ValueError(f"VOICE_PROVIDER must be 'mock' or 'elevenlabs', got {kind!r}")
        _provider = MockVoiceProvider() if kind == "mock" else ElevenLabsVoiceProvider()
        log.warning("voice provider: %s", _provider.name)
    return _provider
