<#
.SYNOPSIS
    Build and publish a Honey installer release.

.DESCRIPTION
    1) Update client/transport/config.py CURRENT_VERSION.
    2) Update client/installer.iss MyAppVersion.
    3) Build client/dist/Honey/ with PyInstaller.
    4) Build client/installer_dist/HoneySetup-<version>.exe with Inno Setup.
    5) Copy the installer to server/releases/HoneySetup-<version>.exe.
    6) Update server/releases/version.json as UTF-8 without BOM.
    7) Append server/releases/release_log.txt after all release steps succeed.

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

$ClientDir     = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot      = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ConfigPy      = Join-Path $ClientDir "transport\config.py"
$SpecFile      = Join-Path $ClientDir "build_honey.spec"
$InstallerIss  = Join-Path $ClientDir "installer.iss"
$DistExe       = Join-Path $ClientDir "dist\Honey\Honey.exe"
$InstallerDist = Join-Path $ClientDir "installer_dist"
$ReleasesDir   = Join-Path $RepoRoot "server\releases"
$VersionJson   = Join-Path $ReleasesDir "version.json"
$ReleaseLog    = Join-Path $ReleasesDir "release_log.txt"
$Utf8NoBom     = New-Object System.Text.UTF8Encoding($false)

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

function Find-Iscc {
    $candidates = @()
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    }
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    }

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return $null
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

$TargetName = "HoneySetup-$Version.exe"
$TargetExe  = Join-Path $ReleasesDir $TargetName
$BuiltSetup = Join-Path $InstallerDist $TargetName

Write-Host "Honey release $Version" -ForegroundColor Green
Write-Host "  client dir : $ClientDir"
Write-Host "  releases   : $ReleasesDir"
Write-Host "  comment    : $Notes"

Write-Step "1/7 Update CURRENT_VERSION"
$newVersionLine = "CURRENT_VERSION = `"$Version`""
$oldVersionLine = $versionMatch.Value
if ($oldVersionLine -eq $newVersionLine) {
    Write-Host "    already $Version"
} else {
    $configText = [regex]::Replace($configText, $versionPattern, $newVersionLine)
    Write-Utf8NoBomText $ConfigPy $configText
    Write-Host "    $oldVersionLine -> $newVersionLine"
}

Write-Step "2/7 Update Inno Setup version"
if (-not (Test-Path $InstallerIss)) {
    throw "Missing installer script: $InstallerIss"
}
$issText = Read-Utf8Text $InstallerIss
$issPattern = '#define\s+MyAppVersion\s+"[^"]*"'
if (-not [regex]::IsMatch($issText, $issPattern)) {
    throw "MyAppVersion was not found in $InstallerIss"
}
$issText = [regex]::Replace($issText, $issPattern, "#define MyAppVersion `"$Version`"")
Write-Utf8NoBomText $InstallerIss $issText
Write-Host "    MyAppVersion -> $Version"

Write-Step "3/7 Build PyInstaller onedir"
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

Write-Step "4/7 Build Inno Setup installer"
$ISCC = Find-Iscc
if (-not $ISCC) {
    throw "Inno Setup (ISCC.exe) was not found. Install: winget install -e --id JRSoftware.InnoSetup"
}

Push-Location $ClientDir
try {
    & $ISCC (Split-Path $InstallerIss -Leaf)
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
if (-not (Test-Path $BuiltSetup)) {
    throw "Installer output was not found: $BuiltSetup"
}
Write-Host "    -> $BuiltSetup"

Write-Step "5/7 Copy installer to server releases"
if (-not (Test-Path $ReleasesDir)) {
    New-Item -ItemType Directory -Path $ReleasesDir | Out-Null
}
Copy-Item $BuiltSetup $TargetExe -Force
Write-Host "    -> $TargetExe"

Write-Step "6/7 Update version.json"
$sha = (Get-FileHash $TargetExe -Algorithm SHA256).Hash.ToLower()
$size = (Get-Item $TargetExe).Length
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

Write-Step "7/7 Append release log"
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
Write-Host "[DONE] Honey $Version release completed." -ForegroundColor Green
Write-Host "Server restart is not required. Clients will see the update on next launch." -ForegroundColor Green
