# syntax=docker/dockerfile:1
#
# VoIP Edge Console — single-process image.
#
# Runtime contract (plan §4/§10):
#   * ONE uvicorn worker. Reload serialization, reload/queue state, the
#     transaction write-lock and session state are process-local, so a second
#     worker would corrupt behaviour (not merely duplicate work).
#   * host networking is used by compose.yml; the app binds CONSOLE_BIND.
#   * console/tables.yaml is baked in with the code; changing it requires a
#     rebuild, never a runtime mount.
#   * /health is console liveness only and always answers 200, even when
#     Kamailio is unreachable (see the HEALTHCHECK below).
#
# Build once, then deploy the pinned image tag:
#   docker build -t voip-edge-console:0.1.0 \
#     --build-arg CONSOLE_GID="$(getent group console | cut -d: -f3)" .
#
# The image defaults to the non-root "console" user. The stock edge stack runs
# Kamailio (and its root-owned SQLite WAL/SHM files) as root, so compose.yml
# deliberately overrides the runtime user to 0:0; see docs/ops/deploy.md for
# the non-root alternative (matching UID/GID plus ACL/umask work).

FROM python:3.13-slim

ARG CONSOLE_UID=10001
ARG CONSOLE_GID=10001

# Safe standalone defaults; compose/.env supplies the production values.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CONSOLE_BIND=127.0.0.1 \
    CONSOLE_PORT=8080 \
    CONSOLE_DB_PATH=/data/edge.sqlite \
    CONSOLE_SIDECAR_DIR=/var/lib/console \
    CONSOLE_RPC_SOCK=/run/kamailio/kamailio_rpc.sock

# curl is only for HEALTHCHECK. No compiler/toolchain is installed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Pinned to the versions verified in this repo's .venv. PyYAML and MarkupSafe
# are direct imports (descriptors.py, app.py), so they are pinned explicitly.
# No test dependencies (pytest, httpx, selectolax) belong in the image.
RUN pip install --no-cache-dir \
      "fastapi==0.162.0" \
      "uvicorn==0.54.0" \
      "jinja2==3.1.6" \
      "MarkupSafe==3.0.4" \
      "PyYAML==6.0.3"

# The group name matters: Kamailio creates the jsonrpcs datagram socket with
# dgram_group "console" and mode 0660. For the non-root recipe the name must
# resolve to the SAME GID in this image and inside the Kamailio container (its
# /etc/group), not merely be added with compose group_add.
RUN groupadd --gid "${CONSOLE_GID}" console \
 && useradd --uid "${CONSOLE_UID}" --gid "${CONSOLE_GID}" \
      --no-create-home --home-dir /app --shell /usr/sbin/nologin console

WORKDIR /app
COPY pyproject.toml ./
COPY console ./console
COPY web ./web
# The build context is the repo root; drop editor/build noise without needing a
# separate .dockerignore.
RUN find /app -type d -name __pycache__ -prune -exec rm -rf {} + \
 && mkdir -p /var/lib/console /data \
 && chown -R "${CONSOLE_UID}:${CONSOLE_GID}" /app /var/lib/console
# /data is a bind mount. Its ownership is set on the host (docs/ops/deploy.md);
# the image cannot fix a root-owned edge.sqlite or its WAL/SHM siblings.

USER console
EXPOSE 8080

# Console liveness only. This must NOT call Kamailio or /live: a down edge must
# leave the container healthy so the operator still gets the console banner.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://${CONSOLE_BIND}:${CONSOLE_PORT}/health" >/dev/null || exit 1

# workers=1 is a hard requirement (process-local locks, queues and session
# state). Do not add --workers or a multi-process manager here.
CMD ["sh", "-c", "exec uvicorn console.app:app --host \"${CONSOLE_BIND}\" --port \"${CONSOLE_PORT}\" --workers 1"]
