@echo off
rem 콘솔을 UTF-8 로 맞춘다. 이 파일은 UTF-8(BOM 없음)이라 이 줄이 없으면
rem 한국어 Windows 기본 코드페이지(949)에서 한글이 깨져 보인다. BOM 을 붙이면
rem 대신 cmd 가 첫 줄(@echo off)을 못 읽어 에러를 내므로, BOM 없이 이 방식을 쓴다.
chcp 65001 >nul
rem 이 파일은 반드시 CRLF 줄바꿈으로 저장할 것 (.gitattributes 가 강제한다).
rem LF 로 저장되면 cmd.exe 가 바이트 오프셋을 잘못 계산해 줄이 뭉개지고
rem "was unexpected at this time" / "cannot find the batch label" 로 죽는다.
setlocal

set "ROOT=%~dp0"

rem ── 기동 설정 로드 (env\server.env 가 정본) ─────────────────────────────────
rem HOST/PORT 는 이 bat 이 아니라 env\server.env 에서 관리한다. 미리 정의된
rem 환경변수는 무시하고 파일 값으로 덮는다 (설정 출처를 파일 하나로 고정).
set "ENV_FILE=%ROOT%env\server.env"
set "HOST="
set "PORT="
if exist "%ENV_FILE%" (
    for /f "usebackq eol=# tokens=1,2 delims== " %%A in ("%ENV_FILE%") do set "%%A=%%B"
) else (
    echo [start] WARN: 설정 파일 없음 - 기본값으로 기동합니다: %ENV_FILE%
)
if not defined HOST set "HOST=0.0.0.0"
if not defined PORT set "PORT=8080"
if not defined DATASET set "DATASET=current"

rem terminate.bat 이 교체 작업 중 오작동을 막으려고 일시 정지시킨 watchdog 을, 기동을
rem 마친 뒤 이 스크립트가 다시 켠다 (:enable_watchdog).
set "TASK_WATCHDOG=report-server-watchdog"

rem ── 파이썬 결정 ─────────────────────────────────────────────────────────────
rem  종전에는 .venv 가 있으면 **버전을 보지 않고** 그대로 썼다. 그래서 3.10 으로 만든
rem  .venv 가 남아 있으면 나중에 3.11+ 를 깔아도 서버는 계속 3.10 으로 떴고, web_report
rem  콜드 빌드가 ProcessPoolExecutor(max_tasks_per_child=) TypeError 로 100% 실패해
rem  화면에는 "리포트 계산이 반복 실패했습니다" 만 보였다(원인을 알 수 없는 증상).
rem  이제 버전을 확인하고, 낮으면 .venv_old 로 밀어둔 뒤 3.11+ 로 다시 만든다.
rem  인터프리터 탐색 규칙은 _find_python.bat 에 모아 두었다(PATH 보다 py -3 우선).
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
set "VENV_BACKED_UP="

if not exist "%VENV_PY%" (
    echo [start] .venv not found - creating virtual environment ...
    goto :make_venv
)
rem 최소 버전 판정은 _find_python.bat 한 곳에만 둔다 (여기서 숫자를 또 쓰지 않는다).
call "%ROOT%_find_python.bat" "%VENV_PY%" >nul 2>&1
if not errorlevel 1 goto :venv_ready

for /f "delims=" %%V in ('"%VENV_PY%" -c "import sys;print(sys.version.split()[0])" 2^>nul') do echo [start] 기존 .venv 의 파이썬이 %%V 입니다 ^(서버 요구: 3.11 이상^).
echo [start] 파이썬을 새로 설치해도 기존 .venv 는 바뀌지 않습니다 - 다시 만듭니다.
goto :make_venv

:make_venv
rem 3.11+ 인터프리터를 찾는다 (stdout 에 경로만 온다 - _find_python.bat 참조).
set "PY_BOOT="
for /f "delims=" %%P in ('call "%ROOT%_find_python.bat"') do set "PY_BOOT=%%P"
if defined PY_BOOT goto :make_venv_go

echo [start] ERROR: 3.11 이상의 파이썬을 찾지 못했습니다.
echo [start]   설치: https://www.python.org/downloads/  ^(설치 시 "Add to PATH" 체크^)
echo [start]   이미 있다면 경로 지정: set "PYTHON=C:\경로\python.exe" 후 다시 실행
if exist "%VENV_PY%" (
    echo [start]   기존 .venv 로 그대로 기동하려면: set "ALLOW_OLD_PYTHON=1" 후 다시 실행
    echo [start]   ^(단 web_report 콜드 빌드는 계속 실패합니다 - 임시 회피용^)
    if "%ALLOW_OLD_PYTHON%"=="1" (
        echo [start] WARN: ALLOW_OLD_PYTHON=1 - 요구 버전 미만인 .venv 로 기동합니다.
        goto :venv_ready
    )
)
pause
exit /b 1

