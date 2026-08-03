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
set "HOST=0.0.0.0"

set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [mypc] ERROR: .venv 가 없습니다 - %PY%
    echo [mypc]        install.bat 을 한 번 실행해 가상환경을 만드세요.
    pause
    exit /b 1
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
echo [mypc]   이 PC          : http://127.0.0.1:%PORT%/pe/report/
echo [mypc]   같은 네트워크  : http://%MYIP%:%PORT%/pe/report/
echo [mypc]   관리자         : http://127.0.0.1:%PORT%/pe/admin-pte/
echo.
echo [mypc] 외부 PC 에서 처음 접속 시 방화벽 허용이 필요할 수 있습니다
echo [mypc]   (관리자 PowerShell 에서 1회)
echo [mypc]   New-NetFirewallRule -DisplayName "report-server %PORT%" -Direction Inbound -LocalPort %PORT% -Protocol TCP -Action Allow
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
