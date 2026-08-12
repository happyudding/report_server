# report-server watchdog — Task Scheduler 가 5분 주기 + 부팅 시 실행 (등록: register_watchdog.bat)
#
# 동작:
#   1) 포트 미리스닝            -> 즉시 재기동
#   2) 리스닝 + /healthz 정상   -> no-op (fail 카운터 리셋)
#   3) 리스닝 + /healthz 무응답 -> 2연속 실패 시에만 재기동
#      (waitress 스레드(env WAITRESS_THREADS)가 전부 heavy 작업에 점유된 일시 지연을
#       재기동으로 오판하지 않는 완충)
#
# 백오프(2026-07-23): 재기동해도 낫지 않는 상태에서 10분마다 재기동을 반복하면(관측 142회/일)
#   서버가 종일 기동 중이라 오히려 복구를 막고 server_*.txt 로그를 밀어내 원인 추적까지 없앤다.
#   최근 1시간 재기동이 임계를 넘으면 재기동을 '건너뛰고'(backoff_skip) 판정만 기록한다.
#   판정 로직(포트/healthz/2연속) 자체는 불변 — 억제 중에도 gap 이 지나면 즉시 재기동한다.
#
# 기록:
#   server\log\watchdog_events.log — 재기동/실패 이벤트 (JSON lines, admin 대시보드 현황 탭 표시)
#   server\log\watchdog.state      — 연속 실패 카운터 (mtime = 마지막 점검 시각)
#
# 수동 점검(terminate.bat 로 서버를 내려두는 시간)에는 watchdog 을 먼저 일시 정지할 것:
#   schtasks /Change /TN report-server-watchdog /DISABLE   (점검 후 /ENABLE)

# HOST/PORT 는 env\server.env 에서 읽는다 (start.bat 과 같은 파일 = 같은 설정).
# 인자로 넘기면 파일 값보다 우선한다. 파일도 인자도 없으면 0.0.0.0:8080.
param(
    [int]$Port = 0,
    [string]$BindHost = ''
)

$ErrorActionPreference = 'SilentlyContinue'
$serverDir  = Split-Path -Parent $MyInvocation.MyCommand.Path   # ...\server

$envFile = Join-Path $serverDir 'env\server.env'
if (Test-Path $envFile) {
    foreach ($line in (Get-Content $envFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s#]+)') {
            Set-Item -Path ('env:' + $Matches[1]) -Value $Matches[2]
        }
    }
}
if (-not $BindHost) {
    if ($env:HOST) { $BindHost = $env:HOST } else { $BindHost = '0.0.0.0' }
}
if (-not $Port) {
    if ($env:PORT) { $Port = [int]$env:PORT } else { $Port = 8080 }
}
$logDir     = Join-Path $serverDir 'log'
$stateFile  = Join-Path $logDir 'watchdog.state'
$eventsFile = Join-Path $logDir 'watchdog_events.log'
$checksFile = Join-Path $logDir 'watchdog_checks.log'   # 매 실행 기록 (재기동 폭주 진단용, events 와 분리)
$python     = Join-Path $serverDir '.venv\Scripts\python.exe'   # start.bat 과 동일 (server\.venv)
$wsgi       = Join-Path $serverDir 'wsgi.py'
$maxEventsBytes = 1MB

# 백오프 임계 (env\server.env 로 조정 가능, 미설정 시 아래 기본값)
function Get-EnvInt([string]$name, [int]$fallback) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v) { try { return [int]$v } catch { } }
    return $fallback
}
$backoffMaxPerHour = Get-EnvInt 'WATCHDOG_BACKOFF_MAX_PER_HOUR' 3    # healthz 계열: 1시간 재기동 허용 횟수
$backoffGapMin     = Get-EnvInt 'WATCHDOG_BACKOFF_GAP_MIN'      30   # 초과 시 다음 재기동까지 간격(분)
$backoffNlMax      = Get-EnvInt 'WATCHDOG_BACKOFF_NL_MAX'       6    # not_listening: 가용성 우선이라 더 관대
$backoffNlGapMin   = Get-EnvInt 'WATCHDOG_BACKOFF_NL_GAP_MIN'   15

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# JSON line 1건을 파일에 append + 1MB 캡(초과 시 최근 절반 유지). events/checks 공용.
# mutex 밖에서도 불릴 수 있어(동시 append) 파일 IO 는 try/catch 로 감싼다 — $ErrorActionPreference
# 는 .NET 메서드 예외에 적용되지 않으므로 여기서 명시적으로 삼킨다(수 ms 창, 유실 허용).
function Write-JsonLine([string]$file, $rec) {
    try {
        $json = ($rec | ConvertTo-Json -Compress)
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::AppendAllText($file, $json + "`r`n", $utf8)
        $fi = Get-Item $file -ErrorAction SilentlyContinue
        if ($fi -and $fi.Length -gt $maxEventsBytes) {
            $lines = [System.IO.File]::ReadAllLines($file)
            $keep = $lines[[int]($lines.Count / 2)..($lines.Count - 1)]
            [System.IO.File]::WriteAllLines($file, $keep)
        }
    } catch { }
}

