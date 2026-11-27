# syntax=docker/dockerfile:1
#
# Interview Assistant API and worker image (M-12, D-79). One image, two roles:
#   API     (default):  uvicorn app.main:get_app --factory ...
#   worker:             dramatiq app.workers.tasks ...         (override the command)
#   migrate (one-shot): alembic upgrade head                   (override the command)
#
# Build from the repository root:   docker build -f services/api/Dockerfile -t interview-assistant-api services/api
#
# Pinning: base images are pinned by tag here; pin them by digest in your registry mirror or CI
# (`docker buildx imagetools inspect <image>`), and rebuild regularly to pick up OS patches.
#
# The image contains NO secrets and no .env file. Configuration is injected at run time
# (docs/DEPLOYMENT.md). It runs as an unprivileged user and needs a writable /tmp and the file-storage
# volume only, so it also runs with a read-only root file system.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.16

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
# Dependencies first (cached until the lock file changes). --frozen: exactly what uv.lock pins.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    APP_ENV=production
# A fixed, unprivileged identity (no shell, no home). Match it on the volume you mount for uploads.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/uploads && chown app:app /data/uploads
WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root app ./app
COPY --chown=root:root alembic ./alembic
COPY --chown=root:root alembic.ini ./alembic.ini
USER 10001:10001
EXPOSE 8000
# Liveness only (never depends on Postgres or Redis; /ready is for the load balancer).
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]
# --no-proxy-headers: never trust a client-supplied X-Forwarded-For (docs/OPERATIONS.md, auth limits).
# --ws-max-size: bound one WebSocket frame (R-47). --timeout-graceful-shutdown: on SIGTERM stop
# accepting, let in-flight requests finish, then exit (the orchestrator's stop grace period must be longer).
CMD ["uvicorn", "app.main:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--ws-max-size", "65536", "--no-proxy-headers", "--timeout-graceful-shutdown", "25"]
