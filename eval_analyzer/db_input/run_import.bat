@echo off
cd /d "%~dp0"

set "PICKED=%TEMP%\db_input_selected.txt"
del "%PICKED%" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0select_csv.ps1" > "%PICKED%"
if errorlevel 1 (
  echo [error] PowerShell file picker failed.
  pause
  exit /b 1
)

set "CSVPATH="
set /p CSVPATH=<"%PICKED%"
del "%PICKED%" >nul 2>&1

if "%CSVPATH%"=="" (
  echo Cancelled.
  pause
  exit /b 1
)

echo Selected CSV: %CSVPATH%
python "%~dp0import_csv.py" "%CSVPATH%"
if errorlevel 1 (
  echo.
  echo [error] Import failed. See messages above.
)

echo.
pause