# 재기동/실패 이벤트 — 대시보드 현황 탭이 이 파일을 읽는다 (포맷·키 불변).
function Write-Event([string]$evt, [string]$reason, [string]$detail) {
    Write-JsonLine $eventsFile @{
        ts     = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
        event  = $evt
        reason = $reason
        detail = $detail
    }
}

# 매 실행 1줄 — 실행 빈도 자체가 남아야 '16회/5분' 재발 시 태스크 과다실행 vs 집계착시를
# 즉시 판별할 수 있다. events 와 분리해 대시보드 최근 이벤트가 check 로 덮이지 않게 한다.
function Write-Check($rec) {
    $rec['ts'] = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')
    Write-JsonLine $checksFile $rec
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

# healthz 를 '어느 주소로' 물어볼지 실제 LISTEN 주소를 보고 정한다.
#
# 2026-07-29 실제 사고: HOST 가 운영 IP 하나로 고정돼 서버가 그 IP 에만 bind 되면
# 127.0.0.1 로는 접속이 거부된다. 사용자는 멀쩡히 쓰는데 점검만 100% 실패해서
# 재기동이 종일 반복됐다(24h 49회 + 억제 110회). 재기동은 bind 주소를 바꾸지 못하므로
# 영원히 낫지 않는다. loopback 고정 점검이 원인이었으므로 주소를 따라가게 한다.
function Get-ProbeHost {
    $addrs = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
               Select-Object -ExpandProperty LocalAddress -Unique)
    if ($addrs -contains '0.0.0.0' -or $addrs -contains '127.0.0.1') { return '127.0.0.1' }
    if ($addrs -contains '::' -or $addrs -contains '::1') { return '[::1]' }
    foreach ($a in $addrs) {
        if ($a -notmatch ':') { return $a }          # 특정 IPv4 에만 bind 된 경우
    }
    if ($addrs.Count -gt 0) { return ('[{0}]' -f $addrs[0]) }   # IPv6 리터럴은 대괄호
    return '127.0.0.1'
}

# 판정은 .ok 만 쓴다(로직 불변). 진단을 위해 코드(503=DB fail)·소요시간·오류를 함께 반환.
# wstat = WebException.Status enum 문자열(Timeout / ConnectFailure / ProtocolError ...).
# 예외 메시지는 OS 언어를 타므로 문자열 매칭 대신 이 enum 으로 실패 종류를 가른다.
function Test-Healthz([string]$probeHost) {
    $uri = ('http://{0}:{1}/healthz' -f $probeHost, $Port)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 30
        $sw.Stop()
        return @{ ok = ($r.StatusCode -eq 200); code = [int]$r.StatusCode; ms = $sw.ElapsedMilliseconds
                  wstat = ''; err = '' }
    } catch {
        $sw.Stop()
        $code = 0
        $wstat = ''
        if ($_.Exception -is [System.Net.WebException]) { $wstat = "$($_.Exception.Status)" }
        if ($_.Exception.Response) { try { $code = [int]$_.Exception.Response.StatusCode } catch { } }
        # 이벤트 detail 이 길어지면 1MB 캡이 이력을 조기에 밀어낸다 — 한 줄로 정규화 후 200자 절단.
        $msg = ("$($_.Exception.Message)" -replace '\s+', ' ')
        if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
        return @{ ok = $false; code = $code; ms = $sw.ElapsedMilliseconds; wstat = $wstat; err = $msg }
    }
}

