"""Function contracts. Bodies are intentionally unimplemented; owners fill them in."""
from typing import Optional

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


def parse_owner_text(tenant_id: str, text: str) -> ParsedOrder:
    """One structured LLM call (Pydantic-validated) turning an owner SMS into items, quantities,
    deadline and the 'same as last time' flag. Logs prompt/completion tokens."""
    raise NotImplementedError


def build_context_pack(tenant_id: str, session: Session, max_tokens: int = 1500) -> str:
    """Tenant profile + open tasks + top 5 memories (Vector Search via the repo) + last session summary.
    Drops lowest-score memories first when over the cap. Returns text; token count measured."""
    raise NotImplementedError


def choose_supplier(items: list[dict], quotes: list[PriceQuote], memories: list[Memory]) -> Decision:
    """PURE CODE, NO LLM, NO DB. Applies memory-derived terms (e.g. 'B gives 8% off list') to valid
    quotes and picks the cheapest. Records exactly which memory and quote ids were used."""
    raise NotImplementedError


def replay_without_memory(tenant_id: str, order_id: str) -> tuple[Decision, Decision]:
    """PURE CODE. Reruns choose_supplier for the order with the memories in `memories_used` removed.
    Returns (with_memory, without_memory). Costs no tokens."""
    raise NotImplementedError


def place_order_call(tenant_id: str, order: Order) -> str:
    """Builds the call brief from memory, triggers the ElevenLabs outbound call, sets status `calling`.
    Returns the conversation_id. Confirmed only later, when the transcript yields a confirmation number."""
    raise NotImplementedError


def place_order_email(tenant_id: str, order: Order) -> str:
    """Fallback: email the order to the supplier. Returns the sent document id."""
    raise NotImplementedError


def ingest_email(tenant_id: str, raw: bytes) -> list[PriceQuote]:
    """Store the raw email as a Document (with hash), extract quotes once, write `price_quotes`."""
    raise NotImplementedError


def extract_quotes_from_pdf(tenant_id: str, pdf_bytes: bytes, supplier_id: str) -> list[PriceQuote]:
    """Text PDFs only: pdfplumber/pypdf text, then structured LLM extraction, source = document id + page."""
    raise NotImplementedError


def close_session(tenant_id: str, session_id: str) -> str:
    """Closes after 30 min silence or task completion. One cheap LLM call: summary (<=150 tokens) and
    memory candidates; a candidate becomes a Memory only if it has a source. Returns the summary."""
    raise NotImplementedError
