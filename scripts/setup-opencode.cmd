@echo off
setlocal
title ChatGPT Web2API for OpenCode - Setup
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-opencode.ps1" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo Setup failed with exit code %CODE%.
  pause
)
exit /b %CODE%
