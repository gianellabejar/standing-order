"""Documents -> price_quotes. Each document is stored with its sha256 and parsed ONCE: a second copy of
the same bytes for the same tenant returns the stored result without an LLM call.

Plain code decides everything it can: which supplier (names and domains in shared/suppliers.json), whether a
PDF has text, whether the page number and date are real. The LLM only reads prices off the text.
"""
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Optional
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from ingest import llm
from ingest import pdf as pdftext
from shared import db as dbm
from shared.models import Document, PriceQuote, now
from shared.suppliers import all_suppliers

log = logging.getLogger("ingest.quotes")


@dataclass
class IngestResult:
    document: Document
    quotes: list[PriceQuote] = field(default_factory=list)
    duplicate: bool = False                       # True: already parsed earlier, nothing new was done
    rejected: list[dict] = field(default_factory=list)  # extracted lines dropped by validation, with reason
    children: list["IngestResult"] = field(default_factory=list)  # PDF attachments of an email

    @property
    def all_quotes(self) -> list[PriceQuote]:
        return self.quotes + [q for c in self.children for q in c.all_quotes]


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def detect_supplier(*texts: Optional[str]) -> Optional[str]:
    """Code, not LLM: the supplier whose name or email domain appears in the text. None if zero or
    several suppliers match (we do not guess)."""
    hay = " ".join(t for t in texts if t).lower()
    hits = {s["id"] for s in all_suppliers()
            if any(n.lower() in hay for n in s.get("names", []) + s.get("domains", []) + [s.get("email", "")] if n)}
    return hits.pop() if len(hits) == 1 else None


def store_document(tenant_id: str, raw: bytes, kind: str, ref: str, db=None, **fields) -> tuple[Document, bool]:
    """Returns (document, created). created=False means these exact bytes were seen before."""
    docs = dbm.Documents(db)
    digest = sha256(raw)
    existing = docs.get_by_hash(tenant_id, digest)
    if existing:
        return existing, False
    doc = Document(id=f"doc_{uuid4().hex[:16]}", tenant_id=tenant_id, kind=kind, ref=ref, sha256=digest, **fields)
    try:
        docs.insert(tenant_id, doc)
    except DuplicateKeyError:  # a concurrent poll stored it first
        return docs.get_by_hash(tenant_id, digest), False
    return doc, True


def notify_owner(tenant_id: str, body: str, db=None) -> None:
    """Text the owner (used when we will not guess). Replaced in tests."""
    from ingest.sms import send_sms
    tenant = dbm.Tenants(db).get(tenant_id)
    if tenant and tenant.phones:
        send_sms(tenant_id, tenant.phones[0], body)


def _end_of_day(d) -> datetime:
    return datetime.combine(d, time(23, 59, 59), tzinfo=timezone.utc)


def extract_from_text(tenant_id: str, text: str, document_id: str, kind: str, supplier_id: Optional[str] = None,
                      page_count: Optional[int] = None, db=None) -> IngestResult:
    """ONE structured LLM call for an already-stored document, then code-side validation and the write."""
    docs, quotes_repo = dbm.Documents(db), dbm.PriceQuotes(db)
    doc = docs.get(tenant_id, document_id)
    supplier_id = supplier_id or detect_supplier(text)
    try:
        extraction, usage = llm.EXTRACTOR(text, kind)
    except Exception as e:
        log.exception("extraction failed for %s", document_id)
        docs.set_status(tenant_id, document_id, "failed", status_reason=f"{type(e).__name__}: {e}"[:300])
        raise
    supplier_id = supplier_id or detect_supplier(extraction.supplier_name)
    if supplier_id is None:
        docs.set_status(tenant_id, document_id, "unknown_supplier", tokens=usage,
                        status_reason=f"supplier not recognised (model said {extraction.supplier_name!r})")
        notify_owner(tenant_id, f"I got a price document ({doc.filename or kind}) but couldn't tell which "
                                "supplier it is from, so I saved no prices. Which supplier is it?", db)
        return IngestResult(docs.get(tenant_id, document_id))

    quotes, rejected, today = [], [], now()
    for q in extraction.quotes:
        page = q.page if (kind == "pdf" and q.page and page_count and 1 <= q.page <= page_count) else None
        if q.valid_until is None:
            rejected.append({"sku": q.sku, "reason": "no validity date in the document"})
            continue
        valid_until = _end_of_day(q.valid_until)
        if valid_until <= today:
            rejected.append({"sku": q.sku, "reason": f"already expired ({q.valid_until})"})
            continue
        quotes.append(PriceQuote(id=f"q_{uuid4().hex[:16]}", tenant_id=tenant_id, supplier_id=supplier_id,
                                 sku=q.sku.strip().upper(), unit=q.unit.strip().lower(), price=round(q.price, 4),
                                 valid_until=valid_until,
                                 source={"document_id": document_id, "page": page, "kind": kind}))
    ids = quotes_repo.insert_many(tenant_id, quotes)
    for q in quotes:
        quotes_repo.supersede_older(tenant_id, q.supplier_id, q.sku, keep_id=q.id)
    reason = "; ".join(f"{r['sku']}: {r['reason']}" for r in rejected) or (None if quotes else "no prices found")
    docs.set_status(tenant_id, document_id, "parsed", supplier_id=supplier_id, quote_ids=ids, tokens=usage,
                    status_reason=reason)
    log.info("doc %s: %d quotes, %d rejected, supplier=%s", document_id, len(quotes), len(rejected), supplier_id)
    return IngestResult(docs.get(tenant_id, document_id), quotes, rejected=rejected)


def ingest_pdf(tenant_id: str, pdf_bytes: bytes, filename: str = "catalogue.pdf", supplier_id: Optional[str] = None,
               parent_id: Optional[str] = None, ref: str = "upload", supplier_hint: Optional[str] = None,
               db=None) -> IngestResult:
    doc, created = store_document(tenant_id, pdf_bytes, "pdf", ref, db=db, filename=filename, parent_id=parent_id)
    if not created:
        return IngestResult(doc, duplicate=True)
    docs = dbm.Documents(db)
    try:
        pages = pdftext.page_texts(pdf_bytes)
    except Exception as e:
        docs.set_status(tenant_id, doc.id, "failed", status_reason=f"unreadable PDF: {type(e).__name__}")
        notify_owner(tenant_id, f"I couldn't open the PDF {filename}. Could you send it again?", db)
        return IngestResult(docs.get(tenant_id, doc.id))
    docs.set_status(tenant_id, doc.id, "received", page_count=len(pages))
    if not pdftext.has_text(pages):
        docs.set_status(tenant_id, doc.id, "no_text",
                        status_reason="no extractable text (scanned or image-only PDF); not guessed")
        notify_owner(tenant_id, f"I got {filename} but it has no readable text (it looks scanned), so I didn't "
                                "guess any prices. Please send a text PDF or text me the prices.", db)
        return IngestResult(docs.get(tenant_id, doc.id))
    text = pdftext.with_page_markers(pages)
    supplier_id = supplier_id or detect_supplier(text) or supplier_hint
    return extract_from_text(tenant_id, text, doc.id, "pdf", supplier_id=supplier_id, page_count=len(pages), db=db)
