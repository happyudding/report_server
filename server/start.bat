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

rem Resolve Python interpreter.
set "PY_CMD="

if defined PYTHON (
    set PY_CMD="%PYTHON%"
    goto :py_ok
)
if exist "%ROOT%.venv\Scripts\python.exe" (
    set PY_CMD="%ROOT%.venv\Scripts\python.exe"
    goto :py_ok
)
if exist "%ROOT%venv\Scripts\python.exe" (
    set PY_CMD="%ROOT%venv\Scripts\python.exe"
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
echo [start] ERROR: Python interpreter not found.
echo [start] Set PYTHON env var, create .venv, or add python to PATH.
pause
exit /b 1

:py_ok
rem -- venv 자동 생성: clone 직후 .venv 가 없으면 만들고 requirements 설치 --
if not exist "%ROOT%.venv\Scripts\python.exe" (
    echo [start] .venv not found - creating virtual environment ...
    %PY_CMD% -m venv "%ROOT%.venv"
    if not exist "%ROOT%.venv\Scripts\python.exe" (
        echo [start] ERROR: failed to create .venv
        pause
        exit /b 1
    )
    call :install_deps
    if errorlevel 1 (
        echo [start] ERROR: 의존성 설치 실패 - 네트워크/프록시를 확인하세요.
        echo [start] 불완전한 .venv 를 제거합니다. 문제 해결 후 start.bat 또는 install.bat 를 다시 실행하세요.
        rmdir /s /q "%ROOT%.venv"
        pause
        exit /b 1
    )
)
rem venv 가 준비됐으니 항상 venv python 으로 고정
set PY_CMD="%ROOT%.venv\Scripts\python.exe"

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
