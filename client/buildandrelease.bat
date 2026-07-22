@echo off
REM Honey ZIP build AND release (bump version): PyInstaller(onedir) -> Honey-<version>.zip
REM Usage: double-click, or run from a command prompt.
REM
REM KEEP THIS FILE PURE ASCII WITH CRLF LINE ENDINGS.
REM cmd.exe mis-parses LF-only batch files (lines get merged and fragments run as
REM commands, e.g. "3.0.0 was unexpected at this time"), and a parse error kills the
REM script before it reaches pause, so the window closes with no visible error.
REM Korean text belongs in release_honey.ps1, which is UTF-8 with BOM.
setlocal EnableExtensions
cd /d "%~dp0"

set "PS1=%~dp0release\release_honey.ps1"
if not exist "%PS1%" (
  echo [ERROR] Release script not found:
  echo   "%PS1%"
  pause
  exit /b 1
)

REM No -Version and no -NoBump: release_honey.ps1 bumps the patch number from
REM CURRENT_VERSION automatically (e.g. 3.1.0 -> 3.1.1) and publishes it as a new
REM client-facing release. To rebuild the CURRENT version WITHOUT bumping, use
REM build_zip.bat instead. To set an explicit version (e.g. a minor bump 3.1.0 -> 3.2.0),
REM run release\release_honey.ps1 -Version x.y.z directly.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "REL_EXIT=%ERRORLEVEL%"

if not "%REL_EXIT%"=="0" (
  echo.
  echo [ERROR] Honey ZIP release build failed with exit code %REL_EXIT%.
  echo [ERROR] Full log: client\release\logs\ - open the newest release_*.log
  echo         Detailed pip/PyInstaller errors are in the console output above.
  echo.
  pause
  exit /b %REL_EXIT%
)

echo.
echo === DONE ===
pause
exit /b 0
