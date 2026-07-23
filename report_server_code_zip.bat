@echo off
REM ============================================================================
REM KEEP THIS FILE PURE ASCII WITH CRLF LINE ENDINGS.
REM Korean text in a .bat makes cmd.exe miscount byte offsets on some machines:
REM it resumes mid-line and runs word fragments as commands ("honey", "eases",
REM "v is not recognized"). A "chcp 65001" line does not reliably prevent this,
REM so this file stays ASCII-only. Same rule as report_server_zip.bat.
REM ============================================================================
REM CODE-ONLY archive, for UPDATING a server PC that is already running.
REM
REM   Output : ..\report_server_code_<YYYYMMDD_HHMM>.zip
REM            If that name exists, _1 / _2 / ... is appended.
REM   Tool   : 7-Zip if installed (multithreaded), otherwise built-in tar.
REM
REM Difference from report_server_zip.bat:
REM   report_server_zip.bat  = MOVE the whole project to a NEW PC.
REM                            It DOES include DB/ and uploads/. Unpacking it
REM                            over a live server WIPES the session database.
REM   this script            = code only. Operational data on the server PC is
REM                            left untouched, so it is safe to unpack on top.
REM
REM Excluded on purpose - operational data owned by the SERVER PC:
REM   DB/                    session DB (report.db + -wal/-shm), eval, voc,
REM                          product_info.db, backup/
REM   uploads/               parquet / images / disk cache
REM   server/env/            server.env - the single source of truth for
REM                          HOST/PORT on that machine
REM   server/wheelhouse/     offline wheels (see the note printed at the end)
REM   server/releases/*.zip  Honey release packages served by /honey/download
REM
REM Excluded as usual - regenerated, or unrelated to running the server:
REM   .git/  server/.venv/  client/data/  dist/  build/  client/release_dist/
REM   log/  __pycache__/  nsw_mirror_tmp/
REM
REM KNOWN LIMIT: unpacking on top overwrites and adds files, but never DELETES.
REM If this change removed a source file, it stays behind on the server PC and
REM must be deleted by hand.
REM ============================================================================
setlocal enabledelayedexpansion

set "PROJ=%~dp0"
set "PROJ=%PROJ:~0,-1%"
for %%I in ("%PROJ%") do set "NAME=%%~nxI"
for %%I in ("%PROJ%") do set "PARENT=%%~dpI"

REM Locale-independent timestamp.
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"`) do set "STAMP=%%T"

REM Pick a free filename: base.zip, then base_1.zip, base_2.zip, ...
set "BASE=%PARENT%%NAME%_code_%STAMP%"
set "OUT=%BASE%.zip"
set /a SEQ=0
:pick_name
if not exist "%OUT%" goto :name_ok
set /a SEQ+=1
set "OUT=%BASE%_%SEQ%.zip"
if %SEQ% lss 100 goto :pick_name
echo [code-zip] ERROR: too many existing archives with the same timestamp.
pause
exit /b 1
:name_ok

set "LIST=%TEMP%\%NAME%_code_zip_list.txt"

echo [code-zip] Source : %PROJ%
echo [code-zip] Output : %OUT%
if %SEQ% gtr 0 echo [code-zip] Note   : base name was taken, using suffix _%SEQ%
echo [code-zip] Mode   : CODE ONLY - DB, uploads and server\env are NOT packed.
echo.

REM Paths inside the archive are relative to the parent folder, so the archive
REM unpacks as "%NAME%\..." and merges onto the server folder of the same name.
pushd "%PARENT%"

set "SEVENZIP="
if exist "%ProgramFiles%\7-Zip\7z.exe" set "SEVENZIP=%ProgramFiles%\7-Zip\7z.exe"
if exist "%ProgramFiles(x86)%\7-Zip\7z.exe" set "SEVENZIP=%ProgramFiles(x86)%\7-Zip\7z.exe"

if defined SEVENZIP goto :use_7z
goto :use_tar

REM --- 7-Zip path (-mx=1: source is small, favor speed) -----------------------
:use_7z
echo [code-zip] Tool   : 7-Zip (%SEVENZIP%)
set "EX="
REM -- operational data (the whole point of this script) --
set "EX=!EX! -x^!%NAME%\DB"
set "EX=!EX! -x^!%NAME%\uploads"
set "EX=!EX! -x^!%NAME%\server\env"
set "EX=!EX! -x^!%NAME%\server\wheelhouse"
REM -- regenerated / irrelevant --
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
goto :verify

REM --- tar fallback (built-in bsdtar on Windows 10+) --------------------------
:use_tar
where tar.exe >nul 2>&1
if errorlevel 1 (
    echo [code-zip] ERROR: neither 7-Zip nor tar is available. Install 7-Zip.
    popd
    pause
    exit /b 1
)
echo [code-zip] Tool   : tar (bsdtar)
echo.
tar.exe -a -c -f "%OUT%" ^
  --exclude="%NAME%/DB" ^
  --exclude="%NAME%/DB/*" ^
  --exclude="%NAME%/uploads" ^
  --exclude="%NAME%/uploads/*" ^
  --exclude="%NAME%/server/env" ^
  --exclude="%NAME%/server/env/*" ^
  --exclude="%NAME%/server/wheelhouse" ^
  --exclude="%NAME%/server/wheelhouse/*" ^
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

REM --- Safety net -------------------------------------------------------------
REM A silent exclusion typo here costs the live session database, so read the
REM archive back and refuse to hand over anything that carries operational data.
:verify
echo.
echo [code-zip] Verifying that no operational data slipped in ...
if defined SEVENZIP goto :verify_7z
tar.exe -tf "%OUT%" > "%LIST%"
goto :verify_scan
:verify_7z
"%SEVENZIP%" l -ba "%OUT%" > "%LIST%"
:verify_scan
findstr /I /C:"%NAME%\DB" /C:"%NAME%/DB" /C:"%NAME%\uploads" /C:"%NAME%/uploads" /C:"%NAME%\server\env" /C:"%NAME%/server/env" "%LIST%" >nul
if not errorlevel 1 goto :leak
del "%LIST%" >nul 2>&1
echo [code-zip]   OK - no DB / uploads / server\env entries.
goto :done

:leak
echo.
echo [code-zip] ERROR: the archive contains operational data. Offending entries:
findstr /I /C:"%NAME%\DB" /C:"%NAME%/DB" /C:"%NAME%\uploads" /C:"%NAME%/uploads" /C:"%NAME%\server\env" /C:"%NAME%/server/env" "%LIST%"
del "%LIST%" >nul 2>&1
if exist "%OUT%" del "%OUT%"
popd
echo.
echo [code-zip] Archive deleted. Do NOT deploy. Fix the exclusion list first.
pause
exit /b 1

:done
popd
echo.
for %%F in ("%OUT%") do echo [code-zip] Done: %%~nxF  (%%~zF bytes)
echo.
echo [code-zip] ===== On the running server PC =====
echo [code-zip]  1. BACK UP FIRST. Stop the server, then copy the whole DB\
echo [code-zip]     folder somewhere safe. Copying it while the server runs is
echo [code-zip]     unsafe - report.db-wal holds uncommitted pages.
echo [code-zip]     Keeping the previous code folder as-is gives you a rollback.
echo [code-zip]  2. server\terminate.bat
echo [code-zip]     This also PAUSES the watchdog. Skip it and the watchdog
echo [code-zip]     restarts the server on the OLD code within 5 minutes.
echo [code-zip]  3. Unpack this ZIP over the parent of the server folder,
echo [code-zip]     overwriting. DB\, uploads\ and server\env\ are not in the
echo [code-zip]     archive, so they survive untouched.
echo [code-zip]  4. If server\requirements.txt changed: server\install.bat
echo [code-zip]     wheelhouse is NOT bundled here. An offline server PC needs
echo [code-zip]     server\wheelhouse\ carried over from report_server_zip.bat.
echo [code-zip]     No dependency change means no action.
echo [code-zip]  5. server\start.bat - re-enables the watchdog and health-checks.
echo [code-zip] ------------------------------------
echo [code-zip]  REMEMBER: unpacking overwrites and adds, it never deletes.
echo [code-zip]  Source files removed in this change must be deleted by hand.
echo [code-zip]  DB schema migration runs on startup and is NOT reversible -
echo [code-zip]  rolling back code alone is not enough, restore the DB backup too.
echo [code-zip] ====================================
echo.
pause
exit /b 0

:fail
popd
echo.
echo [code-zip] ERROR: archiving failed.
if exist "%OUT%" del "%OUT%"
if exist "%LIST%" del "%LIST%"
pause
exit /b 1
