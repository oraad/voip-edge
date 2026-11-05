# VoIP Edge Console

Web admin console for the Kamailio edge proxy described in `docs/plan.md`.

- Spec: `docs/plan.md` (v2, review-audited) · Wireframes: `docs/wireframe.html`
- Research: `docs/research-ui-stack.md`, `docs/research-kamailio-edge.md`
- Tasks: T0–T10 with TDD sub-cards, see plan §8.

## Layout

```
console/        Python package (settings, errors, descriptors, db, rpc, reloads, lifecycle, sidecar)
web/            FastAPI app + Jinja2 templates + static (vendored htmx/PicoCSS)
tests/          unit/ api/ live/ e2e/ fixtures/
```

## Dev

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install fastapi "uvicorn[standard]" jinja2 pytest httpx respx selectolax
pytest -q
```

Nothing in this repo touches the live edge directly during tests; `tests/live/`
uses the loopback lab pattern from `kamailio-edge/tests/` (see plan §8).
