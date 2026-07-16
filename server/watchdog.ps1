# report-server watchdog — Task Scheduler 가 5분 주기 + 부팅 시 실행 (등록: register_watchdog.bat)
#
# 동작:
#   1) 포트 미리스닝            -> 즉시 재기동
#   2) 리스닝 + /healthz 정상   -> no-op (fail 카운터 리셋)
#   3) 리스닝 + /healthz 무응답 -> 2연속 실패 시에만 재기동
#      (waitress 8스레드가 전부 heavy 작업에 점유된 일시 지연을 재기동으로 오판하지 않는 완충)
#
# 기록:
#   server\log\watchdog_events.log — 재기동/실패 이벤트 (JSON lines, admin 대시보드 현황 탭 표시)
#   server\log\watchdog.state      — 연속 실패 카운터 (mtime = 마지막 점검 시각)
#
# 수동 점검(terminate.bat 로 서버를 내려두는 시간)에는 watchdog 을 먼저 일시 정지할 것:
#   schtasks /Change /TN report-server-watchdog /DISABLE   (점검 후 /ENABLE)

param(
    [int]$Port = $(if ($env:PORT) { [int]$env:PORT } else { 8080 }),
    # start.bat 과 동일 규약 — LAN 노출 강제(0.0.0.0). 로컬 전용 운영이면 -BindHost 127.0.0.1
    [string]$BindHost = '0.0.0.0'
)

$ErrorActionPreference = 'SilentlyContinue'
$serverDir  = Split-Path -Parent $MyInvocation.MyCommand.Path   # ...\server
$logDir     = Join-Path $serverDir 'log'
$stateFile  = Join-Path $logDir 'watchdog.state'
$eventsFile = Join-Path $logDir 'watchdog_events.log'
$python     = Join-Path $serverDir '.venv\Scripts\python.exe'   # start.bat 과 동일 (server\.venv)
$wsgi       = Join-Path $serverDir 'wsgi.py'
$maxEventsBytes = 1MB

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Event([string]$evt, [string]$reason, [string]$detail) {
    $rec = @{
        ts     = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
        event  = $evt
        reason = $reason
        detail = $detail
    } | ConvertTo-Json -Compress
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::AppendAllText($eventsFile, $rec + "`r`n", $utf8)
    # 크기 캡 — 초과 시 최근 절반만 유지 (이벤트 파일 자체의 무한 성장 방지)
    $fi = Get-Item $eventsFile -ErrorAction SilentlyContinue
    if ($fi -and $fi.Length -gt $maxEventsBytes) {
        $lines = [System.IO.File]::ReadAllLines($eventsFile)
        $keep = $lines[[int]($lines.Count / 2)..($lines.Count - 1)]
        [System.IO.File]::WriteAllLines($eventsFile, $keep)
    }
}

function Get-FailCount {
    try { return [int](Get-Content $stateFile -TotalCount 1 -ErrorAction Stop) } catch { return 0 }
}

# 매 실행마다 기록 — 파일 mtime 이 "마지막 점검 시각"이 된다 (admin 대시보드 표시용)
function Set-FailCount([int]$n) {
    Set-Content -Path $stateFile -Value $n -Encoding Ascii
}

function Test-Listening {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Test-Healthz {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 30
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Restart-Server([string]$reason) {
    # 기존 리스너 강제 종료 (terminate.bat 준용 — healthz 무응답 hang 프로세스 정리)
    $pids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
              Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($procId in $pids) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }

    if (-not (Test-Path $python)) {
        Write-Event 'error' $reason ".venv python 없음: $python — start.bat 로 venv 를 먼저 생성할 것"
        Set-FailCount 0
        return
    }

    $env:HOST = $BindHost
    $env:PORT = "$Port"
    Start-Process -FilePath $python -ArgumentList '-u', "`"$wsgi`"" `
        -WorkingDirectory $serverDir -WindowStyle Hidden

    # 리스닝 대기 (최대 60초 — start.bat 과 동일)
    for ($i = 0; $i -lt 120; $i++) {
        if (Test-Listening) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Test-Listening) {
        Write-Event 'restart' $reason '재기동 성공 (listening)'
    } else {
        Write-Event 'restart_fail' $reason '재기동 후 60초 내 미리스닝 — server\log\server_*.txt 확인 필요'
    }
    Set-FailCount 0
}

# ── 본체 (5분 주기 태스크와 부팅 태스크의 동시 실행 방지 mutex) ──────────────
$mutex = New-Object System.Threading.Mutex($false, 'Global\report-server-watchdog')
if (-not $mutex.WaitOne(0)) { exit 0 }
try {
    if (-not (Test-Listening)) {
        Restart-Server 'not_listening'
    } elseif (Test-Healthz) {
        Set-FailCount 0
    } else {
        $fails = (Get-FailCount) + 1
        if ($fails -ge 2) {
            Restart-Server 'healthz_fail_x2'
        } else {
            Set-FailCount $fails
            Write-Event 'healthz_fail' 'healthz_timeout' "healthz 무응답 ($fails/2) — 다음 주기에도 실패 시 재기동"
        }
    }
} finally {
    [void]$mutex.ReleaseMutex()
}
