@echo off
setlocal
cd /d "%~dp0"
title AI Router

if not exist ".venv\Scripts\python.exe" (
  echo AI Router virtual environment was not found.
  echo Run setup_windows.ps1 first.
  pause
  exit /b 1
)

echo Starting AI Router...
".venv\Scripts\python.exe" gui.py
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo ========================================
  echo AI Router exited with error code: %ERR%
  echo ========================================
  pause
)
exit /b %ERR%
