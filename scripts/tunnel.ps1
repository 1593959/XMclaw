# XMclaw — cloudflared tunnel helper (Windows / PowerShell, Wave 17).
#
# Exposes the local daemon (port 8765) to the public internet via a
# Cloudflare quick tunnel. No login, no domain, no DNS setup — you get
# a one-shot ``*.trycloudflare.com`` URL that's valid until you Ctrl+C
# this process.
#
# Use cases:
#   * 在外面用手机访问 web UI（飞书已经能在外网，但 web UI 默认只听
#     127.0.0.1，没隧道就过不去）
#   * 给别人临时演示
#   * 触发器测试时让外部 webhook 能 ping 自己
#
# Security note:
# - quick-tunnel URL is unauthenticated at the cloudflared layer.
#   XMclaw's own pairing-token middleware (auth_check) still gates
#   every /api/v2/* request, so a random opener of the URL hits a 401.
#   But the surface IS exposed — use only when you actively need it.
# - For long-lived access prefer a named tunnel: `cloudflared tunnel
#   login` + `cloudflared tunnel create xmclaw` + ingress config.
#
# Install cloudflared first:
#   winget install Cloudflare.cloudflared
# Or download from: https://github.com/cloudflare/cloudflared/releases

param(
    [int]$Port = 8765,
    [string]$Host = "127.0.0.1"
)

$ErrorActionPreference = "Stop"

function Write-Section($msg) {
    Write-Host ""
    Write-Host "── $msg ──" -ForegroundColor Cyan
}

# 1. Check cloudflared is installed.
$cf = Get-Command cloudflared -ErrorAction SilentlyContinue
if (-not $cf) {
    Write-Host "❌ cloudflared 未安装" -ForegroundColor Red
    Write-Host ""
    Write-Host "安装方式 (任选其一):"
    Write-Host "  winget install Cloudflare.cloudflared"
    Write-Host "  scoop install cloudflared"
    Write-Host "  或从 https://github.com/cloudflare/cloudflared/releases 直接下载 exe 放进 PATH"
    exit 1
}

# 2. Check daemon is responding.
Write-Section "检查 daemon (http://${Host}:${Port}/api/v2/health)"
try {
    $resp = Invoke-WebRequest -Uri "http://${Host}:${Port}/api/v2/health" -UseBasicParsing -TimeoutSec 3
    Write-Host "✅ daemon 在线 (status=$($resp.StatusCode))" -ForegroundColor Green
} catch {
    Write-Host "⚠️ daemon 没起来或没在 ${Host}:${Port}" -ForegroundColor Yellow
    Write-Host "   先跑 'xmclaw start' 再回来。"
    exit 1
}

# 3. Run the quick tunnel.
Write-Section "拉 Cloudflare quick tunnel"
Write-Host "URL 会在下面输出，复制到手机浏览器即可。"
Write-Host "Ctrl+C 退出。"
Write-Host ""

# cloudflared writes its banner + the *.trycloudflare.com URL on stderr
# — let it through naturally. We just exec.
& cloudflared tunnel --url "http://${Host}:${Port}"
