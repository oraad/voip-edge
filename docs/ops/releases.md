# Releases and versioning

The console ships as one immutable image. Code, templates, static assets and
`console/tables.yaml` are baked in; nothing is mounted at runtime.

`pyproject.toml` is the version source. The image carries the same version:
`voip-edge-console:<version>`. The rtpengine image tracks the upstream source
release (`mr26.3.1.2` by default), not the console version.

## Rebuild needed

- `console/*.py`, `console/rules/*`, `console/tables.yaml`
- `web/templates/*`, `web/static/*`
- `pyproject.toml`, `Dockerfile.console`, pinned dependencies

Not part of the image: `.env`, `compose.yaml` volume/resource/healthcheck
changes, docs.

## Bump

1. Bump `pyproject.toml`; update `CHANGELOG.md`.
2. Run:
   ```bash
   .venv/bin/python -m pytest -q
   .venv/bin/python -m compileall console
   ./tools/check-privacy.sh --all
   ```
   Baseline: **786 passed, 1 deselected**.
3. Build once:
   ```bash
   docker build -f Dockerfile.console \
     --build-arg CONSOLE_GID="$(getent group console | cut -d: -f3)" \
     -t voip-edge-console:<version> .
   ```
4. Update `compose.yaml` (or `.env` `CONSOLE_IMAGE`) and deploy with
   `CONSOLE_READONLY=1`. Verify `/health`, login, Homes, status, backups.
5. Enable writes, tag the source release.

## Rollback

- **Image:** set the previous tag and redeploy.
- **Edge schema:** the console never changes it.
- **Sidecar:** migrations are forward-only. Restore the pre-upgrade sidecar
  copy or accept that the older image may refuse it.
- **Database:** restore only through `docs/ops/backups.md`, with the stack
  stopped.
- Keep at least the previous image tag.

## Checklist

- [ ] version bumped; changelog updated
- [ ] tests, compileall and privacy guard pass
- [ ] console image built and tagged
- [ ] `v<version>` pushed; both GHCR workflows pass
- [ ] `compose.yaml`/`.env` image tag updated
- [ ] read-only deploy verified; then writes enabled
