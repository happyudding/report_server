# report-server 포트 점유 진단 — 읽기 전용(아무것도 죽이거나 고치지 않는다).
#
# 왜 필요한가: watchdog 이 "서버가 리스닝 중"으로 보고도 접속에 실패하는(healthz_connect)
# 상황에서는, 서비스 포트를 '누가' 쥐고 있는지가 원인 판별의 전부다. Windows 는 소켓 옵션
# (SO_REUSEADDR — waitress 기본값)이 켜져 있으면 이미 쓰는 포트에도 bind 가 성공해서,
# 두 프로세스가 같은 포트를 나눠 갖고 접속이 엉뚱한 쪽으로 갈 수 있다.
#
# 진단 포트(diag_listener)는 서비스 포트와 다른 소켓이라, 둘을 비교하면
# "프로세스가 아픈 것"과 "포트만 잘못된 것"을 가를 수 있다.
#
# 실행: diagnose_port.bat 더블클릭 (또는 이 파일을 powershell -File 로 실행)
# 결과: server\log\diagnose_port_<시각>.txt 에 저장 + 메모장으로 열림
#       (관리자 대시보드 console log 탭에서도 열람 가능 — diagnose_* 화이트리스트)

param(
    [int]$Port = 0,
    [int]$DiagPort = 0
)

$ErrorActionPreference = 'SilentlyContinue'
$serverDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $serverDir 'log'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 포트는 watchdog.ps1 과 같은 규칙으로 찾는다 (env\server.env → 기본값)
$envFile = Join-Path $serverDir 'env\server.env'
if (Test-Path $envFile) {
    foreach ($line in (Get-Content $envFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s#]+)') {
            Set-Item -Path ('env:' + $Matches[1]) -Value $Matches[2]
        }
    }
}
if (-not $Port) {
    if ($env:PORT) { $Port = [int]$env:PORT } else { $Port = 8080 }
}
if (-not $DiagPort) {
    if ($env:DIAG_PORT) { $DiagPort = [int]$env:DIAG_PORT } else { $DiagPort = $Port + 1 }
}

$out = New-Object System.Collections.Generic.List[string]
function Add-Line([string]$s) {
    $out.Add($s) | Out-Null
    Write-Host $s
}

Add-Line ("report-server 포트 진단  {0}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
Add-Line ("서비스 포트 {0} / 진단 포트 {1}" -f $Port, $DiagPort)
$hostSetting = '(미설정 → 0.0.0.0)'
if ($env:HOST) { $hostSetting = $env:HOST }
Add-Line ("env\server.env 의 HOST = {0}" -f $hostSetting)
if ($env:HOST -and $env:HOST -ne '0.0.0.0' -and $env:HOST -ne '::') {
    Add-Line '  *** HOST 가 특정 IP 로 고정돼 있다. 그러면 서버가 127.0.0.1 에서는 응답하지 않고,'
    Add-Line '      watchdog 의 healthz 점검(127.0.0.1)이 항상 실패해 재기동이 무한 반복된다. ***'
}
Add-Line ''

# ── [1] 서비스 포트를 쥐고 있는 프로세스 ─────────────────────────────────────
Add-Line ("== [1] 서비스 포트 {0} LISTEN 주인" -f $Port)
$listen = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$listenPids = @($listen | Select-Object -ExpandProperty OwningProcess -Unique)
if ($listen.Count -eq 0) {
    Add-Line '  (LISTEN 없음 — 서버가 떠 있지 않다)'
} else {
    foreach ($c in $listen) {
        $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        $path = '?'
        $start = '?'
        if ($p) {
            if ($p.Path) { $path = $p.Path }
            if ($p.StartTime) { $start = $p.StartTime.ToString('MM-dd HH:mm:ss') }
        }
        Add-Line ("  {0,-16} PID {1,-6} 기동 {2}  {3}" -f $c.LocalAddress, $c.OwningProcess, $start, $path)
    }
    if ($listenPids.Count -gt 1) {
        Add-Line ('  *** 경고: PID 가 {0} 종류 — 두 프로세스가 같은 포트를 나눠 쥐고 있다 ***' -f $listenPids.Count)
    }
}
Add-Line ''

# ── [2] 진단 포트 응답 (프로세스 자체는 건강한가) ────────────────────────────
Add-Line ("== [2] 진단 포트 {0} /alive" -f $DiagPort)
$alivePid = 0
try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/alive" -f $DiagPort) -UseBasicParsing -TimeoutSec 5
    Add-Line ('  ' + $r.Content)
    if ("$($r.Content)" -match '"pid"\s*:\s*(\d+)') { $alivePid = [int]$Matches[1] }
} catch {
    Add-Line ('  실패: ' + ("$($_.Exception.Message)" -replace '\s+', ' '))
    Add-Line '  (서버가 아직 새 코드로 재기동되지 않았거나, 프로세스 전체가 멈춘 상태)'
}
Add-Line ''

