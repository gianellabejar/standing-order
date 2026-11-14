"""Order ledger. An order is `confirmed` ONLY when a confirmation number appears in the transcript."""
from typing import Optional

from pymongo.errors import DuplicateKeyError

from shared import db as dbm
from shared.interfaces import CallOutcome
from shared.models import AuditEvent, Call, Order, OrderStatus


class Repos:
    def __init__(self, db=None):
        db = db if db is not None else dbm.get_db()
        self.orders, self.calls, self.audit = dbm.Orders(db), dbm.Calls(db), dbm.AuditEvents(db)
        self.quotes, self.memories = dbm.PriceQuotes(db), dbm.Memories(db)
        self.sessions, self.tenants = dbm.Sessions(db), dbm.Tenants(db)


def create_order(tenant_id: str, order: Order, repos: Repos) -> Order:
    """Idempotent on idempotency_key: a repeat returns the existing order."""
    existing = repos.orders.get_by_key(tenant_id, order.idempotency_key)
    if existing:
        return existing
    try:
        order.id = repos.orders.insert(tenant_id, order)
    except DuplicateKeyError:
        return repos.orders.get_by_key(tenant_id, order.idempotency_key)
    return repos.orders.get(tenant_id, order.id)


def start_call(tenant_id: str, order_id: str, context: dict, repos: Repos, provider=None) -> Optional[str]:
    from agent.voice import get_provider
    provider = provider or get_provider()
    order = repos.orders.get(tenant_id, order_id)
    if order is None:
        raise LookupError("order not found for this tenant")
    if order.status != OrderStatus.planned:
        return order.call_id  # already started: never place a second call
    context = {**context, "tenant_id": tenant_id, "order_id": order_id}  # echoed back in the webhook
    try:
        call_id = provider.place_call(order, context)
    except Exception as e:
        repos.orders.set_status(tenant_id, order_id, OrderStatus.failed, needs_owner_reason=f"call not placed: {e}")
        raise
    repos.calls.insert(tenant_id, Call(tenant_id=tenant_id, conversation_id=call_id, order_id=order_id,
                                       brief=context, provider=provider.name))
    repos.orders.set_status(tenant_id, order_id, OrderStatus.calling, call_id=call_id)
    if hasattr(provider, "after_start"):
        provider.after_start(call_id)  # mock only: delivers the simulated post-call payload
    return call_id


def place_with_memory(tenant_id: str, order_id: str, decision, repos: Repos, provider=None) -> Optional[str]:
    """Build the call brief from memory and place the call. A supplier that memory does not say takes phone
    orders is not called: the order goes to the owner (the email fallback is not built yet)."""
    from agent.memory.brief import build_call_brief
    order = repos.orders.get(tenant_id, order_id)
    if order is None:
        raise LookupError("order not found for this tenant")
    brief = build_call_brief(tenant_id, order, decision, repos)
    if brief["channel"] != "phone":
        from agent.orders.email import place_order_email
        place_order_email(tenant_id, order, repos, reason=f"{decision.supplier_id} is not known to take phone orders, "
                                                          "and email ordering is not built yet")
        return None
    memories_used = sorted(set(order.memories_used) | set(brief.pop("memories_used")))
    brief.pop("channel")
    repos.orders.set_status(tenant_id, order_id, OrderStatus.planned, po_number=brief["po_number"],
                            memories_used=memories_used)
    return start_call(tenant_id, order_id, brief, repos, provider=provider)


def apply_outcome(tenant_id: str, conversation_id: str, outcome: CallOutcome, repos: Repos,
                  raw: Optional[dict] = None) -> Order:
    call = repos.calls.get_by_conversation(tenant_id, conversation_id)
    if call is None:
        raise LookupError("no call with that conversation id for this tenant")
    order = repos.orders.get(tenant_id, call.order_id)
    if call.outcome is not None:
        return order  # duplicate webhook: change nothing
    from agent.voice import supplier_said
    number = outcome.confirmation_number
    if outcome.status == "confirmed" and number and supplier_said(outcome.transcript, number):
        status, fields = OrderStatus.confirmed, {"confirmation_number": number}
    else:
        reason = outcome.needs_owner_reason or "confirmation number not found in transcript"
        status = OrderStatus.failed if outcome.status == "failed" else OrderStatus.needs_owner
        fields = {"needs_owner_reason": reason}
    repos.calls.record_outcome(tenant_id, call.id, outcome.transcript, outcome.model_dump(exclude={"transcript"}), raw)
    repos.orders.set_status(tenant_id, order.id, status, **fields)
    repos.audit.insert(tenant_id, AuditEvent(tenant_id=tenant_id, actor="code", event=f"order.{status.value}",
                                             ids=[order.id, call.id]))
    return repos.orders.get(tenant_id, order.id)
