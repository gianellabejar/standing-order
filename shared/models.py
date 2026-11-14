"""Pydantic models matching the collections in CLAUDE.md section 4."""
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(timezone.utc)


# "No expiry" is stored as a far-future date: $vectorSearch filters with $gt would skip nulls.
NEVER = datetime(9999, 12, 31, tzinfo=timezone.utc)


class Doc(BaseModel):
    """Base for tenant-owned records. `id` maps to Mongo `_id` (string)."""
    id: Optional[str] = Field(default=None, alias="_id")
    tenant_id: str
    model_config = {"populate_by_name": True}


class Tenant(BaseModel):
    id: Optional[str] = Field(default=None, alias="_id")
    name: str
    phones: list[str] = []          # owner phone numbers (E.164); inbound SMS routes on these
    emails: list[str] = []          # owner email addresses (lowercase); forwarded supplier emails route on these
    prefs: dict[str, Any] = {}      # e.g. approval_threshold, forwarding_address
    profile: str = ""               # fixed ~150-token profile for the context pack
    seeded: bool = False            # synthetic demo tenant (dashboard shows a "Sample data" chip)


class User(Doc):
    email: str
    password_hash: str              # bcrypt


class Session(Doc):
    thread_id: str
    started_at: datetime = Field(default_factory=now)
    last_activity_at: datetime = Field(default_factory=now)
    task_done: bool = False         # the order finished; the session closes on the next activity
    context_tokens: int = 0         # size of the context pack this session started from (estimate)
    closed_at: Optional[datetime] = None
    summary: Optional[str] = None   # <= 150 tokens
    embedding: Optional[list[float]] = None
    tokens: dict[str, int] = {}     # {"prompt": n, "completion": n}


class Source(BaseModel):
    type: Literal["sms", "email", "pdf", "call", "session", "dashboard"]
    ref: str


class MemoryStatus(str, Enum):
    active = "active"
    superseded = "superseded"
    retracted = "retracted"


class Memory(Doc):
    kind: str
    text: str                       # emptied on forget
    structured: dict[str, Any] = {}  # supplier, key, value
    source: Source                  # no source, no memory
    status: MemoryStatus = MemoryStatus.active
    supersedes: Optional[str] = None
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=now)
    expires_at: datetime = NEVER
    purge_at: Optional[datetime] = None  # TTL index hard-deletes after this
    seeded: bool = False            # disclosed in README and demo
    embedding: Optional[list[float]] = None
    last_used_at: Optional[datetime] = None


class PriceQuote(Doc):
    supplier_id: str
    sku: str
    unit: str
    price: float
    valid_until: datetime
    source: dict[str, Any]          # {"document_id": ..., "page": ...}
    status: str = "active"          # active | superseded
    supersedes: Optional[str] = None     # on the NEW quote: the quote it replaced
    superseded_by: Optional[str] = None  # on the OLD quote: id of the newer quote that replaced it


class Document(Doc):
    kind: Literal["email", "pdf", "csv"]
    ref: str                        # where the raw bytes came from, e.g. "imap:<uidvalidity>:<uid>" or a file path
    sha256: str                     # of the raw bytes; (tenant_id, sha256) is unique, so a document is parsed once
    received_at: datetime = Field(default_factory=now)
    parent_id: Optional[str] = None  # a PDF attachment points at its email
    filename: Optional[str] = None
    message_id: Optional[str] = None
    supplier_id: Optional[str] = None
    page_count: Optional[int] = None
    status: str = "received"        # received | parsed | no_text | unknown_supplier | failed
    status_reason: Optional[str] = None
    quote_ids: list[str] = []
    tokens: dict[str, int] = {}     # {"prompt": n, "completion": n} for the one extraction call


class OrderStatus(str, Enum):
    planned = "planned"
    calling = "calling"
    confirmed = "confirmed"         # only with a captured confirmation number
    needs_owner = "needs_owner"
    failed = "failed"


class Order(Doc):
    status: OrderStatus = OrderStatus.planned
    idempotency_key: str            # unique
    items: list[dict[str, Any]] = []
    supplier_id: Optional[str] = None
    memories_used: list[str] = []
    quotes_used: list[str] = []
    confirmation_number: Optional[str] = None
    po_number: Optional[str] = None
    call_id: Optional[str] = None
    tokens: dict[str, int] = {}
    needs_owner_reason: Optional[str] = None
    request_text: Optional[str] = None   # the owner's original message
    delivery: Optional[str] = None       # requested delivery, as the owner said it ("Friday")
    unit_price: Optional[float] = None   # from the Decision, after memory-derived terms
    total: Optional[float] = None
    awaiting_approval: bool = False      # owner must reply YES before the call is placed
    session_id: Optional[str] = None
    channel: Optional[str] = None        # "phone" or "email": how the order is placed
    seeded: bool = False                 # synthetic demo order, disclosed
    created_at: datetime = Field(default_factory=now)


class Call(Doc):
    conversation_id: str            # ElevenLabs
    order_id: str
    brief: dict[str, Any]
    provider: str = ""              # "mock" or "elevenlabs": which provider actually ran
    transcript: list[dict[str, Any]] = []
    outcome: Optional[dict[str, Any]] = None
    notes: Optional[dict[str, Any]] = None  # LLM-extracted terms/unclear points; never decides confirmation
    raw_post_call: Optional[dict[str, Any]] = None  # the payload exactly as received, for provenance


class AuditEvent(Doc):
    ts: datetime = Field(default_factory=now)
    actor: Literal["code", "agent", "owner"]
    event: str
    ids: list[str] = []
    detail: dict[str, Any] = {}     # small event payload, e.g. an SMS body; never a forgotten memory's text
    tombstone: Optional[dict[str, Any]] = None
