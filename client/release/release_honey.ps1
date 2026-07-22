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
    Release semver in x.y.z format. If omitted, patch is bumped from CURRENT_VERSION
    (or, with -NoBump, CURRENT_VERSION is used as-is).

.PARAMETER Notes
    Release comment for version.json and release_log.txt. If omitted, the script prompts.

.PARAMETER Clean
    Pass --clean to PyInstaller (full rebuild, discards the build cache). Off by default
    so repeated releases reuse the cache and finish much faster.

.PARAMETER NoBump
    Rebuild at the current CURRENT_VERSION without incrementing it, then run the rest of
    the pipeline as usual (zip, copy to server\releases, refresh version.json, log).
    build_zip.bat uses this. Ignored when an explicit -Version is given.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Version = "",

    [Parameter(Mandatory = $false)]
    [AllowNull()]
    [string]$Notes = $null,

    [Parameter(Mandatory = $false)]
    [switch]$NoBump,

    [Parameter(Mandatory = $false)]
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

# 어떤 실패든 원인 한 줄과 로그 경로를 마지막에 또렷이 남기고 exit 1 로 끝낸다.
# 호출한 .bat 이 이 종료코드로 분기해 pause 를 걸어주므로 창이 그냥 닫히지 않는다.
trap {
    Write-Host ""
    Write-Host "[FAILED] $($_.Exception.Message)" -ForegroundColor Red
    if ($_.InvocationInfo) {
        Write-Host ("         at line {0}: {1}" -f $_.InvocationInfo.ScriptLineNumber, $_.InvocationInfo.Line.Trim()) -ForegroundColor DarkGray
    }
    if ($LogPath) {
        Write-Host "         로그 파일: $LogPath" -ForegroundColor DarkGray
    }
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}

