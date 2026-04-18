@echo off
chcp 65001 >/dev/null 2>&1
REM XMclaw 一键启动 - 双击此文件即可
title XMclaw

REM 激活 venv 并启动 daemon，自动打开浏览器
"%~dp0.venv\Scripts\activate.bat" >/dev/null 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to activate .venv
    pause
    exit /b 1
)

xmclaw start
