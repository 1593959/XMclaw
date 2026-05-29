# XMclaw 开发环境隔离启动脚本
# 数据目录: ./.data (隔离自 ~/.xmclaw)
# 端口: 8766 (主端口)

$env:XMC_DATA_DIR = (Resolve-Path "$PSScriptRoot\.data").Path
$port = 8766

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "XMclaw DEV daemon" -ForegroundColor Cyan
Write-Host "Data dir : $env:XMC_DATA_DIR" -ForegroundColor Yellow
Write-Host "Port     : $port" -ForegroundColor Yellow
Write-Host "Config   : daemon/config.json" -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan

$python = "$PSScriptRoot\.venv\Scripts\python.exe"
& $python -m xmclaw.cli.main serve `
    --host 127.0.0.1 `
    --port $port `
    --config daemon/config.json
