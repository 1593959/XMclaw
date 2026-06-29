#!/usr/bin/env bash
# XMclaw one-shot installer for Linux / macOS / WSL.
#
#   curl -fsSL https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.sh | bash
#
# Installs XMclaw directly from GitHub into an isolated virtualenv at
# ~/.xmclaw-venv, installs optional runtime dependencies, installs
# Playwright Chromium, and drops a launcher in ~/.local/bin/xmclaw.
# Re-running upgrades in place. No sudo required.
set -euo pipefail

VENV_DIR="${XMCLAW_VENV:-$HOME/.xmclaw-venv}"
LAUNCHER_DIR="${XMCLAW_LAUNCHER_DIR:-$HOME/.local/bin}"
LAUNCHER="$LAUNCHER_DIR/xmclaw"
PYTHON="${PYTHON:-python3}"
REF="${XMCLAW_REF:-main}"
REPO_URL="${XMCLAW_REPO:-https://github.com/1593959/XMclaw.git}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "error: $PYTHON not found. Install Python 3.10+ and re-run." >&2
    exit 1
fi
if ! command -v git >/dev/null 2>&1; then
    echo "error: git not found. Install git and re-run." >&2
    exit 1
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "error: $PYTHON is $($PYTHON -V 2>&1); XMclaw needs 3.10 or newer." >&2
    exit 1
fi

mkdir -p "$LAUNCHER_DIR"

if [ ! -d "$VENV_DIR" ]; then
    echo "creating venv at $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install --upgrade "xmclaw[all] @ git+$REPO_URL@$REF"

if ! "$VENV_DIR/bin/python" -m playwright install chromium; then
    echo "warning: Playwright Chromium install failed." >&2
    echo "         Browser automation can be installed later with:" >&2
    echo "         $VENV_DIR/bin/python -m playwright install chromium" >&2
fi

cat > "$LAUNCHER" <<'LAUNCH'
#!/usr/bin/env bash
exec "__VENV__/bin/xmclaw" "$@"
LAUNCH
sed -i.bak "s|__VENV__|$VENV_DIR|" "$LAUNCHER" && rm -f "$LAUNCHER.bak"
chmod +x "$LAUNCHER"

echo
echo "[OK] XMclaw installed from $REPO_URL@$REF."
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
