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

MAX_BODY = 1_048_576  # 1 MiB: form/login bodies are tiny


class _BodyTooLarge(Exception):
    """Raised while draining a request whose body exceeds ``MAX_BODY``."""


async def _read_capped_body(request: Request) -> bytes:
    """Read the request body with a hard byte cap and cache it for replay.

    ``Content-Length`` is advisory (HTTP/2 and chunked requests may omit or
    lie about it), so the stream itself is counted.  Caching on the request
    makes Starlette's ``_CachedRequest`` replay the same bytes downstream.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await request._receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > MAX_BODY:
            raise _BodyTooLarge
        if chunk:
            chunks.append(chunk)
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    request._body = body  # consumed here; downstream must replay, not re-read
    return body


# READONLY exemptions: authentication bootstrap and session teardown only.
READONLY_EXEMPT_PATHS = frozenset({"/login", "/logout", "/auth/logout"})


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
        cfg = settings_module.settings
        from .oidc import oidc_enabled as _oidc_enabled

        if cfg.auth_mode not in ("both", "oidc", "token"):
            raise RuntimeError(
                "CONSOLE_AUTH_MODE must be one of: both, oidc, token"
            )
        # Fail closed in SSO-only mode even when credentials are missing;
        # in `both` mode validate as soon as OIDC is actually configured.
        if cfg.auth_mode == "oidc" or _oidc_enabled():
            if not cfg.oidc_issuer or not cfg.oidc_client_id:
                raise RuntimeError(
                    "CONSOLE_AUTH_MODE=oidc requires OIDC_ISSUER and OIDC_CLIENT_ID"
                )
            if not cfg.secure_cookie and not cfg.oidc_insecure:
                raise RuntimeError(
                    "SSO mode requires HTTPS cookies (CONSOLE_SECURE_COOKIE=1); "
                    "set OIDC_INSECURE=1 only for loopback development"
                )

        # Advisory capability probe: a daemon thread, never awaited.  Startup
        # must succeed even when Kamailio is down (docker healthcheck contract).
        threading.Thread(
            target=_startup_probe, name="console-capability-probe", daemon=True
        ).start()

        sweeper_stop = threading.Event()

        def _sweeper() -> None:
            from .lifecycle import resume_pending
            from .sidecar import Sidecar, reissue_pending_reloads

            interval = settings_module.settings.sweeper_interval
            if interval <= 0:
                return
            cfg = settings_module.settings
            while not sweeper_stop.is_set():
                try:
                    sc = Sidecar(Path(cfg.sidecar_dir) / "console.sqlite")
                    worker = _get_worker()
                    resume_pending(
                        sc, cfg.db_path, worker,
                        backup_dir=Path(cfg.sidecar_dir) / "backups",
                    )
                    reissue_pending_reloads(sc, worker, descriptors_module.load())
                    if Path(cfg.rpc_sock).exists():
                        app.state.capabilities = capabilities.probe(_get_rpc())
                except BaseException as exc:
                    observability.log_event("sweeper.error", error=str(exc)[:200])
                if sweeper_stop.wait(max(0.0, float(interval))):
                    return

        sweeper_thread = threading.Thread(
            target=_sweeper, name="console-sweeper", daemon=True
        )
        sweeper_thread.start()
        try:
            yield
        finally:
            sweeper_stop.set()
            sweeper_thread.join(timeout=5.0)
            worker = getattr(app.state, "worker", None)
            if worker is not None and hasattr(worker, "shutdown"):
                try:
                    worker.shutdown()
                except BaseException:
                    pass

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

    # ---- error taxonomy -> HTTP surfaced consistently (plan §4) ----
    from fastapi.responses import JSONResponse

    from .errors import (
        ConflictError,
        ConsoleError,
        DBBusy,
        KamailioUnavailable,
        ReloadError,
        SidecarUnavailable,
        ValidationError,
    )
    from .lifecycle import CollisionError

    _ERROR_STATUS = {
        ValidationError: 422,
        ConflictError: 409,
        CollisionError: 409,
        DBBusy: 503,
        KamailioUnavailable: 503,
        SidecarUnavailable: 503,
        ReloadError: 502,
    }

    @app.exception_handler(ConsoleError)
    async def _console_error(request: Request, exc: ConsoleError):
        status = next(
            (code for cls, code in _ERROR_STATUS.items() if isinstance(exc, cls)), 500
        )
        detail = getattr(exc, "summary", "") or str(exc)
        return JSONResponse(
            {"error": type(exc).__name__, "detail": detail},
            status_code=status,
            headers=NO_STORE,
        )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        # Cap request bodies before any route reads them.  The declared length
        # is an early reject; the stream is counted as well because HTTP/2 and
        # chunked requests can omit or misstate it.  Bodies are buffered (the
        # console has no uploads) and replayed downstream by Starlette.
        try:
            declared = int(request.headers.get("content-length") or "0")
        except ValueError:
            declared = 0
        if declared > MAX_BODY:
            return PlainTextResponse("Request body too large", status_code=413)
        if request.method not in SAFE_METHODS:
            try:
                await _read_capped_body(request)
            except _BodyTooLarge:
                return PlainTextResponse(
                    "Request body too large", status_code=413
                )

        # READONLY gate: refuse writes before CSRF/route dependencies run.
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
            body = await request.body()  # already capped and cached above
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

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        )
        return response

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
        if not expected or not hmac.compare_digest(supplied, expected):
            from .security import record_login_failure

            record_login_failure(client_ip)
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
        issue_session(response, amr="token")
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

    _lazy_lock = threading.RLock()  # worker factory -> rpc factory nests

    def _get_rpc():
        if app.state.rpc is None:
            with _lazy_lock:
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
        throttles: dict[str, int] = {}
        for descriptor in tableset.tables.values():
            throttle = descriptor.reload.throttle_seconds
            throttles[descriptor.table] = throttle
            # Lifecycle/wizard enqueue packed tables under their mtree tree
            # name; forms use the table name.  Both share the same window.
            if descriptor.storage == "packed" and descriptor.reload.params:
                throttles[str(descriptor.reload.params[0])] = throttle

        def _store(status: str, table: str, rpc_name: str, params: list) -> None:
            # Persist pending reloads so a console restart never loses them
            # (plan §5.6); clear on success.  Never let sidecar trouble take
            # the worker down.
            outcome = {"done": "applied", "pending": "scheduled"}.get(status, status)
            try:
                from .sidecar import Sidecar

                sc = Sidecar(Path(settings_module.settings.sidecar_dir) / "console.sqlite")
                if status == "done":
                    sc.clear_pending_reload(table)
                else:
                    sc.record_pending_reload(table, last_error=None)
            except BaseException:
                pass
            observability.log_reload_outcome(
                table=table, status=outcome, rpc=rpc_name
            )

        def _on_enqueue(table: str, rpc_name: str, params: list) -> None:
            # Persist synchronously, before the throttle window: a crash
            # between the write and the RPC must be recoverable by the sweeper.
            try:
                from .sidecar import Sidecar

                sc = Sidecar(
                    Path(settings_module.settings.sidecar_dir) / "console.sqlite"
                )
                sc.record_pending_reload(table, last_error=None)
            except BaseException:
                pass

        return ReloadWorker(
            _get_rpc(), throttles, store=_store, on_enqueue=_on_enqueue
        )

    app.state.worker = None
    app.state.worker_factory = _default_worker

    def _get_worker():
        if app.state.worker is None:
            with _lazy_lock:
                if app.state.worker is None:
                    app.state.worker = app.state.worker_factory()
        return app.state.worker

    app.state.get_worker = _get_worker

    # ------------------------------------------------------------------
    # T7: onboarding wizard (stateless hidden-input draft, one write txn)
    # ------------------------------------------------------------------
    from . import lifecycle_ui, wizard_ui

    app.include_router(lifecycle_ui.router)
    app.include_router(wizard_ui.router)

    # OIDC: /auth/login, /auth/callback, /auth/logout (docs/oidc.md)
    from .oidc import router as oidc_router

    app.include_router(oidc_router)

    return app


# ``uvicorn console.app:app`` convenience for the lab/compose entrypoint.
app = create_app()
