<#
.SYNOPSIS
    런처 구조(versions\ + Honey.exe 런처) 릴리스를 한 번에 만든다.

.DESCRIPTION
    기존 파이프라인(release_honey.ps1 / build_zip.bat / buildandrelease.bat)은 **구 구조**
    (Honey.exe = 앱 본체)를 만든다. 이 스크립트는 그것들을 건드리지 않고, 런처 구조를
    같은 방식으로 "한 방에" 배포할 수 있게 한다.

      1) CURRENT_VERSION 에서 다음 버전을 정한다 (또는 -Version 으로 지정)
      2) server\env\server.env 의 SERVER_BASE_URL 을 읽어 빌드본에 박는다
      3) update_test\build_test_release.ps1 로 런처 구조 빌드
         (런처 + versions\<ver>\앱 + .files.json + zip + version.json + files.json)
      4) 산출물을 server\releases\ 로 복사 (기존 version.json 은 자동 백업)
      5) **여기까지 성공했을 때만** config.py 의 CURRENT_VERSION 을 올린다
      6) release_log.txt 에 한 줄 기록

    5번이 release_honey.ps1 과 다른 점이다. 그쪽은 버전을 **먼저** 올리고 빌드해서,
    빌드가 실패하면 올라간 버전만 남는다(3.1.2 로 배포했는데 config 는 3.1.3 이 되는
    사고가 실제로 있었다). 여기서는 실패하면 아무것도 바뀌지 않는다.

.PARAMETER Version
    x.y.z. 생략하면 CURRENT_VERSION 의 patch 를 +1 한다 (buildandrelease.bat 과 같은 관례).

.PARAMETER Notes
    version.json / release_log.txt 에 남길 코멘트. 생략하면 물어본다.

.PARAMETER NoBump
    버전을 올리지 않고 현재 CURRENT_VERSION 그대로 다시 빌드·배포한다.

.PARAMETER ServerUrl
    빌드본이 접속할 서버 주소를 server\env\server.env 대신 이 값으로 덮는다.
    **테스트 서버로 배포할 때만 사용** (예: http://192.168.0.10:8090 — mypc_start.bat).
    운영 릴리스는 지정하지 말 것 — server.env 가 정본이다.

.PARAMETER Yes
    배포 전 확인 프롬프트를 건너뛴다 (무인 실행용).

.PARAMETER Clean
    PyInstaller --clean (전체 재빌드).
#>
[CmdletBinding()]
param(
    [string]$Version,
    [string]$Notes,
    [switch]$NoBump,
    [switch]$Yes,
    [switch]$Clean,
    [string]$ServerUrl
)

$ErrorActionPreference = "Stop"

trap {
    Write-Host ""
    Write-Host "[FAILED] $($_.Exception.Message)" -ForegroundColor Red
    if ($_.InvocationInfo) {
        Write-Host ("         at line {0}: {1}" -f $_.InvocationInfo.ScriptLineNumber, $_.InvocationInfo.Line.Trim()) -ForegroundColor DarkGray
    }
    Write-Host "         (실패했으므로 CURRENT_VERSION 도 server\releases\ 도 그대로입니다)" -ForegroundColor DarkGray
    exit 1
}

$ClientDir   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot    = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ConfigPy    = Join-Path $ClientDir "transport\config.py"
$BuildScript = Join-Path $ClientDir "update_test\build_test_release.ps1"
$OutDir      = Join-Path $ClientDir "update_test\release"
$ReleasesDir = Join-Path $RepoRoot "server\releases"
$ServerEnv   = Join-Path $RepoRoot "server\env\server.env"
$ReleaseLog  = Join-Path $ReleasesDir "release_log.txt"
$Utf8NoBom   = New-Object System.Text.UTF8Encoding($false)
$Stopwatch   = [System.Diagnostics.Stopwatch]::StartNew()

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ("==> {0}  (+{1}s)" -f $Message, [math]::Round($Stopwatch.Elapsed.TotalSeconds)) -ForegroundColor Cyan
}

function Write-Utf8NoBomText([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, $Utf8NoBom)
}

foreach ($required in @($ConfigPy, $BuildScript, $ServerEnv)) {
    if (-not (Test-Path $required)) { throw "필요한 파일이 없습니다: $required" }
}

# ── 1. 버전 결정 ────────────────────────────────────────────────────────────
$configText = [System.IO.File]::ReadAllText($ConfigPy)
$versionPattern = 'CURRENT_VERSION\s*=\s*"([^"]*)"'
$versionMatch = [regex]::Match($configText, $versionPattern)
if (-not $versionMatch.Success) { throw "CURRENT_VERSION 을 $ConfigPy 에서 찾지 못했습니다" }
$currentVersion = $versionMatch.Groups[1].Value

