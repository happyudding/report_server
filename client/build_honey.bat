@echo off
setlocal EnableExtensions

if /i not "%~1"=="--inner" (
  set "LOG_DIR=%~dp0build_logs"
  if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>nul

  for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "LOG_TS=%%I"
  if not defined LOG_TS set "LOG_TS=%RANDOM%"
  set "LOG_FILE=%LOG_DIR%\Honey_build_%LOG_TS%.txt"

  powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Transcript -Path '%LOG_FILE%' -Force | Out-Null; & '%~f0' --inner %*; $code=$LASTEXITCODE; Stop-Transcript | Out-Null; exit $code"
  set "BUILD_EXIT=%ERRORLEVEL%"

  if "%BUILD_EXIT%"=="0" (
    del "%LOG_FILE%" >nul 2>nul
  ) else (
    echo.
    echo [ERROR] Build failed. Console log saved to:
    echo   "%LOG_FILE%"
  )
  exit /b %BUILD_EXIT%
)

shift /1
cd /d "%~dp0"

set "PYTHON_CMD=python"
where python >nul 2>nul
if errorlevel 1 (
  set "PYTHON_CMD=py -3"
  where py >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] Python was not found.
    echo Install Python or add it to PATH, then run this file again.
    exit /b 1
  )
)

echo === [1/2] Build Honey with PyInstaller ===
REM 빌드 PC 에 requirements.txt 의존성이 빠져 있으면 PyInstaller 가 조용히 누락한 채
REM 빌드를 성공시켜 런타임에 ModuleNotFoundError 로 죽는 깨진 exe 가 나온다
REM (예: requests_toolbelt). 빌드 직전에 의존성을 보장한다.
echo --- pip install -r requirements.txt
%PYTHON_CMD% -m pip install --progress-bar off --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] pip install -r requirements.txt failed.
  exit /b 1
)

REM 기본은 캐시 재사용(--clean 없음) — 반복 빌드가 크게 빨라진다.
REM 캐시를 버리고 전체 재빌드하려면: build_honey.bat --clean
set "PYI_CLEAN="
if /i "%~1"=="--clean" set "PYI_CLEAN=--clean"
echo --- PyInstaller %PYI_CLEAN% --noconfirm (COLLECT 단계는 수 분간 출력이 멈춘 것처럼 보일 수 있음)
%PYTHON_CMD% -m PyInstaller %PYI_CLEAN% --noconfirm build_honey.spec
if errorlevel 1 (
  echo.
  echo [ERROR] PyInstaller build failed.
  exit /b 1
)

set "CLIENT_DIST=%~dp0dist\Honey"
set "ROOT_DIST=%~dp0..\dist\Honey"

if not exist "%CLIENT_DIST%\Honey.exe" (
  echo.
  echo [ERROR] Build output was not found:
  echo   "%CLIENT_DIST%\Honey.exe"
  exit /b 1
)

echo.
echo === [2/2] Copy build output to repo dist\Honey ===
if not exist "%ROOT_DIST%" mkdir "%ROOT_DIST%"

robocopy "%CLIENT_DIST%" "%ROOT_DIST%" /E /NFL /NDL /NJH /NJS /NP
set "ROBOCOPY_EXIT=%ERRORLEVEL%"
if %ROBOCOPY_EXIT% GEQ 8 (
  echo.
  echo [ERROR] Copy failed. Close Honey.exe if it is running, then try again.
  exit /b %ROBOCOPY_EXIT%
)

echo.
echo === DONE ===
echo Built:
echo   "%CLIENT_DIST%\Honey.exe"
echo Copied to:
echo   "%ROOT_DIST%\Honey.exe"
exit /b 0
