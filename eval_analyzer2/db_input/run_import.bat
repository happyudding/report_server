@echo off
rem 콘솔을 UTF-8 로 맞춘다 (import_csv.py 가 한국어 메시지를 출력한다).
rem 이 파일은 BOM 없는 UTF-8 + CRLF 로 저장할 것.
chcp 65001 >nul
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

rem report_server 안에 배치된 사본이면(= ..\..\server\config.py 존재) 서버 소유 eval.db 를
rem 자동으로 가리킨다. EVAL_DB_PATH 를 미리 지정해 두면 그 값이 우선한다.
if not defined EVAL_DB_PATH if exist "%~dp0..\..\server\config.py" (
  for %%I in ("%~dp0..\..") do set "EVAL_DB_PATH=%%~fI\DB\pe\report\eval\eval.db"
)

rem 대상 DB 가 정해졌을 때만 통합 적재(--to-eval-db). 아니면 기존대로 제품군별 분리 파일.
set "TOEVAL="
if defined EVAL_DB_PATH set "TOEVAL=--to-eval-db"
if defined EVAL_DB_PATH echo Target DB: %EVAL_DB_PATH%
if not defined EVAL_DB_PATH echo Target DB: per-family files under db_input\output

echo Selected CSV: %CSVPATH%
python "%~dp0import_csv.py" "%CSVPATH%" %TOEVAL%
if errorlevel 1 (
  echo.
  echo [error] Import failed. See messages above.
)

echo.
pause
