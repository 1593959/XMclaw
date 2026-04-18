@echo off
chcp 65001 >/dev/null 2>&1
echo ========================================
echo   XMclaw Environment Setup
echo ========================================
echo.

set "PROJECT_DIR=%~dp0"
set "VENV_DIR=%PROJECT_DIR%.venv"

echo [1/4] Checking Python...
python --version >/dev/null 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)

echo [2/4] Creating venv: %VENV_DIR%
if exist "%VENV_DIR%" (
    echo   .venv exists, skipping
) else (
    python -m venv "%VENV_DIR%"
)

echo [3/4] Installing dependencies...
call "%VENV_DIR%\Scriptsctivate.bat"
python -m pip install --upgrade pip
pip install -e .
pip install playwright httpx

echo [4/4] Installing Playwright browsers...
playwright install chromium

echo.
echo ========================================
echo   Done! Venv: %VENV_DIR%
echo ========================================
echo.
echo To start:
echo   .venv\Scriptsctivate
echo   xmclaw start
echo.
pause
