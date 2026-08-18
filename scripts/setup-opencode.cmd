@echo off
setlocal
chcp 65001 >nul 2>nul
title ChatGPT-Web2API + OpenCode Installer

set "SCRIPT=%~dp0setup-opencode.ps1"
if not exist "%SCRIPT%" (
  echo ERROR: setup-opencode.ps1 must be in the same folder as this file.
  pause
  exit /b 1
)

echo ChatGPT-Web2API + OpenCode one-click installer
echo.
echo This installer will automatically install Git and Python 3.13 if they are missing.
echo It will then configure the local Web2API bridge for OpenCode.
echo No OpenAI API key is required.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo Installation failed with exit code %RC%.
  echo Read the error above, then run this file again.
) else (
  echo Installation finished successfully.
)
echo.
pause
exit /b %RC%
