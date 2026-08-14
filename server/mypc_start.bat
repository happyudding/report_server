@echo off
rem 이 파일은 UTF-8(BOM 없음) + CRLF 로 저장할 것 (.gitattributes 가 강제).
chcp 65001 >nul
setlocal

rem ===========================================================================
rem  mypc_start.bat — 지금 이 PC 에서 디버깅용으로 "독립" 기동
rem
rem  start.bat 과 일부러 다르게 한 점:
rem    * env\server.env 를 읽지 않는다 → 운영 SERVER_BASE_URL(12.81.220.117) 무시.
rem      대신 지금 이 PC 의 LAN IPv4 를 찾아 SERVER_BASE_URL 로 쓴다.
rem    * 포트 기본 8090 (start.bat/운영의 8080 과 겹치지 않음).
rem      진단 리스너는 자동으로 PORT+1(8091) 을 쓴다.
rem    * terminate.bat 을 부르지 않는다 → 돌고 있는 다른 서버/파이썬을 죽이지 않는다.
rem    * watchdog(schtasks) 을 전혀 건드리지 않는다 → 이 인스턴스는 자동 재기동 대상 아님.
rem    * 별도 창이 아니라 이 창에서 그대로 실행 → 에러가 눈앞에 보이고 Ctrl+C 로 종료.
rem      (start.bat 이 "Waiting for server to listen" 에서 멈추면 서버 창의 에러를
rem       못 보고 기다리기만 하는데, 그 단계 자체가 여기엔 없다.)
rem
rem  사용법 :  mypc_start.bat          → 포트 8090
rem            mypc_start.bat 8095     → 포트 지정
rem
rem  ※ DB/uploads 는 저장소 기본 경로(DB\pe\report\, uploads\)를 그대로 쓴다.
rem     운영 서버는 다른 PC 이므로 파일이 겹치지 않는다.
rem ===========================================================================

set "ROOT=%~dp0"
set "PORT=%~1"
if not defined PORT set "PORT=8090"
set /a DIAGPORT=%PORT%+1
set "HOST=0.0.0.0"

rem -- 이 창의 QuickEdit(빠른 편집) 끄기 -----------------------------------------
rem   서버가 이 창에서 직접 돌기 때문에, 창을 클릭/드래그하면 콘솔이 선택 모드로
rem   들어가 stdout 쓰기가 블록되고 서버 전체가 멈춘다(창은 살아 있고 출력만 정지 →
rem   클라는 업로드 100% 에서 timeout, 브라우저는 네트워크 에러). 실패해도 그냥 진행.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%_disable_quickedit.ps1"

rem -- 파이썬 탐색: 경로를 박아두지 않고 순서대로 찾아 "실제로 실행되는" 것을 고른다 --
rem   존재만 보지 않고 -c 로 한 번 돌려본다. venv 는 만든 뒤 전역 파이썬이 옮겨/지워지면
rem   python.exe 는 남아 있어도 실행 시 "did not find executable at ..." 로 죽기 때문에,
rem   파일 존재 검사만으로는 그 상태를 걸러내지 못한다.
set "PY="
if defined PYTHON call :try_py "%PYTHON%"
if not defined PY call :try_py "%ROOT%.venv\Scripts\python.exe"
if not defined PY call :try_py "%ROOT%venv\Scripts\python.exe"
rem 전역 파이썬 탐색은 _find_python.bat 이 맡는다 (PATH 보다 py -3 우선 + 최소 버전 검사).
if not defined PY for /f "delims=" %%P in ('call "%ROOT%_find_python.bat"') do if not defined PY set "PY=%%P"
if not defined PY (
    echo [mypc] ERROR: 3.11 이상의 실행 가능한 파이썬을 찾지 못했습니다.
    echo [mypc]        install.bat 으로 .venv 를 만들거나, 파이썬을 PATH 에 추가하세요.
    echo [mypc]        특정 파이썬을 쓰려면: set "PYTHON=C:\경로\python.exe" 후 다시 실행.
    pause
    exit /b 1
)

rem -- .venv 가 있는데 탈락했다면 왜인지 알려준다 -------------------------------
rem   탈락 사유는 두 가지고, 실제 사유는 바로 위 :try_py 가 이미 한 줄로 찍는다.
rem     (1) 파이썬 3.11 미만  - 새로 깔아도 기존 .venv 는 안 바뀐다(그래서 다시 만든다)
rem     (2) 실행 자체가 실패 - venv 는 만들 때 쓴 파이썬의 절대경로를 pyvenv.cfg 에
rem         박아두므로 다른 PC/계정으로 복사해 오면 "did not find executable at ..." 로 죽는다.
rem   어느 쪽이든 조치는 같다: 새로 만든다(예전 것은 .venv_broken 으로 밀어둔다).
if exist "%ROOT%.venv\Scripts\python.exe" if /i not "%PY%"=="%ROOT%.venv\Scripts\python.exe" (
    echo [mypc] NOTE: .venv 가 있지만 쓸 수 없어 건너뛰었습니다 ^(사유는 바로 위 줄^).
    if exist "%ROOT%.venv\pyvenv.cfg" (
        for /f "usebackq eol=# tokens=1,* delims== " %%A in ("%ROOT%.venv\pyvenv.cfg") do if /i "%%A"=="home" echo [mypc]       이 .venv 를 만든 파이썬: %%B
    )
    echo [mypc]       파이썬을 새로 깔아도 기존 .venv 는 그 버전에 고정됩니다
    echo [mypc]       ^(다른 PC/계정에서 복사해 올 수도 없습니다^). 새로 만듭니다.
)

