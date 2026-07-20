@echo off
REM Honey ZIP release build: PyInstaller(onedir) -> Honey-<version>.zip
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

REM [TEMPORARY] Version is pinned to 3.0.0. Remove the -Version argument to restore
REM the automatic patch bump from CURRENT_VERSION.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Version "3.0.0"
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
