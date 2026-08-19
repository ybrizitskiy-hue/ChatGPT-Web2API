@echo off
setlocal
chcp 65001 >nul 2>nul
title ChatGPT-Web2API + OpenCode Diagnostics

set "PYTHON=%LOCALAPPDATA%\ChatGPT-Web2API-OpenCode\venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo ERROR: Managed OpenCode bridge installation was not found.
  echo Run setup-opencode.cmd first.
  echo.
  pause
  exit /b 1
)

echo Running OpenCode integration doctor...
echo This command does not print the local API key.
echo.
"%PYTHON%" -m chatgpt_web2api.opencode_setup doctor
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo All doctor checks passed.
) else (
  echo One or more doctor checks failed. Copy this window or send a screenshot.
)
echo.
pause
exit /b %RC%