:make_venv_go
echo [start] 사용할 파이썬: %PY_BOOT%
rem 기존 .venv 는 지우지 않고 밀어둔다 - 새로 만들다 실패하면 되돌려야 하기 때문.
if exist "%ROOT%.venv\" (
    if exist "%ROOT%.venv_old\" rmdir /s /q "%ROOT%.venv_old"
    move "%ROOT%.venv" "%ROOT%.venv_old" >nul 2>&1
    if exist "%ROOT%.venv\" (
        echo [start] ERROR: 기존 .venv 를 옮기지 못했습니다 ^(서버가 아직 돌고 있나요?^).
        echo [start]        terminate.bat 실행 후 다시 시도하세요.
        pause
        exit /b 1
    )
    set "VENV_BACKED_UP=1"
)
"%PY_BOOT%" -m venv "%ROOT%.venv"
if not exist "%VENV_PY%" (
    echo [start] ERROR: failed to create .venv
    goto :venv_rollback
)
call :install_deps
if errorlevel 1 goto :venv_rollback
"%VENV_PY%" -c "import flask" >nul 2>&1
if errorlevel 1 goto :venv_rollback
if defined VENV_BACKED_UP echo [start] 예전 .venv 는 .venv_old 에 남겨 두었습니다 ^(확인 후 삭제하세요^).
goto :venv_ready

:venv_rollback
echo [start] ERROR: 새 .venv 준비 실패 - 네트워크/프록시를 확인하세요.
if exist "%ROOT%.venv\" rmdir /s /q "%ROOT%.venv"
if defined VENV_BACKED_UP (
    echo [start] 원래 .venv 를 되돌립니다 ^(기동은 중단^).
    move "%ROOT%.venv_old" "%ROOT%.venv" >nul 2>&1
)
echo [start] 문제 해결 후 start.bat 또는 install.bat 를 다시 실행하세요.
pause
exit /b 1

:venv_ready
rem venv 가 준비됐으니 항상 venv python 으로 고정
set PY_CMD="%VENV_PY%"

rem -- .venv 는 있는데 의존성이 없는 경우도 복구한다 ----------------------------
rem 위 블록은 python.exe 의 "존재"만 보므로, 설치가 중간에 끊겼거나 다른 PC 에서
rem .venv 를 복사해 왔거나 venv 만 먼저 만들어 둔 상태면 설치 단계를 통째로 건너뛴다.
rem 그러면 빈 venv 로 wsgi.py 가 떠서 ModuleNotFoundError 로 즉사하고, 서버 창은
rem 닫혀 버려 원인이 안 보인 채 "Waiting for server to listen" 이 60초 타임아웃 났다.
rem terminate.bat 보다 먼저 두는 이유: 기동 가능 여부를 확인하기 전에 돌고 있는
rem 서버를 내리지 않기 위해서다.
%PY_CMD% -c "import flask" >nul 2>&1
if errorlevel 1 (
    echo [start] .venv 에 의존성이 없습니다 - 설치합니다 ...
    call :install_deps
    %PY_CMD% -c "import flask" >nul 2>&1
    if errorlevel 1 (
        echo [start] ERROR: 의존성 설치 실패 - 네트워크/프록시를 확인하세요.
        echo [start] install.bat 를 실행해 자세한 로그를 확인하세요.
        pause
        exit /b 1
    )
)

echo [start] Python    : %PY_CMD%
echo [start] Config    : %ENV_FILE%
echo [start] Bind host : %HOST%
echo [start] Port      : %PORT%

call "%ROOT%terminate.bat"

echo.
echo [start] Starting server on %HOST%:%PORT% ...
start "report-server" /D "%ROOT%" %PY_CMD% -u wsgi.py

echo [start] Waiting for server to listen (up to 60s) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$port = [int]'%PORT%'; for ($i = 0; $i -lt 120; $i++) { if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { Write-Host '[start] Server is listening.'; exit 0 } ; Start-Sleep -Milliseconds 500 } ; Write-Host '[start] Timeout waiting for server.'; exit 1"
if errorlevel 1 (
    echo [start] Check the server window for errors.
    rem 기동 실패해도 watchdog 은 되살린다 — 켜두면 5분 뒤 자동 재시도라도 하지만,
    rem 꺼진 채로 방치하면 이후 서버가 죽어도 아무도 되살리지 않는다.
    call :enable_watchdog
    pause
    exit /b 1
)

