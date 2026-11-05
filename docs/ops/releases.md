# Releases and versioning

The console ships as one immutable container image. Code, templates, vendored
static assets and `console/tables.yaml` are all **baked into that image**; none
of them is mounted at runtime. The compose file chooses an image tag, and that
tag is the unit of release and rollback.

## Version source

`pyproject.toml` is the human-readable version source:

```toml
[project]
name = "voip-edge-console"
version = "0.1.0"
```

The image tag must match it: `voip-edge-console:0.1.0`. Use an immutable
version tag (optionally pinned by digest in production), never `latest` for a
deployment that needs rollback.

## What requires a rebuild

Rebuild the image, bump the version and update `compose.yml` when any of these
change:

- `console/*.py`, `console/rules/*` or any other Python module;
- `console/tables.yaml` (descriptor grammar, validators, reloads, help text);
- `web/templates/*` or `web/static/*` (including newly vendored htmx/PicoCSS);
- `pyproject.toml`, the Dockerfile or the pinned dependency versions.

Not part of the image and safe to change without a rebuild:

- `.env` / stack environment values (restart the container after changing
  them; settings are read at import);
- `compose.yml` volume paths, resource limits and healthcheck settings;
- docs.

`tables.yaml` is intentionally not a runtime mount: a descriptor edit that
silently diverges from the code (or from the DB's `version` bookkeeping rows)
is a deployment fault, not a config tweak. The startup consistency check
refuses writes for a table whose declared version does not match the DB.

## Bump procedure

1. **Pick the next version** with semantic versioning. Until 1.0, use
   `0.MINOR.PATCH`; bump MINOR for a new feature set (e.g. T5/T9 landing), PATCH
   for packaging/docs-only changes.
2. **Update `pyproject.toml`** and add the entry to `CHANGELOG.md` under
   `Unreleased`, then move it to the new version on release.
3. **Run the suite** from the repo virtualenv (no network):
   ```bash
   .venv/bin/python -m pytest -q
   .venv/bin/python -m compileall console
   ```
   The current baseline is **676 passed** (619+ was verified before the
   parallel T5/T9 test additions).
4. **Build once** from a clean checkout:
   ```bash
   docker build \
     --build-arg CONSOLE_GID="$(getent group console | cut -d: -f3)" \
     -t voip-edge-console:<version> .
   ```
   Or use Arcane → Images → Build with the same Dockerfile and tag. Tag
   `voip-edge-console:<version>`; keep the previous tag on the host for
   rollback.
5. **Update `compose.yml`** `image:` to the new tag (and optionally pin the
   digest). Do not change `console/tables.yaml` through a mount.
6. **Deploy with `CONSOLE_READONLY=1`** first. Verify `/health`, the login,
   the Homes list, the status strip, and the Audit & backups page. Then set
   `CONSOLE_READONLY=0`.
7. **Tag the source release** if you use git (`v<version>`), and archive the
   `CHANGELOG.md` entry.

## Compatibility and rollback

- **Kamailio schema:** the console never changes `edge.sqlite`'s schema; it
  edits rows in place. There is no edge migration step in a release.
- **Sidecar schema:** `console.sqlite` is migrated forward with
  `PRAGMA user_version` at startup. A newer image may migrate it to a schema an
  older image does not understand. Before rolling back, either restore the
  sidecar copy taken before the upgrade or accept that the old image may refuse
  the sidecar. The console reports `SidecarUnavailable` rather than corrupting
  it.
- **Database:** a release does not imply a restore. If a release goes wrong,
  roll back the image first; restore `edge.sqlite` only through the
  stopped-stack runbook (`docs/ops/backups.md`).
- **Old images:** keep at least the immediately previous tag. Image retention is
  independent of backup rotation (keep-5).

## Release checklist (short)

- [ ] version bumped in `pyproject.toml` + `CHANGELOG.md`
- [ ] `pytest -q` and `compileall console` pass
- [ ] image built once and tagged with the version
- [ ] `compose.yml` image tag updated (and previous tag retained)
- [ ] `CONSOLE_READONLY=1` deploy verified
- [ ] Pangolin SSO resource still reachable and `/health` unchanged
- [ ] `CONSOLE_READONLY=0` after read verification
