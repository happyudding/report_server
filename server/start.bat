@echo off
setlocal

set "ROOT=%~dp0"
if not defined PORT set "PORT=8080"
rem LAN 노출 강제: 환경변수에 HOST 가 미리 정의돼 있어도 무시하고 0.0.0.0 으로 bind 한다.
rem (로컬 전용으로 쓰려면 이 라인을 set "HOST=127.0.0.1" 으로 직접 수정)
set "HOST=0.0.0.0"
if not defined DATASET set "DATASET=current"

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
)
rem venv 가 준비됐으니 항상 venv python 으로 고정
set PY_CMD="%ROOT%.venv\Scripts\python.exe"

echo [start] Python    : %PY_CMD%
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
    pause
    exit /b 1
)

rem Health check via localhost (서버 자신에서는 항상 접근 가능)
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -Uri 'http://127.0.0.1:%PORT%/pe/report/' -UseBasicParsing -TimeoutSec 15; Write-Output ('[start] HTTP ' + $r.StatusCode) } catch { Write-Output ('[start] HTTP check failed: ' + $_.Exception.Message) }"

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