rem Health check via localhost (서버 자신에서는 항상 접근 가능) — 경량 /healthz 사용
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/healthz' -UseBasicParsing -TimeoutSec 15; Write-Output ('[start] HTTP ' + $r.StatusCode) } catch { Write-Output ('[start] HTTP check failed: ' + $_.Exception.Message) }"

call :enable_watchdog

echo.
echo [start] ===== Accessible URLs (HOST=%HOST%) =====
echo [start] Local (이 PC)              : http://127.0.0.1:%PORT%/pe/report/
echo [start] LAN ^(같은 네트워크 다른 PC^):
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -in @('Dhcp','Manual','WellKnown') } | Select-Object -ExpandProperty IPAddress -Unique; if ($ips) { foreach ($ip in $ips) { Write-Host ('[start]                              http://' + $ip + ':%PORT%/pe/report/') } } else { Write-Host '[start]                              (LAN IPv4 주소를 찾지 못함 - ipconfig 로 직접 확인하세요)' }"

echo.
echo [start] ** 처음 외부 PC 에서 접근 시 Windows Defender 방화벽이 차단할 수 있습니다.
echo [start]    차단 시 관리자 권한 PowerShell 에서 1회 실행:
echo [start]      New-NetFirewallRule -DisplayName "report-server %PORT%" -Direction Inbound -LocalPort %PORT% -Protocol TCP -Action Allow
echo [start] ============================
echo.

start "" "http://127.0.0.1:%PORT%/pe/report/"

echo.
echo [start] 서버는 별도 창("report-server") 에서 실행 중입니다.
echo [start] 이 창을 닫으려면 아무 키나 누르세요. (서버는 계속 실행됨)
pause >nul

endlocal
exit /b 0

rem --- 의존성 설치 (wheelhouse 우선, 실패하면 네트워크) -------------------------
rem wheelhouse 는 report_server_zip.bat 가 압축할 때 만들어 넣는 오프라인 wheel 모음이다.
rem wheel 은 Python minor 버전(cp313 등)에 묶여 있어 서버 PC 의 Python 버전이 다르면
rem 안 맞을 수 있다 — 그때는 멈추지 않고 네트워크 설치로 넘어간다.
:install_deps
set "VPY=%ROOT%.venv\Scripts\python.exe"
rem venv 에 pip 자체가 없는 경우(--without-pip / ensurepip 실패)도 여기서 복구한다.
"%VPY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [start] pip 이 없는 venv 입니다 - ensurepip 으로 복구합니다.
    "%VPY%" -m ensurepip --upgrade
)
if not exist "%ROOT%wheelhouse\*.whl" goto :deps_network
echo [start] wheelhouse 발견 - 네트워크 없이 설치합니다 ...
"%VPY%" -m pip install --no-index --find-links="%ROOT%wheelhouse" -r "%ROOT%requirements.txt"
if not errorlevel 1 exit /b 0
echo [start] 오프라인 설치 실패 - 네트워크 설치로 전환합니다.
:deps_network
echo [start] Installing dependencies from requirements.txt ...
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r "%ROOT%requirements.txt"
exit /b %ERRORLEVEL%

rem --- watchdog 재개 (terminate.bat 이 정지시킨 것을 되돌린다) ------------------
rem "등록 안 됨"(이 PC 는 watchdog 을 안 쓴다 = 정상)과 "권한 부족"(조치가 필요한 실패)을
rem 구분해서 알린다. 예전에는 둘 다 한 줄로 뭉뚱그려 원인을 못 찾았다.
:enable_watchdog
if "%WATCHDOG_MANAGE%"=="0" goto :wd_enable_skip
schtasks /Query /TN "%TASK_WATCHDOG%" >nul 2>nul
if errorlevel 1 goto :wd_enable_absent
rem 실패 사유가 보이도록 schtasks 출력을 삼키지 않는다.
schtasks /Change /TN "%TASK_WATCHDOG%" /ENABLE
if errorlevel 1 goto :wd_enable_denied
echo [start] watchdog 재개됨 (%TASK_WATCHDOG%).
exit /b
:wd_enable_absent
echo [start] watchdog 미등록 - 자동 재기동 없이 그냥 동작합니다 (서버 기동에는 문제 없음).
echo [start]   자동 재기동이 필요하면 register_watchdog.bat 를 관리자 권한으로 1회 실행.
exit /b
:wd_enable_denied
echo [start] watchdog 재개 실패 - 바로 위 schtasks 메시지가 원인입니다 (대개 관리자 권한 부족).
echo [start]   수동 재개: schtasks /Change /TN %TASK_WATCHDOG% /ENABLE
exit /b
:wd_enable_skip
echo [start] watchdog 관리 꺼짐 (WATCHDOG_MANAGE=0).
exit /b
