# Container images on GHCR

Both images are published from this repository:

| Image | Workflow |
|---|---|
| `ghcr.io/<owner>/voip-edge-console` | `.github/workflows/publish-images.yml` |
| `ghcr.io/<owner>/voip-edge-rtpengine` | `.github/workflows/publish-images.yml` |

Publishing runs on a `v*` tag or manual dispatch. The console gets the version
without `v`; rtpengine gets its upstream release tag (`mr26.3.1.2` by
default). Both also get a short-SHA tag and `latest`.

`latest` is for lab/staging. Pin a version tag or digest in production.

## Use a published image

For private packages:

```bash
echo "$CR_PAT" | docker login ghcr.io -u <github-user> --password-stdin
```

Set in `.env`:

```bash
CONSOLE_IMAGE=ghcr.io/<owner>/voip-edge-console:0.1.11
RTPENGINE_IMAGE=ghcr.io/<owner>/voip-edge-rtpengine:latest
```

Then `docker compose pull && docker compose up -d`.

## Release

1. Bump the version and tag `vX.Y.Z`.
2. Push; both workflows publish.
3. Verify tags in GitHub → Packages.
4. Set both image variables and pull.
5. Keep the previous tag for rollback.
