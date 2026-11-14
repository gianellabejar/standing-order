"""Owner dashboard routes: login, register, logout, the one Orders page, and the three memory actions.

All reads and writes go through the tenant-scoped repositories; memory changes go through the memory
engine (agent.memory.governance.Governance), which refuses ids the tenant does not own. The tenant id
always comes from the signed session, never from the form.
"""
import os
import re
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pymongo.errors import DuplicateKeyError

from shared import db as dbm
from shared.models import Source, Tenant, User
from ui import auth, views

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

NOTICES = {  # fixed messages only: nothing from the query string is echoed onto the page
    "corrected": "Correction saved. The old memory is superseded and no longer used.",
    "expired": "Memory expired. It is no longer retrieved for new orders.",
    "forgotten": "Memory forgotten. Its content is deleted and an audit entry was written.",
    "not_active": "Only an active memory can be changed that way.",
    "empty": "Write the corrected memory before saving.",
    "bad_value": "That value does not match the kind the agent expects (a number).",
}


def _render(request: Request, name: str, ctx: dict, status: int = 200) -> Response:
    token, new = auth.csrf_token(request)
    resp = templates.TemplateResponse(request, name, {**ctx, "csrf": token}, status_code=status)
    if new:
        auth.set_csrf_cookie(request, resp, token)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def _session(request: Request) -> Optional[dict]:
    """The signed session, but only if the user still exists under that tenant."""
    s = auth.read_session(request)
    if not s or dbm.Users().get(s["tid"], s["uid"]) is None:
        return None
    return s


def _back(order: Optional[str], notice: Optional[str] = None, **extra) -> RedirectResponse:
    q = {k: v for k, v in {"order": order, "notice": notice, **extra}.items() if v}
    return RedirectResponse("/" + (f"?{urlencode(q)}" if q else ""), status_code=303)


# --- auth pages ---------------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _session(request):
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", {"error": None, "email": ""})


@router.post("/login")
def login(request: Request, email: str = Form(""), password: str = Form(""), csrf: str = Form("")):
    auth.verify_csrf(request, csrf)
    email = email.strip().lower()
    user = dbm.Users().resolve_by_email(email) if email else None
    if not auth.check_login(password, user.password_hash if user else None):
        return _render(request, "login.html", {"error": "That email and password don't match.", "email": email}, 401)
    resp = RedirectResponse("/", status_code=303)
    auth.start_session(request, resp, user.id, user.tenant_id)
    return resp


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return _render(request, "register.html", {"error": None, "form": {}})


@router.post("/register")
def register(request: Request, business: str = Form(""), email: str = Form(""), phone: str = Form(""),
             password: str = Form(""), csrf: str = Form("")):
    auth.verify_csrf(request, csrf)
    form = {"business": business.strip(), "email": email.strip().lower(), "phone": phone.strip()}
    error = None
    if not form["business"] or len(form["business"]) > 80:
        error = "Enter your business name (up to 80 characters)."
    elif not EMAIL.match(form["email"]):
        error = "Enter a valid email address."
    elif form["phone"] and not E164.match(form["phone"]):
        error = "Enter the phone in international format, for example +14165550123."
    else:
        error = auth.password_problem(password)
    tenants, users = dbm.Tenants(), dbm.Users()
    if not error and users.resolve_by_email(form["email"]):
        error = "An account with that email already exists. Log in instead."
    if not error and tenants.resolve_by_owner_email(form["email"]):
        error = "That email already belongs to another business."
    if not error and form["phone"] and tenants.resolve_by_phone(form["phone"]):
        error = "That phone number already belongs to another business."
    if error:
        return _render(request, "register.html", {"error": error, "form": form}, 400)

    slug = re.sub(r"[^a-z0-9]+", "-", form["business"].lower()).strip("-")[:24] or "shop"
    tenant_id = f"{slug}-{uuid.uuid4().hex[:6]}"
    tenants.insert(tenant_id, Tenant(name=form["business"], phones=[form["phone"]] if form["phone"] else [],
                                     emails=[form["email"]], seeded=False))
    try:
        uid = users.insert(tenant_id, User(tenant_id=tenant_id, email=form["email"],
                                           password_hash=auth.hash_password(password)))
    except DuplicateKeyError:
        return _render(request, "register.html", {"error": "An account with that email already exists.",
                                                  "form": form}, 400)
    resp = RedirectResponse("/", status_code=303)
    auth.start_session(request, resp, uid, tenant_id)
    return resp


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    auth.verify_csrf(request, csrf)
    resp = RedirectResponse("/login", status_code=303)
    auth.end_session(resp)
    return resp


