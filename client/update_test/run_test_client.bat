@echo off
REM Launch a TEST install of Honey with the versioned-layout auto update enabled.
REM Usage: run_test_client.bat [install root]     default: F:\HoneyUpdateTest\Honey
REM
REM Why this file exists: this PC has a USER environment variable
REM   HONEY_SERVER_URL=http://12.81.220.117:8080  (the production server)
REM and transport\config.py gives that variable priority over honey.env. Without the
REM override below the test build talks to the production server, sees version 3.1.1,
REM decides "9.0.0 is newer, nothing to do" and no update dialog ever appears.
REM
REM KEEP THIS FILE PURE ASCII WITH CRLF LINE ENDINGS (see build_zip.bat for why).
setlocal EnableExtensions

set "ROOT=%~1"
if "%ROOT%"=="" set "ROOT=F:\HoneyUpdateTest\Honey"

if not exist "%ROOT%\Honey.exe" (
  echo [ERROR] Launcher not found: "%ROOT%\Honey.exe"
  echo         Extract Honey-9.0.0.zip first ^(see README.md^).
  pause
  exit /b 1
)

set "HONEY_UPDATE_TEST=1"
set "HONEY_SERVER_URL=http://127.0.0.1:8090"

echo Install root : %ROOT%
echo Server       : %HONEY_SERVER_URL%
echo Update mode  : versioned layout ^(HONEY_UPDATE_TEST=1^)
echo.
start "" "%ROOT%\Honey.exe"
exit /b 0
