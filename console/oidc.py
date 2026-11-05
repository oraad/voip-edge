"""oidc.py — OpenID Connect (authorization code + PKCE) for the console.

Design and threat model: ``docs/oidc.md``.  Summary:

* ``/auth/login`` starts a transaction: ``state``, ``nonce`` and the PKCE
  verifier live in a short-lived signed flow cookie (one per in-flight login,
  so several tabs work).  The browser is sent to the issuer's authorize URL.
* ``/auth/callback`` is single-use: it verifies the flow cookie, exchanges the
  code, validates the ID token (issuer-bound JWKS, iss/aud/exp/nonce/at_hash),
  applies the local group/domain allowlist, rotates the session and redirects
  to a safe ``next`` path.
* ``/auth/logout`` clears the local session first, then performs RP-initiated
  logout when the issuer advertises an end-session endpoint.

No tokens are stored in cookies or logs.  Identity is ``(iss, sub)``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from authlib.jose import JsonWebKey, jwt as jose_jwt
from authlib.jose.errors import JoseError
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import settings as settings_module
from .security import clear_session, issue_session, read_session

router = APIRouter()

FLOW_COOKIE = "oidc_flow"
FLOW_TTL_SECONDS = 300
_DISCOVERY_TTL = 3600.0
_JWKS_FLOOR = 300.0
_CLOCK_SKEW = 60

_discovery_cache: dict | None = None
_discovery_at: float = 0.0
_jwks_cache: JsonWebKey | None = None
_jwks_at: float = 0.0

# error codes surfaced on /login?error=<code>; mapped to operator text there
ERROR_TEXT = {
    "idp_unreachable": "The identity provider is unreachable — retry in a moment.",
    "invalid_state": "That sign-in attempt expired or was tampered with — start again.",
    "token_rejected": "The identity provider's answer failed validation — start again.",
    "claims_denied": "Your account is not allowed to use this console.",
    "callback_mismatch": "The sign-in response did not match this browser session.",
}


def _cfg():
    return settings_module.settings


def oidc_enabled() -> bool:
    cfg = _cfg()
    return cfg.auth_mode in ("both", "oidc") and bool(cfg.oidc_issuer and cfg.oidc_client_id)


def token_enabled() -> bool:
    return _cfg().auth_mode in ("both", "token")


# ---------------------------------------------------------------------------
# signed flow cookie (state/nonce/verifier live server-side in this cookie)
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(raw: str) -> str:
    return _b64(
        hmac.new(_cfg().secret.encode(), raw.encode(), hashlib.sha256).digest()
    )


def sign_flow(payload: dict) -> str:
    body = dict(payload, exp=int(time.time()) + FLOW_TTL_SECONDS)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    enc = _b64(raw.encode())
    return f"{enc}.{_sign(enc)}"


def verify_flow(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    enc, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(enc)):
        return None
    try:
        payload = json.loads(_unb64(enc))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# ---------------------------------------------------------------------------
# PKCE / redirect helpers
# ---------------------------------------------------------------------------


def pkce_challenge(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode()).digest())


def safe_return_to(value: str | None) -> str:
    """Only same-origin relative paths; never scheme-relative or absolute."""
    if not value or not value.startswith("/"):
        return "/homes"
    if value.startswith("//"):
        return "/homes"
    return value


# ---------------------------------------------------------------------------
# discovery + JWKS (issuer-bound, cached; never token-supplied URLs)
# ---------------------------------------------------------------------------


async def load_discovery(force: bool = False) -> dict:
    global _discovery_cache, _discovery_at
    now = time.monotonic()
    if not force and _discovery_cache and now - _discovery_at < _DISCOVERY_TTL:
        return _discovery_cache
    issuer = _cfg().oidc_issuer.rstrip("/")
    url = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        meta = resp.json()
    if str(meta.get("issuer", "")).rstrip("/") != issuer:
        raise ValueError("issuer mismatch in discovery document")
    _discovery_cache, _discovery_at = meta, now
    return meta


async def load_jwks(force: bool = False) -> JsonWebKey:
    global _jwks_cache, _jwks_at
    now = time.monotonic()
    if not force and _jwks_cache and now - _jwks_at < _JWKS_FLOOR:
        return _jwks_cache
    meta = await load_discovery()
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(meta["jwks_uri"])
        resp.raise_for_status()
        jwks = JsonWebKey.import_key_set(resp.json())
    _jwks_cache, _jwks_at = jwks, now
    return jwks


async def exchange_code(code: str, verifier: str) -> dict:
    meta = await load_discovery()
    cfg = _cfg()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.oidc_redirect_uri,
        "code_verifier": verifier,
        "client_id": cfg.oidc_client_id,
    }
    auth = (cfg.oidc_client_id, cfg.oidc_client_secret) if cfg.oidc_client_secret else None
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(meta["token_endpoint"], data=data, auth=auth)
        resp.raise_for_status()
        return resp.json()


ALLOWED_ALGS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "PS256"})


def _jwt_alg(id_token: str) -> str:
    try:
        header_b64 = id_token.split(".", 1)[0]
        header = json.loads(_unb64(header_b64))
    except Exception:
        return ""
    return str(header.get("alg", ""))


async def validate_id_token(id_token: str, nonce: str, access_token: str | None) -> dict:
    cfg = _cfg()
    # Never trust the token to choose its own verification algorithm.
    if _jwt_alg(id_token) not in ALLOWED_ALGS:
        raise ValueError("id_token algorithm not allowed")
    last_error: Exception | None = None
    for attempt in (1, 2):  # second pass refetches JWKS for key rotation
        jwks = await load_jwks(force=attempt == 2)
        try:
            claims = jose_jwt.decode(
                id_token.encode(),
                jwks,
                claims_options={
                    "iss": {"essential": True, "value": cfg.oidc_issuer.rstrip("/")},
                    "aud": {"essential": True, "value": cfg.oidc_client_id},
                    "exp": {"essential": True},
                    "iat": {"essential": False},
                },
            )
            claims.validate(leeway=_CLOCK_SKEW)
            break
        except JoseError as exc:  # includes bad signature / alg / claims
            last_error = exc
    else:
        raise ValueError(f"id_token rejected: {last_error}")

    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ValueError("nonce mismatch")
    if access_token and claims.get("at_hash"):
        digest = hashlib.sha256(access_token.encode()).digest()
        expected = _b64(digest[: len(digest) // 2])
        if not hmac.compare_digest(str(claims["at_hash"]), expected):
            raise ValueError("at_hash mismatch")
    return dict(claims)


def identity_allowed(claims: dict) -> bool:
    cfg = _cfg()
    if not cfg.oidc_allowed_groups and not cfg.oidc_allowed_email_domains:
        return True
    groups = claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if cfg.oidc_allowed_groups and set(groups) & set(cfg.oidc_allowed_groups):
        return True
    email = str(claims.get("email", ""))
    if cfg.oidc_allowed_email_domains and "@" in email:
        domain = email.rsplit("@", 1)[1].lower()
        if domain in {d.lower() for d in cfg.oidc_allowed_email_domains}:
            return True
    return False


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def _unavailable() -> PlainTextResponse:
    return PlainTextResponse("OIDC is not enabled", status_code=404)


@router.get("/auth/login")
async def auth_login(request: Request, next: str = "/homes"):
    if not oidc_enabled():
        return _unavailable()
    cfg = _cfg()
    try:
        meta = await load_discovery()
    except Exception:
        return RedirectResponse("/login?error=idp_unreachable", status_code=303)

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    params = {
        "response_type": "code",
        "client_id": cfg.oidc_client_id,
        "redirect_uri": cfg.oidc_redirect_uri,
        "scope": cfg.oidc_scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    flow = sign_flow(
        {"state": state, "nonce": nonce, "verifier": verifier,
         "next": safe_return_to(next)}
    )
    response = RedirectResponse(
        f"{meta['authorization_endpoint']}?{urlencode(params)}", status_code=303
    )
    response.set_cookie(
        FLOW_COOKIE, flow, max_age=FLOW_TTL_SECONDS, httponly=True,
        samesite="lax", secure=cfg.secure_cookie, path="/auth",
    )
    return response


@router.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    if not oidc_enabled():
        return _unavailable()
    flow = verify_flow(request.cookies.get(FLOW_COOKIE))

    def fail(reason: str, status: int = 303) -> HTMLResponse | RedirectResponse:
        if reason == "claims_denied":
            response = HTMLResponse(
                f"<h1>Access denied</h1><p>{ERROR_TEXT[reason]}</p>"
                "<p>Ask an administrator to grant the required group.</p>",
                status_code=403,
            )
        else:
            response = RedirectResponse(f"/login?error={reason}", status_code=status)
        response.delete_cookie(FLOW_COOKIE, path="/auth")
        return response

    if flow is None or not state or not hmac.compare_digest(state, flow["state"]):
        return fail("invalid_state")
    if not code:
        return fail("callback_mismatch")

    try:
        tokens = await exchange_code(code, flow["verifier"])
        claims = await validate_id_token(
            tokens["id_token"], flow["nonce"], tokens.get("access_token")
        )
    except Exception:
        return fail("token_rejected")
    if not identity_allowed(claims):
        return fail("claims_denied")

    email = str(claims.get("email") or claims.get("preferred_username") or "")
    subject = f"{claims.get('iss')}#{claims.get('sub')}"
    response = RedirectResponse(safe_return_to(flow.get("next")), status_code=303)
    issue_session(response, sub=subject, email=email)
    response.delete_cookie(FLOW_COOKIE, path="/auth")
    return response


@router.post("/auth/logout")
async def auth_logout(request: Request):
    session = read_session(request)
    response = RedirectResponse("/login", status_code=303)
    clear_session(response)
    if not session or not oidc_enabled():
        return response
    try:
        meta = await load_discovery()
        end = meta.get("end_session_endpoint")
    except Exception:
        end = None
    if not end:
        return response
    params = {"client_id": _cfg().oidc_client_id}
    if _cfg().oidc_post_logout_uri:
        params["post_logout_redirect_uri"] = _cfg().oidc_post_logout_uri
    response.headers["Location"] = f"{end}?{urlencode(params)}"
    return response
