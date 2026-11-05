"""app.py — FastAPI application factory and T2 routes.

Only the skeleton lives here: health, token login/logout, the homes landing
placeholder and the shared auth/CSRF plumbing.  Table-specific routes arrive
with T3; ``web/`` must contain zero table logic (plan §4).
"""
from __future__ import annotations

import hmac
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .security import (
    CSRF_COOKIE,
    SAFE_METHODS,
    clear_session,
    csrf_protected,
    csrf_valid,
    csrf_value_matches,
    is_authenticated,
    issue_session,
    login_rate_exceeded,
    read_session,
)
from . import capabilities, observability, system_ui
from . import settings as settings_module

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

NO_STORE = {"Cache-Control": "no-store"}

# READONLY exemptions: authentication bootstrap and session teardown only.
READONLY_EXEMPT_PATHS = frozenset({"/login", "/logout"})


def _mono(value: object) -> Markup:
    """Jinja filter: render *value* inside a ``span.mono`` (escaped)."""
    return Markup(f'<span class="mono">{escape(value)}</span>')


def _wants_fragment(request: Request) -> bool:
    """True when htmx asks for an inner fragment (case-insensitive header)."""
    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _base_context(request: Request) -> dict[str, object]:
    """Template globals shared by every page (T3/T4 routes get these free)."""
    session = read_session(request)
    from .oidc import oidc_enabled

    context: dict[str, object] = {
        "csrf": request.cookies.get(CSRF_COOKIE),
        "account_email": (session or {}).get("email", ""),
        "account_sub": (session or {}).get("sub", ""),
        "signed_in": bool(session),
        "oidc_enabled": oidc_enabled(),
    }
    context.update(system_ui.status_context(request))
    return context


