"""security.py — session cookie signing, CSRF double-submit and login throttling.

Normative spec: ``docs/plan.md`` §2 / §10.

* Session cookie: ``base64url(compact-JSON) + "." + base64url(HMAC-SHA256)``.
  The payload is ``{sub, csrf, exp}``; ``exp`` is an absolute unix timestamp.
  No sliding refresh: a token is valid until ``exp``.
* CSRF: the per-session random value lives in the signed payload *and* in a
  non-HttpOnly ``csrf`` cookie.  Unsafe requests must echo it in
  ``X-CSRF-Token`` (double-submit).
* Login throttling: in-memory sliding window per client IP.  Deliberately
  process-local; the console runs with ``workers=1`` (plan §4).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque

from starlette.requests import Request
from starlette.responses import Response

from . import settings as settings_module

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Endpoints that must be reachable before a CSRF cookie exists.  Keep this
# list tiny and explicit; everything else unsafe is protected.
CSRF_EXEMPT_PATHS = frozenset({"/login"})

# ---------------------------------------------------------------------------
# session token
# ---------------------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(raw: str) -> str:
    digest = hmac.new(
        settings_module.settings.secret.encode("utf-8"),
        raw.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _b64encode(digest)


def sign_session(payload: dict) -> str:
    """Return a signed session token for *payload*.

    ``exp`` is added as an absolute unix timestamp.  A caller-provided ``exp``
    is honoured (useful for tests); normal login uses the configured TTL.
    """
    body = dict(payload)
    body.setdefault("typ", "session")
    body.setdefault(
        "exp", int(time.time()) + settings_module.settings.session_ttl_seconds
    )
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    encoded = _b64encode(raw.encode("utf-8"))
    return f"{encoded}.{_sign(encoded)}"


def verify_session(token: str | None) -> dict | None:
    """Verify a session token; return the payload or ``None`` if invalid/expired."""
    if not token:
        return None
    try:
        encoded, signature = token.split(".", 1)
    except ValueError:
        return None
    try:
        if not hmac.compare_digest(signature, _sign(encoded)):
            return None
    except (TypeError, UnicodeError):
        return None
    try:
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(time.time()):
        return None
    # Domain separation: only session tokens are sessions; a flow cookie (or
    # any other signed payload) must never authenticate a request.
    if payload.get("typ") != "session":
        return None
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        return None
    if not isinstance(payload.get("csrf"), str) or not payload["csrf"]:
        return None
    return payload


def read_session(request: Request) -> dict | None:
    """Return the verified session payload carried by *request*, if any."""
    return verify_session(request.cookies.get(SESSION_COOKIE))


def is_authenticated(request: Request) -> bool:
    return read_session(request) is not None


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_value_matches(request: Request, supplied: str | None) -> bool:
    """True when *supplied* matches both the csrf cookie and the signed session."""
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not supplied:
        return False
    if not hmac.compare_digest(cookie.encode(), supplied.encode()):
        return False
    session = verify_session(request.cookies.get(SESSION_COOKIE))
    if not session:
        return False
    return hmac.compare_digest(session["csrf"].encode(), cookie.encode())


def csrf_valid(request: Request) -> bool:
    """Double-submit check: header value must equal the csrf cookie value."""
    return csrf_value_matches(request, request.headers.get(CSRF_HEADER))


def csrf_protected(request: Request) -> bool:
    """True when *request* must pass the CSRF double-submit check."""
    if request.method in SAFE_METHODS:
        return False
    return request.url.path not in CSRF_EXEMPT_PATHS


# ---------------------------------------------------------------------------
# cookies
# ---------------------------------------------------------------------------


def issue_session(response: Response, sub: str = "token", email: str = "") -> str:
    """Attach session + CSRF cookies to *response*; return the CSRF value."""
    cfg = settings_module.settings
    csrf = new_csrf_token()
    payload = {"sub": sub, "csrf": csrf}
    if email:
        payload["email"] = email
    response.set_cookie(
        SESSION_COOKIE,
        sign_session(payload),
        max_age=cfg.session_ttl_seconds,
        httponly=True,
        samesite="Lax",
        secure=cfg.secure_cookie,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=cfg.session_ttl_seconds,
        httponly=False,
        samesite="Lax",
        secure=cfg.secure_cookie,
        path="/",
    )
    return csrf


def clear_session(response: Response) -> None:
    """Expire both auth cookies."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


# ---------------------------------------------------------------------------
# login rate limit (sliding window, per client IP)
# ---------------------------------------------------------------------------

_login_attempts: dict[str, deque[float]] = {}
_login_lock = threading.Lock()
_LOGIN_WINDOW_SECONDS = 60.0


def reset_login_rate_limit() -> None:
    """Forget all recorded login attempts (tests and operational resets)."""
    with _login_lock:
        _login_attempts.clear()


def login_rate_exceeded(client_ip: str) -> bool:
    """Record an attempt for *client_ip*; return True when it exceeds the limit.

    A rejected attempt is not recorded, so a burst cannot extend the window.
    """
    now = time.monotonic()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    with _login_lock:
        attempts = _login_attempts.setdefault(client_ip or "unknown", deque())
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= settings_module.settings.login_rate_per_min:
            return True
        attempts.append(now)
        return False
