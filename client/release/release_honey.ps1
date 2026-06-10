<#
.SYNOPSIS
    Build and publish a Honey ZIP release.

.DESCRIPTION
    1) Update client/transport/config.py CURRENT_VERSION.
    2) Build client/dist/Honey/ with PyInstaller.
    3) Create client/release_dist/Honey-<version>.zip.
    4) Copy the ZIP to server/releases/Honey-<version>.zip.
    5) Update server/releases/version.json as UTF-8 without BOM.
    6) Append server/releases/release_log.txt after all release steps succeed.

.PARAMETER Version
    Release semver in x.y.z format. If omitted, patch is bumped from CURRENT_VERSION.

.PARAMETER Notes
    Release comment for version.json and release_log.txt. If omitted, the script prompts.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Version = "",

    [Parameter(Mandatory = $false)]
    [AllowNull()]
    [string]$Notes = $null
)

$ErrorActionPreference = "Stop"

$ClientDir   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot    = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ConfigPy    = Join-Path $ClientDir "transport\config.py"
$SpecFile    = Join-Path $ClientDir "build_honey.spec"
$DistDir     = Join-Path $ClientDir "dist\Honey"
$DistExe     = Join-Path $DistDir "Honey.exe"
$ReleaseDist = Join-Path $ClientDir "release_dist"
$D1Storage   = Join-Path $RepoRoot "d1_storage"
$ReleasesDir = Join-Path $RepoRoot "server\releases"
$VersionJson = Join-Path $ReleasesDir "version.json"
$ReleaseLog  = Join-Path $ReleasesDir "release_log.txt"
$Utf8NoBom   = New-Object System.Text.UTF8Encoding($false)

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Read-Utf8Text([string]$Path) {
    return Get-Content -Path $Path -Raw -Encoding UTF8
}

function Write-Utf8NoBomText([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, $Utf8NoBom)
}

if (-not (Test-Path $ConfigPy)) {
    throw "Missing config file: $ConfigPy"
}

$configText = Read-Utf8Text $ConfigPy
$versionPattern = 'CURRENT_VERSION\s*=\s*"([^"]*)"'
$versionMatch = [regex]::Match($configText, $versionPattern)
if (-not $versionMatch.Success) {
    throw "CURRENT_VERSION was not found in $ConfigPy"
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $currentVersion = $versionMatch.Groups[1].Value
    if ($currentVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw "CURRENT_VERSION must be x.y.z, got: $currentVersion"
    }
    $parts = $currentVersion.Split(".")
    $Version = "{0}.{1}.{2}" -f $parts[0], $parts[1], ([int]$parts[2] + 1)
    Write-Host "Auto version bump: $currentVersion -> $Version" -ForegroundColor Green
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must be x.y.z, got: $Version"
}

if ($null -eq $Notes) {
    $Notes = Read-Host "Release comment"
}
if ([string]::IsNullOrWhiteSpace($Notes)) {
    $Notes = "Honey $Version release"
}

$TargetName = "Honey-$Version.zip"
$BuiltZip   = Join-Path $ReleaseDist $TargetName
$TargetZip  = Join-Path $ReleasesDir $TargetName

Write-Host "Honey ZIP release $Version" -ForegroundColor Green
Write-Host "  client dir : $ClientDir"
Write-Host "  releases   : $ReleasesDir"
Write-Host "  comment    : $Notes"

Write-Step "1/6 Update CURRENT_VERSION"
$newVersionLine = "CURRENT_VERSION = `"$Version`""
$oldVersionLine = $versionMatch.Value
if ($oldVersionLine -eq $newVersionLine) {
    Write-Host "    already $Version"
} else {
    $configText = [regex]::Replace($configText, $versionPattern, $newVersionLine)
    Write-Utf8NoBomText $ConfigPy $configText
    Write-Host "    $oldVersionLine -> $newVersionLine"
}

Write-Step "2/6 Build PyInstaller onedir"
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCmd) {
    $PythonCmd = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $PythonCmd) {
    throw "python/py was not found. Install Python and add it to PATH."
}

Push-Location $ClientDir
try {
    if ($PythonCmd.Name -ieq "py.exe" -or $PythonCmd.Name -ieq "py") {
        & $PythonCmd.Source -3 -m PyInstaller --clean --noconfirm (Split-Path $SpecFile -Leaf)
    } else {
        & $PythonCmd.Source -m PyInstaller --clean --noconfirm (Split-Path $SpecFile -Leaf)
    }
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
if (-not (Test-Path $DistExe)) {
    throw "Build output was not found: $DistExe"
}

Write-Step "3/6 Create ZIP package"
if (-not (Test-Path $ReleaseDist)) {
    New-Item -ItemType Directory -Path $ReleaseDist | Out-Null
}
if (Test-Path $BuiltZip) {
    Remove-Item $BuiltZip -Force
}

$stageRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("honey_zip_stage_" + [guid]::NewGuid().ToString("N"))
$stageHoney = Join-Path $stageRoot "Honey"
New-Item -ItemType Directory -Path $stageHoney | Out-Null
Copy-Item (Join-Path $DistDir "*") $stageHoney -Recurse -Force
if (Test-Path $D1Storage) {
    Copy-Item $D1Storage (Join-Path $stageHoney "d1_storage") -Recurse -Force
}
Compress-Archive -Path (Join-Path $stageRoot "Honey") -DestinationPath $BuiltZip -CompressionLevel Optimal
Remove-Item $stageRoot -Recurse -Force
Write-Host "    -> $BuiltZip"

Write-Step "4/6 Copy ZIP to server releases"
if (-not (Test-Path $ReleasesDir)) {
    New-Item -ItemType Directory -Path $ReleasesDir | Out-Null
}
Copy-Item $BuiltZip $TargetZip -Force
Write-Host "    -> $TargetZip"

Write-Step "5/6 Update version.json"
$sha = (Get-FileHash $TargetZip -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $TargetZip).Length
$releasedAt = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss")
Write-Host "    sha256 : $sha"
Write-Host "    size   : $([math]::Round($size / 1MB, 2)) MB"

$manifest = [ordered]@{
    version     = $Version
    file        = $TargetName
    sha256      = $sha
    released_at = $releasedAt
    notes       = $Notes
}
$json = $manifest | ConvertTo-Json -Depth 4
Write-Utf8NoBomText $VersionJson $json
Write-Host "    -> $VersionJson"

Write-Step "6/6 Append release log"
$logBlock = @(
    "[$releasedAt] Honey $Version",
    "  file    : $TargetName",
    "  sha256  : $sha",
    "  size    : $size bytes",
    "  comment : $Notes",
    ""
) -join [Environment]::NewLine
$logBlock += [Environment]::NewLine
[System.IO.File]::AppendAllText($ReleaseLog, $logBlock, $Utf8NoBom)
Write-Host "    -> $ReleaseLog"

Write-Host ""
Write-Host "[DONE] Honey $Version ZIP release completed." -ForegroundColor Green
Write-Host "Server restart is not required. Clients will see the update on next launch." -ForegroundColor Green