rem -- venv 자동 준비: 고른 파이썬이 .venv 것이 아니면 여기서 .venv 를 만들어 쓴다 ---
rem   .venv 가 없는 경우와 깨진 경우 모두 여기로 온다. 사람이 install.bat 을 따로 돌리지
rem   않아도 이 창에서 그대로 서버가 뜨게 하는 것이 목적이다. 실패하면 멈추지 않고
rem   찾아둔 파이썬으로 그냥 진행한다 (에러가 눈앞에 보이는 것이 이 스크립트의 취지).
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
if /i not "%PY%"=="%VENV_PY%" call :prepare_venv

rem -- 최종 점검: 이 파이썬으로 서버가 뜰 수 있는지 --------------------------------
rem   위 prepare_venv 는 .venv 가 "못 쓰는" 경우(실행 실패)에만 돈다. 그런데 .venv 가
rem   멀쩡히 실행되는데 패키지만 없는 경우(설치가 중간에 끊김 / 네트워크 실패 / venv 만
rem   먼저 만들어 둠)가 더 흔하고, 그때는 여기까지 그냥 내려와 ModuleNotFoundError 로
rem   즉사했다. venv 를 다시 만들 필요는 없고 pip install 만 하면 되므로 여기서 설치한다.
"%PY%" -c "import flask" >nul 2>&1
if errorlevel 1 call :install_deps
"%PY%" -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [mypc] WARN: 이 파이썬에는 flask 가 없습니다 - %PY%
    echo [mypc]       아래에서 import 에러가 나면 install.bat 으로 의존성을 설치하세요.
)

rem -- 포트 선점 확인: 조용히 실패하지 않도록 먼저 본다 ------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    echo [mypc] ERROR: 포트 %PORT% 를 이미 다른 프로세스가 사용 중입니다.
    echo [mypc]        다른 포트로 실행하세요 - 예: mypc_start.bat 8095
    pause
    exit /b 1
)

rem -- 이 PC 의 LAN IPv4 탐지 (운영 IP 대신 이 주소가 서버 주소가 된다) --------
rem 어댑터가 여러 개(VPN/Hyper-V 등)면 엉뚱한 주소가 잡힐 수 있다. 아래 출력된
rem 주소가 원하는 것이 아니면 이 줄 다음에 set "MYIP=192.168.x.x" 로 고정할 것.
rem (외부로 나가는 경로의 주소를 먼저 쓰고, 실패하면 어댑터 목록에서 고른다.
rem  파이프 '|' 는 for /f 안에서 이스케이프가 까다로워 쓰지 않았다.)
set "MYIP="
for /f "delims=" %%I in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$u=New-Object Net.Sockets.UdpClient; $u.Connect('8.8.8.8',80); $u.Client.LocalEndPoint.Address.ToString()"') do set "MYIP=%%I"
if not defined MYIP for /f "delims=" %%I in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue).Where({$_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -in @('Dhcp','Manual')})[0].IPAddress"') do set "MYIP=%%I"
if not defined MYIP set "MYIP=127.0.0.1"
set "SERVER_BASE_URL=http://%MYIP%:%PORT%"

echo.
echo [mypc] Python          : %PY%
echo [mypc] Bind host       : %HOST%   (이 PC 의 모든 주소로 열기)
echo [mypc] Port            : %PORT%
echo [mypc] SERVER_BASE_URL : %SERVER_BASE_URL%   ^<- 이 PC 가 서버 주소
echo.
echo [mypc] 접속 주소
echo [mypc]   이 PC          : http://127.0.0.1:%PORT%/pe/
echo [mypc]   같은 네트워크  : http://%MYIP%:%PORT%/pe/
echo [mypc]   관리자         : http://127.0.0.1:%PORT%/pe/admin-pte/
echo.
echo [mypc] 외부 PC 에서 처음 접속 시 방화벽 허용이 필요할 수 있습니다
echo [mypc]   (관리자 PowerShell 에서 1회)
echo [mypc]   New-NetFirewallRule -DisplayName "report-server %PORT%" -Direction Inbound -LocalPort %PORT% -Protocol TCP -Action Allow
echo.
echo [mypc] 서버가 응답이 없을 때 (클라 timeout / 브라우저 네트워크 에러)
echo [mypc]   1) 이 창을 클릭한 적이 있으면 창에서 Enter 를 한 번 눌러 본다
echo [mypc]      ^(선택 모드에 걸린 것이면 그 즉시 다시 응답한다^)
echo [mypc]   2) 그래도 무응답이면 *다른* 창에서 스레드 덤프를 받는다 - 어느 요청이
echo [mypc]      멈춰 있는지 그대로 나온다 ^(이 포트는 서버 스레드 풀 밖에서 돈다^)
echo [mypc]      curl http://127.0.0.1:%DIAGPORT%/threads -o threads.txt
echo [mypc]   3) 서버 콘솔 로그는 log\server_*.txt 에도 그대로 쌓인다
echo.
echo [mypc] 종료하려면 이 창에서 Ctrl+C (또는 창 닫기). watchdog 이 되살리지 않습니다.
echo.

