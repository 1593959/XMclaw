#!/usr/bin/env bash
# XMclaw — cloudflared tunnel helper (Linux / macOS / WSL, Wave 17).
#
# Exposes the local daemon (port 8766) via Cloudflare quick tunnel.
# See scripts/tunnel.ps1 header for the why / when / security note.
#
# Install cloudflared first:
#   macOS:  brew install cloudflared
#   Debian: curl -L --output cloudflared.deb \
#             https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
#             && sudo dpkg -i cloudflared.deb
#   Other:  https://github.com/cloudflare/cloudflared/releases

set -euo pipefail

PORT="${PORT:-8766}"
HOST="${HOST:-127.0.0.1}"

c=$'\033[36m'
r=$'\033[31m'
g=$'\033[32m'
y=$'\033[33m'
n=$'\033[0m'

section() { printf "\n%s── %s ──%s\n" "$c" "$1" "$n"; }

if ! command -v cloudflared >/dev/null 2>&1; then
    printf "%s❌ cloudflared 未安装%s\n\n" "$r" "$n"
    echo "安装方式 (任选其一):"
    echo "  macOS:  brew install cloudflared"
    echo "  Debian: curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb && sudo dpkg -i cloudflared.deb"
    echo "  其他平台:  https://github.com/cloudflare/cloudflared/releases"
    exit 1
fi

section "检查 daemon (http://${HOST}:${PORT}/api/v2/health)"
if curl -fsS --max-time 3 "http://${HOST}:${PORT}/api/v2/health" >/dev/null 2>&1; then
    printf "%s✅ daemon 在线%s\n" "$g" "$n"
else
    printf "%s⚠️ daemon 没起来或没在 %s:%s%s\n" "$y" "$HOST" "$PORT" "$n"
    echo "   先跑 'xmclaw start' 再回来。"
    exit 1
fi

section "拉 Cloudflare quick tunnel"
echo "URL 会在下面输出，复制到手机浏览器即可。"
echo "Ctrl+C 退出。"
echo

exec cloudflared tunnel --url "http://${HOST}:${PORT}"
