# env\server.env 의 HOST 를 0.0.0.0 으로 되돌린다 (bind 주소 사고 복구용).
#
# 배경(2026-07-29 규명): HOST 가 운영 IP 하나로 고정되면 서버가 그 IP 에만 bind 되어
# 127.0.0.1 로는 접속이 거부된다. 사용자는 멀쩡히 쓰는데 watchdog 점검만 100% 실패해
# 재기동이 종일 반복된다(관측 24h 49~66회). 재기동은 bind 주소를 바꾸지 못하므로
# 스스로 낫지 않는다. 0.0.0.0 은 운영 IP 를 포함하므로 사용자 접속은 그대로다.
#
# 안전장치: 수정 전 원본을 env\server.env.bak_<시각> 로 백업하고, 적용 여부를 묻는다.
# server.env 는 반드시 BOM 없는 UTF-8 이어야 하므로(있으면 start.bat 이 첫 키를 깨뜨림)
# 원문을 그대로 두고 HOST 줄 하나만 치환한 뒤 같은 인코딩으로 다시 쓴다.
#
# 실행: fix_bind_host.bat 더블클릭

$ErrorActionPreference = 'Stop'
$serverDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$envFile = Join-Path $serverDir 'env\server.env'

function Read-Choice([string]$prompt) {
    Write-Host ''
    Write-Host $prompt -ForegroundColor Yellow
    $a = Read-Host '  (Y = 예 / 그 외 = 중단)'
    return ($a -eq 'Y' -or $a -eq 'y')
}

if (-not (Test-Path $envFile)) {
    Write-Host "설정 파일이 없습니다: $envFile" -ForegroundColor Red
    exit 1
}

# BOM 유무와 무관하게 읽되, 쓸 때는 BOM 없이 쓴다.
$text = [System.IO.File]::ReadAllText($envFile, [System.Text.Encoding]::UTF8)

# 주석(#로 시작)이 아닌 실제 HOST 설정 줄만 찾는다.
$m = [regex]::Match($text, '(?m)^([ \t]*HOST[ \t]*=[ \t]*)(\S+)')
if (-not $m.Success) {
    Write-Host 'server.env 에 HOST= 줄이 없습니다. 파일을 직접 확인하세요.' -ForegroundColor Red
    exit 1
}
$current = $m.Groups[2].Value

Write-Host '================================================================'
Write-Host ' report-server bind 주소 복구'
Write-Host '================================================================'
Write-Host ("  설정 파일 : {0}" -f $envFile)
Write-Host ("  현재 HOST : {0}" -f $current)

if ($current -eq '0.0.0.0') {
    Write-Host '  → 이미 0.0.0.0 입니다. 파일은 고칠 것이 없습니다.' -ForegroundColor Green
    Write-Host '    (그런데도 특정 IP 에만 LISTEN 중이라면, 지금 도는 서버가 옛 설정으로'
    Write-Host '     뜬 것입니다. 아래에서 재기동하면 해결됩니다.)'
} else {
    Write-Host ("  → HOST 를 '{0}' 에서 '0.0.0.0' 으로 바꿉니다." -f $current)
    Write-Host '    0.0.0.0 은 이 PC 의 모든 주소로 여는 것이라 운영 IP 도 그대로 포함됩니다.'
    Write-Host '    사용자 접속에는 변화가 없고, watchdog 의 127.0.0.1 점검만 살아납니다.'

    if (-not (Read-Choice '설정 파일을 수정할까요?')) {
        Write-Host '중단했습니다. 아무것도 바꾸지 않았습니다.'
        exit 0
    }

    $backup = Join-Path $serverDir ('env\server.env.bak_{0}' -f (Get-Date).ToString('yyyyMMdd_HHmmss'))
    Copy-Item $envFile $backup -Force
    Write-Host ("  백업 : {0}" -f $backup)

    # HOST 줄 하나만 치환 — 나머지 줄·줄바꿈·주석은 원문 그대로 유지된다.
    $new = $text.Substring(0, $m.Groups[2].Index) + '0.0.0.0' +
           $text.Substring($m.Groups[2].Index + $m.Groups[2].Length)
    [System.IO.File]::WriteAllText($envFile, $new, (New-Object System.Text.UTF8Encoding($false)))

    # 되읽어 확인 (start.bat 이 실제로 읽는 값과 같은지)
    $verify = [regex]::Match(
        [System.IO.File]::ReadAllText($envFile, [System.Text.Encoding]::UTF8),
        '(?m)^[ \t]*HOST[ \t]*=[ \t]*(\S+)')
    if ($verify.Success -and $verify.Groups[1].Value -eq '0.0.0.0') {
        Write-Host '  수정 완료: HOST=0.0.0.0' -ForegroundColor Green
    } else {
        Write-Host '  수정 확인 실패 — 백업 파일로 되돌리세요.' -ForegroundColor Red
        exit 1
    }
}

Write-Host ''
Write-Host '설정을 반영하려면 서버를 재기동해야 합니다.'
Write-Host '(start.bat 이 watchdog 정지 → 기존 서버 정리 → 재기동 → watchdog 재개까지 합니다)'

if (Read-Choice '지금 서버를 재기동할까요?') {
    & (Join-Path $serverDir 'start.bat')
} else {
    Write-Host ''
    Write-Host '나중에 server\start.bat 을 더블클릭하면 반영됩니다.'
}

Write-Host ''
Write-Host '재기동 5분 뒤 diagnose_port.bat 을 돌려 아래 두 가지를 확인하세요:'
Write-Host '  [4] 판정 → "바인딩 주소 OK"'
Write-Host "  [6]      → 값='0'"
exit 0