# 킬 이전에 서버 프로세스 상태 요약 (부검). 비정상 경로에서만 호출(정상 no-op 은 비용 유지).
# "procs=0"(프로세스 사망) 인지 "procs=N, 리스너 없음"(포트만 소실) 인지 즉시 판별하게 한다.
function Get-ServerProcSummary {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
               Where-Object { $_.ExecutablePath -eq $python })
    if ($procs.Count -eq 0) { return 'procs=0' }
    $parts = @()
    foreach ($p in $procs) {
        $rssMB = [math]::Round($p.WorkingSetSize / 1MB, 0)
        $tag = if ($p.CommandLine -match 'spawn_main.*?parent_pid=(\d+)') { 'worker' } else { 'parent' }
        $parts += ("{0}:{1}MB:{2}" -f $p.ProcessId, $rssMB, $tag)
    }
    return ("procs={0} [{1}]" -f $procs.Count, ($parts -join ' '))
}

# 킬 이전에 사이드 진단 리스너(diag_listener.py)에서 스레드 덤프를 받아온다.
# waitress 스레드가 전부 묶여 healthz 가 굶는 상황에서도 이 리스너는 별도 소켓·스레드라
# 응답한다 — "어떤 요청이 스레드를 잡고 있었나"를 재기동으로 잃지 않게 하는 유일한 증거다.
# 서버가 이미 죽었거나 구버전(리스너 없음)이면 3초 후 포기하고 그대로 진행한다.
function Get-DiagDump {
    $diagPort = if ($env:DIAG_PORT) { [int]$env:DIAG_PORT } else { $Port + 1 }
    if ($diagPort -le 0) { return '(diag listener 비활성 DIAG_PORT=0)' }
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$diagPort/threads" -UseBasicParsing -TimeoutSec 3
        $txt = "$($r.Content)"
        if ($txt.Length -gt 8192) { $txt = $txt.Substring(0, 8192) + "`r`n... (8KB 절단)" }
        return $txt
    } catch {
        return ("(diag listener 무응답 :{0} — {1})" -f $diagPort, ("$($_.Exception.Message)" -replace '\s+', ' '))
    }
}

