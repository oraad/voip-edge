# Console review — Python 3.15 threading

Date: 2026-11-08
Scope: `console/`, `Dockerfile.console`, runtime threading model

## Verdict

The console is correct on the Python 3.15 build it ships today: the standard
GIL-enabled interpreter. It is **not yet validated on a free-threaded 3.15
build**, and two small shared-state paths would need locks before claiming
that.

## Verified runtime

`Dockerfile.console` and `pyproject.toml` already target Python 3.15. The
base image is the standard GIL build, not a free-threaded one:

```text
$ python:3.15-slim
Python 3.15.2 (main, Nov  5 2026) [GCC 14.2.0]
build_free 0
gil_enabled True
```

Free-threading is a separate CPython build (`--disable-gil`, Docker tags such
as `3.15t-*` where published), not the default in `python:3.15-slim`. The
3.15 macOS installer now includes free-threading support, but that does not
change a Linux container image.

Python 3.15 threading changes that matter here:

- `RLock` no longer accepts arbitrary positional/keyword arguments. The
  console uses `threading.RLock()` with no arguments.
- `threading` adds iterator helpers (`synchronize_iterator`,
  `concurrent_tee`); the console does not share iterators across threads.
- Free-threaded builds inherit the caller's `contextvars.Context` in new
  threads and enable context-aware warning filters. The console uses neither
  across its worker threads.

## Thread inventory

| Thread | Shared state | Protection |
|---|---|---|
| uvicorn event loop | `app.state.rpc`, `app.state.worker`, `app.state.capabilities` | `_lazy_lock` for lazy creation; assignments are whole-object |
| Starlette threadpool | SQLite connections, reload enqueue, lifecycle | one connection per request, `db.write_lock`, `lifecycle._OP_LOCK` |
| `console-reload-worker` | `_pending`, `_current`, `_in_flight`, `errors` | `_cond` for queue fields; `errors` is unlocked |
| `console-sweeper` | sidecar, capabilities cache, pending reloads | `_OP_LOCK` in lifecycle; capability cache is unlocked |
| `console-capability-probe` | capabilities cache | none |
| RPC callers | one datagram socket per call, request id | `RPC_LOCK` (`RLock`), `_id_lock` |
| stdout logging | `sys.stdout` | `_WRITE_LOCK` |
| login throttle | `_login_attempts` | `_login_lock` |

## Findings

### Important — only if the image moves to a free-threaded build

1. **`ReloadWorker.errors` is read while the worker writes it.**
   `console/reloads.py:62,214,232,235` mutates the dict without `_cond`;
   `console/wizard_ui.py:884-888` iterates `worker.errors.items()` after
   `flush()`. A new request can start a reload between the flush and the
   iteration. With the GIL this is a narrow window; on a free-threaded build
   a concurrent mutation can make `dict.items()` raise `RuntimeError`.
   Fix: guard writes with `_cond` and expose `error_snapshot()` that copies
   under the same lock; use it in the wizard and `app.py:641`.

2. **Capability cache is unsynchronized.**
   `console/capabilities.py:74-76,179-214` stores a tuple plus two scalar
   fields; the sweeper and request threads probe concurrently. Tuple
   assignment is atomic, so this cannot corrupt the object, but readers can
   see a new cache with an old `last_uptime`, and two probes can overwrite
   each other. Fix: one `threading.Lock` around `_cache`, `_last_probe_at`
   and `_last_uptime`, and copy the result while holding it.

3. **Third-party C extensions may re-enable the GIL.**
   The image builds PyYAML's libyaml extension and imports cryptography,
   pydantic-core and MarkupSafe C code. A free-threaded interpreter
   automatically enables the GIL when it imports an extension not marked
   free-threading-safe. That is safe but cancels the benefit. It must be
   measured on the target image; do not assume free-threading is active just
   because the base tag says so.

### Low / informational

4. **OIDC locks are asyncio locks, not thread locks.**
   `console/oidc.py:54-65` lazily creates `asyncio.Lock` objects. OIDC routes
   run on one event loop, so this is correct today. If OIDC work ever moves to
   a threadpool, use a thread lock for cache creation and keep the asyncio lock
   for the event-loop section.

5. **Jinja template cache is unlocked.**
   `console/app.py` shares one `Jinja2Templates` environment. Concurrent
   `get_template()` calls can compile the same template twice and assign the
   cache entry twice; the result is correct. No action needed.

6. **SQLite is connection-per-request.**
   `console/db.py:72` sets `check_same_thread=False`; the connection is used by
   one request at a time, and writes serialize on `write_lock` plus SQLite's
   own lock. This remains correct on a free-threaded build.

7. **Test runtime differs from the image runtime.**
   The repo virtualenv is Python 3.13.5 while the image is 3.15.2. The suite
   passes locally, but it does not prove 3.15 behavior. Add a 3.15 CI job (and,
   separately, a 3.15 free-threaded job) before claiming support.

## Recommendation

- Keep `python:3.15-slim` (GIL build) for production. It needs no threading
  changes and is the supported path.
- If free-threading is wanted, treat it as a project: switch the image tag,
  set `PYTHON_GIL=0`, log `sys._is_gil_enabled()` at startup, fix findings 1
  and 2, audit extension GIL re-enabling, and run the full suite plus
  concurrent request/reload stress tests on the free-threaded build.
- Do not add uvicorn workers. The single-process contract remains independent
  of GIL mode.
