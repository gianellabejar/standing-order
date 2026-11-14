"""Call brief from memory: contact name, PO number from the supplier's PO format, whether the supplier takes
phone orders. Plain code, no LLM."""
from typing import Optional

from agent.ledger import Repos
from shared.interfaces import Decision
from shared.models import Order


def _value(memories, supplier_id: str, key: str):
    m = next((m for m in memories if m.structured.get("supplier") == supplier_id and m.structured.get("key") == key), None)
    return (m.structured.get("value"), m.id) if m else (None, None)


def build_call_brief(tenant_id: str, order: Order, decision: Decision, repos: Optional[Repos] = None) -> dict:
    """Returns the ElevenLabs dynamic variables plus `channel` ("phone" or "email") and `memories_used`.
    A supplier whose memory does not say "phone" is NOT called (email is the fallback)."""
    repos = repos or Repos()
    sup = decision.supplier_id
    mems = repos.memories.list_active(tenant_id)
    channel_v, channel_id = _value(mems, sup, "order_channel")
    contact, contact_id = _value(mems, sup, "contact_name")
    po_fmt, po_id = _value(mems, sup, "po_format")
    used = [i for i in (channel_id if channel_v == "phone" else None, contact_id, po_id) if i]
    seq = 1 + sum(1 for o in repos.orders.list(tenant_id, limit=10000) if o.po_number)
    item = order.items[0] if order.items else {}
    tenant = repos.tenants.get(tenant_id)
    return {
        "shop_name": tenant.name if tenant else tenant_id,
        "contact_name": contact or "the person who takes orders",
        "item": item.get("name") or item.get("sku", "the order"),
        "quantity": sum(i.get("qty", 0) for i in order.items) or item.get("qty", 1),
        "unit": item.get("unit", "bag"),
        "price": decision.unit_price,
        "po_number": po_fmt.format(seq) if po_fmt else f"PO-{seq:04d}",
        "channel": "phone" if channel_v == "phone" else "email",
        "memories_used": used,
    }
