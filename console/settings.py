"""settings.py — single config surface (plan §10). Frozen in Phase 0; append-only afterwards.

Plain dataclass + os.environ so the package has no extra dependency.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # auth / session
    token: str = os.environ.get("CONSOLE_TOKEN", "dev-token")
    secret: str = os.environ.get("CONSOLE_SECRET", "") or "dev-secret"
    secure_cookie: bool = _bool("CONSOLE_SECURE_COOKIE")
    # bind
    bind: str = os.environ.get("CONSOLE_BIND", "127.0.0.1")
    port: int = int(os.environ.get("CONSOLE_PORT", "8080"))
    # paths
    db_path: str = os.environ.get("CONSOLE_DB_PATH", "edge.sqlite")
    sidecar_dir: str = os.environ.get("CONSOLE_SIDECAR_DIR", "console-data")
    rpc_sock: str = os.environ.get("CONSOLE_RPC_SOCK", "/run/kamailio/kamailio_rpc.sock")
    # behaviour
    rpc_timeout_ms: int = int(os.environ.get("CONSOLE_RPC_TIMEOUT_MS", "2000"))
    throttle_seconds: dict[str, int] = field(default_factory=lambda: {
        "tenant_registry": 5, "landline": 5, "dispatcher": 5,
        "dialplan": 5, "trusted": 5,
    })
    allowed_suffixes: tuple[str, ...] = tuple(
        s for s in os.environ.get("CONSOLE_ALLOWED_SUFFIXES", "home.example.net").split(",") if s
    )
    poll_ms: int = int(os.environ.get("CONSOLE_POLL_MS", "15000"))
    readonly: bool = _bool("CONSOLE_READONLY")
    tls_live: bool = _bool("CONSOLE_TLS_LIVE", True)
    session_ttl_seconds: int = int(os.environ.get("CONSOLE_SESSION_TTL", "43200"))
    login_rate_per_min: int = int(os.environ.get("CONSOLE_LOGIN_RATE", "5"))
    # --- OIDC (docs/oidc.md) ---
    # both = OIDC + break-glass token; oidc = SSO only; token = legacy only
    auth_mode: str = os.environ.get("CONSOLE_AUTH_MODE", "both")
    oidc_issuer: str = os.environ.get("OIDC_ISSUER", "")
    oidc_client_id: str = os.environ.get("OIDC_CLIENT_ID", "")
    oidc_client_secret: str = os.environ.get("OIDC_CLIENT_SECRET", "")
    oidc_scopes: str = os.environ.get("OIDC_SCOPES", "openid profile email groups")
    oidc_redirect_uri: str = os.environ.get("OIDC_REDIRECT_URI", "")
    oidc_post_logout_uri: str = os.environ.get("OIDC_POST_LOGOUT_URI", "")
    oidc_allowed_groups: tuple[str, ...] = tuple(
        g for g in os.environ.get("OIDC_ALLOWED_GROUPS", "").split(",") if g
    )
    oidc_allowed_email_domains: tuple[str, ...] = tuple(
        d for d in os.environ.get("OIDC_ALLOWED_EMAIL_DOMAINS", "").split(",") if d
    )
    oidc_provider_name: str = os.environ.get("OIDC_PROVIDER_NAME", "SSO")


settings = Settings()
