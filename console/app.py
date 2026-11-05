"""app.py — FastAPI application factory and T2 routes.

Only the skeleton lives here: health, token login/logout, the homes landing
placeholder and the shared auth/CSRF plumbing.  Table-specific routes arrive
with T3; ``web/`` must contain zero table logic (plan §4).
"""
from __future__ import annotations

import hmac
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from .security import (
    CSRF_COOKIE,
    clear_session,
    csrf_protected,
    csrf_valid,
    is_authenticated,
    issue_session,
    login_rate_exceeded,
    read_session,
)
from . import settings as settings_module

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

NO_STORE = {"Cache-Control": "no-store"}


def _mono(value: object) -> Markup:
    """Jinja filter: render *value* inside a ``span.mono`` (escaped)."""
    return Markup(f'<span class="mono">{escape(value)}</span>')


def _wants_fragment(request: Request) -> bool:
    """True when htmx asks for an inner fragment (case-insensitive header)."""
    return request.headers.get("HX-Request", "").strip().lower() == "true"


def _base_context(request: Request) -> dict[str, object]:
    """Template globals shared by every page (T3/T4 routes get these free)."""
    return {"csrf": request.cookies.get(CSRF_COOKIE)}


def create_app() -> FastAPI:
    app = FastAPI(title="VoIP Edge Console", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(
        directory=str(TEMPLATES_DIR), context_processors=[_base_context]
    )
    templates.env.filters["mono"] = _mono

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def csrf_middleware(request: Request, call_next):
        # Double-submit: unsafe methods need X-CSRF-Token == csrf cookie.
        # POST /login is the single bootstrap exemption (no cookie yet).
        if csrf_protected(request) and not csrf_valid(request):
            return PlainTextResponse(
                "CSRF token missing or invalid", status_code=403, headers=NO_STORE
            )
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
        return templates.TemplateResponse(
            request,
            "login.html",
            {"show_chrome": False, "error": None},
            headers=NO_STORE,
        )

    @app.post("/login")
    async def login_submit(request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if login_rate_exceeded(client_ip):
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

    @app.get("/homes", response_class=HTMLResponse)
    def homes(request: Request):
        session = read_session(request)
        if session is None:
            return RedirectResponse("/login", status_code=303, headers=NO_STORE)
        fragment = _wants_fragment(request)
        return templates.TemplateResponse(
            request,
            "homes.html",
            {
                "nav": "homes",
                "fragment": fragment,
                "session": session,
            },
            headers=NO_STORE,
        )

    return app


# ``uvicorn console.app:app`` convenience for the lab/compose entrypoint.
app = create_app()
