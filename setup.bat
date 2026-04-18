@echo off
chcp 65001 >/dev/null 2>&1
echo ========================================
echo   XMclaw Setup (Global Install)
echo ========================================
echo.

set "PROJECT_DIR=%~dp0"

REM Find system Python (not copaw)
where python >/dev/null 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python not found in PATH
    pause
    exit /b 1
)

REM Get Python path
for /f "delims=" %%i in ('where python') do set PY_PATH=%%i
echo Found Python: %PY_PATH%

REM Check it is NOT copaw
echo %PY_PATH% | findstr /I "copaw" >/dev/null
if %ERRORLEVEL% EQU 0 (
    echo.
    echo ERROR: Current Python is from copaw!
    echo Please use your system Python, e.g.:
    echo   C:\\Users\\15978\\AppData\\Local\\Programs\\Python\\Python310\\python.exe -m venv .venv
    echo.
    pause
    exit /b 1
)

echo.
echo [1/3] Creating .venv...
if exist ".venv" (
    echo   .venv exists, skipping
) else (
    python -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo ERROR: Failed to create venv
        pause
        exit /b 1
    )
)

echo.
echo [2/3] Installing xmclaw globally...
.venv\Scripts\pip install -e .
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip install failed
    pause
    exit /b 1
)
.venv\Scripts\pip install playwright httpx
playwright install chromium

echo.
echo ========================================
echo   Done!
echo ========================================
echo.
echo Now you can run from ANY terminal:
echo   xmclaw start
echo   xmclaw chat
echo   xmclaw status
echo.
pause
