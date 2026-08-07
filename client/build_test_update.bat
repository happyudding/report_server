@echo off
REM Build a TEST release for the versioned-layout auto update (launcher + versions folder).
REM Usage: build_test_update.bat 9.0.0      (version argument is required)
REM
REM This does NOT touch server\releases or the normal release pipeline
REM (build_zip.bat / buildandrelease.bat stay exactly as they were).
REM Output goes to client\update_test\release\ only.
REM
REM KEEP THIS FILE PURE ASCII WITH CRLF LINE ENDINGS (same reason as build_zip.bat:
REM cmd.exe mis-parses LF-only batch files and the window closes with no error).
setlocal EnableExtensions
cd /d "%~dp0"

if "%~1"=="" (
  echo [ERROR] Version argument is required.
  echo   Example: build_test_update.bat 9.0.0
  echo   Use 9.x.x so test builds are never confused with real releases.
  pause
  exit /b 1
)

set "PS1=%~dp0update_test\build_test_release.ps1"
if not exist "%PS1%" (
  echo [ERROR] Test build script not found:
  echo   "%PS1%"
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -Version %1
set "REL_EXIT=%ERRORLEVEL%"

if not "%REL_EXIT%"=="0" (
  echo.
  echo [ERROR] Test release build failed with exit code %REL_EXIT%.
  echo.
  pause
  exit /b %REL_EXIT%
)

echo.
echo === DONE ===  see client\update_test\README.md for the next step
pause
exit /b 0
