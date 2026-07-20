@echo off
rem 콘솔을 UTF-8 로 맞춘다. 이 파일은 UTF-8(BOM 없음)이라 이 줄이 없으면
rem 한국어 Windows 기본 코드페이지(949)에서 한글이 깨져 보인다. BOM 을 붙이면
rem 대신 cmd 가 첫 줄(@echo off)을 못 읽어 에러를 내므로, BOM 없이 이 방식을 쓴다.
chcp 65001 >nul
setlocal
set "ROOT=%~dp0"

if exist "%ROOT%.venv\Scripts\python.exe" (
    set PY="%ROOT%.venv\Scripts\python.exe"
) else (
    for /f "delims=" %%P in ('where python.exe 2^>nul') do (
        set PY="%%P"
        goto :run
    )
    echo [check] Python 을 찾을 수 없습니다. install.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

:run
echo [check] server + web_report import 스캔 ...
%PY% "%ROOT%tools\check_requirements.py"
pause
endlocal
