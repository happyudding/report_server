@echo off
REM Honey ZIP release build: PyInstaller(onedir) -> Honey-<version>.zip
REM Usage: double-click or run from a command prompt.
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File ".\release\release_honey.ps1"
if errorlevel 1 (
  echo.
  echo [ERROR] Honey ZIP release build failed.
  exit /b 1
)

echo.
echo === DONE ===
exit /b 0
