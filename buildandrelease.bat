@echo off
REM Honey build + release.
REM   buildandrelease.bat          auto-bump patch from CURRENT_VERSION
REM   buildandrelease.bat 3.1.0    release the given version
REM PowerShell prompts for a release comment. Blank uses "Honey <version> release".
setlocal EnableExtensions
cd /d "%~dp0"

set "REL_VERSION=%~1"
set "PS1=%~dp0client\release\release_honey.ps1"

if not exist "%PS1%" (
  echo [ERROR] release script not found: "%PS1%"
  exit /b 1
)

if defined REL_VERSION (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Version "%REL_VERSION%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
)
set "REL_EXIT=%ERRORLEVEL%"

if not "%REL_EXIT%"=="0" (
  echo.
  echo [ERROR] release failed with exit code %REL_EXIT%.
)
exit /b %REL_EXIT%
