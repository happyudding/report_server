@echo off
REM Honey LAUNCHER-LAYOUT rebuild at the CURRENT version (NO bump).
REM   build_zip_new.bat                              rebuild + republish current version
REM   build_zip_new.bat http://192.168.0.10:8090     same, pointed at a TEST server
REM
REM This is the launcher-layout counterpart of build_zip.bat: it produces the NEW
REM structure (Honey.exe launcher + versions\<ver>\HoneyApp.exe) by calling
REM release\release_launcher.ps1 -NoBump, then republishes it (copy to
REM server\releases, refresh version.json sha256, append release_log.txt).
REM
REM A same-version rebuild does NOT trigger client updates - clients compare by
REM version number, so 3.2.0 over 3.2.0 is "already latest" to them. It only
REM refreshes the ZIP the server hands out. For a real client-facing update use
REM launcher_version_up_release.bat instead: it increments the version.
REM
REM Failure changes nothing: CURRENT_VERSION and server\releases\ are only
REM touched after a successful build AND copy (see release_launcher.ps1).
REM
REM KEEP THIS FILE PURE ASCII WITH CRLF LINE ENDINGS.
REM cmd.exe mis-parses LF-only batch files (lines get merged and fragments run as
REM commands, e.g. "3.0.0 was unexpected at this time"), and a parse error kills the
REM script before it reaches pause, so the window closes with no visible error.
REM Korean text belongs in release_launcher.ps1, which is UTF-8 with BOM.
setlocal EnableExtensions
cd /d "%~dp0"

set "PS1=%~dp0release\release_launcher.ps1"
if not exist "%PS1%" (
  echo [ERROR] Release script not found:
  echo   "%PS1%"
  pause
  exit /b 1
)

REM Optional argument: a http(s) URL overrides the server address the build is
REM pointed at (test server). Without it the address comes from
REM server\env\server.env, which this never modifies.
set "REL_URL="
set "ARG=%~1"
if not "%ARG%"=="" (
  if /i "%ARG:~0,4%"=="http" (
    set "REL_URL=%ARG%"
  ) else (
    echo [ERROR] Unexpected argument: %ARG%
    echo         This script does not take a version - it rebuilds the current one.
    echo         Only a http:// server URL is accepted here.
    echo         To release a NEW version use launcher_version_up_release.bat
    pause
    exit /b 1
  )
)

if defined REL_URL (
  echo [WARN] Server address override: %REL_URL%
  echo [WARN] This build will ONLY talk to that server - test releases only.
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -NoBump -ServerUrl %REL_URL%
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -NoBump
)
set "REL_EXIT=%ERRORLEVEL%"

if not "%REL_EXIT%"=="0" (
  echo.
  echo [ERROR] Launcher-layout rebuild failed with exit code %REL_EXIT%.
  echo [ERROR] Nothing changed - CURRENT_VERSION and server\releases\ are untouched.
  echo [ERROR] Build log: %%TEMP%%\honey_launcher_build_^<version^>.log
  echo         Detailed pip/PyInstaller errors are in the console output above.
  echo.
  pause
  exit /b %REL_EXIT%
)

echo.
echo === DONE ===
pause
exit /b 0