# --- the one page ---------------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def orders_page(request: Request, order: Optional[str] = None, notice: Optional[str] = None,
                transcript: Optional[str] = None, correct: Optional[str] = None, forget: Optional[str] = None):
    s = _session(request)
    if not s:
        return RedirectResponse("/login", status_code=303)
    page = views.build_page(s["tid"], order)
    if page.tenant is None:
        resp = RedirectResponse("/login", status_code=303)
        auth.end_session(resp)
        return resp
    return _render(request, "orders.html", {
        "p": page, "v": views, "notice": NOTICES.get(notice or ""),
        "show_transcript": transcript == "1", "correcting": correct, "forgetting": forget,
        "sel_key": views.order_key(page.selected) if page.selected else None})


# --- memory actions (POST + CSRF, then back to the same order) ------------------------------------

def _governance():
    from agent.ledger import Repos
    from agent.memory.embed import VoyageEmbedder
    from agent.memory.governance import Governance
    key = os.environ.get("VOYAGE_API_KEY")
    return Governance(Repos(), VoyageEmbedder(key) if key else None)


def _require(request: Request, csrf: str) -> dict:
    s = _session(request)
    if not s:
        raise HTTPException(401, "Log in first.")
    auth.verify_csrf(request, csrf)
    return s


def _owned_memory(tenant_id: str, memory_id: str):
    m = dbm.Memories().get(tenant_id, memory_id)
    if m is None:  # another tenant's id looks exactly like a missing one
        raise HTTPException(404, "No such memory.")
    return m


def _coerce(old_value, raw: str):
    """Keep the type the agent's code expects: numbers stay numbers."""
    raw = raw.strip()
    if isinstance(old_value, (int, float)) and not isinstance(old_value, bool):
        n = float(raw)  # ValueError -> caller shows bad_value
        return int(n) if n.is_integer() and isinstance(old_value, int) else n
    return raw


@router.post("/memories/{memory_id}/correct")
def correct_memory(request: Request, memory_id: str, text: str = Form(""), value: Optional[str] = Form(None),
                   order: str = Form(""), csrf: str = Form("")):
    s = _require(request, csrf)
    old = _owned_memory(s["tid"], memory_id)
    if views.memory_state(old) != "active":
        return _back(order, "not_active")
    text = text.strip()
    if not text:
        return _back(order, "empty", correct=memory_id)
    structured = dict(old.structured)
    if value is not None and "value" in structured:
        try:
            structured["value"] = _coerce(structured["value"], value)
        except ValueError:
            return _back(order, "bad_value", correct=memory_id)
    _governance().correct(s["tid"], memory_id, text[:500], Source(type="dashboard", ref=f"user:{s['uid']}"),
                          structured=structured, actor="owner")
    return _back(order, "corrected")


@router.post("/memories/{memory_id}/expire")
def expire_memory(request: Request, memory_id: str, order: str = Form(""), csrf: str = Form("")):
    s = _require(request, csrf)
    if views.memory_state(_owned_memory(s["tid"], memory_id)) != "active":
        return _back(order, "not_active")
    _governance().expire(s["tid"], memory_id, actor="owner")
    return _back(order, "expired")


@router.post("/memories/{memory_id}/forget")
def forget_memory(request: Request, memory_id: str, order: str = Form(""), csrf: str = Form("")):
    s = _require(request, csrf)
    if _owned_memory(s["tid"], memory_id).status.value == "retracted":
        return _back(order, "not_active")
    _governance().forget(s["tid"], memory_id)
    return _back(order, "forgotten")
