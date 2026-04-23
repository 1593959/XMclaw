#!/usr/bin/env bash
# XMclaw one-shot installer for Linux / macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.sh | bash
#
# Installs the latest published `xmclaw` into an isolated virtualenv at
# ~/.xmclaw-venv and drops a launcher in ~/.local/bin/xmclaw. The venv
# is fully owned by the user — no sudo. Re-running upgrades in place.
#
# Skips service registration — that's platform-specific and belongs in
# deploy/systemd or deploy/launchd, not a generic installer. Run
# `xmclaw start` or set up the relevant unit file afterwards.
set -euo pipefail

VENV_DIR="${XMCLAW_VENV:-$HOME/.xmclaw-venv}"
LAUNCHER_DIR="${XMCLAW_LAUNCHER_DIR:-$HOME/.local/bin}"
LAUNCHER="$LAUNCHER_DIR/xmclaw"
PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON not found. Install Python 3.10+ and re-run." >&2
    exit 1
fi

# Python 3.10+ required — XMclaw uses PEP 604 union syntax throughout.
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "error: $PYTHON is $($PYTHON -V 2>&1); XMclaw needs 3.10 or newer." >&2
    exit 1
fi

mkdir -p "$LAUNCHER_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "creating venv at $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
fi

# `pip install -U xmclaw` upgrades on repeat runs. Using the venv's pip
# directly avoids inheriting a globally-activated venv that'd scramble
# which interpreter hosts XMclaw.
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install --upgrade xmclaw

# Launcher is a thin shim so users can `xmclaw ...` without activating
# the venv. Re-created each run so a venv relocation (renamed $HOME,
# moved to another disk) is a one-command fix.
cat > "$LAUNCHER" <<'LAUNCH'
#!/usr/bin/env bash
exec "__VENV__/bin/xmclaw" "$@"
LAUNCH
sed -i.bak "s|__VENV__|$VENV_DIR|" "$LAUNCHER" && rm -f "$LAUNCHER.bak"
chmod +x "$LAUNCHER"

echo
echo "✓ XMclaw installed."
echo "  venv:     $VENV_DIR"
echo "  launcher: $LAUNCHER"
echo
if ! echo ":$PATH:" | grep -q ":$LAUNCHER_DIR:"; then
    echo "  NOTE: $LAUNCHER_DIR is not in PATH."
    echo "  Add this to your shell rc:"
    echo "    export PATH=\"$LAUNCHER_DIR:\$PATH\""
    echo
fi
echo "Next: xmclaw config init"
echo "Then: xmclaw start"