if ([string]::IsNullOrWhiteSpace($Version)) {
    if ($currentVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "CURRENT_VERSION 이 x.y.z 형식이 아닙니다: $currentVersion"
    }
    if ($NoBump) {
        $Version = $currentVersion
    } else {
        $parts = $currentVersion.Split(".")
        $Version = "{0}.{1}.{2}" -f $parts[0], $parts[1], ([int]$parts[2] + 1)
    }
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version 은 x.y.z 형식이어야 합니다: $Version" }

# ── 2. 서버 주소 (정본은 server\env\server.env 하나) ─────────────────────────
# 여기서 멈추지 않고 넘어가면 빌드본이 엉뚱한 주소(빌드 스크립트 기본값 127.0.0.1)로
# 나가고, 배포된 뒤에야 "아무도 업데이트를 못 받는다"로 드러난다.
#
# -ServerUrl 은 그 정본을 **이 실행에 한해** 덮는다 (테스트 서버 배포용). server.env
# 파일 자체는 건드리지 않으므로, 인자 없이 다시 돌리면 곧바로 운영 주소로 돌아온다.
$BaseUrlOverridden = -not [string]::IsNullOrWhiteSpace($ServerUrl)
if ($BaseUrlOverridden) {
    $BaseUrl = $ServerUrl.Trim()
    if ($BaseUrl -notmatch '^https?://') {
        throw "ServerUrl 은 http:// 또는 https:// 로 시작해야 합니다: $BaseUrl"
    }
} else {
    $BaseUrl = $null
    foreach ($line in (Get-Content -Path $ServerEnv -Encoding UTF8)) {
        $t = $line.Trim()
        if ($t -eq "" -or $t.StartsWith("#")) { continue }
        $m = [regex]::Match($t, '^SERVER_BASE_URL\s*=\s*(\S+)$')
        if ($m.Success) { $BaseUrl = $m.Groups[1].Value }
    }
    if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
        throw "SERVER_BASE_URL 을 $ServerEnv 에서 찾지 못했습니다."
    }
}

Write-Host ""
Write-Host "Honey 런처 구조 릴리스" -ForegroundColor Green
Write-Host "  현재 버전   : $currentVersion"
Write-Host "  만들 버전   : $Version"
Write-Host "  서버 주소   : $BaseUrl   <- 빌드본이 접속할 주소"
Write-Host "  배포 위치   : $ReleasesDir"

if ($BaseUrlOverridden) {
    Write-Host ""
    Write-Host "  [오버라이드] 서버 주소를 -ServerUrl 로 덮었습니다 (server.env 무시)." -ForegroundColor Yellow
    Write-Host "               테스트 배포용입니다. 이 빌드본은 위 주소만 바라봅니다 —" -ForegroundColor Yellow
    Write-Host "               운영 배포라면 지금 중단하고 인자 없이 다시 실행하세요." -ForegroundColor Yellow
}

if ($BaseUrl -match '127\.0\.0\.1|localhost') {
    Write-Host ""
    Write-Host "  [경고] 서버 주소가 로컬입니다. 이대로 배포하면 사용자 PC 가 자기 자신을" -ForegroundColor Yellow
    Write-Host "         서버로 보게 되어 아무도 업데이트를 받지 못합니다." -ForegroundColor Yellow
    Write-Host "         server\env\server.env 를 먼저 확인하세요." -ForegroundColor Yellow
}

if ($null -eq $Notes -or [string]::IsNullOrWhiteSpace($Notes)) {
    $Notes = Read-Host "릴리스 코멘트 (그냥 Enter 면 기본값)"
}
if ([string]::IsNullOrWhiteSpace($Notes)) { $Notes = "Honey $Version release (launcher layout)" }

if (-not $Yes) {
    Write-Host ""
    $answer = Read-Host "위 내용으로 빌드하고 server\releases\ 에 배포합니다. 진행할까요? (y/N)"
    if ($answer -ne "y" -and $answer -ne "Y") {
        Write-Host "취소했습니다. 아무것도 바뀌지 않았습니다." -ForegroundColor DarkGray
        exit 0
    }
}

# ── 3. 빌드 ─────────────────────────────────────────────────────────────────
Write-Step "1/4 런처 구조 빌드 ($Version)"
$buildArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $BuildScript,
               "-Version", $Version, "-ServerUrl", $BaseUrl)
if ($Clean) { $buildArgs += "-Clean" }
$BuildLog = Join-Path $env:TEMP "honey_launcher_build_$Version.log"
& powershell @buildArgs 2>&1 | Tee-Object -FilePath $BuildLog
if ($LASTEXITCODE -ne 0) { throw "빌드 실패 (exit $LASTEXITCODE)" }

