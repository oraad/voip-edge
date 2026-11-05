"""system_ui.py — audit & backups page and the global status-strip context.

Routes (authenticated, read-only):

``GET /system/audit``
    Console-recorded audit rows (newest first) plus the online backup list.
``GET /system/backups``
    The same page with the backups section as its anchor.

The page is deliberately a projection over exported APIs:

* audit rows come from :meth:`console.sidecar.Sidecar.audit_rows`;
* backups come from :func:`console.backups.list_backups` (purpose tag parsed
  from the filename, retention position from the global keep-5 rule);
* capability/health data comes from the TTL cache in
  :mod:`console.capabilities` (never a blocking RPC from a page render).

Nothing here writes to ``edge.sqlite`` or ``console.sqlite``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import backups as backups_module
from . import capabilities
from . import settings as settings_module
from .errors import SidecarUnavailable
from .security import read_session, is_authenticated
from .sidecar import Sidecar

__all__ = [
    "router",
    "backup_dir_path",
    "build_status",
    "sidecar_path",
    "status_context",
]

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}
SIDECAR_FILENAME = "console.sqlite"
BACKUP_DIRNAME = "backups"
AUDIT_LIMIT = 200

_BACKUP_RE = re.compile(r"^edge-(\d{8}T\d{12}Z)-(.+)\.sqlite$")
_OUTCOMES = ("scheduled", "applied", "verified", "failed")

_OUTCOME_BY_STATE = {
    "scheduled": "scheduled",
    "queued": "scheduled",
    "pending": "scheduled",
    "started": "scheduled",
    "applied": "applied",
    "reloaded": "applied",
    "rows_inserted": "applied",
    "rows_deleted": "applied",
    "backed_up": "applied",
    "snapshotted": "applied",
    "collision_checked": "applied",
    "snapshot_discarded": "applied",
    "purged": "verified",
    "verified": "verified",
    "done": "verified",
    "failed": "failed",
    "error": "failed",
}

_TAG_LABELS = {
    "park": "before park",
    "restore": "before restore",
    "destroy": "before destroy",
    "wizard-apply": "wizard apply",
    "operator": "manual",
}


# ---------------------------------------------------------------------------
# paths / auth
# ---------------------------------------------------------------------------
def sidecar_path() -> Path:
    """``console.sqlite`` under ``CONSOLE_SIDECAR_DIR``."""
    return Path(settings_module.settings.sidecar_dir) / SIDECAR_FILENAME


def backup_dir_path() -> Path:
    """Backup directory under ``CONSOLE_SIDECAR_DIR`` (plan §10)."""
    return Path(settings_module.settings.sidecar_dir) / BACKUP_DIRNAME


def _requires_login(request: Request) -> RedirectResponse | None:
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303, headers=NO_STORE)
    return None


def _wants_fragment(request: Request) -> bool:
    return request.headers.get("HX-Request", "").strip().lower() == "true"


# ---------------------------------------------------------------------------
# status strip (injected as a template context processor by console.app)
# ---------------------------------------------------------------------------
def _app_capabilities(app: Any) -> dict[str, bool] | None:
    # The TTL cache is refreshed by the startup probe and the sweeper; the
    # app.state snapshot is only a fallback (a one-shot value goes stale when
    # Kamailio restarts or the first probe ran before the socket existed).
    fresh = capabilities.cached()
    if isinstance(fresh, Mapping):
        return dict(fresh)
    current = getattr(getattr(app, "state", None), "capabilities", None)
    if isinstance(current, Mapping):
        return dict(current)
    return None


def _pending_reload_count() -> tuple[int | None, str | None]:
    """Read the sidecar pending count without creating a missing sidecar."""
    path = sidecar_path()
    if not path.is_file():
        return None, f"sidecar unavailable at {path}"
    try:
        sidecar = Sidecar(path)
        return len(sidecar.pending_reloads()), None
    except (SidecarUnavailable, OSError, ValueError) as exc:
        return None, str(exc)
    except Exception as exc:  # defensive: a status strip must never 500 a page
        return None, str(exc)


def _edge_status(caps: Mapping[str, Any] | None) -> tuple[str, str, str]:
    """Return ``(state, label, tone)`` for the edge-health chip."""
    if not caps:
        return "unknown", "not wired yet", "muted"
    if caps.get("reachable") is False or caps.get("unknown") is True:
        return "unreachable", "edge unreachable", "err"
    if caps.get("ok") is True:
        return "ok", "edge reachable", "ok"
    return "degraded", "edge reachable · capability mismatch", "warn"


def build_status(app: Any = None) -> dict[str, Any]:
    """Data for the status strip; tolerant of an absent RPC and sidecar."""
    cfg = settings_module.settings
    caps = _app_capabilities(app)
    edge_state, edge_label, edge_tone = _edge_status(caps)
    pending, sidecar_error = _pending_reload_count()
    missing = capabilities.missing_capabilities(caps) if caps else []
    uptime = capabilities.last_uptime()
    return {
        "readonly": bool(cfg.readonly),
        "descriptor_problems": list(
            getattr(getattr(app, "state", None), "descriptor_problems", []) or []
        ),
        "pending_reloads": pending,
        "sidecar_available": pending is not None,
        "sidecar_error": sidecar_error,
        "edge": edge_state,
        "edge_text": edge_label,
        "edge_tone": edge_tone,
        "uptime": uptime if edge_state == "ok" else None,
        "capabilities": caps,
        "capabilities_unknown": not bool(caps) or bool(caps.get("unknown")),
        "missing_capabilities": missing,
        "capability_mismatch": bool(missing),
    }


def status_context(request: Request) -> dict[str, Any]:
    """Jinja context processor: ``{"status": {...}}`` on every request."""
    return {"status": build_status(getattr(request, "app", None))}


# ---------------------------------------------------------------------------
# audit / capability views
# ---------------------------------------------------------------------------
def _outcome_for(state: str, detail: str = "") -> str:
    lowered = str(state).strip().lower()
    if lowered in _OUTCOME_BY_STATE:
        return _OUTCOME_BY_STATE[lowered]
    if "fail" in lowered or "error" in lowered or "fail" in str(detail).lower():
        return "failed"
    if "verif" in lowered or "done" in lowered:
        return "verified"
    if "sched" in lowered or "pend" in lowered or "queue" in lowered:
        return "scheduled"
    return "applied"


def _audit_view(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    view: list[dict[str, Any]] = []
    for row in rows:
        outcome = _outcome_for(str(row.get("state", "")), str(row.get("detail", "")))
        view.append(
            {
                **row,
                "outcome": outcome,
                "outcome_label": outcome,
                "badge_tone": {
                    "verified": "ok",
                    "applied": "ok",
                    "scheduled": "warn",
                    "failed": "err",
                }.get(outcome, "muted"),
            }
        )
    return view


def _recent_audit(sidecar: Sidecar, limit: int = AUDIT_LIMIT) -> list[dict[str, Any]]:
    # Newest first, straight from the database (no truncation of a window).
    return sidecar.audit_rows(limit=limit, newest_first=True)


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _human_tag(tag: str) -> str:
    op, separator, target = tag.partition(":")
    label = _TAG_LABELS.get(op, op.replace("-", " ").replace("_", " "))
    return f"{label} · {target}" if separator and target else label


def _parse_backup(path: Path) -> tuple[datetime, str]:
    match = _BACKUP_RE.match(path.name)
    if match:
        stamp, tag = match.group(1), match.group(2)
        try:
            moment = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(
                tzinfo=timezone.utc
            )
            return moment, tag
        except ValueError:
            pass
    return (
        datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
        path.stem.removeprefix("edge-"),
    )


def _when_label(moment: datetime) -> str:
    today = datetime.now(timezone.utc).date()
    if moment.date() == today:
        return moment.strftime("today %H:%M")
    return moment.strftime("%Y-%m-%d %H:%M")


def _backup_view() -> list[dict[str, Any]]:
    files = backups_module.list_backups(backup_dir_path())
    keep = backups_module.KEEP_BACKUPS
    rows: list[dict[str, Any]] = []
    for index, path in enumerate(files):
        moment, tag = _parse_backup(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        position = index + 1
        rows.append(
            {
                "name": path.name,
                "when": _when_label(moment),
                "timestamp": moment.isoformat(),
                "tag": tag,
                "purpose": _human_tag(tag),
                "size": _human_size(size),
                "size_bytes": size,
                "position": position,
                "keep": keep,
                "retention": (
                    f"{position} of {keep} kept"
                    if position <= keep
                    else f"{position} of {keep} — beyond rotation"
                ),
            }
        )
    return rows


def _capability_banner(caps: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not caps:
        return {
            "tone": "warn",
            "message": "capabilities unknown (edge unreachable)",
            "detail": "Reload and lifecycle controls may be refused until the edge answers.",
        }
    if caps.get("reachable") is False or caps.get("unknown") is True:
        return {
            "tone": "warn",
            "message": "capabilities unknown (edge unreachable)",
            "detail": "Reload and lifecycle controls may be refused until the edge answers.",
        }
    missing = capabilities.missing_capabilities(caps)
    if not missing:
        return None
    if "htable.delete" in missing:
        message = "htable.delete unavailable — park and destroy are refused"
    else:
        message = "capability mismatch: " + ", ".join(missing)
    return {
        "tone": "err" if "htable.delete" in missing else "warn",
        "message": message,
        "detail": "The console refuses operations it cannot verify end to end.",
    }


def _render(
    request: Request,
    *,
    anchor: str = "",
) -> HTMLResponse:
    sidecar_error: str | None = None
    audit_rows: list[dict[str, Any]] = []
    open_intents: list[dict[str, Any]] = []
    try:
        path = sidecar_path()
        if not path.is_file():
            raise SidecarUnavailable(f"sidecar unavailable at {path}")
        sidecar = Sidecar(path)
        audit_rows = _recent_audit(sidecar)
        open_intents = [
            row for row in sidecar.list_intents() if row.get("last_error")
        ]
    except (SidecarUnavailable, OSError, ValueError) as exc:
        sidecar_error = str(exc)
    except Exception as exc:  # defensive: the page renders a banner, never 500
        sidecar_error = str(exc)

    caps = _app_capabilities(request.app)
    return request.app.state.templates.TemplateResponse(
        request,
        "system_audit.html",
        {
            "nav": "audit",
            "fragment": _wants_fragment(request),
            "anchor": anchor,
            "audit_rows": _audit_view(audit_rows),
            "open_intents": open_intents,
            "backups": _backup_view(),
            "keep": backups_module.KEEP_BACKUPS,
            "sidecar_error": sidecar_error,
            "capability_banner": _capability_banner(caps),
            "capabilities": caps,
            "restore_note": "stop the stack, replace the file (never hot-swap)",
        },
        headers=NO_STORE,
    )


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.get("/system/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    return _render(request)


@router.get("/system/backups", response_class=HTMLResponse)
def backups_page(request: Request):
    redirect = _requires_login(request)
    if redirect is not None:
        return redirect
    return _render(request, anchor="backups")


@router.get("/audit")
def legacy_audit_page() -> RedirectResponse:
    """The wireframe/temporary nav used ``/audit``; keep old links working."""
    return RedirectResponse("/system/audit", status_code=303, headers=NO_STORE)
