<#
.SYNOPSIS
    Honey 클라이언트 새 버전 릴리스 자동화 스크립트.

.DESCRIPTION
    1) client/config.py 의 CURRENT_VERSION 을 -Version 으로 교체 (빌드 전 필수)
    2) PyInstaller 로 build_honey.spec 빌드 → dist/Honey.exe
    3) server/releases/Honey-<version>.exe 로 복사
    4) sha256 계산
    5) server/releases/version.json 갱신 (BOM 없는 UTF-8)

    서버 재시작은 필요 없다 — version.json 은 /honey/version 요청마다 다시 읽힌다.

.PARAMETER Version
    릴리스할 semver (예: 0.2.0). 'a.b.c' 형태 필수.

.PARAMETER Notes
    version.json 의 notes 필드. 변경 사항 요약.

.PARAMETER SkipBuild
    exe 를 이미 빌드해 둔 경우 PyInstaller 단계를 건너뛴다.
    (dist/Honey.exe 가 존재해야 함)

.EXAMPLE
    .\release_honey.ps1 -Version 0.2.0 -Notes "차트 렌더 버그 수정"

.EXAMPLE
    .\release_honey.ps1 -Version 0.2.1 -Notes "hotfix" -SkipBuild
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [Parameter(Mandatory = $false)]
    [string]$Notes = "",

    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

# ── 경로 (스크립트 위치 기준) ────────────────────────────────────────────────
$ClientDir   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot    = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ConfigPy    = Join-Path $ClientDir "config.py"
$SpecFile    = Join-Path $ClientDir "build_honey.spec"
$DistExe     = Join-Path $ClientDir "dist\Honey.exe"
$ReleasesDir = Join-Path $RepoRoot "server\releases"
$VersionJson = Join-Path $ReleasesDir "version.json"
$TargetName  = "Honey-$Version.exe"
$TargetExe   = Join-Path $ReleasesDir $TargetName

function Write-Step([string]$msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }

# ── 0. 버전 형식 검증 ────────────────────────────────────────────────────────
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version 형식이 'a.b.c' 가 아닙니다: '$Version'  (예: 0.2.0)"
}

Write-Host "Honey 릴리스 $Version" -ForegroundColor Green
Write-Host "  client dir : $ClientDir"
Write-Host "  releases   : $ReleasesDir"

# ── 1. config.py 의 CURRENT_VERSION 교체 ────────────────────────────────────
Write-Step "1/5  config.py CURRENT_VERSION 갱신"
if (-not (Test-Path $ConfigPy)) { throw "config.py 를 찾을 수 없습니다: $ConfigPy" }

$configText = Get-Content $ConfigPy -Raw
$pattern    = 'CURRENT_VERSION\s*=\s*"[^"]*"'
$match      = [regex]::Match($configText, $pattern)
if (-not $match.Success) {
    throw "config.py 에서 CURRENT_VERSION 라인을 찾지 못했습니다."
}
$oldLine = $match.Value
$newLine = "CURRENT_VERSION = `"$Version`""
if ($oldLine -eq $newLine) {
    Write-Host "    이미 $Version 입니다 (변경 없음)."
} else {
    $configText = [regex]::Replace($configText, $pattern, $newLine)
    # config.py 는 BOM 없는 UTF-8 로 저장
    [System.IO.File]::WriteAllText($ConfigPy, $configText, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "    $oldLine  ->  $newLine"
}

# ── 2. PyInstaller 빌드 ─────────────────────────────────────────────────────
if ($SkipBuild) {
    Write-Step "2/5  빌드 건너뜀 (-SkipBuild)"
    if (-not (Test-Path $DistExe)) {
        throw "-SkipBuild 인데 dist\Honey.exe 가 없습니다: $DistExe"
    }
} else {
    Write-Step "2/5  PyInstaller 빌드"
    if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
        throw "pyinstaller 를 찾을 수 없습니다. 'pip install pyinstaller' 후 다시 실행하세요."
    }
    Push-Location $ClientDir
    try {
        & pyinstaller (Split-Path $SpecFile -Leaf) --noconfirm
        if ($LASTEXITCODE -ne 0) { throw "pyinstaller 가 종료코드 $LASTEXITCODE 로 실패했습니다." }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path $DistExe)) {
        throw "빌드 후 dist\Honey.exe 가 생성되지 않았습니다: $DistExe"
    }
}

# ── 3. releases 로 버전명 복사 ──────────────────────────────────────────────
Write-Step "3/5  $TargetName 로 복사"
if (-not (Test-Path $ReleasesDir)) {
    New-Item -ItemType Directory -Path $ReleasesDir | Out-Null
}
Copy-Item $DistExe $TargetExe -Force
Write-Host "    -> $TargetExe"

# ── 4. sha256 계산 ──────────────────────────────────────────────────────────
Write-Step "4/5  sha256 계산"
$sha = (Get-FileHash $TargetExe -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $TargetExe).Length
Write-Host "    sha256 : $sha"
Write-Host "    size   : $([math]::Round($size / 1MB, 2)) MB"

# ── 5. version.json 갱신 ────────────────────────────────────────────────────
Write-Step "5/5  version.json 갱신"
$manifest = [ordered]@{
    version     = $Version
    file        = $TargetName
    sha256      = $sha
    released_at = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")
    notes       = $Notes
}
$json = ($manifest | ConvertTo-Json -Depth 4)
# version.json 은 BOM 없는 UTF-8 (Python json.loads 가 BOM 에 걸리지 않도록)
[System.IO.File]::WriteAllText($VersionJson, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "    -> $VersionJson"
Write-Host $json

Write-Host "`n[완료] Honey $Version 릴리스 준비됨." -ForegroundColor Green
Write-Host "서버 재시작 불필요. 다음 클라이언트 기동 시 자동 업데이트 안내가 뜹니다." -ForegroundColor Green
