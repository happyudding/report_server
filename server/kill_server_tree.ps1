# report-server 종료 헬퍼 — terminate.bat 과 watchdog.ps1 이 공유한다.
#
# 왜 별도 파일인가:
#   서버는 waitress 프로세스 1개로 끝나지 않는다. web_report 컴퓨트 워커
#   (ProcessPoolExecutor, 기본 2개 — web_report\compute.py)가 별도 python.exe 로 뜨는데
#   이 워커들은 포트를 LISTEN 하지 않는다. 그래서 "LISTEN PID 만 죽이기"로는 워커가 남는다.
#   Stop-Process -Force 는 TerminateProcess 라 파이썬 atexit 이 돌지 않고, Windows 는
#   Job Object 없이 자식을 자동 정리하지 않으므로 워커는 고아가 된다. 워커당 tables 캐시가
#   최대 4GB 라(server\README.md) 방치하면 메모리를 크게 잠식한다. 특히 watchdog 은 5분
#   주기라, 워커를 안 죽이면 재기동마다 고아가 2개씩 누적된다.
#
# 하는 일:
#   1) 포트를 LISTEN 하는 PID 수집
#   2) 그 PID 의 컴퓨트 워커 수집  ← 부모를 죽이기 전에! 죽은 뒤엔 트리 추적이 불안정하다
#   3) 부모 종료  ->  4) 워커 종료
#   5) 부모가 이미 사라진 고아 워커 회수 (과거 재기동에서 쌓인 것)
#   6) 포트 해제 확인
#
# exit 0 = 포트 해제 확인(또는 애초에 리스너 없음) / 1 = 포트가 아직 LISTEN
#
# 이 파일은 UTF-8 BOM + CRLF 로 저장할 것 (.gitattributes 강제). BOM 이 없으면
# PowerShell 5.1 이 시스템 ANSI(cp949)로 읽어 한글 주석이 깨지고 파싱이 어긋난다.

param(
    [Parameter(Mandatory = $true)][int]$Port,
    [string]$Tag = 'kill-tree'
)

$ErrorActionPreference = 'SilentlyContinue'

$serverDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $serverDir '.venv\Scripts\python.exe'

function Write-Line([string]$msg) { Write-Host "[$Tag] $msg" }

# CommandLine 이 필요하므로 Get-Process 가 아니라 CIM 을 쓴다.
function Get-PythonProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
}

# multiprocessing spawn 자식의 커맨드라인에는
#   spawn_main(parent_pid=<PID>, pipe_handle=<N>)
# 이 들어 있다. 부모 PID 를 여기서 뽑는다 (해당 없으면 0).
function Get-SpawnParentPid($proc) {
    if ($proc.CommandLine -and $proc.CommandLine -match 'spawn_main.*?parent_pid=(\d+)') {
        return [int]$Matches[1]
    }
    return 0
}

# $ParentPid 가 띄운 컴퓨트 워커. ParentProcessId 만 보면 부모 사망 후 PID 재사용으로
# 오탐할 수 있어 spawn 커맨드라인의 parent_pid 도 함께 본다 — 이쪽이 더 정확하다.
function Get-ComputeWorkers([int]$ParentPid, $Snapshot) {
    $Snapshot | Where-Object {
        $_.ProcessId -ne $ParentPid -and
        ($_.ParentProcessId -eq $ParentPid -or (Get-SpawnParentPid $_) -eq $ParentPid)
    }
}

# 부모가 더 이상 살아있는 python 이 아닌 spawn 자식 = 고아.
# 무관한 프로젝트의 python 을 죽이지 않도록 실행 파일이 이 서버의 .venv 인 것만 건드린다.
# (살아있는 워커의 부모는 반드시 python 이므로 오검출로 정상 워커를 죽이지 않는다.)
function Remove-OrphanWorkers($Snapshot) {
    $alivePids = @($Snapshot | Select-Object -ExpandProperty ProcessId)
    $killed = 0
    foreach ($p in $Snapshot) {
        $parentPid = Get-SpawnParentPid $p
        if ($parentPid -le 0) { continue }
        if ($alivePids -contains $parentPid) { continue }
        if ($p.ExecutablePath -ne $venvPython) { continue }
        Write-Line ("  - 고아 워커 회수: PID {0} (부모 {1} 없음)" -f $p.ProcessId, $parentPid)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }
    return $killed
}

# ── 1) 리스너 확인 ───────────────────────────────────────────────────────────
$listenPids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique)

if ($listenPids.Count -eq 0) {
    Write-Line "포트 $Port 에 LISTEN 중인 프로세스 없음."
} else {
    # ── 2) 워커 수집 (부모가 살아있는 지금) ──────────────────────────────────
    $snapshot = @(Get-PythonProcs)
    $workers = @()
    foreach ($procId in $listenPids) {
        $workers += @(Get-ComputeWorkers $procId $snapshot)
    }
    $workers = @($workers | Sort-Object ProcessId -Unique)

    Write-Line ("서버 PID: {0}" -f ($listenPids -join ', '))
    if ($workers.Count -gt 0) {
        Write-Line ("컴퓨트 워커 PID: {0}" -f (($workers | ForEach-Object { $_.ProcessId }) -join ', '))
    } else {
        Write-Line "컴퓨트 워커 없음 (콜드 빌드가 없었거나 이미 회수됨)."
    }

    # ── 3) 부모 -> 4) 워커 순서로 종료 ───────────────────────────────────────
    foreach ($procId in $listenPids) {
        Write-Line "서버 종료: PID $procId"
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    foreach ($w in $workers) {
        Write-Line ("워커 종료: PID {0}" -f $w.ProcessId)
        Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# ── 5) 고아 회수 ─────────────────────────────────────────────────────────────
# 방금 죽인 워커가 여기서 다시 잡혀도 무해하다 (이중 안전망).
$orphans = Remove-OrphanWorkers @(Get-PythonProcs)
if ($orphans -gt 0) { Write-Line "고아 워커 $orphans 개 회수됨." }

# ── 6) 포트 해제 확인 ────────────────────────────────────────────────────────
if ($listenPids.Count -eq 0) { exit 0 }
for ($i = 0; $i -lt 40; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
        Write-Line "종료 완료. 포트 해제 확인."
        exit 0
    }
    Start-Sleep -Milliseconds 250
}
Write-Line "WARNING: 포트가 아직 LISTEN 상태입니다. 남은 프로세스를 확인하세요."
exit 1
