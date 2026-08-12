@echo off
REM Honey LAUNCHER-LAYOUT release in one shot (versions\ + Honey.exe launcher).
REM   launcher_version_up_release.bat           -> bump patch from CURRENT_VERSION
REM   launcher_version_up_release.bat 3.2.0     -> explicit version
REM
REM This does NOT touch the existing pipeline (build_zip.bat / buildandrelease.bat /
REM release_honey.ps1 / build_honey.spec) - those keep producing the OLD layout.
REM
REM On failure NOTHING changes: CURRENT_VERSION is bumped only after a successful
REM build AND copy, unlike release_honey.ps1 which bumps first.
REM
REM KEEP THIS FILE PURE ASCII WITH CRLF LINE ENDINGS (see build_zip.bat for why).
setlocal EnableExtensions
cd /d "%~dp0"

set "PS1=%~dp0release\release_launcher.ps1"
if not exist "%PS1%" (
  echo [ERROR] Release script not found:
  echo   "%PS1%"
  pause
  exit /b 1
)

if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Version %1
)
set "REL_EXIT=%ERRORLEVEL%"

if not "%REL_EXIT%"=="0" (
  echo.
  echo [ERROR] Launcher-layout release failed with exit code %REL_EXIT%.
  echo [ERROR] Nothing changed - CURRENT_VERSION and server\releases\ are untouched.
  echo.
  pause
  exit /b %REL_EXIT%
)

echo.
echo === DONE ===
pause
exit /b 0
