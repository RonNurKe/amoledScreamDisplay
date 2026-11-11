@echo off
title Windows Hardware Monitor AMOLED Dashboard - Backend
setlocal

rem ============================================================
rem  后端一键启动脚本
rem  1. 检查 Python 版本（需要 3.7+）
rem  2. 创建/复用虚拟环境 venv
rem  3. 安装依赖
rem  4. 启动后端
rem ============================================================

cd /d "%~dp0"

rem ---- 1. 检测 Python 解释器（优先 python，其次 py 启动器） ----
set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY (
    py -3 --version >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    echo [错误] 未找到 Python，请先安装 Python 3.7 或更高版本。
    echo        下载地址: https://www.python.org/downloads/
    echo        安装时务必勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

rem ---- 检查版本是否 >= 3.7 ----
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 7) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [错误] 当前 Python 版本过低，本项目需要 Python 3.7 或更高版本。
    %PY% --version
    pause
    exit /b 1
)
for /f "tokens=*" %%i in ('%PY% --version') do echo [信息] 检测到 %%i

rem ---- 2. 创建虚拟环境（已存在则跳过） ----
if not exist "venv\Scripts\python.exe" (
    echo [信息] 正在创建虚拟环境 venv ...
    %PY% -m venv venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败，将直接使用系统 Python 继续。
    )
)

rem ---- 3. 安装依赖（虚拟环境优先） ----
if exist "venv\Scripts\python.exe" (
    set "RUNPY=venv\Scripts\python.exe"
) else (
    set "RUNPY=%PY%"
)
echo [信息] 正在安装依赖（psutil 等）...
%RUNPY% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [警告] 依赖安装失败。若为网络问题，可尝试:
    echo        %RUNPY% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo [提示] 即使不安装 psutil，项目也会以 Mock 模拟数据模式运行。
)

rem ---- 4. 启动后端 ----
echo.
echo [信息] 正在启动后端 ... (按 Ctrl+C 停止)
echo [提示] 如手机无法访问页面，请放行 Windows 防火墙端口:
echo        控制面板 -^> Windows Defender 防火墙 -^> 高级设置 -^> 入站规则 -^> 新建规则
echo        放行 TCP 8090（网页）与 TCP 8091（WebSocket）。
echo.
%RUNPY% main.py

echo.
echo [信息] 后端已停止。
pause
