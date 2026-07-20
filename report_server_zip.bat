@echo off
REM ============================================================================
REM KEEP THIS FILE PURE ASCII WITH CRLF LINE ENDINGS.
REM Korean text in a .bat makes cmd.exe miscount byte offsets on some machines:
REM it resumes mid-line and runs word fragments as commands ("honey", "eases",
REM "v is not recognized"). A "chcp 65001" line does not reliably prevent this,
REM so this file stays ASCII-only. Same rule as client\build_zip.bat.
REM ============================================================================
REM Pack the whole report_server folder into a ZIP in the parent folder,
REM for moving the project to another server PC.
REM
REM   Output : ..\report_server_<YYYYMMDD_HHMM>.zip
REM            If that name exists, _1 / _2 / ... is appended.
REM   Tool   : 7-Zip if installed (multithreaded), otherwise built-in tar.
REM
REM Excluded - regenerated on the new PC, or unrelated to running the server:
REM   .git/                  git history (including nested web_report\.git)
REM   server/.venv/          start.bat recreates it on the new PC
REM   client/data/           local test CSV/xlsx (3GB+)
REM   dist/ build/           PyInstaller output
REM   client/release_dist/   Honey ZIP build output
REM   server/releases/*.zip  Honey release packages (version.json IS included)
REM   log/ __pycache__/      runtime logs / bytecode
REM   nsw_mirror_tmp/        temporary merge output
REM
REM Included (operational data is carried over):
REM   DB/        session DB (report.db)
REM   uploads/   parquet / images and other artifacts
REM   server/env/server.env  startup config - check HOST on the new PC
REM   server/wheelhouse/     offline pip wheels, rebuilt by this script (~70MB)
REM                          so the new PC installs without network access
REM ============================================================================
setlocal enabledelayedexpansion

set "PROJ=%~dp0"
set "PROJ=%PROJ:~0,-1%"
for %%I in ("%PROJ%") do set "NAME=%%~nxI"
for %%I in ("%PROJ%") do set "PARENT=%%~dpI"

REM Locale-independent timestamp.
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"`) do set "STAMP=%%T"

REM Pick a free filename: base.zip, then base_1.zip, base_2.zip, ...
set "BASE=%PARENT%%NAME%_%STAMP%"
set "OUT=%BASE%.zip"
set /a SEQ=0
:pick_name
if not exist "%OUT%" goto :name_ok
set /a SEQ+=1
set "OUT=%BASE%_%SEQ%.zip"
if %SEQ% lss 100 goto :pick_name
echo [zip] ERROR: too many existing archives with the same timestamp.
pause
exit /b 1
:name_ok

echo [zip] Source : %PROJ%
echo [zip] Output : %OUT%
if %SEQ% gtr 0 echo [zip] Note   : base name was taken, using suffix _%SEQ%
echo.

REM --- Offline wheelhouse -----------------------------------------------------
REM Download every dependency as a .whl into server\wheelhouse so the new PC can
REM install without network access. install.bat / start.bat use it when present
REM and fall back to a normal network install if it does not match.
REM NOTE: wheels are tied to the Python minor version (cp313 etc). Use the same
REM Python minor version on the new PC, or the fallback will kick in.
set "WHEELDIR=%PROJ%\server\wheelhouse"
set "WHEELPY="
if exist "%PROJ%\server\.venv\Scripts\python.exe" set "WHEELPY=%PROJ%\server\.venv\Scripts\python.exe"
if defined WHEELPY goto :have_wheelpy
for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined WHEELPY set "WHEELPY=%%P"
if defined WHEELPY goto :have_wheelpy
echo [zip] WARNING: no Python found - skipping wheelhouse.
echo [zip]          The new PC will need network access on first start.bat run.
goto :wheels_done

:have_wheelpy
echo [zip] Building offline wheelhouse ...
if exist "%WHEELDIR%" rd /s /q "%WHEELDIR%"
"%WHEELPY%" -m pip download -r "%PROJ%\server\requirements.txt" -d "%WHEELDIR%" --quiet
if errorlevel 1 goto :wheels_failed
set "WHEELCOUNT=0"
for /f %%C in ('dir /b "%WHEELDIR%\*.whl" 2^>nul ^| find /c /v ""') do set "WHEELCOUNT=%%C"
REM No ">" in this echo - cmd would treat it as a redirection operator.
echo [zip] wheelhouse: %WHEELCOUNT% packages in server\wheelhouse
goto :wheels_done

:wheels_failed
echo [zip] WARNING: pip download failed - archive will NOT contain a wheelhouse.
echo [zip]          The new PC will need network access on first start.bat run.
if exist "%WHEELDIR%" rd /s /q "%WHEELDIR%"

:wheels_done
echo.

REM Paths inside the archive are relative to the parent folder.
pushd "%PARENT%"

set "SEVENZIP="
if exist "%ProgramFiles%\7-Zip\7z.exe" set "SEVENZIP=%ProgramFiles%\7-Zip\7z.exe"
if exist "%ProgramFiles(x86)%\7-Zip\7z.exe" set "SEVENZIP=%ProgramFiles(x86)%\7-Zip\7z.exe"

if defined SEVENZIP goto :use_7z
goto :use_tar

REM --- 7-Zip path (-mx=1: most content is already compressed, favor speed) ----
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

REM --- tar fallback (built-in bsdtar on Windows 10+) --------------------------
:use_tar
where tar.exe >nul 2>&1
if errorlevel 1 (
    echo [zip] ERROR: neither 7-Zip nor tar is available. Install 7-Zip.
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
for %%F in ("%OUT%") do echo [zip] Done: %%~nxF  (%%~zF bytes)
echo.
echo [zip] ===== On the new server PC =====
echo [zip]  1. Unzip, then check HOST in server\env\server.env for that PC.
echo [zip]  2. Run server\start.bat - .venv is missing so it is created
echo [zip]     automatically. server\wheelhouse\ is bundled, so this installs
echo [zip]     offline with no network access. Use the same Python minor
echo [zip]     version as this PC, or it falls back to a network install.
echo [zip]  3. Honey release ZIPs (server\releases\*.zip) were excluded.
echo [zip]     Copy them in manually if you need /honey/download.
echo [zip] ===============================
echo.
pause
exit /b 0

:fail
popd
echo.
echo [zip] ERROR: archiving failed.
if exist "%OUT%" del "%OUT%"
pause
exit /b 1
