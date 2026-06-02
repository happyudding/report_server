@echo off
setlocal EnableExtensions

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
%PYTHON_CMD% -m PyInstaller --clean --noconfirm build_honey.spec
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
