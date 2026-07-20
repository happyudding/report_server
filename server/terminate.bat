@echo off
setlocal

if not defined PORT set "PORT=8080"
rem drain 최대 대기 시간(초). 이 시간을 넘기면 진행 중 요청이 남아 있어도 강제 종료한다.
if not defined DRAIN_TIMEOUT_SEC set "DRAIN_TIMEOUT_SEC=90"

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
echo [terminate] watchdog 일시 정지 (%TASK_WATCHDOG%) ...
schtasks /Change /TN "%TASK_WATCHDOG%" /DISABLE >nul 2>nul
if errorlevel 1 goto :wd_none
echo [terminate]   - 정지됨. start.bat 으로 다시 열면 자동 재개됩니다.
echo [terminate]   - start.bat 을 실행하지 않을 거면 반드시 수동으로 재개할 것:
echo [terminate]       schtasks /Change /TN %TASK_WATCHDOG% /ENABLE
goto :wd_done
:wd_none
echo [terminate]   - 미등록이거나 권한 부족으로 실패. 무시하고 계속합니다.
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
if errorlevel 1 goto :done

rem ---------------------------------------------------------------------------
rem 3) graceful drain
rem    /healthz 의 inflight(진행 중 요청 수, 자기 자신 제외)가 0 이 되는 순간을 노려 내린다.
rem    한계: waitress 를 "신규 요청 차단" 상태로 만들 수는 없다. 따라서 이것은 요청이 없는
rem    순간을 포착하는 것이지 완전한 drain 이 아니다. 진행 중인 업로드/리포트 빌드가 통째로
rem    끊기는 것을 막는 것이 목적이며, 그 목적에는 충분하다.
rem ---------------------------------------------------------------------------
echo.
echo [terminate] 진행 중 요청이 끝나기를 기다립니다 (최대 %DRAIN_TIMEOUT_SEC%초) ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port = [int]'%PORT%'; $deadline = (Get-Date).AddSeconds([int]'%DRAIN_TIMEOUT_SEC%'); $idle = 0; " ^
  "while ($true) { " ^
  "  if ((Get-Date) -ge $deadline) { Write-Host '[terminate] WARNING: 제한시간 초과 - 진행 중 요청을 남긴 채 강제 종료합니다.'; break }; " ^
  "  try { $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $port + '/healthz') -UseBasicParsing -TimeoutSec 5; $j = $r.Content | ConvertFrom-Json } " ^
  "  catch { Write-Host '[terminate] healthz 무응답 - drain 생략 (이미 응답 불능 상태).'; break }; " ^
  "  $n = $j.inflight; " ^
  "  if ($null -eq $n) { Write-Host '[terminate] 서버가 inflight 를 보고하지 않음 (metrics 비활성) - 5초 고정 대기 후 종료.'; Start-Sleep -Seconds 5; break }; " ^
  "  if ([int]$n -le 0) { $idle++; if ($idle -ge 2) { Write-Host '[terminate] 진행 중 요청 없음 - 안전하게 종료합니다.'; break } } " ^
  "  else { $idle = 0; Write-Host ('[terminate]   진행 중 요청 ' + $n + '건 - 완료 대기 중 ...') }; " ^
  "  Start-Sleep -Seconds 1 " ^
  "}"

rem ---------------------------------------------------------------------------
rem 4) 종료 + 포트 해제 확인
rem ---------------------------------------------------------------------------
echo.
echo [terminate] Stopping server on port %PORT% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$pids = @(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); " ^
  "if ($pids.Count -eq 0) { Write-Host '[terminate] 이미 종료됨.'; exit 0 }; " ^
  "foreach ($procId in $pids) { Write-Host ('[terminate] Killing PID ' + $procId); Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }; " ^
  "for ($i = 0; $i -lt 40; $i++) { " ^
  "  if (-not (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue)) { Write-Host '[terminate] Done. 포트 해제 확인.'; exit 0 }; " ^
  "  Start-Sleep -Milliseconds 250 " ^
  "}; " ^
  "Write-Host '[terminate] WARNING: 포트가 아직 LISTEN 상태입니다. 남은 프로세스를 확인하세요.'; exit 1"

:done
endlocal