# 킬 이전에 최신 server_*.txt 의 마지막 20줄 + 스레드 덤프를 스냅샷 파일로 보존 (죽은 이유 원문).
# 큰 텍스트를 이벤트 detail 에 넣으면 1MB 캡이 이벤트 이력을 조기 삭제하므로 별도 파일로 둔다.
function Save-Snapshot([string]$reason, [string]$autopsy) {
    try {
        $latest = Get-ChildItem -Path $logDir -Filter 'server_*.txt' -ErrorAction SilentlyContinue |
                  Sort-Object CreationTime | Select-Object -Last 1
        $snapPath = Join-Path $logDir ("watchdog_snap_{0}.txt" -f (Get-Date).ToString('yyyyMMdd_HHmmss'))
        $content = @(
            ("reason : {0}" -f $reason),
            ("autopsy: {0}" -f $autopsy),
            ("ts     : {0}" -f (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')),
            '--- 스레드 덤프 (diag listener) ---'
        )
        $content += @((Get-DiagDump) -split "`r?`n")
        if ($latest) {
            $content += ("--- {0} (마지막 20줄) ---" -f $latest.Name)
            $content += @(Get-Content $latest.FullName -Tail 20 -ErrorAction SilentlyContinue)
        } else {
            $content += '(server_*.txt 없음)'
        }
        $utf8bom = New-Object System.Text.UTF8Encoding($true)
        [System.IO.File]::WriteAllLines($snapPath, $content, $utf8bom)
        # 최신 30개 초과분 prune
        $snaps = @(Get-ChildItem -Path $logDir -Filter 'watchdog_snap_*.txt' -ErrorAction SilentlyContinue | Sort-Object CreationTime)
        if ($snaps.Count -gt 30) {
            $snaps[0..($snaps.Count - 31)] | ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }
        }
        return (Split-Path -Leaf $snapPath)
    } catch { return '' }
}

# 최근 $minutes 분 내 재기동(restart/restart_fail) 시각 배열. 별도 상태 파일을 두지 않고
# events 로그를 재활용한다 — 이벤트에 ts 가 이미 있고 1MB 캡이 '최근 절반 유지'라 1시간분은
# 항상 살아있다. 폭주 시 파일이 수천 줄이 되므로 마지막 400줄만 파싱한다.
function Get-RecentRestarts([int]$minutes) {
    $out = @()
    try {
        if (-not (Test-Path $eventsFile)) { return $out }
        $lines = [System.IO.File]::ReadAllLines($eventsFile)
        if ($lines.Count -gt 400) { $lines = $lines[($lines.Count - 400)..($lines.Count - 1)] }
        $cut = (Get-Date).AddMinutes(-$minutes)
        foreach ($ln in $lines) {
            if (-not $ln) { continue }
            try { $rec = $ln | ConvertFrom-Json } catch { continue }
            if ($rec.event -ne 'restart' -and $rec.event -ne 'restart_fail') { continue }
            try {
                $t = [datetime]::ParseExact($rec.ts, 'yyyy-MM-ddTHH:mm:ss', $null)
            } catch { continue }
            if ($t -ge $cut) { $out += $t }
        }
    } catch { }
    return $out
}

# 재기동을 억제할지 판정. 억제면 사유 문자열, 아니면 $null.
# not_listening(서버 완전 다운)은 가용성 우선이라 더 관대한 임계를 쓴다.
function Test-BackoffSkip([string]$reason) {
    if ($reason -eq 'not_listening') {
        $max = $backoffNlMax; $gap = $backoffNlGapMin
    } else {
        $max = $backoffMaxPerHour; $gap = $backoffGapMin
    }
    if ($max -le 0) { return $null }   # 0 이하면 백오프 비활성
    $recent = @(Get-RecentRestarts 60)
    if ($recent.Count -lt $max) { return $null }
    $last = ($recent | Sort-Object)[-1]
    $next = $last.AddMinutes($gap)
    if ((Get-Date) -ge $next) { return $null }
    return ("재기동 억제: 최근1h {0}회(임계 {1}) — 다음 허용 {2}" -f `
            $recent.Count, $max, $next.ToString('HH:mm'))
}

function Restart-Server([string]$reason) {
    # 부검 — 킬 이전에 프로세스 상태·서버 로그 tail 을 확보한다 (재기동 원인 보존).
    # 결과는 script 변수로 남겨 본체의 check 레코드가 함께 기록한다.
    $autopsy = Get-ServerProcSummary
    $snap = Save-Snapshot $reason $autopsy
    $script:lastAutopsy = $autopsy
    $script:lastSnap = $snap

    # 기존 리스너 + 컴퓨트 워커 강제 종료 (terminate.bat 과 kill_server_tree.ps1 을 공유).
    # 리스너만 죽이면 web_report 컴퓨트 워커(포트를 LISTEN 하지 않는 별도 python.exe)가
    # 재기동마다 2개씩 고아로 쌓인다. 워커당 tables 캐시가 최대 4GB 라 메모리를 잠식하고,
    # 그 지연이 다시 healthz 오판 -> 재기동을 부르는 악순환이 된다.
    $killScript = Join-Path $serverDir 'kill_server_tree.ps1'
    if (Test-Path $killScript) {
        $killLog = ((& $killScript -Port $Port -Tag 'watchdog' 2>&1 | Out-String).Trim() -replace '\s*\r?\n\s*', ' | ')
    } else {
        # 배포 누락 폴백 — 워커는 못 잡아도 리스너는 반드시 정리해야 포트 충돌을 피한다.
        $pids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                  Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($procId in $pids) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
        $killLog = "WARNING: kill_server_tree.ps1 없음 - 리스너만 종료(워커 고아 가능)"
    }

    if (-not (Test-Path $python)) {
        Write-Event 'error' $reason ".venv python 없음: $python — start.bat 로 venv 를 먼저 생성할 것"
        Set-FailCount 0
        $script:lastRestartResult = 'restart_fail'
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
        Write-Event 'restart' $reason "재기동 성공 (listening). autopsy=[$autopsy] snap=$snap. $killLog"
        $script:lastRestartResult = 'restart_ok'
    } else {
        Write-Event 'restart_fail' $reason "재기동 후 60초 내 미리스닝 — snap=$snap 확인. autopsy=[$autopsy]. $killLog"
        $script:lastRestartResult = 'restart_fail'
    }
    Set-FailCount 0
}

# ── 본체 (5분 주기 태스크와 부팅 태스크의 동시 실행 방지 mutex) ──────────────
$mutex = New-Object System.Threading.Mutex($false, 'Global\report-server-watchdog')
if (-not $mutex.WaitOne(0)) {
    # 다른 watchdog 인스턴스가 실행 중 = 태스크가 겹쳐서 떴다는 직접 증거.
    # 현재는 무기록 exit 였으나, 이 1줄이 '태스크 과다기동 vs 집계착시' 판별의 핵심이다.
    Write-Check @{ result = 'mutex_busy'; detail = '다른 watchdog 인스턴스 실행 중(동시 기동)' }
    exit 0
}
$swRun = [System.Diagnostics.Stopwatch]::StartNew()
try {
    if (-not (Test-Listening)) {
        $skip = Test-BackoffSkip 'not_listening'
        if ($skip) {
            Write-Event 'backoff_skip' 'not_listening' $skip
            Write-Check @{ result = 'backoff_skip'; reason = 'not_listening'; listen = 0
                           detail = $skip; elapsed_ms = $swRun.ElapsedMilliseconds }
        } else {
            Restart-Server 'not_listening'
            Write-Check @{ result = $script:lastRestartResult; reason = 'not_listening'; listen = 0
                           procs = $script:lastAutopsy; snap = $script:lastSnap; elapsed_ms = $swRun.ElapsedMilliseconds }
        }
    } else {
        $probeHost = Get-ProbeHost
        $hz = Test-Healthz $probeHost
        if ($hz.ok) {
            Set-FailCount 0
            Write-Check @{ result = 'ok'; listen = 1; code = $hz.code; ms = $hz.ms
                           addr = $probeHost; elapsed_ms = $swRun.ElapsedMilliseconds }
        } else {
            $fails = (Get-FailCount) + 1
            # 원인 3분류. healthz_timeout 은 기존 문자열을 유지한다(구 로그·운영 관습 호환) —
            # '연결 거부'만 healthz_connect 로 떼어낸다. 서버가 리스닝 중인데 연결이 거부되면
            # 스레드 고갈(=응답 지연)이 아니라 프로세스가 방금 죽었다는 뜻이라 대응이 다르다.
            $hzReason = if ($hz.code -eq 503) { 'healthz_503' }
                        elseif ($hz.wstat -eq 'ConnectFailure') { 'healthz_connect' }
                        else { 'healthz_timeout' }
            $hzInfo = "addr=$probeHost code=$($hz.code) ms=$($hz.ms) wstat=$($hz.wstat) err=$($hz.err)"
            if ($fails -ge 2) {
                $skip = Test-BackoffSkip 'healthz_fail_x2'
                if ($skip) {
                    # fail 카운터는 리셋하지 않는다 — gap 이 지나면 다음 주기에 곧바로 재기동된다.
                    Set-FailCount $fails
                    Write-Event 'backoff_skip' $hzReason "$skip ($hzInfo)"
                    Write-Check @{ result = 'backoff_skip'; reason = $hzReason; listen = 1; code = $hz.code; ms = $hz.ms
                                   wstat = $hz.wstat; err = $hz.err; addr = $probeHost
                                   fails = $fails; detail = $skip; elapsed_ms = $swRun.ElapsedMilliseconds }
                } else {
                    Restart-Server 'healthz_fail_x2'
                    Write-Check @{ result = $script:lastRestartResult; reason = $hzReason; listen = 1; code = $hz.code; ms = $hz.ms
                                   wstat = $hz.wstat; err = $hz.err; addr = $probeHost
                                   fails = $fails; procs = $script:lastAutopsy; snap = $script:lastSnap; elapsed_ms = $swRun.ElapsedMilliseconds }
                }
            } else {
                Set-FailCount $fails
                Write-Event 'healthz_fail' $hzReason "healthz 무응답 ($fails/2) $hzInfo — 다음 주기에도 실패 시 재기동"
                Write-Check @{ result = 'healthz_fail'; reason = $hzReason; listen = 1; code = $hz.code; ms = $hz.ms
                               wstat = $hz.wstat; err = $hz.err; addr = $probeHost
                               fails = $fails; elapsed_ms = $swRun.ElapsedMilliseconds }
            }
        }
    }
} finally {
    [void]$mutex.ReleaseMutex()
}
