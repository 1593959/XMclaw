#!/bin/bash
# XMclaw Docker entrypoint — initialise config from env vars,
# then start the daemon.
set -e

CONFIG_DIR="${XMC_DATA_DIR:-$HOME/.xmclaw}"
CONFIG_FILE="${CONFIG_DIR}/v2/daemon_config.json"

mkdir -p "${CONFIG_DIR}/v2"

# Generate a minimal config from environment variables if none exists.
if [ ! -f "$CONFIG_FILE" ]; then
    cat > "$CONFIG_FILE" <<EOF
{
  "workspace_root": "${XMC_WORKSPACE:-/workspace}",
  "llm": {
    "profiles": [
      {
        "id": "default",
        "provider": "${XMC_LLM_PROVIDER:-anthropic}",
        "model": "${XMC_LLM_MODEL:-claude-sonnet-4-6}",
        "api_key": "${XMC_LLM_API_KEY:-}",
        "base_url": "${XMC_LLM_BASE_URL:-}"
      }
    ]
  },
  "tools": {
    "enable_bash": true,
    "enable_web": true,
    "enable_browser": false,
    "allowed_dirs": ["${XMC_WORKSPACE:-/workspace}"]
  },
  "memory": {
    "enabled": true
  },
  "agent": {
    "persona": "assistant"
  }
}
EOF
    echo "[xmclaw] Generated config at $CONFIG_FILE"
fi

exec python -m xmclaw.cli.main serve --host 0.0.0.0 --port "${XMC_PORT:-8766}"
