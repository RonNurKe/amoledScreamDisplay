@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "HERE=%~dp0"

echo ============================================================
echo  AMOLED Dashboard - switch backend to Python 3.12 venv
echo ============================================================
echo  1. stop backend processes that run from this venv
echo  2. venv      -^> venv37_backup   (kept for rollback)
echo  3. venv312   -^> venv
echo  4. start venv\Scripts\pythonw.exe main.py
echo.
echo  Downtime about 5 seconds.
echo ============================================================
pause

echo.
echo [1/4] Locating backend processes ...

set "PIDS="
for /f "usebackq delims=" %%p in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^pythonw?\.exe$' -and $_.ExecutablePath -like '%HERE%venv\*' } | ForEach-Object { $_.ProcessId }"`) do set "PIDS=!PIDS! %%p"

if defined PIDS (
  echo       found:!PIDS!
  for %%p in (!PIDS!) do (
    echo       killing pid %%p
    taskkill /PID %%p /T /F >nul 2>&1
  )
  timeout /t 3 /nobreak >nul
) else (
  echo       no process runs from this venv - checking port 8090 owner
  set "OPID="
  for /f "usebackq delims=" %%p in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "try { (Get-NetTCPConnection -LocalPort 8090 -ErrorAction Stop | Where-Object { $_.OwningProcess -ne 0 } | Select-Object -First 1).OwningProcess } catch { }"`) do set "OPID=%%p"
  if not defined OPID (
    echo       nothing is listening on 8090, continue
  ) else (
    echo       port 8090 is owned by pid !OPID!
    for /f "usebackq delims=" %%n in (`powershell -NoProfile -Command "(Get-Process -Id !OPID! -ErrorAction SilentlyContinue).ProcessName"`) do echo       process name: %%n
    choice /c YN /m "      stop it?"
    if errorlevel 2 (
      echo       aborted by user - stop the backend yourself, then run this script again
      goto :done
    )
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=(Get-NetTCPConnection -LocalPort 8090 -ErrorAction SilentlyContinue | Where-Object {$_.State -eq 'Listen' -and $_.OwningProcess -ne 0} | Select-Object -First 1).OwningProcess; while($p){ $pr=Get-CimInstance Win32_Process -Filter ('ProcessId='+$p) -ErrorAction SilentlyContinue; if(-not $pr){ break }; if($pr.Name -ne 'pythonw.exe'){ Write-Host ('      stop: owner is '+$pr.Name+', not a pythonw process'); break }; Write-Host ('      killed pid '+$pr.ProcessId); taskkill /PID $pr.ProcessId /T /F | Out-Null; $p=$pr.ParentProcessId }"
    timeout /t 3 /nobreak >nul
  )
)

echo [2/4] Backing up current venv ...
if not exist "venv\Scripts\python.exe" (
  echo       venv not found, nothing to back up
) else (
  if exist "venv37_backup" rmdir /s /q "venv37_backup"
  move /y "venv" "venv37_backup" >nul
  if errorlevel 1 (
    echo       [FAIL] cannot rename venv - a python process from it is still running
    goto :fail
  )
)

echo [3/4] Activating new venv ...
if not exist "venv312\Scripts\python.exe" (
  echo       [FAIL] venv312 not found, rolling back
  if exist "venv37_backup" move /y "venv37_backup" "venv" >nul
  goto :fail
)
move /y "venv312" "venv" >nul
if errorlevel 1 goto :fail

"venv\Scripts\python.exe" -c "import sys, psutil, hid; print('       python', sys.version.split()[0], '| psutil', psutil.__version__, '| hidapi ok')"
if errorlevel 1 (
  echo       [FAIL] dependency check failed, rolling back
  move /y "venv" "venv312" >nul
  move /y "venv37_backup" "venv" >nul
  goto :fail
)

echo [4/4] Starting backend ...
start "" "venv\Scripts\pythonw.exe" main.py
timeout /t 4 /nobreak >nul
"venv\Scripts\python.exe" -c "import json,urllib.request;d=json.load(urllib.request.urlopen('http://127.0.0.1:8090/api/status',timeout=4));k=d.get('keys',{});m=k.get('matrix') or {};print('       UP | keys:',k.get('available'),'| layer:',k.get('layer'),'| layer_src:',k.get('layer_src'),'| matrix:',m.get('available'),m.get('reason',''))"
if errorlevel 1 echo       [WARN] status query failed - check autostart.log

echo.
echo ============================================================
echo  Done.  Rollback:  venv -^> venv312 , venv37_backup -^> venv
echo ============================================================
goto :done

:fail
echo.
echo [ABORTED] see messages above

:done
pause
exit /b 0
