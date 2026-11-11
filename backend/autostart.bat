@echo off
rem ---------------------------------------------------------------
rem AMOLED dashboard backend - autostart entry point
rem Triggered by scheduled task "AMOLED-Dashboard-Backend" at logon.
rem
rem IMPORTANT 1 - keep this file ASCII-only.
rem cmd.exe reads .bat files in the OEM codepage (GBK on zh-CN
rem Windows). UTF-8 Chinese comments get mis-decoded into an odd
rem number of bytes; a stray half-character swallows the leading
rem letters of the next ASCII word and the remainder is executed
rem as a command. That produced the popup
rem   "...Dashboard-Backend is not recognized as a command"
rem on every logon. All Chinese text lives in autostart_run.py,
rem which Python decodes as UTF-8 correctly.
rem
rem IMPORTANT 2 - launch detached via "start".
rem pythonw.exe is a GUI subsystem binary. Run in the foreground it
rem inherits this cmd console, so sys.stdout is NOT None and
rem autostart_run.py cannot re-bind it to the log file. Started
rem detached it has no console at all, stdout/stderr are None, and
rem the log redirect works as intended. "start" also lets this
rem script return at once, so the console window closes immediately
rem instead of staying open for the whole session.
rem
rem NOTE: because we detach, the scheduled task reports "completed"
rem right away and RestartOnFailure cannot track crashes. Duplicate
rem logons are guarded by the port check inside autostart_run.py.
rem ---------------------------------------------------------------

cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo [autostart] venv pythonw.exe not found, abort > "autostart-error.log"
    exit /b 1
)

start "" "venv\Scripts\pythonw.exe" "autostart_run.py"
exit /b 0
