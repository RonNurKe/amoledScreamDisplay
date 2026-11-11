@echo off
title Frontend debug server (optional)
setlocal
cd /d "%~dp0.."

rem ============================================================
rem  前端本地调试服务器（可选）
rem  正常使用请直接运行 backend\start_backend.bat，
rem  后端会自动托管本目录的静态页面。
rem  本脚本仅用于在没有后端时单独调试前端页面（会显示本地模拟数据）。
rem ============================================================

echo.
echo [提示] 正常使用请直接运行 backend\start_backend.bat（推荐）。
echo [提示] 本脚本只托管静态页面，页面将使用内置模拟数据演示。
echo [提示] 手机调试时，手机与电脑必须在同一局域网，
echo        并访问 http://电脑局域网IP:8092/
echo.

set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 --version >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo [错误] 未找到 Python，无法启动本地调试服务器。
    pause
    exit /b 1
)

echo [信息] 启动本地静态服务器: http://0.0.0.0:8092/  (Ctrl+C 停止)
%PY% -m http.server 8092 --bind 0.0.0.0 --directory frontend
pause
