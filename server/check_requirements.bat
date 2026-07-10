@echo off
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
