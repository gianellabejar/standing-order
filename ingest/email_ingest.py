"""Forwarded supplier emails. The owner forwards a supplier's email to the throwaway inbox; the
forwarder's address (From) picks the tenant. The body and every PDF and CSV attachment become documents,
each parsed once.

Handles inline forwards (Gmail "---------- Forwarded message ---------") and forward-as-attachment
(message/rfc822 parts).
"""
import email
import html
import logging
import re
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Optional

from ingest import quotes as q
from shared import db as dbm
from shared.models import Tenant

log = logging.getLogger("ingest.email")
PRICE_LIKE = re.compile(r"\$\s?\d|\d+\.\d{2}")  # plain-code gate: no price-looking text, no LLM call


def parse(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)


def sender_address(msg: EmailMessage) -> str:
    return parseaddr(str(msg.get("From", "")))[1].strip().lower()


def route_tenant(msg: EmailMessage, db=None) -> Optional[Tenant]:
    """The forwarding owner's address maps to a tenant. Unknown senders map to nothing."""
    addr = sender_address(msg)
    return dbm.Tenants(db).resolve_by_owner_email(addr) if addr else None


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</div>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def body_text(msg: EmailMessage) -> str:
    """Text of this message plus any forwarded-as-attachment messages, with their headers, so the
    original supplier's From line stays visible for supplier detection."""
    parts = []
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        content = body.get_content()
        parts.append(_html_to_text(content) if body.get_content_type() == "text/html" else content)
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else part.get_content()
            parts.append(f"From: {inner.get('From', '')}\nSubject: {inner.get('Subject', '')}\n{body_text(inner)}")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def pdf_attachments(msg: EmailMessage) -> list[tuple[str, bytes]]:
    out = []
    for part in msg.walk():
        name = part.get_filename() or ""
        if part.get_content_type() == "application/pdf" or name.lower().endswith(".pdf"):
            data = part.get_payload(decode=True)
            if data:
                out.append((name or "attachment.pdf", data))
    return out


def csv_attachments(msg: EmailMessage) -> list[tuple[str, bytes]]:
    out = []
    for part in msg.walk():
        name = part.get_filename() or ""
        if part.get_content_type() in ("text/csv", "application/csv") or name.lower().endswith(".csv"):
            data = part.get_payload(decode=True)
            if data:
                out.append((name or "attachment.csv", data))
    return out


def ingest_email(tenant_id: str, raw: bytes, ref: str = "upload", db=None) -> q.IngestResult:
    msg = parse(raw)
    doc, created = q.store_document(tenant_id, raw, "email", ref, db=db,
                                    message_id=str(msg.get("Message-ID", "")) or None,
                                    filename=str(msg.get("Subject", "")) or None)
    if not created:
        return q.IngestResult(doc, duplicate=True)

    text = body_text(msg)
    supplier = q.detect_supplier(text)
    if PRICE_LIKE.search(text):
        result = q.extract_from_text(tenant_id, text, doc.id, "email", supplier_id=supplier, db=db)
    else:
        dbm.Documents(db).set_status(tenant_id, doc.id, "parsed", status_reason="no prices in the body; no LLM call")
        result = q.IngestResult(dbm.Documents(db).get(tenant_id, doc.id))

    for name, data in pdf_attachments(msg):
        result.children.append(q.ingest_pdf(tenant_id, data, filename=name, parent_id=doc.id,
                                            ref=f"{ref}#{name}", supplier_hint=supplier, db=db))
    from ingest.csv_quotes import ingest_csv
    for name, data in csv_attachments(msg):
        result.children.append(ingest_csv(tenant_id, data, filename=name, parent_id=doc.id,
                                          ref=f"{ref}#{name}", supplier_hint=supplier, db=db))
    return result
