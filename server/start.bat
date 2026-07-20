@echo off
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
    echo [start] Installing dependencies from requirements.txt ...
    "%ROOT%.venv\Scripts\python.exe" -m pip install --upgrade pip
    "%ROOT%.venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
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

rem --- watchdog 재개 (terminate.bat 이 정지시킨 것을 되돌린다) ------------------
:enable_watchdog
schtasks /Change /TN "%TASK_WATCHDOG%" /ENABLE >nul 2>nul
if errorlevel 1 goto :wd_enable_fail
echo [start] watchdog 재개됨 (%TASK_WATCHDOG%).
exit /b
:wd_enable_fail
echo [start] watchdog 재개 실패 또는 미등록.
echo [start]   - 등록돼 있는데 실패했다면 권한 문제입니다. 관리자 권한으로 실행하거나 수동 재개:
echo [start]       schtasks /Change /TN %TASK_WATCHDOG% /ENABLE
echo [start]   - 등록한 적이 없다면 register_watchdog.bat 로 등록할 수 있습니다.
exit /b
