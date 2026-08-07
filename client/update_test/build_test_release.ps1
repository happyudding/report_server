<#
.SYNOPSIS
    버전 폴더 + 런처 방식 자동 업데이트 **테스트용** 릴리스를 만든다.

.DESCRIPTION
    운영 릴리스 파이프라인(release_honey.ps1 / build_zip.bat / buildandrelease.bat)과
    완전히 분리돼 있다. server\releases 를 건드리지 않고, 결과물은 전부
    client\update_test\release\ 에만 만든다.

    1) transport\config.py 의 CURRENT_VERSION 을 -Version 으로 임시 변경
       (빌드가 끝나거나 실패하면 반드시 원래 값으로 되돌린다)
    2) 런처 빌드      : build_launcher.spec  -> client\dist_launcher\Honey.exe
    3) 앱 빌드        : build_honeyapp.spec  -> client\dist\HoneyApp\HoneyApp.exe
    4) honey.env 생성 : 테스트 서버 주소를 앱 폴더 안에 넣는다
    5) zip 패키징     : Honey/Honey.exe + Honey/current.txt + Honey/versions/<ver>/**
    6) version.json   : 테스트 서버가 /honey/version 으로 돌려줄 manifest

.PARAMETER Version
    x.y.z 형식. 테스트는 운영 버전(3.x)과 헷갈리지 않게 9.0.0 / 9.0.1 을 권장.

.PARAMETER ServerUrl
    빌드본이 바라볼 서버 주소 (honey.env 에 기록). 기본값은 테스트 서버.

.PARAMETER Python
    사용할 파이썬 실행 파일. 기본 "python".

.PARAMETER Clean
    PyInstaller --clean (전체 재빌드). 기본은 캐시 재사용.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [string]$ServerUrl = "http://127.0.0.1:8090",

    [string]$Python = "python",

    [switch]$Clean
)

$ErrorActionPreference = "Stop"

trap {
    Write-Host ""
    Write-Host "[FAILED] $($_.Exception.Message)" -ForegroundColor Red
    if ($_.InvocationInfo) {
        Write-Host ("         at line {0}: {1}" -f $_.InvocationInfo.ScriptLineNumber, $_.InvocationInfo.Line.Trim()) -ForegroundColor DarkGray
    }
    exit 1
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version 은 x.y.z 형식이어야 합니다. 입력값: $Version"
}

$ClientDir    = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConfigPy     = Join-Path $ClientDir "transport\config.py"
$AppDist      = Join-Path $ClientDir "dist\HoneyApp"
$AppExe       = Join-Path $AppDist "HoneyApp.exe"
$LauncherDist = Join-Path $ClientDir "dist_launcher"
$LauncherExe  = Join-Path $LauncherDist "Honey.exe"
$OutDir       = Join-Path $PSScriptRoot "release"
$ZipPath      = Join-Path $OutDir "Honey-$Version.zip"
$VersionJson  = Join-Path $OutDir "version.json"
$Utf8NoBom    = New-Object System.Text.UTF8Encoding($false)
$Stopwatch    = [System.Diagnostics.Stopwatch]::StartNew()

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ("==> {0}  (+{1}s)" -f $Message, [math]::Round($Stopwatch.Elapsed.TotalSeconds)) -ForegroundColor Cyan
}

function Write-Utf8NoBomText([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, $Utf8NoBom)
}

Write-Host "Honey 테스트 릴리스 $Version" -ForegroundColor Green
Write-Host "  client dir : $ClientDir"
Write-Host "  server url : $ServerUrl"
Write-Host "  output     : $OutDir"

$originalConfig = [System.IO.File]::ReadAllText($ConfigPy)
$versionPattern = 'CURRENT_VERSION\s*=\s*"([^"]*)"'
if (-not [regex]::IsMatch($originalConfig, $versionPattern)) {
    throw "CURRENT_VERSION 을 $ConfigPy 에서 찾지 못했습니다"
}

try {
    Write-Step "1/5 CURRENT_VERSION 임시 변경 -> $Version"
    $patched = [regex]::Replace($originalConfig, $versionPattern, "CURRENT_VERSION = `"$Version`"")
    Write-Utf8NoBomText $ConfigPy $patched

    $PyiArgs = @("--noconfirm")
    if ($Clean) { $PyiArgs = @("--clean") + $PyiArgs }

    Push-Location $ClientDir
    try {
        Write-Step "2/5 런처 빌드 (build_launcher.spec)"
        # workpath/distpath 를 따로 주는 이유: 앱 spec 의 산출 이름도 'Honey' 라
        # 기본 경로(build\Honey, dist\Honey)를 공유하면 서로의 캐시를 덮어쓴다.
        & $Python -m PyInstaller @PyiArgs --workpath build_launcher --distpath dist_launcher build_launcher.spec
        if ($LASTEXITCODE -ne 0) { throw "런처 빌드 실패 (exit $LASTEXITCODE)" }

        Write-Step "3/5 앱 빌드 (build_honeyapp.spec)"
        Write-Host "    ※ COLLECT 단계는 수천 개 파일을 복사하느라 수 분간 멈춘 것처럼 보입니다." -ForegroundColor DarkGray
        & $Python -m PyInstaller @PyiArgs build_honeyapp.spec
        if ($LASTEXITCODE -ne 0) { throw "앱 빌드 실패 (exit $LASTEXITCODE)" }
    } finally {
        Pop-Location
    }

    if (-not (Test-Path $LauncherExe)) { throw "런처 산출물이 없습니다: $LauncherExe" }
    if (-not (Test-Path $AppExe))      { throw "앱 산출물이 없습니다: $AppExe" }

    Write-Step "4/5 honey.env + zip 패키징"
    $honeyEnvText = @(
        "# build_test_release.ps1 이 만든 테스트용 설정 (운영 배포본과 무관)",
        "SERVER_BASE_URL=$ServerUrl",
        ""
    ) -join [Environment]::NewLine
    Write-Utf8NoBomText (Join-Path $AppDist "honey.env") $honeyEnvText

    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

    $currentTxt = Join-Path $OutDir "current.txt.tmp"
    Write-Utf8NoBomText $currentTxt ("$Version" + [Environment]::NewLine)

    # 엔트리 경로를 직접 '/' 로 쓴다 — .NET Framework 의 CreateFromDirectory 는
    # 역슬래시로 기록해 탐색기 수동 압축 해제가 깨진다 (release_honey.ps1 과 같은 이유).
    Write-Host "    압축 중 (수 분 걸릴 수 있고 진행 표시가 없습니다)..." -ForegroundColor DarkGray
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        $fastest = [System.IO.Compression.CompressionLevel]::Fastest
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $LauncherExe, "Honey/Honey.exe", $fastest) | Out-Null
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $currentTxt, "Honey/current.txt", $fastest) | Out-Null
        $rootLen = $AppDist.TrimEnd('\').Length + 1
        foreach ($file in [System.IO.Directory]::EnumerateFiles($AppDist, '*', [System.IO.SearchOption]::AllDirectories)) {
            $entryName = "Honey/versions/$Version/" + $file.Substring($rootLen).Replace('\', '/')
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file, $entryName, $fastest) | Out-Null
        }
    } finally {
        $zip.Dispose()
        Remove-Item $currentTxt -Force -ErrorAction SilentlyContinue
    }
    Write-Host "    -> $ZipPath"

    Write-Step "5/5 version.json"
    $sha  = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLower()
    $size = (Get-Item $ZipPath).Length
    $manifest = [ordered]@{
        version     = $Version
        file        = "Honey-$Version.zip"
        sha256      = $sha
        size        = $size
        released_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")
        notes       = "Honey $Version test release (versioned layout)"
    }
    Write-Utf8NoBomText $VersionJson ($manifest | ConvertTo-Json -Depth 4)
    Write-Host "    sha256 : $sha"
    Write-Host "    size   : $([math]::Round($size / 1MB, 2)) MB"
    Write-Host "    -> $VersionJson"
} finally {
    # 실패하든 성공하든 repo 의 CURRENT_VERSION 은 원래대로 돌려놓는다.
    [System.IO.File]::WriteAllText($ConfigPy, $originalConfig, $Utf8NoBom)
    Write-Host ""
    Write-Host "CURRENT_VERSION 원복 완료 ($ConfigPy)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "완료 — 다음 단계는 client\update_test\README.md 참조" -ForegroundColor Green
