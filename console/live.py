"""live.py — T6 live dashboard: pure view builders + APIRouter factory.

The dashboard is deliberately thin:

* :func:`build_regcache_view` turns a raw ``htable.dump`` result into rows for
  the registered-phones panel.  ``regcache`` has **no reaper** in the current
  edge config (plan §3.8), so the view keeps the raw ``expires`` value at most
  and never renders a countdown.
* :func:`build_dispatcher_view` normalises ``dispatcher.list`` (structured
  JSON or the plain-text form) into rows with the plan §3.5 flag tokens:
  ``A`` active, ``T`` trying, ``I`` inactive, ``D`` disabled, plus ``P``
  probed / ``X`` not probed.  Latency is out of v1 scope and is never shown.
* :func:`edge_health` probes ``core.uptime`` and never raises: any transport
  failure or JSON-RPC fault is reported as ``{"reachable": False, ...}``.

Wiring (the composition root owns this line)::

    app.include_router(build_live_router(lambda: app.state.rpc))

``GET /live`` renders the full page; ``GET /live/partial`` renders the inner
fragment used by htmx polling.  When the edge is unreachable the partial still
returns **200** with the banner, and adds ``HX-Stop-Polling: true`` so htmx
stops hammering a down edge.  Polling resumes on the next page load (or when
the user navigates back); stopping is preferable to a permanent 15 s timeout
loop against an unreachable socket.  The same choice is documented in the
template.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import settings as settings_module
from .rpc import flatten_htable_dump
from .security import CSRF_COOKIE, is_authenticated, read_session

__all__ = [
    "build_dispatcher_view",
    "build_live_router",
    "build_regcache_view",
    "edge_health",
]

NO_STORE = {"Cache-Control": "no-store"}
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates"

_UPTIME_KEYS = ("uptime", "UPTIME", "up_time", "UP_TIME", "uptime_seconds")


# ---------------------------------------------------------------------------
# regcache
# ---------------------------------------------------------------------------


def _split_domain(domain: str) -> tuple[str, str]:
    """Split *domain* at the longest configured allowed suffix.

    Returns ``(prefix, suffix)``.  The suffix is ``""`` when the AOR domain is
    outside the configured tenant suffixes; the full domain stays in ``domain``
    either way, so display code can show whatever it needs.
    """
    if not domain:
        return "", ""
    suffixes = sorted(
        (str(s).strip().lower() for s in settings_module.settings.allowed_suffixes if s),
        key=len,
        reverse=True,
    )
    lowered = domain.lower()
    for suffix in suffixes:
        if lowered == suffix:
            return "", domain
        if lowered.endswith("." + suffix):
            return domain[: -(len(suffix) + 1)], suffix
    return domain, ""


def _transport_from_flow(flow: str) -> str:
    """Return the flow transport, from ``;transport=`` (default ``udp``)."""
    match = re.search(r"(?:[;?&]|\b)transport=([A-Za-z0-9_-]+)", flow or "", re.I)
    if match:
        return match.group(1).lower()
    # A scheme-prefixed value without a transport parameter still tells us the
    # transport (some regcache values are stored as ``tls:host:port``).
    scheme = (flow or "").split(":", 1)[0].strip().lower()
    if scheme in {"udp", "tcp", "tls", "sips"}:
        return "tls" if scheme == "sips" else scheme
    return "udp"


def build_regcache_view(dump: Any) -> list[dict[str, Any]]:
    """Build registered-phone rows from a raw ``htable.dump`` result.

    Each row has ``aor``, ``user``, ``domain``, ``domain_prefix``, ``suffix``,
    ``flow``, ``transport`` and the raw ``expires`` (or ``None``).  There is
    intentionally no ``expires_in``/countdown: regcache has no autoexpire.
    """
    flat = flatten_htable_dump(dump)
    rows: list[dict[str, Any]] = []
    for aor in sorted(flat, key=str.lower):
        entry = flat[aor]
        user, separator, domain = str(aor).partition("@")
        if not separator:
            user, domain = str(aor), ""
        prefix, suffix = _split_domain(domain)
        flow = entry.get("value", "")
        rows.append(
            {
                "aor": str(aor),
                "user": user,
                "domain": domain,
                "domain_prefix": prefix,
                "suffix": suffix,
                "domain_suffix": suffix,
                "flow": flow,
                "transport": _transport_from_flow(flow),
                # Diagnostic only; never rendered as a countdown (plan §3.8).
                "expires": entry.get("expires"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------

_STATE_META = {
    "active": ("Active", "ok"),
    "trying": ("Trying", "warn"),
    "inactive": ("Inactive", "err"),
    "disabled": ("Disabled", "err"),
    "unknown": ("Unknown", "muted"),
}
_STATE_FROM_LETTER = {"A": "active", "T": "trying", "I": "inactive", "D": "disabled"}

_URI_KEYS = ("URI", "uri", "TARGET", "target", "DESTINATION", "destination")
_FLAGS_KEYS = ("FLAGS", "flags", "FLAG", "flag")
_SETID_KEYS = ("SETID", "setid", "SET_ID", "set_id")
_SET_OBJECT_ID_KEYS = ("ID", "id", "SETID", "setid", "SET_ID", "set_id")

# Destination metadata that must not be mistaken for nested targets.
_SKIP_RECURSE_KEYS = frozenset(
    {
        "ATTRS",
        "attrs",
        "PRIORITY",
        "priority",
        "WEIGHT",
        "weight",
        "DUID",
        "duid",
        "SOCKET",
        "socket",
        "LOCATION",
        "location",
        "XAVP",
        "xavp",
        "BODY",
        "body",
        "RURI",
        "ruri",
    }
)

_SET_RE = re.compile(r"\bSET(?:\s*ID)?\s*[:=]\s*(\d+)", re.I)
_FLAGS_RE = re.compile(r"\bFLAGS?\s*[:=]\s*([A-Za-z0-9]+)", re.I)
_FLAGS_TOKEN_RE = re.compile(r"\b([AITD][PX])\b", re.I)
_URI_RE = re.compile(r"((?:sips?|udp|tcp|tls):\S+)", re.I)
_LEADING_SET_RE = re.compile(r"^\s*(\d+)\b")


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _normalise_setid(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    return text or None


def _decode_flags(raw: Any) -> tuple[str, str | None, str]:
    """Return ``(state, probe-token, flags-display)`` for one destination."""
    if raw is None:
        return "unknown", None, ""

    if isinstance(raw, bool):
        return ("active" if raw else "disabled"), ("P" if raw else None), str(raw)

    if isinstance(raw, int):
        # Runtime dispatcher flags: bit 0 inactive, bit 1 trying, bit 2
        # disabled, bit 3 probing.  0 is an active destination.
        state = (
            "disabled"
            if raw & 4
            else "trying"
            if raw & 2
            else "inactive"
            if raw & 1
            else "active"
        )
        probe = "P" if raw & 8 else "X"
        return state, probe, str(raw)

    text = str(raw).strip().upper()
    if not text:
        return "unknown", None, ""
    if text.isdigit():
        return _decode_flags(int(text))

    state_letter = next((letter for letter in text if letter in _STATE_FROM_LETTER), None)
    state = _STATE_FROM_LETTER[state_letter] if state_letter else "unknown"
    probe = "P" if "P" in text else "X" if "X" in text else None
    return state, probe, text


def _label(state: str, probe: str | None, flags: str) -> str:
    title = _STATE_META.get(state, _STATE_META["unknown"])[0]
    if state == "unknown":
        return f"Unknown flags ({flags})" if flags else "Unknown"
    if probe == "P":
        if state in {"trying", "inactive"}:
            return f"{title} · last probe: no answer"
        return f"{title} · probed"
    if probe == "X":
        return f"{title} · not probed"
    return title


def _target_display(target: str) -> str:
    text = str(target).strip()
    if text.lower().startswith(("sip:", "sips:")):
        return text.split(":", 1)[1]
    return text


def _iter_struct_records(
    node: Any, inherited_setid: int | str | None = None
) -> Iterator[tuple[int | str | None, str, Any]]:
    if isinstance(node, list):
        for item in node:
            yield from _iter_struct_records(item, inherited_setid)
        return
    if not isinstance(node, dict):
        return

    setid = inherited_setid
    for key in _SETID_KEYS:
        if key in node:
            setid = _normalise_setid(node[key])
            break

    for set_key in ("SET", "set"):
        if set_key not in node:
            continue
        set_object = node[set_key]
        if isinstance(set_object, dict):
            for key in _SET_OBJECT_ID_KEYS:
                if key in set_object:
                    setid = _normalise_setid(set_object[key])
                    break
        else:
            setid = _normalise_setid(set_object)
        break

    uri = _first(node, _URI_KEYS)
    if isinstance(uri, (dict, list)):
        uri = None
    if uri is not None:
        yield setid, str(uri), _first(node, _FLAGS_KEYS)
        return

    # A flat record may use DEST/TARGET as a scalar URI.
    for key in ("DEST", "dest", "TARGET", "target"):
        value = node.get(key)
        if isinstance(value, str) and _URI_RE.search(value):
            yield setid, value, _first(node, _FLAGS_KEYS)
            return

    for key, value in node.items():
        if key in _SKIP_RECURSE_KEYS:
            continue
        if isinstance(value, (dict, list)):
            yield from _iter_struct_records(value, setid)


def _iter_text_records(text: str) -> Iterator[tuple[int | str | None, str, Any]]:
    stripped = text.strip()
    if not stripped:
        return
    if stripped.startswith(("{", "[")):
        try:
            yield from _iter_struct_records(json.loads(stripped))
            return
        except (TypeError, ValueError, json.JSONDecodeError):
            pass

    records: list[tuple[int | str | None, str, Any]] = []
    pending_setid: int | str | None = None
    pending_uri: str | None = None
    pending_flags: Any = None

    def flush() -> None:
        nonlocal pending_uri, pending_flags
        if pending_uri is not None:
            records.append((pending_setid, pending_uri, pending_flags))
            pending_uri = None
            pending_flags = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        uri_match = _URI_RE.search(line)
        set_match = _SET_RE.search(line)
        flags_match = _FLAGS_RE.search(line)

        if uri_match:
            # A new URI ends the previous (possibly multi-line) DEST block.
            flush()
            if set_match:
                pending_setid = _normalise_setid(set_match.group(1))
            else:
                leading = _LEADING_SET_RE.match(line)
                if leading:
                    pending_setid = _normalise_setid(leading.group(1))

            pending_uri = uri_match.group(1).rstrip(",}")
            if flags_match:
                pending_flags = flags_match.group(1)
                flush()
            else:
                token = _FLAGS_TOKEN_RE.search(line)
                if token:
                    pending_flags = token.group(1)
                    flush()
            continue

        if set_match:
            # A set header ("SET: 11" / "SET ID: 11") may precede DEST blocks.
            flush()
            pending_setid = _normalise_setid(set_match.group(1))
            continue

        if flags_match:
            pending_flags = flags_match.group(1)
            # FLAGS usually follows URI in kamcmd's pretty text output.
            if pending_uri is not None:
                flush()
            continue

        if pending_uri is not None and (
            line.startswith("}") or line.upper().startswith(("DEST", "SET"))
        ):
            flush()

    flush()
    yield from records


def build_dispatcher_view(list_result: Any) -> list[dict[str, Any]]:
    """Build route-set probe rows from ``dispatcher.list`` text or JSON.

    Flag mapping follows plan §3.5: first letter ``A``/``T``/``I``/``D``,
    second letter ``P`` (probed) / ``X`` (not probed).  There is no latency
    field in v1; a failing probe is labelled ``last probe: no answer``.
    """
    if list_result is None:
        return []
    if isinstance(list_result, str):
        records = list(_iter_text_records(list_result))
    elif isinstance(list_result, (dict, list)):
        records = list(_iter_struct_records(list_result))
    else:
        return []

    rows: list[dict[str, Any]] = []
    for setid, target, raw_flags in records:
        state, probe, flags = _decode_flags(raw_flags)
        state_token = {"active": "A", "trying": "T", "inactive": "I", "disabled": "D"}.get(
            state, "?"
        )
        rows.append(
            {
                "setid": "" if setid is None else setid,
                "set_id": "" if setid is None else setid,
                "target": target,
                "target_display": _target_display(target),
                "flags": flags,
                "state": state,
                "state_token": state_token,
                "tone": _STATE_META.get(state, _STATE_META["unknown"])[1],
                "probe": probe or "",
                "probed": (probe == "P") if probe else None,
                "label": _label(state, probe, flags),
            }
        )

    def sort_key(row: dict[str, Any]) -> tuple[int, Any, str]:
        setid = row["setid"]
        if isinstance(setid, int):
            return (0, setid, row["target"])
        return (1, str(setid), row["target"])

    return sorted(rows, key=sort_key)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def _uptime_text(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, str):
        return result.strip() or None
    if isinstance(result, (int, float)) and not isinstance(result, bool):
        return f"{result} s"
    if isinstance(result, dict):
        for key in _UPTIME_KEYS:
            if key in result:
                return _uptime_text(result[key])
        if not result:
            return None
        if len(result) == 1:
            return _uptime_text(next(iter(result.values())))
        return str(result)
    return str(result)


def edge_health(rpc: Any) -> dict[str, Any]:
    """Probe ``core.uptime``; return reachability without ever raising."""
    if rpc is None:
        return {"reachable": False, "uptime": None}
    try:
        result = rpc.call("core.uptime")
    except Exception:  # KamailioUnavailable, RPCFault, or a broken client
        return {"reachable": False, "uptime": None}
    return {"reachable": True, "uptime": _uptime_text(result)}


# ---------------------------------------------------------------------------
# router
# ---------------------------------------------------------------------------


def _load_live_state(rpc: Any) -> dict[str, Any]:
    health = edge_health(rpc)
    reachable = bool(health["reachable"])
    registrations: list[dict[str, Any]] = []
    dispatchers: list[dict[str, Any]] = []
    data_errors: list[str] = []

    if reachable:
        try:
            dump = rpc.call("htable.dump", ["regcache"])
            registrations = build_regcache_view(dump)
        except Exception as exc:  # keep the other panel useful
            data_errors.append(f"registered phones unavailable: {exc}")

        try:
            listing = rpc.call("dispatcher.list")
            dispatchers = build_dispatcher_view(listing)
        except Exception as exc:
            data_errors.append(f"route-set probes unavailable: {exc}")

    return {
        "reachable": reachable,
        "uptime": health["uptime"],
        "registrations": registrations,
        "dispatchers": dispatchers,
        "data_errors": data_errors,
    }


def _get_rpc_safely(get_rpc: Any) -> Any:
    try:
        return get_rpc()
    except Exception:
        return None


def build_live_router(get_rpc: Any) -> APIRouter:
    """Create the authenticated ``/live`` router.

    ``get_rpc`` is a zero-argument callable returning the RPC client (or
    ``None`` while the composition root is still booting).  Include it with::

        app.include_router(build_live_router(lambda: app.state.rpc))
    """
    router = APIRouter()
    fallback_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def _templates(request: Request):
        # Prefer the application's shared environment so base.html gets the
        # T9 status-strip context processor; fall back for bare test apps.
        return getattr(request.app.state, "templates", None) or fallback_templates

    def _render(request: Request, *, partial: bool):
        state = _load_live_state(_get_rpc_safely(get_rpc))
        headers = dict(NO_STORE)
        if partial and not state["reachable"]:
            # 286-capable response (plan T6): stop the poll loop while the edge
            # is down; a page load restarts it.  Deliberately no HX-Reswap.
            headers["HX-Stop-Polling"] = "true"
        return _templates(request).TemplateResponse(
            request,
            "live.html",
            {
                "nav": "live",
                "fragment": partial,
                "session": read_session(request),
                "csrf": request.cookies.get(CSRF_COOKIE),
                "poll_ms": settings_module.settings.poll_ms,
                **state,
            },
            headers=headers,
        )

    def _login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303, headers=NO_STORE)

    @router.get("/live", response_class=HTMLResponse)
    def live_page(request: Request):
        if not is_authenticated(request):
            return _login_redirect()
        return _render(request, partial=False)

    @router.get("/live/partial", response_class=HTMLResponse)
    def live_partial(request: Request):
        if not is_authenticated(request):
            return _login_redirect()
        return _render(request, partial=True)

    return router
