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

rem 기동 설정 로드 — start.bat 과 같은 정본(env\server.env). 단독 실행될 때도
rem PORT / WATCHDOG_MANAGE 를 같은 파일에서 읽도록 한다.
rem 단, start.bat 이 부를 때는 이미 값을 넘겨받은 상태다. 그때 파일을 다시 읽으면
rem start.bat 이 정한 PORT 를 덮어써 엉뚱한 포트를 종료하려 든다 — PORT 가 이미
rem 정해져 있으면 건드리지 않는다.
set "ENV_FILE=%ROOT%env\server.env"
if defined PORT goto :env_done
if exist "%ENV_FILE%" (
    for /f "usebackq eol=# tokens=1,2 delims== " %%A in ("%ENV_FILE%") do set "%%A=%%B"
)
:env_done
if not defined PORT set "PORT=8080"
rem drain 최대 대기 시간(초). 이 시간을 넘기면 진행 중 요청이 남아 있어도 강제 종료한다.
if not defined DRAIN_TIMEOUT_SEC set "DRAIN_TIMEOUT_SEC=90"
rem 진행 중 요청 수가 이 시간(초) 동안 **줄지 않으면** 멈춘 것으로 보고 조기 종료한다.
rem 안 끝나는 요청을 붙들고 90초를 꽉 채워 기다리는 것은 아무 소용이 없다 (2026-08-19).
if not defined DRAIN_STALL_SEC set "DRAIN_STALL_SEC=15"

rem 인자 force / -f / /f  ->  drain 을 아예 건너뛰고 즉시 종료 (스레드 덤프는 그래도 남긴다)
set "FORCE_ARG="
if /i "%~1"=="force" set "FORCE_ARG=-Force"
if /i "%~1"=="-f"    set "FORCE_ARG=-Force"
if /i "%~1"=="/f"    set "FORCE_ARG=-Force"
set "DIAG_ARG="
if defined DIAG_PORT set "DIAG_ARG=-DiagPort %DIAG_PORT%"

set "TASK_WATCHDOG=report-server-watchdog"

echo.
echo [terminate] 참고: report_view.html / report_analysis_index.html / static\webreport\*.js
echo [terminate]       만 고쳤다면 재시작이 필요 없습니다. 파일만 덮어쓰면 즉시 반영됩니다.
echo [terminate]       재시작 대상: Python 코드, 환경변수, cache_policy 스키마 버전 변경.
echo.

rem ---------------------------------------------------------------------------
rem 1) watchdog 일시 정지
rem    끄지 않으면 서버를 내려둔 사이 5분 주기 watchdog 이 끼어들어 "옛 코드로" 재기동한다.
rem    start.bat 이 기동을 마치면 자동으로 다시 켠다.
rem ---------------------------------------------------------------------------
rem 이 PC 에서 watchdog 을 아예 다루고 싶지 않으면 env\server.env 에 WATCHDOG_MANAGE=0.
if "%WATCHDOG_MANAGE%"=="0" goto :wd_skip

echo [terminate] watchdog 일시 정지 (%TASK_WATCHDOG%) ...
rem 먼저 존재 여부를 확인한다 — "등록 안 됨"(정상)과 "권한 부족"(조치 필요)은 전혀 다른
rem 상황인데, /Change 의 실패 코드만으로는 구분이 안 된다.
schtasks /Query /TN "%TASK_WATCHDOG%" >nul 2>nul
if errorlevel 1 goto :wd_absent
rem 실패 사유를 사용자가 볼 수 있도록 schtasks 출력을 삼키지 않는다.
schtasks /Change /TN "%TASK_WATCHDOG%" /DISABLE
if errorlevel 1 goto :wd_denied
echo [terminate]   - 정지됨. start.bat 으로 다시 열면 자동 재개됩니다.
echo [terminate]   - start.bat 을 실행하지 않을 거면 반드시 수동으로 재개할 것:
echo [terminate]       schtasks /Change /TN %TASK_WATCHDOG% /ENABLE
goto :wd_done
:wd_absent
echo [terminate]   - 이 PC 에는 watchdog 작업이 등록돼 있지 않습니다. 건너뜁니다.
echo [terminate]     자동 재기동이 필요하면 register_watchdog.bat 를 관리자 권한으로 1회 실행.
goto :wd_done
:wd_denied
echo [terminate]   - 정지 실패. 바로 위 schtasks 메시지가 원인입니다 (대개 관리자 권한 부족).
echo [terminate]     계속 진행하지만, 교체 중 watchdog 이 옛 코드로 되살릴 수 있습니다.
goto :wd_done
:wd_skip
echo [terminate] watchdog 관리 꺼짐 (WATCHDOG_MANAGE=0) - 건너뜁니다.
:wd_done

rem ---------------------------------------------------------------------------
rem 2) 리스닝 프로세스 확인
rem ---------------------------------------------------------------------------
echo.
echo [terminate] Checking server on port %PORT% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pids = @(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); " ^
  "if ($pids.Count -eq 0) { Write-Host '[terminate] No LISTENING process on port %PORT%.'; exit 1 }; " ^
  "Write-Host ('[terminate] LISTENING PID: ' + ($pids -join ', ')); exit 0"
rem 리스너가 없어도 :kill 로 간다 — 과거 재기동에서 남은 고아 워커 회수는 해야 한다.
if errorlevel 1 goto :kill

rem ---------------------------------------------------------------------------
rem 3) graceful drain  (구현: drain_wait.ps1 — 인라인으로는 담기 어려워 파일로 뺐다)
rem    inflight(진행 중 요청 수)가 0 이 되는 순간을 노려 내린다. 다만 **줄지 않으면**
rem    기다려도 소용없으므로 DRAIN_STALL_SEC 만에 포기하고, 그때는 종료 직전에 스레드
rem    덤프(log\diagnose_terminate_*.txt)를 남긴다 — 서버를 내리면 안 끝나던 요청의
rem    현행범 스택이 통째로 사라져 원인 규명이 막히기 때문이다 (2026-08-19 실제 사고).
rem    한계는 종전과 같다: waitress 를 "신규 요청 차단" 상태로 만들 수는 없어서, 이것은
rem    요청이 없는 순간을 포착하는 것이지 완전한 drain 이 아니다.
rem ---------------------------------------------------------------------------
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%drain_wait.ps1" ^
  -Port %PORT% -TimeoutSec %DRAIN_TIMEOUT_SEC% -StallSec %DRAIN_STALL_SEC% %DIAG_ARG% %FORCE_ARG%

rem ---------------------------------------------------------------------------
rem 4) 종료 + 포트 해제 확인
rem    서버 프로세스 하나만 죽이면 안 된다 — web_report 컴퓨트 워커(ProcessPoolExecutor,
rem    기본 2개)는 포트를 LISTEN 하지 않아 고아로 남고, 워커당 tables 캐시가 최대 4GB 다.
rem    실제 종료 로직은 watchdog.ps1 과 공유한다 (kill_server_tree.ps1 — 중복 방지).
rem ---------------------------------------------------------------------------
:kill
echo.
echo [terminate] Stopping server on port %PORT% ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%kill_server_tree.ps1" -Port %PORT% -Tag terminate

:done
endlocal
