"""Supplier choice and replay. Pure code: no LLM, no tokens."""
from typing import Optional

from agent.ledger import Repos
from shared.interfaces import Decision
from shared.models import Memory, PriceQuote, now


def _discounts(memories: list[Memory]) -> dict[str, Memory]:
    """supplier_id -> memory with structured {supplier, key: 'discount_pct', value: number}."""
    return {m.structured["supplier"]: m for m in memories
            if m.structured.get("key") == "discount_pct" and "supplier" in m.structured}


def choose_supplier(items: list[dict], quotes: list[PriceQuote], memories: list[Memory]) -> Decision:
    """items: [{"sku", "qty"}]. Cheapest total among suppliers quoting every SKU (valid quotes only)."""
    live = [q for q in quotes if q.valid_until > now() and q.status == "active"]
    disc, best = _discounts(memories), None
    for sup in sorted({q.supplier_id for q in live}):
        by_sku = {q.sku: q for q in live if q.supplier_id == sup}
        if any(i["sku"] not in by_sku for i in items):
            continue
        m = disc.get(sup)
        factor = 1 - float(m.structured["value"]) / 100 if m else 1
        total = round(sum(round(by_sku[i["sku"]].price * factor, 2) * i["qty"] for i in items), 2)
        if best is None or total < best[0]:
            best = (total, sup, by_sku, m)
    if best is None:
        raise ValueError("no valid quote covers every item")
    total, sup, by_sku, m = best
    qty = sum(i["qty"] for i in items)
    return Decision(supplier_id=sup, unit_price=round(total / qty, 2), memories_used=[m.id] if m else [],
                    quotes_used=[by_sku[i["sku"]].id for i in items],
                    reason=f"{sup} lowest total {total}" + (f" using memory {m.id}" if m else ""))


def replay_without_memory(tenant_id: str, order_id: str, repos: Optional[Repos] = None) -> tuple[Decision, Decision]:
    """Order.quotes_used holds every quote that was considered; memories_used the memories that applied."""
    repos = repos or Repos()
    order = repos.orders.get(tenant_id, order_id)
    if order is None:
        raise LookupError("order not found for this tenant")
    quotes = repos.quotes.list(tenant_id, _id={"$in": order.quotes_used})
    memories = repos.memories.list(tenant_id, _id={"$in": order.memories_used})  # even if since superseded
    return choose_supplier(order.items, quotes, memories), choose_supplier(order.items, quotes, [])


def memory_impact(tenant_id: str, repos: Optional[Repos] = None) -> tuple[int, int]:
    """(decisions where memory changed the chosen supplier, decisions replayed). Real counts only."""
    repos = repos or Repos()
    orders = [o for o in repos.orders.list(tenant_id, limit=1000) if o.quotes_used]
    changed = 0
    for o in orders:
        with_m, without = replay_without_memory(tenant_id, o.id, repos)
        changed += with_m.supplier_id != without.supplier_id
    return changed, len(orders)
