"""Email ordering is NOT built. The stub records that honestly: the order goes to the owner with a reason."""
from typing import Optional

from shared.models import OrderStatus


def place_order_email(tenant_id: str, order, repos, reason: Optional[str] = None) -> None:
    repos.orders.set_status(tenant_id, order.id, OrderStatus.needs_owner,
                            needs_owner_reason=reason or "email ordering is not built yet; the owner must place this order")
    return None
