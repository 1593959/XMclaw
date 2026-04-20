# XMclaw Environment Setup
Write-Host '========================================'
Write-Host '  XMclaw Environment Setup'
Write-Host '========================================'
Write-Host ''

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $projectDir '.venv'

Write-Host '[1/4] Checking Python...'
try {
    $pythonVersion = python --version 2>&1
    Write-Host "  $pythonVersion"
} catch {
    Write-Host 'ERROR: Python not found. Install Python 3.10+ first.' -ForegroundColor Red
    Read-Host 'Press Enter to exit'
    exit 1
}

Write-Host '[2/4] Creating venv...'
if (Test-Path $venvDir) {
    Write-Host '  .venv exists, skipping'
} else {
    python -m venv $venvDir
}

Write-Host '[3/4] Installing dependencies...'
& (Join-Path $venvDir 'Scripts\Activate.ps1')
python -m pip install --upgrade pip
pip install -e .
pip install playwright httpx

Write-Host '[4/4] Installing Playwright browsers...'
playwright install chromium

Write-Host ''
Write-Host '========================================'
Write-Host '  Done! Venv: ' + $venvDir
Write-Host '========================================'
Write-Host ''
Write-Host 'To start the daemon:'
Write-Host '  .venv\Scripts\Activate.ps1'
Write-Host '  xmclaw start'
Write-Host ''
Read-Host 'Press Enter to exit'
