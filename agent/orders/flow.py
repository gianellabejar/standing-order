"""The order flow behind /sms/inbound: understand -> price from memory -> approval -> call -> text the outcome."""
import logging
import os
import re
from typing import Callable, Optional

from agent import ledger
from agent.decision import choose_supplier
from agent.memory.context_pack import build_context_pack
from agent.memory.embed import VoyageEmbedder
from agent.memory.governance import Governance
from agent.orders.email import place_order_email  # noqa: F401  (re-exported)
from agent.orders.outcome import extract_notes, llm_notes
from agent.orders.parse import parse_text
from agent.orders.resolve import alias_map, best_memory_match, resolve_items
from agent.sessions.manager import SessionManager
from shared.interfaces import Decision
from shared.models import AuditEvent, Order, OrderStatus, PriceQuote, Source, now

log = logging.getLogger("orders")
YES = re.compile(r"^\s*(y|yes|yep|yeah|ok|okay|confirm|go ahead|approved?)\W*$", re.I)
NO = re.compile(r"^\s*(n|no|nope|cancel|stop|don'?t)\W*$", re.I)
DEFAULT_THRESHOLD = 100.0


def label(supplier_id: str) -> str:
    return "Supplier " + supplier_id.split("_")[-1].upper()


def _money(x: float) -> str:
    return f"${x:,.2f}"


