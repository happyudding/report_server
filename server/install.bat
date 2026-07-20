@echo off
rem 콘솔을 UTF-8 로 맞춘다. 이 파일은 UTF-8(BOM 없음)이라 이 줄이 없으면
rem 한국어 Windows 기본 코드페이지(949)에서 한글이 깨져 보인다. BOM 을 붙이면
rem 대신 cmd 가 첫 줄(@echo off)을 못 읽어 에러를 내므로, BOM 없이 이 방식을 쓴다.
chcp 65001 >nul
setlocal

rem ============================================================
rem  report-server 의존성 일괄 설치 (다른 PC 최초 세팅용)
rem  - .venv 생성 후 requirements.txt 전체 설치
rem  - 이미 .venv 가 있으면 최신 requirements 로 갱신만 함
rem  - 서버 실행은 start.bat 사용
rem ============================================================

set "ROOT=%~dp0"

rem -- Python 인터프리터 탐색 (start.bat 과 동일 우선순위) --
set "PY_BOOT="
if defined PYTHON (
    set PY_BOOT="%PYTHON%"
    goto :boot_ok
)
if exist "%ROOT%.venv\Scripts\python.exe" goto :have_venv
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    set PY_BOOT="%%P"
    goto :boot_ok
)
where py.exe >nul 2>&1
if not errorlevel 1 (
    set "PY_BOOT=py -3"
    goto :boot_ok
)
echo [install] ERROR: Python 을 찾을 수 없습니다.
echo [install] Python 3 를 설치해 PATH 에 추가하거나 PYTHON 환경변수를 지정하세요.
pause
exit /b 1

:boot_ok
echo [install] 부트스트랩 Python : %PY_BOOT%
echo [install] .venv 생성 중 ...
%PY_BOOT% -m venv "%ROOT%.venv"
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo [install] ERROR: .venv 생성 실패
    pause
    exit /b 1
)

:have_venv
set PY="%ROOT%.venv\Scripts\python.exe"
echo [install] venv Python     : %PY%
echo.
echo [install] pip 업그레이드 ...
%PY% -m pip install --upgrade pip
echo.
echo [install] requirements.txt 설치 ...
%PY% -m pip install -r "%ROOT%requirements.txt"
if errorlevel 1 (
    echo.
    echo [install] ERROR: 설치 실패 - 위 로그를 확인하세요.
    pause
    exit /b 1
)

echo.
echo [install] 핵심 모듈 import 점검 ...
%PY% -c "import flask, werkzeug, waitress, boto3, PIL, pandas, pyarrow, numpy, psutil; print('[install] OK: 모든 핵심 의존성 import 성공')"
if errorlevel 1 (
    echo [install] WARNING: 일부 모듈 import 실패 - requirements.txt 를 확인하세요.
    pause
    exit /b 1
)

echo.
echo [install] ===== 설치 완료 =====
echo [install] 서버 실행: start.bat
pause
endlocal
