@echo off
rem 콘솔을 UTF-8 로 맞춘다. 이 파일은 UTF-8(BOM 없음)이라 이 줄이 없으면
rem 한국어 Windows 기본 코드페이지(949)에서 한글이 깨져 보인다. BOM 을 붙이면
rem 대신 cmd 가 첫 줄(@echo off)을 못 읽어 에러를 내므로, BOM 없이 이 방식을 쓴다.
rem 이 파일은 반드시 CRLF 줄바꿈으로 저장할 것 (.gitattributes 가 강제한다).
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"

rem Python 인터프리터 결정. 이 폴더엔 venv 가 없고 서버 .venv 도 쓰지 않는다 —
rem 임포터는 Excel 이 있는 다른 PC 에서 도는 standalone 이고, 서버 venv 에는
rem pywin32 가 없다(서버는 Excel 을 쓰지 않는다).
set "PY_CMD="
if defined PYTHON (
    set PY_CMD="%PYTHON%"
    goto :py_ok
)
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    set PY_CMD="%%P"
    goto :py_ok
)
where py.exe >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto :py_ok
)
echo [import] ERROR: Python 을 찾을 수 없습니다.
echo [import] PYTHON 환경변수를 지정하거나 python 을 PATH 에 추가하세요.
pause
exit /b 1
:py_ok

rem CSV 경로 — 인자로 주면 그대로 쓰고, 없으면 파일 선택 창을 띄운다.
set "CSVPATH=%~1"
if not "%CSVPATH%"=="" goto :got_csv

set "PICKED=%TEMP%\product_info_selected.txt"
del "%PICKED%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%select_csv.ps1" > "%PICKED%"
if errorlevel 1 (
    echo [import] ERROR: 파일 선택 창을 열지 못했습니다.
    pause
    exit /b 1
)
set /p CSVPATH=<"%PICKED%"
del "%PICKED%" >nul 2>&1
if "%CSVPATH%"=="" (
    echo [import] 취소되었습니다.
    pause
    exit /b 1
)
:got_csv

echo [import] 선택한 CSV: %CSVPATH%
%PY_CMD% "%ROOT%import_product_info.py" "%CSVPATH%"
if errorlevel 1 (
    echo.
    echo [import] ERROR: 변환에 실패했습니다. 위 메시지를 확인하세요.
) else (
    echo.
    echo [import] output 폴더의 product_info.db 를 서버로 복사하세요.
)

echo.
pause
