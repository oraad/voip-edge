# Container images on GitHub Packages (GHCR)

Both edge images are published to **GitHub Container Registry (GHCR)** from
this repository:

| Image | Workflow | Package |
|---|---|---|
| Console | `.github/workflows/publish-ghcr.yml` | `ghcr.io/<owner>/voip-edge-console` |
| rtpengine | `.github/workflows/publish-rtpengine.yml` | `ghcr.io/<owner>/voip-edge-rtpengine` |

`<owner>` is the GitHub user/org that owns the repository;
`github.repository_owner` is used automatically and lowercased for GHCR. The
package names are fixed so they do not change if the repository is renamed.

## Publishing

Both workflows run on:

- **push of a release tag** `v*` — the console gets the tag without `v`
  (for example `v0.1.11` → `0.1.11`); the rtpengine image gets the **upstream
  rtpengine source release** (`mr26.3.1.2` by default), because its version
  tracks upstream, not the console;
- **manual dispatch** — tags the build with the short SHA and also moves
  `latest`. For rtpengine the dispatch input `rtpe_version` selects the
  upstream source tag; leaving it empty uses the default in
  [`Dockerfile.rtpengine`](../../Dockerfile.rtpengine).

Both images also get a short-SHA tag and `latest`.

The rtpengine image is built from
[`Dockerfile.rtpengine`](../../Dockerfile.rtpengine), which compiles upstream
source. A manual rtpengine run is the way to test a new upstream release
without creating a console version tag.

`latest` is emitted by an explicit `type=raw,value=latest` rule, so both
workflows always publish it alongside the version and short-SHA tags.

The workflows use the repository-scoped `GITHUB_TOKEN` with `packages: write`;
no PAT is needed to publish. The first publish creates the package under the
owner/org. Set package visibility and access in GitHub → Packages → the image →
Settings.

Prerequisites:

1. The repository is on GitHub and Actions are enabled.
2. `Workflow permissions` allows read/write, or the workflow's `permissions:`
   block is retained (it requests `packages: write` explicitly).
3. A release tag is pushed, or the workflow is started manually.

## Consuming a published image

For a **public** package no login is needed. For a **private** package, log in
with a PAT that has `read:packages`:

```bash
echo "$CR_PAT" | docker login ghcr.io -u <github-user> --password-stdin
```

Set the image variables in the deployment environment (both compose files keep
a local-build default so nothing breaks without GHCR):

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
newest published build and is intended for lab/staging only.

## Release checklist

1. Bump the version in the repository and tag it (`vX.Y.Z`).
2. Push the tag; both workflows publish their image.
3. Verify the package tags in GitHub → Packages: the console has `X.Y.Z`; the
   rtpengine image has the upstream `mr...` tag. For private packages, check
   that the deployment host can `docker login ghcr.io`.
4. Set `CONSOLE_IMAGE` / `RTPENGINE_IMAGE` in the deployment environment and
   pull. Keep the previous image tag for rollback.
5. After a successful rollout, consider pinning the verified digest in `.env`.
