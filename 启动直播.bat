@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RobotLive Pipeline
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem Usage:
rem   double-click                 -> use the last remembered room
rem   start-live.bat 98466269897   -> use the given room id
rem This window IS the bot brain. Closing it stops replying.
rem No WorkBuddy needed, no console page needed. The collector auto-starts.

if exist "secrets\qiyuan_t1" (
  icacls "secrets\qiyuan_t1" /inheritance:r /grant:r "%USERNAME%:R" /grant:r "SYSTEM:R" >nul 2>&1
)

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

set ROOM=%~1
if "%ROOM%"=="" set ROOM=auto

echo.
echo   RobotLive starting, room: %ROOM%
echo   Close this window to stop. Ctrl+C also works.
echo.

"%PY%" -u pipeline.py %ROOM% --source go

echo.
echo   Stopped.
pause