# 빌드 전 과정을 로그 파일로 남긴다 — 더블클릭 실행 중 에러로 창이 닫혀도(또는 놓쳐도)
# 나중에 client\release\logs\release_<시각>.log 로 원인을 확인할 수 있다. 콘솔에도 그대로
# 출력되고 대화형 릴리스 코멘트 입력도 정상 동작한다. 실패(throw) 시에도 자동으로 저장된다.
# 주의(PowerShell 5.1 한계): 이 로그는 단계 표시·에러·종료코드는 남기지만 pip/PyInstaller
# 같은 네이티브 exe 의 상세 출력은 남기지 못한다. 그 상세는 열려 있는 콘솔 창에서 확인.
$LogDir = Join-Path $PSScriptRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$LogPath = Join-Path $LogDir ("release_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
try { Start-Transcript -Path $LogPath -Force | Out-Null } catch { }
Write-Host "로그 파일: $LogPath" -ForegroundColor DarkGray

$ClientDir   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoRoot    = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$ConfigPy    = Join-Path $ClientDir "transport\config.py"
$SpecFile    = Join-Path $ClientDir "build_honey.spec"
$DistDir     = Join-Path $ClientDir "dist\Honey"
$DistExe     = Join-Path $DistDir "Honey.exe"
$ReleaseDist = Join-Path $ClientDir "release_dist"
$ReleasesDir = Join-Path $RepoRoot "server\releases"
$VersionJson = Join-Path $ReleasesDir "version.json"
$ReleaseLog  = Join-Path $ReleasesDir "release_log.txt"
$Utf8NoBom   = New-Object System.Text.UTF8Encoding($false)

# 단계별 경과시간 — PyInstaller COLLECT 나 ZIP 압축은 수 분 동안 아무 출력이 없어
# "멈춘 것"처럼 보인다. 각 단계 머리에 누적 경과를 찍어 어디서 오래 걸렸는지 남긴다.
$Stopwatch = [System.Diagnostics.Stopwatch]::StartNew()

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host ("==> {0}  (+{1}s)" -f $Message, [math]::Round($Stopwatch.Elapsed.TotalSeconds)) -ForegroundColor Cyan
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
    if ($NoBump) {
        $Version = $currentVersion
        Write-Host "No version bump: rebuild at $Version" -ForegroundColor Green
    } else {
        $parts = $currentVersion.Split(".")
        $Version = "{0}.{1}.{2}" -f $parts[0], $parts[1], ([int]$parts[2] + 1)
        Write-Host "Auto version bump: $currentVersion -> $Version" -ForegroundColor Green
    }
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

# python / py 중에서 쓸 인터프리터를 고른다.
# 원칙: 이 함수는 '고르기'만 하고 '막지' 않는다. 예전에는 Get-Command 결과를 그대로 썼는데,
# 여기서 실행 가능 여부를 검사해 떨어뜨리기 시작하면 멀쩡히 빌드되던 PC 가 갑자기 막힌다.
# 그래서 검사에 다 실패해도 첫 후보로 진행하고, 진짜 문제면 뒤의 pip 단계가 제대로 실패하게 둔다.
# - WindowsApps 경로는 '제외'가 아니라 '뒤로 미루기'. 그 경로에는 Microsoft Store 를 열기만
#   하는 가짜 스텁도 있지만, Store 로 설치한 진짜 Python 도 같은 경로에 있다. 경로가 아니라
#   실제 실행 결과로 판단해야 한다.
# - 프로브는 stderr 까지 합쳐서 본다. 버전을 stderr 로 찍는 래퍼(pyenv-win 셰임 등)가 있고,
#   $ErrorActionPreference='Stop' 에서는 네이티브 stderr 가 예외로 바뀌어 정상 파이썬이 탈락한다.
function Resolve-Python {
    $ErrorActionPreference = "Continue"   # 함수 지역 — 프로브 중 stderr 로 죽지 않게

    $found = @()
    foreach ($name in @("python", "py")) {
        foreach ($cmd in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
            if ($cmd.Source) { $found += $cmd }
        }
    }
    if ($found.Count -eq 0) { return $null }

    $ordered = @($found | Where-Object { $_.Source -notlike "*\WindowsApps\*" }) +
               @($found | Where-Object { $_.Source -like "*\WindowsApps\*" })

    $probe = @()
    foreach ($cmd in $ordered) {
        $isLauncher = ($cmd.Name -ieq "py.exe" -or $cmd.Name -ieq "py")
        $code = $null
        try {
            if ($isLauncher) { $raw = & $cmd.Source -3 --version 2>&1 } else { $raw = & $cmd.Source --version 2>&1 }
            $code = $LASTEXITCODE
        } catch {
            $raw = $_.Exception.Message
        }
        $text = ((($raw | Out-String) -replace '\s+', ' ')).Trim()
        if ($text.Length -gt 200) { $text = $text.Substring(0, 200) + "..." }
        $probe += ("      {0}{1} => exit={2} {3}" -f $cmd.Source, $(if ($isLauncher) { " -3" } else { "" }), $code, $text)
        if ($code -eq 0) {
            # stderr 로 찍는 래퍼는 PowerShell 이 에러레코드로 감싸므로 버전 문자열만 뽑아 쓴다.
            $m = [regex]::Match($text, 'Python\s+\d+\.\d+(\.\d+)?')
            $version = if ($m.Success) { $m.Value } else { $text }
            return [pscustomobject]@{ Source = $cmd.Source; IsLauncher = $isLauncher; Version = $version; Probe = $probe }
        }
    }

    # 전부 프로브 실패 — 그래도 막지 않고 첫 후보로 진행한다 (예전 동작과 동일).
    $first = $ordered[0]
    return [pscustomobject]@{
        Source     = $first.Source
        IsLauncher = ($first.Name -ieq "py.exe" -or $first.Name -ieq "py")
        Version    = "확인 실패 - 그대로 진행"
        Probe      = $probe
    }
}

$Python = Resolve-Python
if (-not $Python) {
    throw "python / py 를 PATH 에서 찾지 못했습니다. python.org 에서 Python 3 을 설치할 때 'Add python.exe to PATH' 를 켜고, 설치 후 명령 프롬프트를 새로 열어 다시 실행하세요."
}
$PythonExe    = $Python.Source
$IsPyLauncher = $Python.IsLauncher
Write-Host "    python : $PythonExe  ($($Python.Version))"
if ($Python.Version -eq "확인 실패 - 그대로 진행") {
    Write-Host "    [WARN] python --version 확인에 실패했지만 그대로 진행합니다. 프로브 결과:" -ForegroundColor Yellow
    $Python.Probe | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray }
}
Push-Location $ClientDir
try {
    # 빌드 PC 에 requirements.txt 의존성이 빠져 있으면 PyInstaller 가 조용히 누락한 채
    # 빌드를 성공시켜 런타임에 ModuleNotFoundError 로 죽는 깨진 exe 가 배포된다
    # (예: requests_toolbelt). 빌드 직전에 의존성을 보장한다.
    # --progress-bar off: 다운로드 진행바가 콘솔/트랜스크립트에 수만 줄로 쏟아지는 것을 막는다.
    $PipQuiet = @("--progress-bar", "off", "--disable-pip-version-check")

    Write-Host "    pip install -r requirements.txt"
    if ($IsPyLauncher) {
        & $PythonExe -3 -m pip install @PipQuiet -r requirements.txt
    } else {
        & $PythonExe -m pip install @PipQuiet -r requirements.txt
    }
    if ($LASTEXITCODE -ne 0) {
        throw "pip install -r requirements.txt failed with exit code $LASTEXITCODE"
    }

    # PyInstaller 는 빌드 전용 도구라 requirements.txt(런타임 의존성 목록)에 없다. 아무것도
    # 설치 안 된 새 빌드 PC 에서 자동으로 갖춰지도록 여기서 함께 설치한다 (미설치 시 바로
    # 아래 python -m PyInstaller 가 'No module named PyInstaller' 로 죽는다).
    Write-Host "    pip install pyinstaller"
    if ($IsPyLauncher) {
        & $PythonExe -3 -m pip install @PipQuiet pyinstaller
    } else {
        & $PythonExe -m pip install @PipQuiet pyinstaller
    }
    if ($LASTEXITCODE -ne 0) {
        throw "pip install pyinstaller failed with exit code $LASTEXITCODE"
    }

    # 기본은 캐시 재사용(--clean 없음) — 반복 릴리스에서 분석/수집 단계가 크게 짧아진다.
    # 캐시가 의심스러우면 -Clean 스위치로 전체 재빌드.
    $PyiArgs = @("--noconfirm")
    if ($Clean) {
        $PyiArgs = @("--clean") + $PyiArgs
        Write-Host "    (-Clean) 캐시를 버리고 전체 재빌드합니다"
    }
    $SpecName = Split-Path $SpecFile -Leaf
    Write-Host "    PyInstaller $($PyiArgs -join ' ') $SpecName"
    Write-Host "    ※ 마지막 COLLECT 단계는 6000개 파일을 복사하느라 수 분간 출력이 멈춘 것처럼 보일 수 있습니다." -ForegroundColor DarkGray
    if ($IsPyLauncher) {
        & $PythonExe -3 -m PyInstaller @PyiArgs $SpecName
    } else {
        & $PythonExe -m PyInstaller @PyiArgs $SpecName
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

# 서버 주소 정본은 server\env\server.env 의 SERVER_BASE_URL 하나다. 빌드본은 repo 가
# 없으니 그 값을 Honey.exe 옆 honey.env 로 복사해 넣는다 (transport\config.py 가 읽는다).
# 값이 없으면 여기서 멈춘다 — 그냥 넘어가면 하드코딩 폴백 주소로 붙는 배포본이 나가서
# "왜 옛 서버로 가지" 를 나중에 현장에서 디버깅하게 된다.
$ServerEnv = Join-Path $RepoRoot "server\env\server.env"
if (-not (Test-Path $ServerEnv)) {
    throw "서버 설정 파일이 없습니다: $ServerEnv (SERVER_BASE_URL 을 여기서 읽습니다)"
}
$BaseUrl = $null
foreach ($line in (Get-Content -Path $ServerEnv -Encoding UTF8)) {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#")) { continue }
    $m = [regex]::Match($t, '^SERVER_BASE_URL\s*=\s*(\S+)$')
    if ($m.Success) { $BaseUrl = $m.Groups[1].Value }
}
if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    throw "SERVER_BASE_URL 을 $ServerEnv 에서 찾지 못했습니다. 'SERVER_BASE_URL=http://<ip>:<port>' 한 줄을 추가하세요."
}
$HoneyEnv = Join-Path $DistDir "honey.env"
$honeyEnvText = @(
    "# 이 파일은 build_zip 이 server\env\server.env 에서 생성한다 — 직접 고치지 말고",
    "# server\env\server.env 를 고친 뒤 build_zip 을 다시 실행할 것.",
    "SERVER_BASE_URL=$BaseUrl",
    ""
) -join [Environment]::NewLine
Write-Utf8NoBomText $HoneyEnv $honeyEnvText
Write-Host "    server address : $BaseUrl  (-> $HoneyEnv)" -ForegroundColor Green

if (-not (Test-Path $ReleaseDist)) {
    New-Item -ItemType Directory -Path $ReleaseDist | Out-Null
}
if (Test-Path $BuiltZip) {
    Remove-Item $BuiltZip -Force
}

# PowerShell 5.1 의 Compress-Archive 는 700MB/6000파일 규모에서 극단적으로 느려(수십 분,
# 그동안 출력 없음) 빌드가 멈춘 것처럼 보였다. .NET ZipFile 로 dist\Honey 를 직접 압축한다
# — %TEMP% 로 700MB 를 통째 복사하던 스테이징 단계도 함께 사라진다.
# includeBaseDirectory=$true 라 ZIP 내부 루트는 종전과 같은 Honey\ 유지
# (transport\updater.py _find_payload_dir 가 기대하는 구조).
# CreateFromDirectory 를 쓰지 않고 파일별로 엔트리를 만드는 이유: PowerShell 5.1 이 얹힌
# .NET Framework 의 CreateFromDirectory 는 엔트리 경로를 'Honey\_internal\...' 처럼
# 역슬래시로 기록한다(ZIP 규격은 '/' 필수). 탐색기 수동 압축해제가 깨지므로 직접 '/' 로 쓴다.
Write-Host "    압축 중 (수 분 걸릴 수 있고 진행 표시가 없습니다)..." -ForegroundColor DarkGray
Add-Type -AssemblyName System.IO.Compression          # ZipArchive/ZipArchiveMode
Add-Type -AssemblyName System.IO.Compression.FileSystem  # ZipFile/ZipFileExtensions
$zipArchive = [System.IO.Compression.ZipFile]::Open($BuiltZip, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    $rootLen = $DistDir.TrimEnd('\').Length + 1
    foreach ($file in [System.IO.Directory]::EnumerateFiles($DistDir, '*', [System.IO.SearchOption]::AllDirectories)) {
        $entryName = "Honey/" + $file.Substring($rootLen).Replace('\', '/')
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $zipArchive, $file, $entryName, [System.IO.Compression.CompressionLevel]::Fastest) | Out-Null
    }
} finally {
    $zipArchive.Dispose()
}
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
try { Stop-Transcript | Out-Null } catch { }
