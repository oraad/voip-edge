# Container images on GitHub Packages (GHCR)

Both edge images are published to **GitHub Container Registry (GHCR)**:

| Image | Workflow | Package |
|---|---|---|
| Console | `voip-edge/.github/workflows/publish-ghcr.yml` | `ghcr.io/<owner>/voip-edge-console` |
| rtpengine | `kamailio-edge/.github/workflows/publish-ghcr.yml` | `ghcr.io/<owner>/voip-edge-rtpengine` |

`<owner>` is the GitHub user/org that owns the repository; `github.repository_owner`
is used automatically. The image package name is fixed so it does not change if
a repository is renamed.

## Publishing

The workflows run on:

- **push of a release tag** `v*` — the image version is the tag without `v`
  (for example `v0.1.11` → `0.1.11`), plus a short-SHA tag and `latest`;
- **manual dispatch** — tags the build with the short SHA and also moves
  `latest`; for rtpengine the dispatch input `rtpe_version` selects the
  upstream source tag (default `mr26.3.1.2`).

`latest` is emitted by an explicit `type=raw,value=latest` rule, so both
workflows always publish it alongside the version and short-SHA tags.

They use the repository-scoped `GITHUB_TOKEN` with `packages: write`; no PAT
is needed to publish. The first package publish creates the package under the
owner/org. Set package visibility and access in GitHub → Packages → the image →
Settings.

Prerequisites:

1. Each repository is pushed to GitHub (the console repo and the edge repo that
   owns `docker/Dockerfile.rtpengine`).
2. Actions are enabled for the repository.
3. `Workflow permissions` allows read/write or the workflow's `permissions:`
   block is retained (it requests `packages: write` explicitly).
4. A release tag is pushed, or the workflow is started manually.

## Consuming a published image

For a **public** package no login is needed. For a **private** package, log in
with a PAT that has `read:packages`:

```bash
echo "$CR_PAT" | docker login ghcr.io -u <github-user> --password-stdin
```

Set the image variables in the stack environment (both compose files keep a
local-build default so nothing breaks without GHCR):

```bash
# console stack (.env)
CONSOLE_IMAGE=ghcr.io/<owner>/voip-edge-console:0.1.11

# edge stack (docker/.env)
RTPENGINE_IMAGE=ghcr.io/<owner>/voip-edge-rtpengine:latest
```

Then pull and deploy:

```bash
docker compose pull
docker compose up -d
```

Pin production deployments to an immutable version tag, and preferably the
manifest digest (`name@sha256:...`), not `latest`. The `latest` tag follows the
newest published tag and is intended for lab/staging only.

## Release checklist

1. Bump the version in the owning repository and tag it (`vX.Y.Z`).
2. Push the tag; the relevant workflow publishes the image.
3. Verify the package tags in GitHub → Packages and (for private packages) that
   the deployment host can `docker login ghcr.io`.
4. Set `CONSOLE_IMAGE` / `RTPENGINE_IMAGE` in the deployment environment and
   pull. Keep the previous image tag for rollback.
5. After a successful rollout, consider pinning the verified digest in `.env`.
