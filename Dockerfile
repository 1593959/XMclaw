# XMclaw daemon container image.
#
# Multi-stage build:
#   * `builder` resolves dependencies against requirements-lock.txt into a
#     throwaway layer so the final image doesn't ship pip, the build
#     toolchain, or the package index cache.
#   * `runtime` copies the prepared site-packages plus the xmclaw source
#     onto python:3.11-slim. Result is ~150 MiB vs ~900 MiB for a naive
#     single-stage build with ``pip install -e .``.
#
# The daemon binds 0.0.0.0:8765 inside the container — localhost-only
# is the host default, but a container's network namespace is already
# isolated, so exposing to the container's own 0.0.0.0 is correct. The
# user-facing surface is still controlled by the port publish flag
# (`-p 127.0.0.1:8765:8765` keeps it local; `-p 8765:8765` opens it).
#
# Persistent state (events.db, memory.db, skills/, pairing token) lives
# under ``/data`` inside the container, exported via VOLUME. Mount a host
# dir with ``-v $HOME/.xmclaw:/data`` to preserve data across rebuilds.
#
# Secrets: never bake API keys into the image. Pass them at runtime via
# ``XMC__llm__anthropic__api_key`` env vars or by mounting
# ``/data/config.json``.

# ----------------------------------------------------------------------
# Stage 1: build wheels
# ----------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# System deps for sqlite-vec native build. Kept minimal — tree-sitter and
# playwright browsers are optional extras and intentionally left out; a
# user who needs them can extend this Dockerfile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy only dependency manifests first so Docker's layer cache keeps the
# install step warm when source-only changes happen.
COPY pyproject.toml requirements-lock.txt ./

RUN pip install --no-cache-dir --prefix=/install -r requirements-lock.txt

# Now copy the package source and install it editable-style into the
# same prefix. Using --no-deps because all deps are already pinned via
# requirements-lock.txt — we don't want pip to resolve again.
COPY xmclaw/ ./xmclaw/
COPY README.md ./
RUN pip install --no-cache-dir --prefix=/install --no-deps .

# ----------------------------------------------------------------------
# Stage 2: runtime
# ----------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# Non-root user — daemon never needs root, and the data volume should be
# chown'd to this UID so bind-mounts on host Linux work without sudo.
RUN useradd --create-home --uid 1000 --shell /bin/bash xmclaw

COPY --from=builder /install /usr/local

# Default data dir. XMC_DATA_DIR is the one documented lever that
# relocates the workspace (see xmclaw/utils/paths.py).
ENV XMC_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /data && chown xmclaw:xmclaw /data
VOLUME ["/data"]

USER xmclaw
WORKDIR /home/xmclaw

EXPOSE 8765

# ``xmclaw serve`` runs the FastAPI app in-process without spawning a
# background daemon; that's the right shape for container PID-1. Binding
# to 0.0.0.0 lets the Docker port-publish flag decide reachability.
ENTRYPOINT ["xmclaw", "serve", "--host", "0.0.0.0", "--port", "8765"]