class OrderFlow:
    def __init__(self, repos: ledger.Repos, sessions: SessionManager, parser: Callable = parse_text, embedder=None,
                 provider=None):
        self.r, self.sessions, self.parser, self.embedder, self.provider = repos, sessions, parser, embedder, provider

    # ---- entry point ----
    def handle(self, tenant_id: str, text: str, sid: str) -> Optional[str]:
        session = self.sessions.current(tenant_id)
        self._record(tenant_id, session, "owner", text, {"type": "sms", "ref": sid})
        reply = self._route(tenant_id, session, text, sid)
        if reply:
            self._record(tenant_id, session, "agent", reply)
        return reply

    def _record(self, tenant_id, session, role, text, source=None) -> None:
        if self.sessions.graph is None:
            return
        try:
            self.sessions.record(tenant_id, session, role, text, source)
        except Exception as e:  # recording is best effort; never lose the owner's order over it
            log.warning("could not record turn (%s)", type(e).__name__)

    def _route(self, tenant_id, session, text, sid) -> Optional[str]:
        pending = self.r.orders.awaiting_approval(tenant_id)
        if pending and YES.match(text):
            return self._approve(tenant_id, pending)
        if pending and NO.match(text):
            self.r.orders.set_status(tenant_id, pending.id, OrderStatus.failed, awaiting_approval=False,
                                     needs_owner_reason="owner declined")
            return "Okay, I won't place that order."
        pack = build_context_pack(tenant_id, session, self.r, query=text, embedder=self.embedder)
        skus = sorted({q.sku for q in self.r.quotes.list_valid(tenant_id)})
        try:
            parsed, usage = self.parser(text, pack.text, skus)
        except Exception as e:
            log.warning("could not parse owner text (%s)", type(e).__name__)
            return "Sorry, I couldn't read that. Could you say it a different way?"
        tokens = {**usage, "context_pack_est": pack.tokens}
        log.info("owner text tenant=%s intent=%s tokens=%s", tenant_id, parsed.intent, tokens)
        if parsed.intent in ("order", "add_to_order") or (parsed.intent == "other" and parsed.items):
            return self._order(tenant_id, session, parsed, sid, tokens, pack.memory_ids)
        if parsed.intent == "price_correction":
            return self._correct_price(tenant_id, parsed, sid)
        if parsed.intent == "forget":
            return self._forget(tenant_id, parsed)
        if parsed.intent == "question":
            return self._answer(tenant_id, parsed)
        return "I can place orders, fix a supplier's price, or forget something I remembered."

    # ---- orders ----
    def _order(self, tenant_id, session, parsed, sid, tokens, pack_memory_ids) -> str:
        r = self.r
        mems = r.memories.list_active(tenant_id)
        valid = r.quotes.list_valid(tenant_id)
        base = r.orders.awaiting_approval(tenant_id) if parsed.intent == "add_to_order" else None
        last = r.orders.last_confirmed(tenant_id)
        items, alias_ids, problem = resolve_items(
            parsed.items, parsed.same_as_last_time, alias_map(mems), sorted({q.sku for q in valid}),
            last.items if last else [], base.items if base else [])
        if problem:
            return problem
        wanted = {i["sku"] for i in items}
        quotes = [q for q in valid if q.sku in wanted]
        try:
            dec = choose_supplier(items, quotes, mems)
        except ValueError:
            return "No supplier has a current quote for all of that. Forward me a fresh price list."
        for i in items:
            i["unit"] = next(q.unit for q in quotes if q.sku == i["sku"])
        qty = sum(i["qty"] for i in items)
        total = round(dec.unit_price * qty, 2)
        thr_m = next((m for m in mems if m.structured.get("key") == "approval_threshold"), None)
        tenant = r.tenants.get(tenant_id)
        threshold = float(thr_m.structured["value"]) if thr_m else float((tenant.prefs if tenant else {}).get(
            "approval_threshold", DEFAULT_THRESHOLD))
        needs_approval = total > threshold
        used = sorted(set(dec.memories_used) | set(alias_ids) | ({thr_m.id} if thr_m else set()))
        fields = dict(items=items, supplier_id=dec.supplier_id, memories_used=used, quotes_used=[q.id for q in quotes],
                      unit_price=dec.unit_price, total=total, awaiting_approval=needs_approval, tokens=tokens,
                      session_id=session.id)
        if base:
            r.orders.update_fields(tenant_id, base.id, **{k: v for k, v in fields.items() if k != "session_id"})
            order = r.orders.get(tenant_id, base.id)
        else:
            order = ledger.create_order(tenant_id, Order(tenant_id=tenant_id, idempotency_key=f"sms:{sid}", **fields), r)
        if needs_approval:
            return f"{self._summary(order, mems)} Reply YES to order."
        return self._place(tenant_id, order)

    def _summary(self, order, mems) -> str:
        items = " and ".join(f"{i['qty']} {i['name']}" for i in order.items)
        disc = next((m for m in mems if m.id in order.memories_used and m.structured.get("key") == "discount_pct"), None)
        extra = f" (your {disc.structured['value']:g}% discount applied)" if disc else ""
        each = f" at {_money(order.unit_price)} each" if len(order.items) == 1 else ""  # a blended price misleads
        return f"{items} from {label(order.supplier_id)}{each} = {_money(order.total)}{extra}."

    def _approve(self, tenant_id, order) -> str:
        self.r.orders.update_fields(tenant_id, order.id, awaiting_approval=False)
        return self._place(tenant_id, self.r.orders.get(tenant_id, order.id))

    def _place(self, tenant_id, order) -> str:
        dec = Decision(supplier_id=order.supplier_id, unit_price=order.unit_price, memories_used=order.memories_used)
        try:
            ledger.place_with_memory(tenant_id, order.id, dec, self.r, provider=self.provider)
        except Exception as e:
            log.warning("call not placed: %s", e)
            return f"I couldn't place the call to {label(order.supplier_id)}: {type(e).__name__}. It's with you."
        got = self.r.orders.get(tenant_id, order.id)
        if got.status == OrderStatus.needs_owner:
            return f"I can't order from {label(got.supplier_id)} by phone yet, so it's with you: {got.needs_owner_reason}."
        if got.status in (OrderStatus.confirmed,):  # the mock delivered inline
            return f"Confirmed with {label(got.supplier_id)}. Confirmation number {got.confirmation_number}."
        return f"Calling {label(got.supplier_id)} now. I'll text you the confirmation number."

    # ---- corrections, forgetting, questions ----
    def _correct_price(self, tenant_id, parsed, sid) -> str:
        c, r = parsed.correction, self.r
        if not c or not c.sku or not c.new_price or c.new_price <= 0 or not c.supplier:
            return "Which item, which supplier, and what's the new price?"
        norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # "Supplier B", "supplier_b", "B" all name the same one
        hint = norm(c.supplier)
        cands = [q for q in r.quotes.list_valid(tenant_id, c.sku)
                 if hint in (norm(q.supplier_id), norm(q.supplier_id.split("_")[-1]))]
        if len(cands) != 1:
            return "I couldn't tell which supplier's price that is. Which one?"
        old = cands[0]
        new = PriceQuote(tenant_id=tenant_id, supplier_id=old.supplier_id, sku=old.sku, unit=old.unit,
                         price=c.new_price, valid_until=old.valid_until,
                         source={"type": "owner_sms", "message_sid": sid})
        new_id = r.quotes.supersede(tenant_id, old.id, new)
        r.audit.insert(tenant_id, AuditEvent(tenant_id=tenant_id, actor="owner", event="quote.supersede",
                                             ids=[old.id, new_id], detail={"old_price": old.price, "new_price": c.new_price}))
        return f"Updated {label(old.supplier_id)}: {old.sku} is now {_money(c.new_price)} (was {_money(old.price)}). I kept the old price in the history."

    def _forget(self, tenant_id, parsed) -> str:
        m, ambiguous = best_memory_match(parsed.forget_target or "", self.r.memories.list_active(tenant_id))
        if m is None:
            return "I couldn't find that in what I remember."
        if ambiguous:
            return "That matches more than one thing I remember. Can you be more specific?"
        Governance(self.r).forget(tenant_id, m.id)
        return "Forgotten. I'll no longer use that."

    def _answer(self, tenant_id, parsed) -> str:
        sku = parsed.question_sku
        last = self.r.orders.last_confirmed(tenant_id)
        if not last or (sku and not any(i["sku"] == sku for i in last.items)):
            return "I don't have a past order like that."
        i = next((i for i in last.items if i["sku"] == sku), last.items[0])
        price = f" at {_money(last.unit_price)} each" if last.unit_price else " (the price wasn't recorded)"
        return f"Last time: {i['qty']} {i.get('name') or i['sku']} from {label(last.supplier_id)}{price}, confirmation {last.confirmation_number}."


