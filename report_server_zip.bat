@echo off
rem 콘솔을 UTF-8 로 맞춘다. 이 파일은 UTF-8(BOM 없음)이라 이 줄이 없으면
rem 한국어 Windows 기본 코드페이지(949)에서 한글이 깨져 보인다. BOM 을 붙이면
rem 대신 cmd 가 첫 줄(@echo off)을 못 읽어 에러를 내므로, BOM 없이 이 방식을 쓴다.
chcp 65001 >nul
setlocal enabledelayedexpansion
rem ============================================================================
rem report_server 폴더 전체를 ZIP 으로 묶어 상위 폴더에 만든다 (서버 PC 이전용).
rem
rem   출력 : ..\report_server_<YYYYMMDD_HHMM>.zip
rem   도구 : 7-Zip 있으면 7z(멀티스레드), 없으면 Windows 내장 tar 로 폴백
rem
rem 제외 대상 — 새 서버에서 재생성되거나 서버 구동과 무관한 것들:
rem   .git/                  git 이력 (중첩 저장소 web_report\.git 포함 - 모든 위치)
rem   server/.venv/          start.bat 이 새 PC 에서 자동 재생성 (pip 접근 필요)
rem   client/data/           로컬 테스트 CSV/xlsx (3GB+)
rem   dist/ build/           PyInstaller 빌드 산출물
rem   client/release_dist/   Honey ZIP 빌드 산출물
rem   server/releases/*.zip  Honey 배포 패키지 (version.json 은 포함)
rem   log/ __pycache__/      런타임 로그·바이트코드
rem   nsw_mirror_tmp/        병합 폴더의 임시 산출물
rem
rem 포함 대상 (운영 데이터 이전):
rem   DB/        세션 DB(report.db)
rem   uploads/   parquet·이미지 등 산출물
rem   server/env/server.env  기동 설정 → 새 PC 에서 HOST 값 확인할 것
rem ============================================================================

set "PROJ=%~dp0"
set "PROJ=%PROJ:~0,-1%"
for %%I in ("%PROJ%") do set "NAME=%%~nxI"
for %%I in ("%PROJ%") do set "PARENT=%%~dpI"

rem 로케일 무관 타임스탬프
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"`) do set "STAMP=%%T"
set "OUT=%PARENT%%NAME%_%STAMP%.zip"

echo [zip] Source : %PROJ%
echo [zip] Output : %OUT%
echo.

if exist "%OUT%" (
    echo [zip] ERROR: 같은 이름의 파일이 이미 있습니다. 지우고 다시 실행하세요.
    pause
    exit /b 1
)

rem 압축 대상 경로는 아카이브 내부 경로 기준 (부모 폴더에서 실행)
pushd "%PARENT%"

set "SEVENZIP="
if exist "%ProgramFiles%\7-Zip\7z.exe" set "SEVENZIP=%ProgramFiles%\7-Zip\7z.exe"
if exist "%ProgramFiles(x86)%\7-Zip\7z.exe" set "SEVENZIP=%ProgramFiles(x86)%\7-Zip\7z.exe"

if defined SEVENZIP goto :use_7z
goto :use_tar

rem ── 7-Zip 경로 (-mx=1: 대부분 이미 압축된 산출물이라 속도 우선) ─────────────
:use_7z
echo [zip] Tool   : 7-Zip (%SEVENZIP%)
set "EX="
set "EX=!EX! -xr^!.git"
set "EX=!EX! -x^!%NAME%\server\.venv"
set "EX=!EX! -x^!%NAME%\server\log"
set "EX=!EX! -x^!%NAME%\server\releases\*.zip"
set "EX=!EX! -x^!%NAME%\server\releases\*.exe"
set "EX=!EX! -x^!%NAME%\client\data"
set "EX=!EX! -x^!%NAME%\client\dist"
set "EX=!EX! -x^!%NAME%\client\build"
set "EX=!EX! -x^!%NAME%\client\release_dist"
set "EX=!EX! -x^!%NAME%\client\_profiles"
set "EX=!EX! -x^!%NAME%\client\log"
set "EX=!EX! -x^!%NAME%\dist"
set "EX=!EX! -x^!%NAME%\build"
set "EX=!EX! -xr^!__pycache__"
set "EX=!EX! -xr^!*.pyc"
set "EX=!EX! -xr^!nsw_mirror_tmp"
echo.
"%SEVENZIP%" a -tzip -mx=1 -mmt=on -bso0 -bsp1 "%OUT%" "%NAME%\" !EX!
if errorlevel 1 goto :fail
goto :done

rem ── tar 폴백 (Windows 10+ 내장 bsdtar) ──────────────────────────────────────
:use_tar
where tar.exe >nul 2>&1
if errorlevel 1 (
    echo [zip] ERROR: 7-Zip 도 tar 도 없습니다. 7-Zip 을 설치하세요.
    popd
    pause
    exit /b 1
)
echo [zip] Tool   : tar (bsdtar)
echo.
tar.exe -a -c -f "%OUT%" ^
  --exclude="*/.git" ^
  --exclude="*/.git/*" ^
  --exclude="%NAME%/server/.venv" ^
  --exclude="%NAME%/server/log" ^
  --exclude="%NAME%/server/releases/*.zip" ^
  --exclude="%NAME%/server/releases/*.exe" ^
  --exclude="%NAME%/client/data" ^
  --exclude="%NAME%/client/dist" ^
  --exclude="%NAME%/client/build" ^
  --exclude="%NAME%/client/release_dist" ^
  --exclude="%NAME%/client/_profiles" ^
  --exclude="%NAME%/client/log" ^
  --exclude="%NAME%/dist" ^
  --exclude="%NAME%/build" ^
  --exclude="*/__pycache__/*" ^
  --exclude="*.pyc" ^
  --exclude="*/nsw_mirror_tmp/*" ^
  "%NAME%"
if errorlevel 1 goto :fail

:done
popd
echo.
for %%F in ("%OUT%") do echo [zip] 완료: %%~nxF  (%%~zF bytes)
echo.
echo [zip] ===== 새 서버 PC 에서 할 일 =====
echo [zip]  1. ZIP 을 풀고 server\env\server.env 의 HOST 값을 그 PC 에 맞게 확인
echo [zip]  2. server\start.bat 실행 - .venv 가 없으므로 자동 생성됩니다
echo [zip]     (Python 3.11+ 설치 + pip 접근 필요, 첫 실행은 수 분 걸림)
echo [zip]  3. Honey 배포 ZIP(server\releases\*.zip)은 제외됐습니다.
echo [zip]     /honey/download 를 쓰려면 수동으로 복사해 넣으세요.
echo [zip] ==================================
echo.
pause
exit /b 0

:fail
popd
echo.
echo [zip] ERROR: 압축 실패.
if exist "%OUT%" del "%OUT%"
pause
exit /b 1