pushd "%ROOT%"
"%PY%" -u wsgi.py
popd

echo.
echo [mypc] 서버가 종료되었습니다. (위에 에러가 있으면 그것이 원인입니다)
pause
endlocal
exit /b 0

rem --- .venv 를 만들고 의존성을 설치해 PY 를 그쪽으로 바꾼다 --------------------
rem 못 쓰는 .venv 는 지우지 않고 .venv_broken 으로 밀어둔다 (되돌릴 수 있게).
:prepare_venv
if exist "%ROOT%.venv\" (
    echo [mypc] 못 쓰는 .venv 를 .venv_broken 으로 옮깁니다.
    if exist "%ROOT%.venv_broken\" rmdir /s /q "%ROOT%.venv_broken"
    move "%ROOT%.venv" "%ROOT%.venv_broken" >nul 2>&1
    if exist "%ROOT%.venv\" (
        echo [mypc] WARN: .venv 를 옮기지 못했습니다 ^(다른 프로그램이 사용 중?^).
        echo [mypc]       찾아둔 파이썬으로 그대로 진행합니다.
        exit /b
    )
)
echo [mypc] 가상환경 생성 중 ... (%PY%)
"%PY%" -m venv "%ROOT%.venv"
if not exist "%VENV_PY%" (
    echo [mypc] WARN: .venv 생성 실패 - 찾아둔 파이썬으로 그대로 진행합니다.
    exit /b
)
echo [mypc] 의존성 설치 중 ... (처음이면 몇 분 걸립니다)
if exist "%ROOT%wheelhouse\*.whl" (
    "%VENV_PY%" -m pip install --no-index --find-links="%ROOT%wheelhouse" -r "%ROOT%requirements.txt"
    if not errorlevel 1 goto :venv_check
    echo [mypc] 오프라인 설치 실패 - 네트워크 설치로 전환합니다.
)
"%VENV_PY%" -m pip install -r "%ROOT%requirements.txt"
:venv_check
"%VENV_PY%" -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [mypc] WARN: 새 .venv 에 의존성 설치가 안 됐습니다 ^(네트워크/프록시 확인^).
    echo [mypc]       찾아둔 파이썬으로 그대로 진행합니다.
    exit /b
)
set "PY=%VENV_PY%"
echo [mypc] 새 .venv 준비 완료.
exit /b

rem --- 지금 고른 PY 에 requirements 설치 (wheelhouse 우선) ----------------------
rem 이미 있는 venv 에 패키지만 채우는 경로다. venv 를 새로 만들지 않으므로 되돌릴 것이 없다.
:install_deps
echo [mypc] 의존성이 없습니다 - 지금 설치합니다 ... (처음이면 몇 분 걸립니다)
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [mypc] pip 이 없는 환경입니다 - ensurepip 으로 복구합니다.
    "%PY%" -m ensurepip --upgrade
)
if exist "%ROOT%wheelhouse\*.whl" (
    "%PY%" -m pip install --no-index --find-links="%ROOT%wheelhouse" -r "%ROOT%requirements.txt"
    if not errorlevel 1 exit /b
    echo [mypc] 오프라인 설치 실패 - 네트워크 설치로 전환합니다.
)
"%PY%" -m pip install -r "%ROOT%requirements.txt"
exit /b

rem --- 후보 파이썬 1개 검증: 실행되고 요구 버전 이상이면 PY 에 담는다 -----------
rem   버전까지 보는 이유: 3.10 으로 만든 .venv 가 남아 있으면 나중에 3.11+ 를 깔아도
rem   계속 그것으로 떠서 web_report 콜드 빌드가 TypeError 로 100% 실패했다
rem   (ProcessPoolExecutor(max_tasks_per_child=) 는 3.11 신설). 최소 버전 판정은
rem   _find_python.bat 한 곳에만 둔다.
:try_py
if "%~1"=="" exit /b
if not exist "%~1" exit /b
"%~1" -c "pass" >nul 2>&1
if errorlevel 1 (
    echo [mypc] 건너뜀 ^(있지만 실행 실패^): %~1
    exit /b
)
call "%ROOT%_find_python.bat" "%~1" >nul 2>&1
if errorlevel 1 (
    echo [mypc] 건너뜀 ^(파이썬 3.11 미만^): %~1
    exit /b
)
set "PY=%~1"
exit /b