# ── [3] 서비스 포트 실제 응답 ────────────────────────────────────────────────
Add-Line ("== [3] 서비스 포트 {0} /healthz (watchdog 과 같은 방식)" -f $Port)
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $r = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/healthz" -f $Port) -UseBasicParsing -TimeoutSec 30
    $sw.Stop()
    Add-Line ('  정상 code={0} ms={1}' -f [int]$r.StatusCode, $sw.ElapsedMilliseconds)
} catch {
    $sw.Stop()
    $wstat = ''
    if ($_.Exception -is [System.Net.WebException]) { $wstat = "$($_.Exception.Status)" }
    Add-Line ('  실패 ms={0} wstat={1} — {2}' -f $sw.ElapsedMilliseconds, $wstat,
              ("$($_.Exception.Message)" -replace '\s+', ' '))
}
Add-Line ''

# ── [4] 판정 (두 포트의 주인이 같은가) ───────────────────────────────────────
Add-Line '== [4] 판정'
# (a) 바인딩 주소 — watchdog 은 127.0.0.1 로 점검하므로, 서버가 특정 IP 에만 붙어 있으면
#     서비스는 멀쩡한데 점검만 계속 실패한다(= 재기동 무한 반복의 전형적 원인).
$addrs = @($listen | Select-Object -ExpandProperty LocalAddress -Unique)
if ($listen.Count -gt 0) {
    $loopbackOk = @($addrs | Where-Object { $_ -eq '0.0.0.0' -or $_ -eq '127.0.0.1' }).Count -gt 0
    if ($loopbackOk) {
        Add-Line ('  바인딩 주소 OK: {0} — 127.0.0.1 로 접속 가능한 주소다' -f ($addrs -join ', '))
    } else {
        Add-Line ('  *** 바인딩 주소 문제: {0} 에만 LISTEN 중 ***' -f ($addrs -join ', '))
        Add-Line '  → 사용자는 정상 접속되지만 watchdog 의 127.0.0.1 점검은 항상 실패한다.'
        Add-Line '  → env\server.env 의 HOST 를 0.0.0.0 으로 되돌리고 서버를 재기동하면 해결된다.'
    }
}
# (b) 포트 주인이 서버 프로세스와 같은가 (Windows 포트 가로채기 확인)
if ($alivePid -gt 0 -and $listenPids.Count -gt 0) {
    if ($listenPids -contains $alivePid) {
        Add-Line ('  프로세스 OK: 서비스/진단 포트 주인이 같은 프로세스(PID {0}) — 포트 가로채기 아님' -f $alivePid)
    } else {
        Add-Line ('  *** 불일치: 서버 프로세스는 PID {0} 인데 포트 {1} 주인은 PID {2} ***' -f `
                  $alivePid, $Port, ($listenPids -join ', '))
        Add-Line '  → 다른 프로세스가 서비스 포트를 쥐고 있다. 이게 접속 실패의 원인이다.'
    }
} else {
    Add-Line '  프로세스 대조 불가 (위 [1]/[2] 중 하나가 응답 없음)'
}
Add-Line ''

# ── [5] 살아있는 python 프로세스 전부 ────────────────────────────────────────
# .venv 밖 python 이 포트를 쥐고 있으면 watchdog 의 부검(procs=N)에는 안 잡힌다.
Add-Line '== [5] 살아있는 python 프로세스 (venv 밖 포함)'
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue)
if ($procs.Count -eq 0) {
    Add-Line '  (없음)'
} else {
    foreach ($p in $procs) {
        $tag = 'parent'
        if ($p.CommandLine -match 'spawn_main.*?parent_pid=(\d+)') { $tag = ('worker(부모 {0})' -f $Matches[1]) }
        Add-Line ('  PID {0,-6} {1,6}MB {2,-18} {3}' -f $p.ProcessId,
                  [math]::Round($p.WorkingSetSize / 1MB, 0), $tag, $p.ExecutablePath)
    }
}
Add-Line ''

# ── [6] watchdog 연속실패 카운터 ─────────────────────────────────────────────
# healthz 실패가 연속인데 재기동으로 이어지지 않으면 이 파일이 안 쌓이고 있다는 뜻이다.
Add-Line '== [6] watchdog 연속실패 카운터 (watchdog.state)'
$stateFile = Join-Path $logDir 'watchdog.state'
if (Test-Path $stateFile) {
    $raw = (Get-Content $stateFile -Raw -ErrorAction SilentlyContinue)
    Add-Line ("  값='{0}'  마지막 기록 {1}" -f ("$raw".Trim()),
              (Get-Item $stateFile).LastWriteTime.ToString('MM-dd HH:mm:ss'))
} else {
    Add-Line '  (파일 없음 — watchdog 이 한 번도 안 돌았거나 기록에 실패 중)'
}
Add-Line ''

# ── [7] 최근 점검/이벤트 기록 ────────────────────────────────────────────────
# Get-Content 는 BOM 없는 UTF-8 을 시스템 코드페이지로 읽어 한글이 깨진다 — 명시적으로 읽는다.
function Show-Tail([string]$path, [int]$n, [string]$title) {
    Add-Line $title
    if (-not (Test-Path $path)) { Add-Line '  (파일 없음)'; return }
    $lines = @([System.IO.File]::ReadAllLines($path, [System.Text.Encoding]::UTF8))
    if ($lines.Count -eq 0) { Add-Line '  (비어 있음)'; return }
    $take = [Math]::Min($n, $lines.Count)
    foreach ($l in $lines[($lines.Count - $take)..($lines.Count - 1)]) { Add-Line ('  ' + $l) }
}
Show-Tail (Join-Path $logDir 'watchdog_checks.log') 12 '== [7] watchdog_checks.log 최근 12줄'
Add-Line ''
Show-Tail (Join-Path $logDir 'watchdog_events.log') 10 '== [8] watchdog_events.log 최근 10줄'
Add-Line ''

# ── [9] 서버 기동 로그 — 새 코드 반영 여부 + 재기동 빈도 ─────────────────────
Add-Line '== [9] 최근 server_*.txt (파일 1개 = 기동 1회)'
$svr = @(Get-ChildItem -Path $logDir -Filter 'server_*.txt' -ErrorAction SilentlyContinue |
         Sort-Object CreationTime | Select-Object -Last 6)
foreach ($f in $svr) {
    Add-Line ('  {0}  {1}' -f $f.CreationTime.ToString('MM-dd HH:mm:ss'), $f.Name)
}
$newest = @($svr | Select-Object -Last 1)
if ($newest.Count -gt 0) {
    $txt = [System.IO.File]::ReadAllText($newest[0].FullName, [System.Text.Encoding]::UTF8)
    if ($txt -match '\[diag\] listener on') {
        Add-Line '  → 새 코드(진단 리스너) 반영됨'
    } else {
        Add-Line '  → 최신 기동 로그에 [diag] 줄 없음 = 서버가 아직 구 코드로 돌고 있다'
    }
}
Add-Line ''

# ── [10] TCP 접속 자체를 직접 재본다 (HTTP 이전 단계) ────────────────────────
# 주의: Windows 에서는 '접속 거부'도 약 2초가 걸린다(실측 2033ms). 걸린 시간으로
# 거부/무응답을 가르면 안 되고, 소켓 오류 코드(SocketErrorCode)로 봐야 한다.
#   ConnectionRefused = 그 주소:포트에 리스너가 없다  (watchdog 의 ConnectFailure 정체)
#   TimedOut          = 응답 자체가 없다(패킷 유실 — 방화벽/보안 SW 등)
function Test-TcpConnect([string]$addr, [int]$p) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect($addr, $p, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne(10000, $false)) {
            $sw.Stop()
            return ('  {0}:{1} → 10초 내 무응답 (패킷 유실 = 방화벽/보안 SW 의심)' -f $addr, $p)
        }
        $client.EndConnect($ar)
        $sw.Stop()
        return ('  {0}:{1} → 접속 성공 ({2}ms)' -f $addr, $p, $sw.ElapsedMilliseconds)
    } catch {
        $sw.Stop()
        $codeName = ''
        $inner = $_.Exception.InnerException
        if ($inner -and $inner -is [System.Net.Sockets.SocketException]) {
            $codeName = "$($inner.SocketErrorCode)"
        }
        $kind = ('소켓오류 {0}' -f $codeName)
        if ($codeName -eq 'ConnectionRefused') { $kind = '거부 — 이 주소:포트에는 리스너가 없다' }
        elseif ($codeName -eq 'TimedOut') { $kind = '무응답 — 패킷 유실(방화벽/보안 SW 의심)' }
        return ('  {0}:{1} → 실패 {2}ms — {3}' -f $addr, $p, $sw.ElapsedMilliseconds, $kind)
    } finally {
        $client.Close()
    }
}
Add-Line '== [10] TCP 접속 직접 시험 (주소별)'
Add-Line (Test-TcpConnect '127.0.0.1' $Port)
Add-Line (Test-TcpConnect '127.0.0.1' $DiagPort)
# LISTEN 이 특정 IP 에만 걸려 있으면, 그 IP 로는 되는지도 확인해 대비시킨다.
foreach ($a in @($listen | Select-Object -ExpandProperty LocalAddress -Unique)) {
    if ($a -ne '127.0.0.1' -and $a -ne '0.0.0.0' -and $a -ne '::' -and $a -ne '::1') {
        Add-Line (Test-TcpConnect $a $Port)
    }
}
Add-Line ''

# ── [11] 방화벽 규칙 (관리자 권한 필요) ──────────────────────────────────────
Add-Line ("== [11] 포트 {0} 관련 방화벽 규칙" -f $Port)
try {
    $hit = @(Get-NetFirewallPortFilter -ErrorAction Stop |
             Where-Object { "$($_.LocalPort)" -eq "$Port" })
    if ($hit.Count -eq 0) {
        Add-Line '  (Windows 방화벽에 이 포트 전용 규칙 없음)'
    } else {
        foreach ($f in $hit) {
            $rule = $f | Get-NetFirewallRule -ErrorAction SilentlyContinue
            if ($rule) {
                Add-Line ('  [{0}] {1} / {2} / {3}' -f $rule.Action, $rule.DisplayName,
                          $rule.Direction, $(if ($rule.Enabled) { '사용' } else { '해제' }))
            }
        }
    }
} catch {
    Add-Line '  조회 권한 없음 — 이 항목까지 보려면 이 배치를 관리자 권한으로 실행할 것'
    Add-Line ('  (원문: ' + ("$($_.Exception.Message)" -replace '\s+', ' ') + ')')
}
Add-Line '  ※ 백신·사내 보안 프로그램(EDR)이 막는 경우는 여기 안 나온다 — 그 로그를 따로 봐야 한다.'
Add-Line ''
Add-Line '(이 파일 내용을 그대로 복사해 전달하면 됩니다. 아무것도 변경하지 않았습니다.)'

$outPath = Join-Path $logDir ("diagnose_port_{0}.txt" -f (Get-Date).ToString('yyyyMMdd_HHmmss'))
$utf8bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllLines($outPath, $out, $utf8bom)
Write-Host ''
Write-Host ("저장됨: {0}" -f $outPath)
Start-Process notepad.exe $outPath
exit 0
