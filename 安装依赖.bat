@echo off
chcp 65001 >nul
cd /d "%~dp0"
title RobotLive Setup
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem One-click setup: detect first, install only what is missing.
rem   1) find Python: bundled venv > py launcher > python on PATH
rem   2) if none, offer to install Python 3.12 via winget
rem   3) hand over to setup.py for full self-check + deps

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

if not defined PY (
  py -3 --version >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  python --version >nul 2>&1
  if not errorlevel 1 set "PY=python"
)

if defined PY goto have_python

echo.
echo   Python not found.
echo.
where winget >nul 2>&1
if errorlevel 1 goto manual

choice /c YN /m "   Install Python 3.12 automatically via winget (about 30MB, needs internet)"
if errorlevel 2 goto manual

echo.
echo   Installing Python 3.12, please wait...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo   Done. Close this window, then run this file again.
echo.
pause
exit /b 0

:manual
echo   Please install Python 3.10 or newer manually:
echo     https://www.python.org/downloads/windows/
echo   Remember to check "Add python.exe to PATH", then run this file again.
echo.
pause
exit /b 1

:have_python
echo.
echo   Using Python: %PY%
"%PY%" setup.py
if errorlevel 1 (
  echo.
  echo   Self-check failed. Fix the [X] items above and run this file again.
  echo.
  pause
  exit /b 1
)

echo.
pause
