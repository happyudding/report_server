# report-server 종료 전 drain — terminate.bat 이 호출한다 (watchdog 도 쓸 수 있다).
#
# 왜 별도 파일인가:
#   terminate.bat 안의 인라인 PowerShell 로는 아래 세 가지를 담을 수 없었다.
#     1) 정체 감지  — 진행 중 요청이 StallSec 동안 **줄지 않으면** 기다려도 소용없다.
#                      종전에는 그런 상황에서도 TimeoutSec(90초)을 꽉 채웠다.
#     2) 무엇이 걸렸나 — "10건" 만으로는 기다릴지 끊을지 판단할 수 없다. 라우트와 경과를 찍는다.
#     3) 증거 보존   — 강제 종료 직전 스레드 덤프를 남긴다. 2026-08-19 업로드 hang 때
#                      서버를 내리는 순간 현행범 스택이 통째로 사라져 원인 규명이 막혔다.
#
# inflight 는 **사이드 진단 리스너(/alive)** 를 먼저 본다 — waitress 스레드가 전부 묶인
# 상황에서도 응답하기 때문이다(그게 정확히 이 스크립트가 필요한 순간이다). 리스너가
# 꺼져 있으면 종전대로 /healthz 로 폴백한다.
#
# exit 0 = 종료해도 되는 상태(정상 drain 또는 판단 후 포기). 이 스크립트는 프로세스를
# 죽이지 않는다 — 실제 종료는 kill_server_tree.ps1 이 한다.
#
# 이 파일은 UTF-8 BOM + CRLF 로 저장할 것 (.gitattributes 강제). BOM 이 없으면
# PowerShell 5.1 이 시스템 ANSI(cp949)로 읽어 한글이 깨지고 파싱이 어긋난다.

param(
    [Parameter(Mandatory = $true)][int]$Port,
    [int]$DiagPort = 0,
    [int]$TimeoutSec = 90,
    [int]$StallSec = 15,
    [switch]$Force,
    [string]$Tag = 'terminate'
)

$ErrorActionPreference = 'SilentlyContinue'

$serverDir = $PSScriptRoot
if (-not $serverDir) { $serverDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
$logDir = Join-Path $serverDir 'log'
if ($DiagPort -le 0) { $DiagPort = $Port + 1 }

function Write-Line([string]$msg) { Write-Host "[$Tag] $msg" }

function Get-Json([string]$url, [int]$sec) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $sec
        return ($r.Content | ConvertFrom-Json)
    } catch {
        return $null
    }
}

# 종료 직전 현행범 스택. 이게 없으면 "서버 로그에 아무것도 없다" 로 끝난다.
function Save-ThreadDump([string]$why) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$DiagPort/threads" -UseBasicParsing -TimeoutSec 10
    } catch {
        Write-Line "스레드 덤프 실패 - 진단 리스너(:$DiagPort) 무응답. 종료는 계속합니다."
        return
    }
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $path = Join-Path $logDir "diagnose_terminate_$stamp.txt"
    $head = "# terminate 직전 스레드 덤프 (사유: $why)`r`n" +
            "# 아래 스택에서 여러 스레드가 함께 멈춰 있는 지점이 '안 끝나는 요청' 의 원인이다.`r`n`r`n"
    Set-Content -Path $path -Value ($head + $r.Content) -Encoding utf8
    Write-Line "스레드 덤프 저장: log\diagnose_terminate_$stamp.txt (관리자 console log 탭에서 열람 가능)"
}

if ($Force) {
    Write-Line 'force - drain 을 건너뜁니다. 증거만 남기고 바로 종료합니다.'
    Save-ThreadDump 'force'
    exit 0
}

Write-Line "진행 중 요청이 끝나기를 기다립니다 (최대 $TimeoutSec 초, $StallSec 초 동안 진행이 없으면 조기 종료) ..."

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$idle = 0
$lastN = -1
$lastChange = Get-Date
$giveUp = $false

while ($true) {
    if ((Get-Date) -ge $deadline) {
        Write-Line '제한시간 초과 - 진행 중 요청을 남긴 채 종료합니다.'
        $giveUp = $true
        break
    }
    $j = Get-Json "http://127.0.0.1:$DiagPort/alive" 5
    if ($null -eq $j) { $j = Get-Json "http://127.0.0.1:$Port/healthz" 5 }
    if ($null -eq $j) {
        Write-Line '서버 무응답 - drain 생략 (이미 응답 불능 상태).'
        $giveUp = $true
        break
    }
    $n = $j.inflight
    if ($null -eq $n) {
        Write-Line 'inflight 미보고 (metrics 비활성) - 5초 고정 대기 후 종료.'
        Start-Sleep -Seconds 5
        break
    }
    $n = [int]$n
    if ($n -le 0) {
        $idle++
        if ($idle -ge 2) {
            Write-Line '진행 중 요청 없음 - 안전하게 종료합니다.'
            break
        }
        Start-Sleep -Seconds 1
        continue
    }
    $idle = 0
    if ($n -ne $lastN) {
        $lastN = $n
        $lastChange = Get-Date
    } elseif (((Get-Date) - $lastChange).TotalSeconds -ge $StallSec) {
        Write-Line "진행 중 요청 $n 건이 $StallSec 초 동안 줄지 않았습니다 - 멈춘 것으로 보고 종료합니다."
        $giveUp = $true
        break
    }
    # 무엇이 걸렸는지 — 사이드 리스너가 주는 진행 중 요청 목록(가장 오래된 것)
    $detail = ''
    if ($j.requests -and $j.requests.Count -gt 0) {
        $top = $j.requests[0]
        $detail = " (최장: $($top.route) $($top.elapsed)초째)"
    }
    Write-Line "  진행 중 요청 $n 건$detail - 완료 대기 중 ..."
    Start-Sleep -Seconds 1
}

if ($giveUp) { Save-ThreadDump 'drain 실패(정체/제한시간/무응답)' }
exit 0
