# report-server watchdog 진단 — 재기동 폭주(짧은 시간 다수 재기동) 원인 규명용.
#
# 운영 PC 에서 관리자 권한으로 1회 실행한다 (우클릭 > 관리자 권한 실행 또는 관리자 PowerShell):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\diagnose_watchdog.ps1
#
# read-only 다 — 프로세스를 죽이거나 재기동하지 않는다. 유일한 쓰기는 리포트 파일 1개
#   (server\log\diagnose_<timestamp>.txt) + 콘솔 출력이다.
#
# 세 가설을 독립 증거로 교차 대조한다:
#   (a) 태스크 중복 등록 / 외부 반복 실행  (b) Task Scheduler 이상(부팅 루프 포함)
#   (c) 이벤트 ts 해석·집계 착시
#
# 이 파일은 UTF-8 BOM + CRLF 로 저장할 것 (.gitattributes 강제, watchdog.ps1 과 동일).

param(
    [int]$Hours = 48,
    [int]$Port = 0
)

$ErrorActionPreference = 'SilentlyContinue'
$serverDir = Split-Path -Parent $MyInvocation.MyCommand.Path   # ...\server
$logDir    = Join-Path $serverDir 'log'
$venvPython = Join-Path $serverDir '.venv\Scripts\python.exe'

# HOST/PORT 는 watchdog.ps1 과 같은 env\server.env 에서 읽는다 (인자가 우선).
if ($Port -le 0) {
    $envFile = Join-Path $serverDir 'env\server.env'
    if (Test-Path $envFile) {
        foreach ($line in (Get-Content $envFile -ErrorAction SilentlyContinue)) {
            if ($line -match '^\s*PORT\s*=\s*([0-9]+)') { $Port = [int]$Matches[1] }
        }
    }
}
if ($Port -le 0) { $Port = 8080 }

$since = (Get-Date).AddHours(-$Hours)

# ── 리포트 버퍼 (콘솔 + 파일 동시) ───────────────────────────────────────────
$script:report = New-Object System.Collections.Generic.List[string]
function Out([string]$msg = '') {
    Write-Host $msg
    $script:report.Add($msg)
}
function Section([string]$title) {
    Out ''
    Out ('=' * 78)
    Out ("## $title")
    Out ('=' * 78)
}

# 요약 판정에 쓰려고 섹션들이 채우는 값
$script:evtRestarts = $null      # watchdog_events.log 의 restart+restart_fail 수
$script:worst5min   = $null      # events 기준 최악 5분 창 재기동 수
$script:opStarts    = $null      # TaskScheduler operational ID 100 (실제 기동) 수
$script:opEnabled   = $null      # operational 로그 활성 여부
$script:newServerLogs = $null    # 분석 창 내 server_*.txt 개수
$script:taskCount   = $null      # watchdog.ps1 을 실행하는 태스크 수

