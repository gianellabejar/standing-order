"""Dashboard auth: bcrypt passwords, a signed session cookie, CSRF tokens for every POST form.

- Session cookie: itsdangerous-signed {uid, tid}, HttpOnly, SameSite=Lax, Secure on HTTPS, 7 days.
  The signature key is SESSION_SECRET; without it the dashboard refuses to start a session.
- CSRF: double-submit token. A random token lives in its own cookie and in a hidden field of every
  form; a POST is accepted only if they match (constant-time compare). SameSite=Lax adds a second layer.
"""
import hmac
import os
import secrets
from typing import Optional

import bcrypt
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE, CSRF_COOKIE = "od_session", "od_csrf"
SESSION_AGE = 7 * 24 * 3600
MIN_PASSWORD, MAX_PASSWORD_BYTES = 8, 72  # bcrypt only uses the first 72 bytes; refuse longer, never truncate


def _signer() -> URLSafeTimedSerializer:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise HTTPException(500, "SESSION_SECRET is not set; the dashboard cannot sign sessions")
    return URLSafeTimedSerializer(secret, salt="orderdesk-session")


def password_problem(password: str) -> Optional[str]:
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return "Use at most 72 bytes (bcrypt's limit)."
    return None


def hash_password(password: str) -> str:
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:  # malformed hash or over-long password
        return False


# A real hash to compare against when the email is unknown, so a miss takes as long as a hit.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt()).decode("ascii")


def check_login(password: str, password_hash: Optional[str]) -> bool:
    ok = check_password(password, password_hash or _DUMMY_HASH)
    return ok and password_hash is not None


def _secure(request: Request) -> bool:
    """Secure cookies whenever THIS request came over HTTPS (directly or via the platform's proxy). Not
    PUBLIC_BASE_URL: on http://localhost a Secure cookie is never sent back, which breaks login."""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def start_session(request: Request, response: Response, user_id: str, tenant_id: str) -> None:
    token = _signer().dumps({"uid": user_id, "tid": tenant_id})
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_AGE, httponly=True, samesite="lax",
                        secure=_secure(request), path="/")
    # A new session gets a new CSRF token (no fixation across login).
    response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(32), max_age=SESSION_AGE, httponly=True,
                        samesite="lax", secure=_secure(request), path="/")


def end_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def read_session(request: Request) -> Optional[dict]:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        data = _signer().loads(raw, max_age=SESSION_AGE)
    except BadSignature:  # includes expired
        return None
    return data if isinstance(data, dict) and data.get("uid") and data.get("tid") else None


def csrf_token(request: Request) -> tuple[str, bool]:
    """(token, is_new). Pages call this and set the cookie when it is new."""
    tok = request.cookies.get(CSRF_COOKIE)
    return (tok, False) if tok else (secrets.token_urlsafe(32), True)


def set_csrf_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(CSRF_COOKIE, token, max_age=SESSION_AGE, httponly=True, samesite="lax",
                        secure=_secure(request), path="/")


def verify_csrf(request: Request, submitted: Optional[str]) -> None:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not submitted or not hmac.compare_digest(cookie, submitted):
        raise HTTPException(403, "The form expired or came from another site. Reload the page and try again.")
