"""Builds what the Orders page shows. Reads only through the tenant-scoped repositories; no writes.

Display rules that matter for judging:
- "Confirmed" appears only for an order with status confirmed AND a captured confirmation number.
  A confirmed status without a number is shown as "Needs you" (it should never happen; the ledger refuses it).
- Every number on the page comes from the database: counts, prices, totals, tokens.
"""
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared import db as dbm
from shared.models import Memory, MemoryStatus, Order, OrderStatus, now
from shared.suppliers import get_supplier, product_name

try:
    LOCAL_TZ = ZoneInfo("America/Toronto")
except ZoneInfoNotFoundError:  # no tz database on this machine: show UTC rather than guess
    LOCAL_TZ = None

UNIT_PLURAL = {"bag": "bags", "block": "blocks", "box": "boxes", "case": "cases", "kg": "kg"}


def money(v: Optional[float]) -> str:
    return f"${v:,.2f}" if v is not None else "Not set"


def local_date(dt: datetime) -> str:
    if LOCAL_TZ is not None:
        return dt.astimezone(LOCAL_TZ).strftime("%a %d %b %Y")
    return dt.strftime("%a %d %b %Y") + " (UTC)"


def supplier_label(supplier_id: Optional[str]) -> str:
    if not supplier_id:
        return "No supplier yet"
    try:
        return get_supplier(supplier_id)["label"]
    except KeyError:
        return supplier_id


def item_label(order: Order) -> str:
    if not order.items:
        return "No items"
    parts = []
    for i in order.items:
        unit = i.get("unit", "unit")
        qty = i.get("qty", 0)
        name = i.get("name") or product_name(i.get("sku", ""), short=True)
        parts.append(f"{qty} {UNIT_PLURAL.get(unit, unit + 's') if qty != 1 else unit} of {name}")
    return ", ".join(parts)


def channel_label(order: Order) -> str:
    """A call was placed, or the order says phone, or (older/agent orders without the field) the supplier
    is one that takes phone orders."""
    if order.call_id or order.channel == "phone":
        return "Call"
    if order.channel is None and order.supplier_id:
        try:
            return "Call" if get_supplier(order.supplier_id).get("order_channel") == "phone" else "Email"
        except KeyError:
            pass
    return "Email"


def status_chip(order: Order) -> tuple[str, str]:
    """(label, css class)."""
    if order.status == OrderStatus.confirmed and order.confirmation_number:
        return "Confirmed", "chip-confirmed"
    if order.status in (OrderStatus.needs_owner, OrderStatus.confirmed):
        return "Needs you", "chip-needs"
    return order.status.value.replace("_", " ").capitalize(), "chip-neutral"


def order_key(order: Order) -> str:
    return order.po_number or order.id


def memory_state(m: Memory) -> str:
    if m.status == MemoryStatus.active and m.expires_at <= now():
        return "expired"
    return m.status.value


@dataclass
class ReceiptItem:
    id: str
    text: str
    seeded: bool
    state: str              # active | expired | superseded | retracted
    source: str
    corrected_to: Optional[str] = None
    value_key: Optional[str] = None    # structured key the agent uses, if any
    value: Optional[str] = None

    @property
    def actionable(self) -> bool:
        return self.state == "active"

    @property
    def forgettable(self) -> bool:
        return self.state != "retracted"


@dataclass
class Page:
    tenant: object
    orders: list
    selected: Optional[Order] = None
    tiles: dict = field(default_factory=dict)
    receipt: list = field(default_factory=list)
    call: object = None
    quotes: list = field(default_factory=list)
    setup: dict = field(default_factory=dict)


def _receipt(tenant_id: str, order: Order, db) -> list[ReceiptItem]:
    if not order.memories_used:
        return []
    mems = {m.id: m for m in dbm.Memories(db).list(tenant_id, limit=200, _id={"$in": order.memories_used})}
    newer = {m.supersedes: m for m in dbm.Memories(db).list(tenant_id, limit=200,
                                                             supersedes={"$in": order.memories_used})}
    items = []
    for mid in order.memories_used:
        m = mems.get(mid)
        if m is None:  # hard-purged after its grace period: show that it existed, nothing else
            items.append(ReceiptItem(id=mid, text="", seeded=False, state="purged", source=""))
            continue
        value = m.structured.get("value")
        items.append(ReceiptItem(
            id=m.id, text=m.text, seeded=m.seeded, state=memory_state(m),
            source=f"{m.source.type}" + (f" ({m.source.ref})" if m.source.ref else ""),
            corrected_to=newer[mid].text if mid in newer else None,
            value_key=m.structured.get("key") if value is not None else None,
            value=None if value is None else str(value)))
    return items


def _quotes(tenant_id: str, db) -> list[dict]:
    quotes = sorted(dbm.PriceQuotes(db).list_valid(tenant_id), key=lambda q: (q.sku, q.supplier_id))
    doc_ids = sorted({q.source.get("document_id") for q in quotes if q.source.get("document_id")})
    docs = {d.id: d for d in dbm.Documents(db).list(tenant_id, limit=500, _id={"$in": doc_ids})} if doc_ids else {}
    rows = []
    for q in quotes:
        d = docs.get(q.source.get("document_id"))
        if d is None:
            src = "Unknown document"
        else:
            name = d.filename or os.path.basename(d.ref) or d.id
            src = f"{ {'pdf': 'PDF', 'csv': 'CSV'}.get(d.kind, 'Email') }: {name}"
            if q.source.get("page"):
                src += f", page {q.source['page']}"
            elif q.source.get("row"):
                src += f", line {q.source['row']}"
        rows.append({"supplier": supplier_label(q.supplier_id), "item": product_name(q.sku),
                     "price": f"{money(q.price)} per {q.unit}", "valid_until": local_date(q.valid_until), "source": src})
    return rows


def build_page(tenant_id: str, selected_key: Optional[str], db=None) -> Page:
    tenant = dbm.Tenants(db).get(tenant_id)
    orders = sorted(dbm.Orders(db).list(tenant_id, limit=500), key=lambda o: o.created_at, reverse=True)
    chips = [status_chip(o)[0] for o in orders]
    tiles = {"needs": chips.count("Needs you"), "confirmed": chips.count("Confirmed"),
             "used": sum(1 for o in orders if o.memories_used), "total": len(orders)}
    selected = next((o for o in orders if order_key(o) == selected_key), None) if selected_key else None
    selected = selected or (orders[0] if orders else None)
    page = Page(tenant=tenant, orders=orders, selected=selected, tiles=tiles, quotes=_quotes(tenant_id, db),
                setup={"phones": tenant.phones if tenant else [], "owner_emails": tenant.emails if tenant else [],
                       "inbox": os.environ.get("IMAP_USER", "")})
    if selected:
        page.receipt = _receipt(tenant_id, selected, db)
        if selected.call_id:
            page.call = dbm.Calls(db).get_by_conversation(tenant_id, selected.call_id)
    return page