Out ("report-server watchdog 진단 리포트")
Out ("생성 시각 : {0}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
Out ("분석 창   : 최근 {0} 시간 (>= {1})" -f $Hours, $since.ToString('yyyy-MM-dd HH:mm:ss'))
Out ("포트      : {0}" -f $Port)
Out ("서버 경로 : {0}" -f $serverDir)

# ── 요약은 맨 앞에 두되, 각 섹션이 값을 채운 뒤 계산해야 하므로 자리만 표시 ──
Out ''
Out '(요약 판정은 리포트 맨 끝 [SUMMARY] 섹션 참조 — 각 증거 수집 후 종합)'

# =============================================================================
# 1) watchdog_events.log 분석
# =============================================================================
Section '1) watchdog_events.log — 재기동/실패 이벤트 이력'
try {
    $eventsFile = Join-Path $logDir 'watchdog_events.log'
    if (-not (Test-Path $eventsFile)) {
        Out 'watchdog_events.log 없음 — watchdog 이 아직 이벤트를 기록하지 않았거나 미등록.'
    } else {
        $lines = @(Get-Content $eventsFile -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
        $events = @()
        foreach ($ln in $lines) {
            try { $events += (ConvertFrom-Json $ln) } catch { }
        }
        Out ("총 이벤트 라인: {0} (파싱 성공 {1})" -f $lines.Count, $events.Count)

        # 이 파일은 1MB 초과 시 watchdog 이 최근 절반만 남긴다 — 과거 증거가 이미 잘렸을 수 있음
        if ($events.Count -gt 0) {
            Out ("이벤트 커버리지 시작: {0}  (이보다 과거는 1MB 캡으로 삭제됐을 수 있음)" -f $events[0].ts)
        }

        # 종류별 집계
        Out ''
        Out '이벤트 종류별 집계:'
        $events | Group-Object event | Sort-Object Count -Descending | ForEach-Object {
            Out ("  {0,-14} {1}" -f $_.Name, $_.Count)
        }
        Out 'reason 별 집계:'
        $events | Group-Object reason | Sort-Object Count -Descending | ForEach-Object {
            Out ("  {0,-18} {1}" -f $_.Name, $_.Count)
        }

        # 재기동(restart/restart_fail)만 뽑아 시각·간격 분석
        $restarts = @()
        foreach ($e in $events) {
            if ($e.event -eq 'restart' -or $e.event -eq 'restart_fail') {
                $dt = $null
                try { $dt = [datetime]::ParseExact($e.ts, 'yyyy-MM-ddTHH:mm:ss', $null) } catch { }
                if ($dt) { $restarts += [pscustomobject]@{ dt = $dt; event = $e.event; reason = $e.reason } }
            }
        }
        $restarts = @($restarts | Sort-Object dt)
        $script:evtRestarts = $restarts.Count
        Out ''
        Out ("재기동 이벤트(restart+restart_fail) 총 {0}건" -f $restarts.Count)

        # 최악 5분 창 = 임의 5분 안에 몇 건이 몰렸나 (= '16회/5분' 재현 여부)
        $worst = 0
        for ($i = 0; $i -lt $restarts.Count; $i++) {
            $windowEnd = $restarts[$i].dt.AddMinutes(5)
            $cnt = 0
            for ($j = $i; $j -lt $restarts.Count; $j++) {
                if ($restarts[$j].dt -le $windowEnd) { $cnt++ } else { break }
            }
            if ($cnt -gt $worst) { $worst = $cnt }
        }
        $script:worst5min = $worst
        Out ("임의 5분 창 최대 재기동 수: {0}  (대시보드 '5분 16회' 와 대조)" -f $worst)

        # 최근 분석 창 타임라인 + 5분 미만 간격 강조
        Out ''
        Out ("최근 {0}시간 재기동 타임라인 (직전과 5분 미만 간격이면 '<<' 표시):" -f $Hours)
        $prev = $null
        $shown = 0
        foreach ($r in $restarts) {
            if ($r.dt -lt $since) { $prev = $r.dt; continue }
            $gap = ''
            if ($prev) {
                $mins = ($r.dt - $prev).TotalMinutes
                $gap = ("(+{0:N1}분)" -f $mins)
                if ($mins -lt 5) { $gap += ' <<' }
            }
            Out ("  {0}  {1,-13} {2,-16} {3}" -f $r.dt.ToString('MM-dd HH:mm:ss'), $r.event, $r.reason, $gap)
            $prev = $r.dt
            $shown++
        }
        if ($shown -eq 0) { Out '  (분석 창 내 재기동 이벤트 없음)' }

        # 동일 ts 중복 라인 (착시 가설 c)
        $dupTs = $events | Group-Object ts | Where-Object { $_.Count -gt 1 }
        Out ''
        if ($dupTs) {
            Out ("[의심-c] 동일 ts(초 단위) 중복 이벤트 발견 — 집계 착시 가능:")
            $dupTs | Sort-Object Count -Descending | Select-Object -First 10 | ForEach-Object {
                Out ("  ts={0} x{1}" -f $_.Name, $_.Count)
            }
        } else {
            Out '동일 ts 중복 이벤트 없음 (착시 가설 c 근거 약함).'
        }
    }
} catch {
    Out ("[섹션1 오류] {0}" -f $_.Exception.Message)
}

# =============================================================================
# 2) schtasks — 등록된 watchdog 관련 작업
# =============================================================================
Section '2) 예약 작업(schtasks) — watchdog 관련 작업 등록 상태'
try {
    $allTasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue)
    $wdTasks = @($allTasks | Where-Object {
        ($_.TaskName -like 'report-server-*') -or
        (@($_.Actions | Where-Object { $_.Arguments -match 'watchdog\.ps1' -or $_.Execute -match 'watchdog\.ps1' }).Count -gt 0)
    })
    $script:taskCount = $wdTasks.Count
    Out ("watchdog.ps1 을 실행하는(또는 report-server-*) 작업 수: {0}" -f $wdTasks.Count)
    if ($wdTasks.Count -gt 2) {
        Out '[의심-a] 예상(2개: watchdog + boot)보다 많다 — 중복/추가 등록 가능성.'
    }
    foreach ($t in $wdTasks) {
        $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath -ErrorAction SilentlyContinue
        Out ''
        Out ("  [작업] {0}{1}   상태={2}" -f $t.TaskPath, $t.TaskName, $t.State)
        foreach ($a in $t.Actions) {
            Out ("    실행: {0} {1}" -f $a.Execute, $a.Arguments)
        }
        # 트리거 — 반복(Repetition) 이 붙어 있으면 5분보다 잦은 자체 반복일 수 있어 강조
        foreach ($trg in $t.Triggers) {
            $rep = ''
            if ($trg.Repetition -and $trg.Repetition.Interval) {
                $rep = (" [의심-b] Repetition Interval={0} Duration={1}" -f $trg.Repetition.Interval, $trg.Repetition.Duration)
            }
            Out ("    트리거: {0}{1}" -f $trg.CimClass.CimClassName, $rep)
        }
        if ($info) {
            Out ("    LastRun={0}  Result=0x{1:X}  NextRun={2}" -f $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
        }
    }
    if ($wdTasks.Count -eq 0) {
        Out '[주의] watchdog 관련 예약 작업이 하나도 없다 — register_watchdog.bat 미등록이거나, 다른 사용자 컨텍스트에 등록됨.'
    }

    # 교차검증: schtasks CSV 원문에서 watchdog 문자열 라인 (한국어 헤더라 컬럼 파싱 안 함, 문자열 매칭만)
    Out ''
    Out '교차검증 — schtasks /Query 원문 중 "watchdog" 포함 라인:'
    $csv = schtasks /Query /V /FO CSV 2>$null
    $hit = @($csv | Where-Object { $_ -match 'watchdog' })
    if ($hit.Count -gt 0) {
        $hit | Select-Object -First 20 | ForEach-Object { Out ("  {0}" -f $_) }
    } else {
        Out '  (없음)'
    }
} catch {
    Out ("[섹션2 오류] {0}" -f $_.Exception.Message)
}

# =============================================================================
# 3) Task Scheduler operational 이벤트 로그 — 실제 기동 횟수 (핵심 증거)
# =============================================================================
Section '3) TaskScheduler/Operational 이벤트 — 태스크가 실제 몇 번 떴나 (핵심 증거)'
try {
    $logName = 'Microsoft-Windows-TaskScheduler/Operational'
    $logInfo = Get-WinEvent -ListLog $logName -ErrorAction SilentlyContinue
    if (-not $logInfo) {
        Out '이 로그를 조회할 수 없음 (권한 부족? 관리자 권한으로 재실행하세요).'
    } elseif (-not $logInfo.IsEnabled) {
        $script:opEnabled = $false
        Out '[중요] TaskScheduler/Operational 로그가 *비활성* 상태다 — 실제 기동 횟수를 확인할 수 없다.'
        Out '       아래 명령으로 활성화하면(관리자) 다음 사건부터 실기동 이력이 남는다:'
        Out ("         wevtutil sl `"{0}`" /e:true" -f $logName)
        Out '       (이 진단 스크립트는 read-only 라 자동 활성화하지 않는다.)'
    } else {
        $script:opEnabled = $true
        $evts = @(Get-WinEvent -FilterHashtable @{ LogName = $logName; StartTime = $since } -ErrorAction SilentlyContinue)
        # report-server 관련만 (메시지/기록 문자열에 태스크명 포함)
        $mine = @($evts | Where-Object { $_.Message -match 'report-server' -or $_.Message -match 'watchdog' })
        Out ("분석 창 내 TaskScheduler operational 이벤트: 전체 {0}건, report-server 관련 {1}건" -f $evts.Count, $mine.Count)

        if ($mine.Count -gt 0) {
            Out ''
            Out '이벤트 ID 별 집계 (100=작업시작 102=완료 107=스케줄트리거 110=수동 118=부팅 129=프로세스생성 322=이미실행중이라무시):'
            $mine | Group-Object Id | Sort-Object { [int]$_.Name } | ForEach-Object {
                Out ("  ID {0,-4} {1}" -f $_.Name, $_.Count)
            }
            $starts = @($mine | Where-Object { $_.Id -eq 100 })
            $script:opStarts = $starts.Count
            Out ''
            Out ("** ID 100(작업 시작) = 실제 기동 {0}회 — 이 값이 재기동 폭주의 확정 증거 **" -f $starts.Count)
            $ignored = @($mine | Where-Object { $_.Id -eq 322 })
            if ($ignored.Count -gt 0) {
                Out ("   ID 322(이미 실행 중이라 무시) {0}회 — 태스크가 자주 떴으나 mutex/스케줄러가 걸러냄" -f $ignored.Count)
            }
            Out ''
            Out '최근 시간순 타임라인 (최대 40건):'
            $mine | Sort-Object TimeCreated | Select-Object -Last 40 | ForEach-Object {
                $short = ($_.Message -split "`r?`n")[0]
                if ($short.Length -gt 80) { $short = $short.Substring(0, 80) }
                Out ("  {0}  ID{1,-4} {2}" -f $_.TimeCreated.ToString('MM-dd HH:mm:ss'), $_.Id, $short)
            }
        } else {
            Out '분석 창 내 report-server 관련 operational 이벤트 없음 (기동이 없었거나 로그가 최근에 켜짐).'
        }
    }
} catch {
    Out ("[섹션3 오류] {0}" -f $_.Exception.Message)
}

# =============================================================================
# 4) server_*.txt 부검 — 재기동마다 새 파일이 생기므로 파일 수 = 재기동 횟수
# =============================================================================
Section '4) server_*.txt — 콘솔 로그 파일 (재기동 1회 = 새 파일 1개)'
try {
    $srvLogs = @(Get-ChildItem -Path $logDir -Filter 'server_*.txt' -ErrorAction SilentlyContinue | Sort-Object CreationTime)
    $recent = @($srvLogs | Where-Object { $_.CreationTime -ge $since })
    $script:newServerLogs = $recent.Count
    Out ("전체 server_*.txt: {0}개, 분석 창 내 생성: {1}개" -f $srvLogs.Count, $recent.Count)

    Out ''
    Out '분석 창 내 파일 목록 (생성시각·크기, 직전과 5분 미만 간격이면 "<<"):'
    $prev = $null
    foreach ($f in $recent) {
        $gap = ''
        if ($prev) {
            $mins = ($f.CreationTime - $prev).TotalMinutes
            $gap = ("(+{0:N1}분)" -f $mins)
            if ($mins -lt 5) { $gap += ' <<' }
        }
        Out ("  {0}  {1,8:N0}B  {2} {3}" -f $f.CreationTime.ToString('MM-dd HH:mm:ss'), $f.Length, $f.Name, $gap)
        $prev = $f.CreationTime
    }
    if ($recent.Count -eq 0) { Out '  (분석 창 내 생성된 파일 없음)' }

    # 각 파일의 마지막 30줄 — 죽기 직전 출력 (기동 실패 원인이 여기 남는다)
    Out ''
    Out '── 각 파일의 마지막 30줄 (죽기 직전 출력 원문) ──'
    foreach ($f in $recent) {
        Out ''
        Out ("----- {0} (마지막 30줄) -----" -f $f.Name)
        $tail = @(Get-Content $f.FullName -Tail 30 -ErrorAction SilentlyContinue)
        foreach ($t in $tail) { Out ("  | {0}" -f $t) }
    }
} catch {
    Out ("[섹션4 오류] {0}" -f $_.Exception.Message)
}

# =============================================================================
# 5) Windows 이벤트 로그 — python 크래시 / PC 비정상 종료·재부팅
# =============================================================================
Section '5) Windows 이벤트 — python 크래시 / PC 재부팅 (가설 b 판별 보강)'
try {
    Out 'Application 로그 — python.exe 크래시(ID 1000) / WER(1001):'
    $appEvts = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000, 1001; StartTime = $since } -ErrorAction SilentlyContinue |
                 Where-Object { $_.Message -match 'python' })
    if ($appEvts.Count -gt 0) {
        Out ("  발견 {0}건 — 서버 프로세스가 네이티브 크래시로 죽었을 수 있음:" -f $appEvts.Count)
        $appEvts | Sort-Object TimeCreated | Select-Object -Last 15 | ForEach-Object {
            $short = ($_.Message -split "`r?`n")[0]
            if ($short.Length -gt 90) { $short = $short.Substring(0, 90) }
            Out ("    {0}  ID{1}  {2}" -f $_.TimeCreated.ToString('MM-dd HH:mm:ss'), $_.Id, $short)
        }
    } else {
        Out '  python 관련 크래시 이벤트 없음.'
    }

    Out ''
    Out 'System 로그 — 비정상 종료(6008) / 예기치 않은 재시작(41) / 이벤트로그 시작·정지(6005/6006):'
    $sysEvts = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 6008, 41, 6005, 6006; StartTime = $since } -ErrorAction SilentlyContinue)
    if ($sysEvts.Count -gt 0) {
        Out ("  발견 {0}건 (재부팅이 잦으면 report-server-boot 태스크가 기동 폭주 주범):" -f $sysEvts.Count)
        $sysEvts | Sort-Object TimeCreated | Select-Object -Last 20 | ForEach-Object {
            $short = ($_.Message -split "`r?`n")[0]
            if ($short.Length -gt 80) { $short = $short.Substring(0, 80) }
            Out ("    {0}  ID{1,-5} {2}" -f $_.TimeCreated.ToString('MM-dd HH:mm:ss'), $_.Id, $short)
        }
    } else {
        Out '  관련 이벤트 없음 (PC 재부팅 흔적 없음).'
    }
} catch {
    Out ("[섹션5 오류] {0}" -f $_.Exception.Message)
}

# =============================================================================
# 6) 현재 프로세스 상태 — 리스너 / 컴퓨트 워커 / 고아 / 리소스
# =============================================================================
Section '6) 현재 상태 — 서버 프로세스 · 메모리 · 디스크'
try {
    $listenPids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty OwningProcess -Unique)
    if ($listenPids.Count -gt 0) {
        Out ("포트 {0} LISTEN PID: {1}" -f $Port, ($listenPids -join ', '))
    } else {
        Out ("포트 {0} 에 LISTEN 중인 프로세스 없음 (서버가 안 떠 있음)." -f $Port)
    }

    # 서버 .venv python 프로세스 전수 — kill_server_tree.ps1 과 동일한 spawn parent_pid 정규식
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
               Where-Object { $_.ExecutablePath -eq $venvPython })
    Out ("서버 .venv python 프로세스: {0}개" -f $procs.Count)
    foreach ($p in $procs) {
        $rssMB = [math]::Round($p.WorkingSetSize / 1MB, 0)
        $role = 'listener/parent'
        if ($p.CommandLine -match 'spawn_main.*?parent_pid=(\d+)') {
            $ppid = [int]$Matches[1]
            $alive = @($procs | Where-Object { $_.ProcessId -eq $ppid }).Count -gt 0
            if ($alive) { $role = "worker(parent=$ppid)" } else { $role = "ORPHAN(parent=$ppid 없음)" }
        }
        Out ("  PID {0,-7} RSS {1,6}MB  {2}" -f $p.ProcessId, $rssMB, $role)
    }
    $orphans = @($procs | Where-Object {
        $_.CommandLine -match 'spawn_main.*?parent_pid=(\d+)' -and
        @($procs | Where-Object { $_.ProcessId -eq [int]$Matches[1] }).Count -eq 0
    })
    if ($orphans.Count -gt 0) {
        Out ("[주의] 고아 워커 {0}개 — 재기동 시 정리 실패로 누적됐을 수 있음(메모리 잠식 → healthz 오판 악순환)." -f $orphans.Count)
    }

    Out ''
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($os) {
        $freeMB = [math]::Round($os.FreePhysicalMemory / 1KB, 0)
        $totMB  = [math]::Round($os.TotalVisibleMemorySize / 1KB, 0)
        Out ("물리 메모리: 여유 {0:N0}MB / 전체 {1:N0}MB" -f $freeMB, $totMB)
    }
    $drive = (Get-Item $serverDir).PSDrive
    if ($drive) {
        Out ("디스크({0}:): 여유 {1:N1}GB / 전체 {2:N1}GB" -f $drive.Name, ($drive.Free / 1GB), (($drive.Free + $drive.Used) / 1GB))
    }
} catch {
    Out ("[섹션6 오류] {0}" -f $_.Exception.Message)
}

# =============================================================================
# 7) healthz 실측 1회
# =============================================================================
Section '7) /healthz 실측 (응답코드 · 소요시간)'
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 10
        $sw.Stop()
        Out ("응답 {0} — {1}ms" -f $r.StatusCode, $sw.ElapsedMilliseconds)
        Out ("본문: {0}" -f $r.Content)
    } catch {
        $sw.Stop()
        $code = $null
        if ($_.Exception.Response) { try { $code = [int]$_.Exception.Response.StatusCode } catch { } }
        if ($code) {
            Out ("응답 {0}(비정상) — {1}ms  (503 이면 DB 체크 실패)" -f $code, $sw.ElapsedMilliseconds)
        } else {
            Out ("무응답/거부 — {0}ms  ({1})" -f $sw.ElapsedMilliseconds, $_.Exception.Message)
        }
    }
} catch {
    Out ("[섹션7 오류] {0}" -f $_.Exception.Message)
}

# =============================================================================
# 요약 판정
# =============================================================================
Section '[SUMMARY] 종합 판정'
try {
    if ($null -ne $script:evtRestarts) {
        Out ("watchdog_events.log 재기동 이벤트 : {0}건 (최악 5분 창 {1}건)" -f $script:evtRestarts, $script:worst5min)
    } else {
        Out 'watchdog_events.log 재기동 이벤트 : (데이터 없음 — 로그 미기록/미등록)'
    }
    if ($null -eq $script:opStarts) {
        if ($script:opEnabled -eq $false) {
            Out 'TaskScheduler 실제 기동 횟수     : (operational 로그 비활성 — 확인 불가, 위 3번 참조)'
        } else {
            Out 'TaskScheduler 실제 기동 횟수     : (데이터 없음)'
        }
    } else {
        Out ("TaskScheduler 실제 기동(ID100)   : {0}회" -f $script:opStarts)
    }
    Out ("분석 창 내 server_*.txt 생성      : {0}개" -f $script:newServerLogs)
    Out ("watchdog 관련 예약 작업 수        : {0}개 (정상=2)" -f $script:taskCount)
    Out ''

    $hint = @()
    if ($script:taskCount -ne $null -and $script:taskCount -gt 2) {
        $hint += '가설(a) 태스크 중복 등록: watchdog 관련 작업이 2개보다 많다 → 2번 섹션에서 중복 제거.'
    }
    if ($script:opStarts -ne $null -and $script:worst5min -ne $null -and $script:opStarts -ge 5 -and $script:worst5min -ge 5) {
        $hint += '가설(a/b) 실기동 폭주: operational ID100 과 events 재기동 수가 함께 높다 → 태스크가 실제로 자주 떴다.'
    }
    if ($script:opStarts -ne $null -and $script:worst5min -ne $null -and $script:opStarts -le 2 -and $script:worst5min -ge 5) {
        $hint += '가설(c) 집계 착시: 실제 기동은 적은데 events/대시보드 재기동 수만 높다 → events 중복 라인/ts 파싱 확인.'
    }
    if ($script:newServerLogs -ne $null -and $script:newServerLogs -ge 5) {
        $hint += ('server_*.txt 가 {0}개 새로 생김 → 프로세스가 실제로 여러 번 재기동했다는 독립 증거.' -f $script:newServerLogs)
    }
    if ($hint.Count -eq 0) {
        Out '뚜렷한 폭주 신호가 이 창에서는 확인되지 않음. -Hours 를 늘려 사건 시각을 포함해 재실행하거나, 3번 operational 로그가 활성인지 확인할 것.'
    } else {
        foreach ($h in $hint) { Out ("• " + $h) }
    }
} catch {
    Out ("[SUMMARY 오류] {0}" -f $_.Exception.Message)
}

# ── 리포트 파일 저장 (UTF-8 BOM — 한국어 Windows 메모장에서 안 깨지게) ──────
try {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $outPath = Join-Path $logDir ("diagnose_{0}.txt" -f (Get-Date).ToString('yyyyMMdd_HHmmss'))
    $utf8bom = New-Object System.Text.UTF8Encoding($true)
    [System.IO.File]::WriteAllLines($outPath, $script:report, $utf8bom)
    Write-Host ''
    Write-Host ("리포트 저장됨: {0}" -f $outPath)
} catch {
    Write-Host ("리포트 파일 저장 실패: {0}" -f $_.Exception.Message)
}
