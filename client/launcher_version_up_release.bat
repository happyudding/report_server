@echo off
REM Honey LAUNCHER-LAYOUT release in one shot (versions\ + Honey.exe launcher).
REM   launcher_version_up_release.bat           -> bump patch from CURRENT_VERSION
REM   launcher_version_up_release.bat 3.2.0     -> explicit version
REM
REM A http(s) argument overrides the server address the build points at, for
REM releasing to a TEST server (server\mypc_start.bat) instead of production.
REM Order does not matter - an argument starting with "http" is the URL, any
REM other argument is the version.
REM   launcher_version_up_release.bat 3.2.0 http://192.168.0.10:8090
REM   launcher_version_up_release.bat http://192.168.0.10:8090
REM Without it the address comes from server\env\server.env (production).
REM server.env itself is never modified, so the next run without the argument
REM is a production release again.
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

REM Classify the arguments: "http..." is the server URL, anything else is the
REM version. Done in a subroutine so both positions accept either kind.
set "REL_VERSION="
set "REL_URL="
if not "%~1"=="" call :classify "%~1"
if not "%~2"=="" call :classify "%~2"

set "PSARGS="
if defined REL_VERSION set "PSARGS=%PSARGS% -Version %REL_VERSION%"
if defined REL_URL set "PSARGS=%PSARGS% -ServerUrl %REL_URL%"

if defined REL_URL (
  echo [WARN] Server address override: %REL_URL%
  echo [WARN] This build will ONLY talk to that server - test releases only.
  echo.
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"%PSARGS%
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

REM --- Sort one argument into REL_URL or REL_VERSION ---------------------------
REM Values set here survive the call - same setlocal scope as the caller.
:classify
set "ARG=%~1"
if /i "%ARG:~0,4%"=="http" (
  set "REL_URL=%ARG%"
) else (
  set "REL_VERSION=%ARG%"
)
exit /b 0