# honey_parse 실물이 없는 PC(개발 PC)에서 돌리면 원본 spec 이 요구하는 데이터 파일이
# 빠진 채로도 빌드가 "성공"한다 — 그 빌드본을 배포하면 해당 기능이 조용히 죽는다.
# 배포 스크립트에서는 그걸 실패로 취급한다 (테스트 빌드에서만 허용되는 완화다).
if (Select-String -Path $BuildLog -Pattern "build_honeyapp.spec\]" -Quiet) {
    throw ("빌드에서 데이터 파일이 누락됐습니다 (위 출력의 '없는 데이터 파일을 건너뜁니다' 배너). " +
           "이 PC 에는 honey_parse 실물이 없습니다 — 실물이 있는 빌드 PC 에서 실행하세요. " +
           "로그: $BuildLog")
}

$ZipPath   = Join-Path $OutDir "Honey-$Version.zip"
$FilesJson = Join-Path $OutDir "Honey-$Version.files.json"
$BuiltJson = Join-Path $OutDir "version.json"
foreach ($required in @($ZipPath, $FilesJson, $BuiltJson)) {
    if (-not (Test-Path $required)) { throw "빌드 산출물이 없습니다: $required" }
}

$manifest = Get-Content $BuiltJson -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.version -ne $Version) {
    throw "version.json 의 버전($($manifest.version))이 만들려던 버전($Version)과 다릅니다"
}
if ([string]::IsNullOrWhiteSpace($manifest.sha256)) {
    throw "version.json 에 sha256 이 없습니다 (클라이언트가 무결성 검증을 못 합니다)"
}

# ── 4. 배포 ─────────────────────────────────────────────────────────────────
Write-Step "2/4 server\releases\ 로 복사"
if (-not (Test-Path $ReleasesDir)) { New-Item -ItemType Directory -Path $ReleasesDir | Out-Null }

$LiveJson = Join-Path $ReleasesDir "version.json"
if (Test-Path $LiveJson) {
    # 되돌릴 때 필요하다 — 배포한 버전에 문제가 생기면 이 파일을 되돌려 놓는다.
    $backup = Join-Path $ReleasesDir ("version.json.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    Copy-Item $LiveJson $backup -Force
    Write-Host "    이전 version.json 백업 -> $(Split-Path $backup -Leaf)"
}

Copy-Item $ZipPath   $ReleasesDir -Force
Copy-Item $FilesJson $ReleasesDir -Force
Write-Host ("    Honey-$Version.zip        {0:N1} MB" -f ((Get-Item $ZipPath).Length / 1MB))
Write-Host ("    Honey-$Version.files.json {0:N2} MB  (델타 업데이트용)" -f ((Get-Item $FilesJson).Length / 1MB))

# version.json 은 코멘트만 갈아끼워 쓴다 (sha256/size 는 빌드가 만든 값 그대로).
$manifest.notes = $Notes
Write-Utf8NoBomText $LiveJson ($manifest | ConvertTo-Json -Depth 4)
Write-Host "    version.json -> $Version"

# ── 5. 성공했으므로 CURRENT_VERSION 반영 ─────────────────────────────────────
Write-Step "3/4 CURRENT_VERSION 갱신"
if ($currentVersion -eq $Version) {
    Write-Host "    이미 $Version"
} else {
    $newText = [regex]::Replace($configText, $versionPattern, "CURRENT_VERSION = `"$Version`"")
    Write-Utf8NoBomText $ConfigPy $newText
    Write-Host "    $currentVersion -> $Version   ($ConfigPy)"
}

Write-Step "4/4 release_log.txt"
$logLine = "{0}  {1}  launcher-layout  {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Version, $Notes
Add-Content -Path $ReleaseLog -Value $logLine -Encoding UTF8
Write-Host "    $logLine"

Write-Host ""
Write-Host "완료 — $Version 배포됨 ($([math]::Round($Stopwatch.Elapsed.TotalSeconds))s)" -ForegroundColor Green
Write-Host ""
Write-Host "다음:" -ForegroundColor Green
Write-Host "  * 서버 재시작은 필요 없다 (version.json 은 요청마다 다시 읽는다)."
Write-Host "  * 확인: <서버주소>/honey/version  ->  $Version 이 보이면 배포 완료."
Write-Host "  * 런처 구조로 처음 넘어가는 사용자는 이 zip 을 받아 **새 폴더에** 풀어야 한다."
Write-Host "    (기존 폴더에 덮어쓰면 옛 _internal 이 잔재로 남는다. 그 다음부터는 자동)"
Write-Host "  * 되돌리려면 server\releases\version.json.bak-* 을 version.json 으로 되돌린다."
Write-Host "    단 이미 새 버전을 받은 PC 는 내려가지 않는다(더 높은 버전만 설치)."
