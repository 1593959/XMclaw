# XMclaw one-shot installer for Windows (PowerShell).
#
#   irm https://raw.githubusercontent.com/1593959/XMclaw/main/scripts/install.ps1 | iex
#
# Installs XMclaw directly from GitHub into an isolated venv at
# %USERPROFILE%\.xmclaw-venv, installs optional runtime dependencies,
# installs Playwright Chromium, and drops a launcher in
# %USERPROFILE%\.xmclaw-venv\Scripts\ (added to User PATH if missing).
# No admin required.
#
# Service registration is intentionally out of scope — see
# deploy/windows-service/ for NSSM or pywin32 paths.

$ErrorActionPreference = "Stop"

$VenvDir = if ($env:XMCLAW_VENV) { $env:XMCLAW_VENV } else { Join-Path $HOME ".xmclaw-venv" }
$Python  = if ($env:PYTHON)       { $env:PYTHON       } else { "python" }
$Ref     = if ($env:XMCLAW_REF)   { $env:XMCLAW_REF   } else { "main" }
$RepoUrl = if ($env:XMCLAW_REPO)  { $env:XMCLAW_REPO  } else { "https://github.com/1593959/XMclaw.git" }

function Test-PythonVersion {
    param([string]$Exe)
    try {
        $raw = & $Exe -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')" 2>$null
    } catch {
        return $false
    }
    if (-not $raw) { return $false }
    $parts = $raw.Trim().Split(".")
    return ([int]$parts[0] -gt 3) -or (([int]$parts[0] -eq 3) -and ([int]$parts[1] -ge 10))
}

if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    Write-Error "error: $Python not found. Install Python 3.10+ from https://python.org and re-run."
    exit 1
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "error: git not found. Install Git for Windows from https://git-scm.com/download/win and re-run."
    exit 1
}
if (-not (Test-PythonVersion -Exe $Python)) {
    Write-Error "error: $Python is too old (need 3.10+)."
    exit 1
}

if (-not (Test-Path $VenvDir)) {
    Write-Host "creating venv at $VenvDir"
    & $Python -m venv $VenvDir
}

$VenvPip     = Join-Path $VenvDir "Scripts\pip.exe"
$VenvXmclaw  = Join-Path $VenvDir "Scripts\xmclaw.exe"

& $VenvPip install --upgrade pip
& $VenvPip install --upgrade "xmclaw[all] @ git+$RepoUrl@$Ref"

try {
    & $VenvDir\Scripts\python.exe -m playwright install chromium
} catch {
    Write-Warning "Playwright Chromium install failed. Browser automation can still be installed later with: python -m playwright install chromium"
}

Write-Host ""
Write-Host "[OK] XMclaw installed from $RepoUrl@$Ref."
Write-Host "  venv:     $VenvDir"
Write-Host "  launcher: $VenvXmclaw"
Write-Host ""

# Add venv's Scripts dir to the USER PATH (persists across sessions)
# only if it isn't already there — overwriting with duplicates pollutes
# $env:Path for every future shell.
$ScriptsDir = Join-Path $VenvDir "Scripts"
$UserPath   = [Environment]::GetEnvironmentVariable("Path", "User")
if ($UserPath -notlike "*$ScriptsDir*") {
    $NewPath = if ($UserPath) { "$UserPath;$ScriptsDir" } else { "$ScriptsDir" }
    [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
    Write-Host "  added $ScriptsDir to user PATH (open a new shell to pick it up)"
    Write-Host ""
}

Write-Host "Next: xmclaw config init"
Write-Host "Then: xmclaw start"
