# XMclaw Docker image — local-first AI agent runtime
#
# Build:  docker build -t xmclaw:latest -f deploy/docker/Dockerfile .
# Run:    docker run -d -p 8766:8766 \
#           -v ~/.xmclaw:/root/.xmclaw \
#           -v /path/to/workspace:/workspace \
#           -e XMC__LLM__ANTHROPIC__API_KEY=sk-ant-... \
#           --name xmclaw xmclaw:latest
#
# Data persistence: mount ~/.xmclaw to keep events, memory,
# persona files, and config across container restarts.
# The workspace mount gives the agent access to your files.

FROM python:3.11-slim-bookworm AS base

LABEL org.opencontainers.image.title="XMclaw"
LABEL org.opencontainers.image.description="Local-first AI agent runtime"
LABEL org.opencontainers.image.source="https://github.com/1593959/XMclaw"

# ── system deps ────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git build-essential \
    && rm -rf /var/lib/apt/lists/*

# ── app user ───────────────────────────────────────────────────
RUN useradd --create-home --shell /bin/bash xmclaw
WORKDIR /home/xmclaw/app

# ── Python deps ────────────────────────────────────────────────
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[all]" \
    && pip install --no-cache-dir websockets

# ── app source ─────────────────────────────────────────────────
COPY xmclaw/ ./xmclaw/
COPY daemon/ ./daemon/
COPY docs/ ./docs/

# ── runtime config ─────────────────────────────────────────────
RUN mkdir -p /home/xmclaw/.xmclaw/v2 /workspace \
    && chown -R xmclaw:xmclaw /home/xmclaw /workspace
COPY daemon/config.example.json /home/xmclaw/.xmclaw/config.example.json

USER xmclaw
EXPOSE 8766

# Populate an initial config from env vars if no config.json exists,
# then start the daemon.
COPY deploy/docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
