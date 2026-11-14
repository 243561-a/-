@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RobotLive Console
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem Tighten SSH key permissions: copying to another PC widens the ACL and
rem OpenSSH refuses to load the key (UNPROTECTED PRIVATE KEY FILE).
if exist "secrets\qiyuan_t1" (
  icacls "secrets\qiyuan_t1" /inheritance:r /grant:r "%USERNAME%:R" /grant:r "SYSTEM:R" >nul 2>&1
)

rem Prefer the bundled virtual env, fall back to system Python.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" -c "import websocket" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Missing required package, installing...
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo   Install failed. Please install Python 3.10 or newer
    echo   with "Add python.exe to PATH" checked, then run this file again.
    echo.
    pause
    exit /b 1
  )
)

rem Already listening: just open the browser, do not start a second instance.
netstat -ano | findstr ":8780" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo Console is already running, opening browser...
  start "" "http://127.0.0.1:8780/"
  exit /b 0
)

echo Starting console, please wait...
start "" "http://127.0.0.1:8780/"

"%PY%" gui.py --port 8780

echo.
echo Console exited.
pause