def notify_outcome(repos: ledger.Repos, tenant_id: str, order_id: str, sessions: Optional[SessionManager] = None,
                   send: Optional[Callable] = None, notes_llm: Optional[Callable] = None) -> str:
    """After the post-call webhook updated the order: read the call for changed terms (one LLM call, only if the
    supplier spoke), text the owner, and mark the session's task done (it closes on the next activity)."""
    order = repos.orders.get(tenant_id, order_id)
    call = repos.calls.get_by_conversation(tenant_id, order.call_id) if order.call_id else None
    notes = None
    if call:
        try:
            got = extract_notes(call.brief, call.transcript, notes_llm or llm_notes)
            if got:
                notes = got[0].model_dump()
                repos.calls.record_notes(tenant_id, call.id, notes)
        except Exception as e:
            log.warning("could not extract call notes (%s)", type(e).__name__)
    sup = label(order.supplier_id) if order.supplier_id else "the supplier"
    if order.status == OrderStatus.confirmed:
        body = (f"Confirmed with {sup}: " + " and ".join(f"{i['qty']} {i.get('name') or i['sku']}" for i in order.items)
                + (f" at {_money(order.unit_price)} each" if order.unit_price and len(order.items) == 1
                   else f" for {_money(order.total)}" if order.total else "")
                + f". Confirmation number {order.confirmation_number}." + (f" PO {order.po_number}." if order.po_number else ""))
    else:
        body = f"The order with {sup} needs you: {order.needs_owner_reason or 'the call did not finish'}."
    if notes and notes.get("changed_terms"):
        body += " The supplier said: " + "; ".join(t.rstrip(". ") for t in notes["changed_terms"]) + "."
    if notes and notes.get("unclear"):
        body += " Unclear: " + "; ".join(t.rstrip(". ") for t in notes["unclear"]) + "."
    tenant = repos.tenants.get(tenant_id)
    if send is None:
        from ingest.sms import send_sms as send
    if tenant and tenant.phones:
        send(tenant_id, tenant.phones[0], body)
    if order.session_id:
        repos.sessions.mark_done(tenant_id, order.session_id)
        if sessions is not None and call:
            said = " ".join(t["message"] for t in call.transcript if t.get("role") == "user" and t.get("message"))[:500]
            s = repos.sessions.get(tenant_id, order.session_id)
            if said and s and sessions.graph is not None:
                try:
                    sessions.record(tenant_id, s, "supplier", said, {"type": "call", "ref": call.conversation_id})
                except Exception as e:
                    log.warning("could not record supplier turn (%s)", type(e).__name__)
    return body


def for_app(repos: Optional[ledger.Repos] = None) -> OrderFlow:
    """Production wiring. The embedder never retries here: Twilio gives us ~15 s, and a rate-limited Voyage key
    would stall the request; without a vector the context pack falls back to plain code."""
    repos = repos or ledger.Repos()
    key = os.environ.get("VOYAGE_API_KEY")
    embedder = VoyageEmbedder(key, retries=0) if key else None
    return OrderFlow(repos, SessionManager.for_app(repos, embedder), embedder=embedder)
