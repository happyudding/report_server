@echo off
rem This file must be saved as ASCII/UTF-8 (no BOM) + CRLF (.gitattributes enforces CRLF).
rem
rem ASCII-ONLY ON PURPOSE (the rest of this repo comments in Korean):
rem   cmd.exe restores its read position by BYTE OFFSET after `call :label`, but counts
rem   multi-byte characters inconsistently under chcp 65001. With Korean comments above
rem   a `call :label` the resume point lands mid-line and cmd tries to execute the tail
rem   of a comment ("TypeError" -> "Typ" + "eError: not recognized"). This file's stdout
rem   is captured by its callers, so it must parse cleanly. Korean explanation lives in
rem   the callers (start.bat / mypc_start.bat / install.bat) and server/README.md.
rem
rem ===========================================================================
rem  _find_python.bat - print the path of ONE Python interpreter that meets the
rem  server's minimum version, on STDOUT. Prints nothing and returns 1 if none.
rem
rem  Why a shared file: start.bat / mypc_start.bat / install.bat each picked an
rem  interpreter with slightly different rules, which produced "I installed 3.14
rem  but the server still runs 3.10". The search order lives here, once.
rem
rem  Usage A - search (diagnostics go to stderr, so stdout carries only the path):
rem      set "PY_BOOT="
rem      for /f "delims=" %%P in ('call "%ROOT%_find_python.bat"') do set "PY_BOOT=%%P"
rem      if not defined PY_BOOT ( ...report and stop... )
rem
rem  Usage B - validate ONE interpreter (keeps the minimum version defined here only):
rem      call "%ROOT%_find_python.bat" "%VENV_PY%" >nul 2>&1
rem      if not errorlevel 1  ...it is new enough...
rem
rem  Search order (every candidate is actually RUN, not just tested for existence):
rem    1. %PYTHON%          - explicit override always wins
rem    2. py -3             - Windows launcher picks the NEWEST installed 3.x
rem    3. where python.exe  - PATH order
rem
rem  Step 2 before step 3 is the point of this file. It used to be the other way
rem  round, so an old Python earlier in PATH won over a newly installed one.
rem ===========================================================================
setlocal

rem Minimum version: web_report compute workers use
rem ProcessPoolExecutor(max_tasks_per_child=), added in 3.11. On 3.10 and older
rem every cold build fails with TypeError. See server/README.md.
set "MIN_MAJOR=3"
set "MIN_MINOR=11"

set "FOUND="

rem Usage B: one explicit candidate, no searching.
if not "%~1"=="" (
    call :check "%~1"
    goto :done
)

if defined PYTHON call :check "%PYTHON%"
if not defined FOUND for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do if not defined FOUND call :check "%%P"
if not defined FOUND for /f "delims=" %%P in ('where python.exe 2^>nul') do if not defined FOUND call :check "%%P"

:done

if not defined FOUND (
    echo [find-python] no Python ^>= %MIN_MAJOR%.%MIN_MINOR% found. >&2
    exit /b 1
)
echo %FOUND%
exit /b 0

rem --- Validate one candidate: keep it in FOUND if it runs and is new enough ---
rem Run it instead of only checking the file exists: a venv whose base Python was
rem moved or removed still has python.exe but dies with "did not find executable at".
:check
if "%~1"=="" exit /b
if not exist "%~1" exit /b
"%~1" -c "import sys;sys.exit(0 if sys.version_info>=(%MIN_MAJOR%,%MIN_MINOR%) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [find-python] skipped ^(older than %MIN_MAJOR%.%MIN_MINOR%^): %~1 >&2
    exit /b
)
set "FOUND=%~1"
exit /b
