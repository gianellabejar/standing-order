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


def find_confirmation_number(transcript: list[dict]) -> Optional[str]:
    """Only the supplier's (`user`) turns count, and a number must contain a digit."""
    for turn in transcript:
        if turn.get("role") != "user":
            continue
        for m in CONF_RE.finditer(turn.get("message") or ""):
            if any(ch.isdigit() for ch in m.group(1)):
                return m.group(1)
    return None


def parse_post_call_payload(payload: dict) -> CallOutcome:
    """Plain code, no LLM. Parses the ElevenLabs-shaped post-call payload (unverified against a real call)."""
    if payload.get("type") != "post_call_transcription":
        return CallOutcome(status="needs_owner", transcript=[],
                           needs_owner_reason=f"unsupported webhook type {payload.get('type')!r}")
    data = payload.get("data") or {}
    transcript = data.get("transcript") or []
    number = find_confirmation_number(transcript)
    if number:
        return CallOutcome(status="confirmed", transcript=transcript, confirmation_number=number)
    supplier = [t for t in transcript if t.get("role") == "user" and t.get("message")]
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
        if self.delay is not None:  # delay=None: tests deliver by hand
            threading.Timer(self.delay, self.deliver, [call_id]).start()
        return call_id

    def deliver(self, call_id: str) -> None:
        self.post(self.pending.pop(call_id))

    def parse_post_call(self, payload: dict) -> CallOutcome:
        return parse_post_call_payload(payload)


class ElevenLabsVoiceProvider:
    """TODO: not written until we have an API key. Endpoint and payload notes are in docs/contracts.md."""
    name = "elevenlabs"

    def place_call(self, order: Order, context: dict) -> str:
        raise NotImplementedError("ElevenLabsVoiceProvider: waiting for an API key")

    def parse_post_call(self, payload: dict) -> CallOutcome:
        raise NotImplementedError("ElevenLabsVoiceProvider: waiting for an API key")


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
