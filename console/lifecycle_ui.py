"""lifecycle_ui.py — Park / Restore / Destroy for a home (T8 UI surface).

The state machines live in ``console.lifecycle``; this module only wraps them
in authenticated, CSRF-protected routes with consequence-naming confirmations
(plan §5.3/§6).  All calls run off the event loop via Starlette's threadpool
because lifecycle performs blocking sqlite + RPC work.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import lifecycle, settings as settings_module
from .errors import ConsoleError, ValidationError
from .security import is_authenticated
from .sidecar import Sidecar

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}


def _require_auth(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303, headers=NO_STORE)
    return None


def _paths() -> tuple[Path, Path, Path]:
    cfg = settings_module.settings
    sidecar_path = Path(cfg.sidecar_dir) / "console.sqlite"
    backup_dir = Path(cfg.sidecar_dir) / "backups"
    # Sidecar refuses an absent parent (never delete what we cannot snapshot),
    # so the UI creates its own state directory on first use.
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    return sidecar_path, Path(cfg.db_path), backup_dir


def _worker(request: Request):
    return request.app.state.get_worker()


def _render(request: Request, template: str, context: dict, status_code: int = 200):
    return request.app.state.templates.TemplateResponse(
        request, template, context, status_code=status_code, headers=NO_STORE
    )


def _flash(domain: str, action: str) -> str:
    return f"/homes?flash={action}%20{domain}&tone=applied"


def _error_page(exc: ConsoleError) -> HTMLResponse:
    from markupsafe import escape

    status = 422 if isinstance(exc, ValidationError) else 503
    detail = getattr(exc, "summary", "") or str(exc)
    return HTMLResponse(
        '<div class="banner err" role="alert">'
        "<strong>Lifecycle operation failed</strong>"
        f"<p>{escape(detail)}</p>"
        "<p>The last committed state is preserved; fix the cause and retry "
        "(the sweeper also retries unfinished intents).</p>"
        "</div>",
        status_code=status,
        headers=NO_STORE,
    )


@router.get("/homes/{domain}/park", response_class=HTMLResponse)
def park_confirm(request: Request, domain: str):
    denied = _require_auth(request)
    if denied:
        return denied
    return _render(request, "lifecycle_confirm.html", {"action": "park", "domain": domain})


@router.post("/homes/{domain}/park")
async def park_submit(request: Request, domain: str):
    denied = _require_auth(request)
    if denied:
        return denied
    sidecar_path, edge_path, backup_dir = _paths()
    try:
        await run_in_threadpool(
            lifecycle.park, sidecar_path, edge_path, _worker(request), domain,
            backup_dir=backup_dir,
        )
    except ConsoleError as exc:
        return _error_page(exc)
    return RedirectResponse(_flash(domain, "Parked"), status_code=303, headers=NO_STORE)


@router.post("/homes/{domain}/restore")
async def restore_submit(request: Request, domain: str):
    denied = _require_auth(request)
    if denied:
        return denied
    sidecar_path, edge_path, backup_dir = _paths()
    try:
        await run_in_threadpool(
            lifecycle.restore, sidecar_path, edge_path, _worker(request), domain,
            backup_dir=backup_dir,
        )
    except lifecycle.CollisionError as exc:
        from markupsafe import escape

        items = "".join(f"<li>{escape(item)}</li>" for item in exc.collisions)
        return HTMLResponse(
            '<div class="banner err" role="alert">'
            "<strong>Restore blocked by a collision</strong>"
            f"<ul>{items}</ul>"
            "<p>No edge rows were changed. Fix the collision and retry.</p>"
            "</div>",
            status_code=409,
            headers=NO_STORE,
        )
    except ConsoleError as exc:
        return _error_page(exc)
    return RedirectResponse(_flash(domain, "Restored"), status_code=303, headers=NO_STORE)


@router.get("/homes/{domain}/destroy", response_class=HTMLResponse)
def destroy_confirm(request: Request, domain: str):
    denied = _require_auth(request)
    if denied:
        return denied
    sidecar_path, edge_path, _ = _paths()
    active = False
    try:
        active = lifecycle._tenant_row(Path(edge_path), domain) is not None
    except Exception:
        active = False
    parked = False
    if not active:
        try:
            parked = Sidecar(sidecar_path).load_snapshot(domain) is not None
        except Exception:
            parked = False
    return _render(
        request,
        "lifecycle_confirm.html",
        {"action": "destroy", "domain": domain, "parked": parked},
    )


@router.post("/homes/{domain}/destroy")
async def destroy_submit(request: Request, domain: str):
    denied = _require_auth(request)
    if denied:
        return denied
    sidecar_path, edge_path, backup_dir = _paths()
    body = await request.body()
    from urllib.parse import parse_qs

    confirm = parse_qs(body.decode("utf-8", "replace")).get("confirm", [""])[0]
    try:
        await run_in_threadpool(
            lifecycle.destroy, sidecar_path, edge_path, _worker(request), domain,
            confirm=confirm, backup_dir=backup_dir,
        )
    except ConsoleError as exc:
        return _error_page(exc)
    return RedirectResponse(_flash(domain, "Destroyed"), status_code=303, headers=NO_STORE)