def create_app() -> FastAPI:
    @asynccontextmanager
    async def _lifespan(app_instance: FastAPI):
        # Advisory capability probe: a daemon thread, never awaited.  Startup
        # must succeed even when Kamailio is down (docker healthcheck contract).
        threading.Thread(
            target=_startup_probe, name="console-capability-probe", daemon=True
        ).start()
        yield

    app = FastAPI(
        title="VoIP Edge Console",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    templates = Jinja2Templates(
        directory=str(TEMPLATES_DIR), context_processors=[_base_context]
    )
    templates.env.filters["mono"] = _mono
    app.state.templates = templates

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        # READONLY is a cross-cutting gate, not a per-route check (plan §6/T9).
        # Unsafe methods are refused before any route or DB dependency runs;
        # GET/HEAD/OPTIONS are unaffected.  /login must stay reachable so an
        # operator can authenticate into the read-only console, and /logout is
        # allowed so a session can always be ended.
        if (
            settings_module.settings.readonly
            and request.method not in SAFE_METHODS
            and request.url.path not in READONLY_EXEMPT_PATHS
        ):
            observability.log_readonly_refusal(
                method=request.method,
                path=request.url.path,
                client=request.client.host if request.client else None,
            )
            return HTMLResponse(
                '<div class="banner err" role="alert">'
                "<strong>console is in read-only mode</strong> — writes are refused.</div>",
                status_code=403,
                headers=NO_STORE,
            )

        # Double-submit: unsafe methods need the token from the cookie, supplied
        # either as X-CSRF-Token (htmx / machine clients) or as the hidden
        # `csrf_token` form field (plain browser form posts — htmx is optional,
        # the console must stay usable with JavaScript disabled).
        # POST /login is the single bootstrap exemption (no cookie yet).
        if csrf_protected(request) and not csrf_valid(request):
            body = await request.body()
            supplied = None
            ctype = request.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in ctype:
                from urllib.parse import parse_qs

                supplied = parse_qs(body.decode("utf-8", "replace")).get(
                    "csrf_token", [None]
                )[0]
            if not csrf_value_matches(request, supplied):
                observability.log_auth_failure(
                    reason="csrf",
                    path=request.url.path,
                    client=request.client.host if request.client else None,
                )
                return PlainTextResponse(
                    "CSRF token missing or invalid", status_code=403, headers=NO_STORE
                )

            # Replay the body for the route: reading it here consumed the stream.
            async def _replay():
                return {"type": "http.request", "body": body, "more_body": False}

            request = Request(request.scope, _replay)
        return await call_next(request)

    # ------------------------------------------------------------------
    # health — never touches Kamailio; always 200 and unauthenticated
    # ------------------------------------------------------------------
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------
    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        from . import oidc

        error_code = request.query_params.get("error", "")
        next_path = oidc.safe_return_to(request.query_params.get("next"))
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "show_chrome": False,
                "error": oidc.ERROR_TEXT.get(error_code) if error_code else None,
                "oidc_enabled": oidc.oidc_enabled(),
                "token_enabled": oidc.token_enabled(),
                "provider_name": settings_module.settings.oidc_provider_name,
                "next_path": next_path,
            },
            status_code=403 if error_code == "claims_denied" else 200,
            headers=NO_STORE,
        )

    @app.post("/login")
    async def login_submit(request: Request):
        if not settings_module.settings.auth_mode in ("both", "token"):
            return PlainTextResponse(
                "Token sign-in is disabled (SSO only).", status_code=403, headers=NO_STORE
            )
        client_ip = request.client.host if request.client else "unknown"
        if login_rate_exceeded(client_ip):
            observability.log_auth_failure(
                reason="rate_limited", client=client_ip, path="/login"
            )
            return PlainTextResponse(
                "Too many sign-in attempts. Try again in a minute.",
                status_code=429,
                headers={**NO_STORE, "Retry-After": "60"},
            )

        # Parse the URL-encoded form directly: python-multipart is not a T2
        # dependency (the login form is application/x-www-form-urlencoded by
        # default, which needs no multipart parser).
        body = await request.body()
        token = parse_qs(body.decode("utf-8", "replace")).get("token", [""])[0]
        supplied = token.encode("utf-8")
        expected = settings_module.settings.token.encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            observability.log_auth_failure(
                reason="invalid_token", client=client_ip, path="/login"
            )
            return templates.TemplateResponse(
                request,
                "login.html",
                {"show_chrome": False, "error": "Invalid token — try again."},
                status_code=401,
                headers=NO_STORE,
            )

        response = RedirectResponse("/", status_code=303, headers=NO_STORE)
        issue_session(response)
        return response

    @app.post("/logout")
    def logout() -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303, headers=NO_STORE)
        clear_session(response)
        return response

    # ------------------------------------------------------------------
    # pages
    # ------------------------------------------------------------------
    @app.get("/")
    def index(request: Request) -> RedirectResponse:
        target = "/homes" if is_authenticated(request) else "/login"
        return RedirectResponse(target, status_code=303, headers=NO_STORE)

    # ------------------------------------------------------------------
    # T3 routers: /homes landing + generic read-only table browser
    # T4.1: descriptor-driven create/edit forms.  Include forms_ui *before*
    # tables_ui so GET /tables/{table}/new is not captured by the detail
    # route /tables/{table}/{key}.
    # ------------------------------------------------------------------
    from . import forms_ui, homes_ui, tables_ui

    app.include_router(homes_ui.router)
    app.include_router(forms_ui.router)
    app.include_router(tables_ui.router)

    # T9: audit & backups page (read-only projection over sidecar + backups).
    app.include_router(system_ui.router)

    # ------------------------------------------------------------------
    # T6: live dashboard (registered phones + route-set probes)
    # RPC endpoint is lazily built so tests can inject app.state.rpc.
    # ------------------------------------------------------------------
    from .live import build_live_router
    from .rpc import UnixDatagramRPC

    def _default_rpc():
        return UnixDatagramRPC(
            settings_module.settings.rpc_sock,
            timeout_ms=settings_module.settings.rpc_timeout_ms,
        )

    app.state.rpc_factory = _default_rpc
    app.state.rpc = None  # set on first use; tests may pre-inject
    app.state.capabilities = None  # T9 startup probe result (advisory only)

    def _get_rpc():
        if app.state.rpc is None:
            app.state.rpc = app.state.rpc_factory()
        return app.state.rpc

    def _startup_probe() -> None:
        """Advisory probe on a daemon thread; startup never waits on Kamailio."""
        try:
            if not Path(settings_module.settings.rpc_sock).exists():
                # Missing socket: leave capabilities unknown ("not wired yet")
                # instead of burning a timeout per method.
                return
            result = capabilities.probe(_get_rpc())
            app.state.capabilities = result
        except BaseException:
            # A probe must never take the console down.
            app.state.capabilities = None

    app.include_router(build_live_router(_get_rpc))

    # ------------------------------------------------------------------
    # T4.1: one lazy reload worker shared by the write forms.  Tests may
    # pre-inject app.state.worker with a recorder; the factory is only used
    # when nothing was injected.  Throttles come from the descriptors so a
    # later family cannot drift from tables.yaml.
    # ------------------------------------------------------------------
    from . import descriptors as descriptors_module
    from .reloads import ReloadWorker

    def _default_worker() -> ReloadWorker:
        tableset = descriptors_module.load()
        throttles = {
            descriptor.table: descriptor.reload.throttle_seconds
            for descriptor in tableset.tables.values()
        }

        def _store(status: str, table: str, rpc_name: str, params: list) -> None:
            # Structured observability only; pending-intent persistence is
            # owned by the lifecycle/startup sweeper paths.  Worker vocabulary
            # (done/pending) maps to the operator-facing reload outcomes.
            outcome = {"done": "applied", "pending": "scheduled"}.get(
                status, status
            )
            observability.log_reload_outcome(
                table=table, status=outcome, rpc=rpc_name
            )

        return ReloadWorker(_get_rpc(), throttles, store=_store)

    app.state.worker = None
    app.state.worker_factory = _default_worker

    def _get_worker():
        if app.state.worker is None:
            app.state.worker = app.state.worker_factory()
        return app.state.worker

    app.state.get_worker = _get_worker

    # ------------------------------------------------------------------
    # T7: onboarding wizard (stateless hidden-input draft, one write txn)
    # ------------------------------------------------------------------
    from . import wizard_ui

    app.include_router(wizard_ui.router)

    # OIDC: /auth/login, /auth/callback, /auth/logout (docs/oidc.md)
    from .oidc import router as oidc_router

    app.include_router(oidc_router)

    return app


# ``uvicorn console.app:app`` convenience for the lab/compose entrypoint.
app = create_app()
