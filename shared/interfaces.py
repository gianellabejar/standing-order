"""Function contracts. Bodies are intentionally unimplemented; owners fill them in."""
from typing import Literal, Optional, Protocol

from pydantic import BaseModel

from shared.models import Memory, Order, PriceQuote, Session


class ParsedOrder(BaseModel):
    items: list[dict]                # [{"name": "flour", "qty": 40, "unit": "bag"}]
    deadline: Optional[str] = None
    same_as_last_time: bool = False


class Decision(BaseModel):
    supplier_id: str
    unit_price: float
    memories_used: list[str] = []
    quotes_used: list[str] = []
    reason: str = ""


class CallOutcome(BaseModel):
    status: Literal["confirmed", "needs_owner", "failed"]
    transcript: list[dict]
    confirmation_number: Optional[str] = None   # only set when found in the transcript
    needs_owner_reason: Optional[str] = None


class VoiceProvider(Protocol):
    name: str  # "mock" | "elevenlabs"; stored on the call record and logged

    def place_call(self, order: Order, context: dict) -> str:
        """Start a call for `order` with brief variables `context`. Returns the call/conversation id."""

    def parse_post_call(self, payload: dict) -> CallOutcome:
        """Turn a post-call webhook payload into a CallOutcome."""


def handle_owner_text(tenant_id: str, text: str, message_sid: str) -> Optional[str]:
    """Entry point from /sms/inbound (real Twilio or the simulator) into the agent. Returns the short
    reply sent back as TwiML, or None for no immediate reply. Must return within a few seconds
    (Twilio times out at 15 s); slow work replies later via ingest.sms.send_sms."""
    raise NotImplementedError


def parse_owner_text(tenant_id: str, text: str) -> ParsedOrder:
    """One structured LLM call (Pydantic-validated) turning an owner SMS into items, quantities,
    deadline and the 'same as last time' flag. Logs prompt/completion tokens."""
    raise NotImplementedError


def build_context_pack(tenant_id: str, session: Session, query: str = "", max_tokens: int = 1500) -> str:
    """Tenant profile + open tasks + top 5 memories (Vector Search via the repo) + last session summary.
    Drops lowest-score memories first when over the cap. Returns the text; the token count is logged and stored
    on the session. Implemented in agent/memory/context_pack.py."""
    from agent.memory.context_pack import build_context_pack as impl
    from agent.memory.embed import get_embedder
    return impl(tenant_id, session, query=query, embedder=get_embedder(), max_tokens=max_tokens).text


def choose_supplier(items: list[dict], quotes: list[PriceQuote], memories: list[Memory]) -> Decision:
    """PURE CODE, NO LLM, NO DB. Applies memory-derived terms (e.g. 'B gives 8% off list') to valid
    quotes and picks the cheapest. Records exactly which memory and quote ids were used.
    Implemented in agent/decision.py."""
    from agent.decision import choose_supplier as impl
    return impl(items, quotes, memories)


def replay_without_memory(tenant_id: str, order_id: str) -> tuple[Decision, Decision]:
    """PURE CODE. Reruns choose_supplier for the order with the memories in `memories_used` removed.
    Returns (with_memory, without_memory). Costs no tokens. Implemented in agent/decision.py."""
    from agent.decision import replay_without_memory as impl
    return impl(tenant_id, order_id)


def place_order_call(tenant_id: str, order: Order, context: dict) -> str:
    """Builds the call brief from memory, triggers the ElevenLabs outbound call, sets status `calling`.
    Returns the conversation_id. Confirmed only later, when the transcript yields a confirmation number.
    Provider chosen by VOICE_PROVIDER (agent/voice.py); implemented in agent/ledger.py."""
    from agent.ledger import Repos, start_call
    return start_call(tenant_id, order.id, context, Repos())


def place_order_email(tenant_id: str, order: Order) -> str:
    """Fallback: email the order to the supplier. Returns the sent document id."""
    raise NotImplementedError


def ingest_email(tenant_id: str, raw: bytes, ref: str = "upload") -> list[PriceQuote]:
    """Store the raw email as a Document (with hash), extract quotes from its body and each PDF attachment
    once, write `price_quotes`. Routing a mailbox message to a tenant is ingest.email_ingest.route_tenant.
    Implemented in ingest/email_ingest.py."""
    from ingest.email_ingest import ingest_email as impl
    return impl(tenant_id, raw, ref=ref).quotes


def extract_quotes_from_pdf(tenant_id: str, pdf_bytes: bytes, supplier_id: Optional[str] = None,
                            filename: str = "catalogue.pdf") -> list[PriceQuote]:
    """Text PDFs only (pypdf), then ONE structured LLM call; source = document id + page. supplier_id is
    detected from the text when not given. A PDF with no extractable text is not guessed at: the
    document is marked no_text and the owner is texted. Implemented in ingest/quotes.py."""
    from ingest.quotes import ingest_pdf
    return ingest_pdf(tenant_id, pdf_bytes, filename=filename, supplier_id=supplier_id).quotes


def extract_quotes_from_email_text(tenant_id: str, text: str, document_id: str,
                                   supplier_id: Optional[str] = None) -> list[PriceQuote]:
    """Email-body equivalent of extract_quotes_from_pdf for an already-stored email Document.
    Implemented in ingest/quotes.py."""
    from ingest.quotes import extract_from_text
    return extract_from_text(tenant_id, text, document_id, kind="email", supplier_id=supplier_id).quotes


def close_session(tenant_id: str, session_id: str) -> str:
    """Closes after 30 min silence or task completion. One cheap LLM call: summary (<=150 tokens) and
    memory candidates; a candidate becomes a Memory only if it has a source. Returns the summary.
    Implemented in agent/sessions/manager.py."""
    from agent.memory.embed import get_embedder
    from agent.sessions.manager import for_app
    return for_app(embedder=get_embedder()).close(tenant_id, session_id)
