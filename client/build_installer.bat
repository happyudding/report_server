@echo off
REM Honey 설치본 빌드: PyInstaller(onedir) -> Inno Setup(HoneySetup.exe)
REM 사용: 더블클릭 또는 명령창에서 build_installer.bat
setlocal
cd /d "%~dp0"

echo === [1/2] PyInstaller onedir build ===
python -m PyInstaller --clean --noconfirm build_honey.spec
if errorlevel 1 (
  echo.
  echo [ERROR] PyInstaller build failed.
  exit /b 1
)

echo.
echo === [2/2] Inno Setup compile ===
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  for /f "delims=" %%I in ('where ISCC.exe 2^>nul') do set "ISCC=%%I"
)
if not exist "%ISCC%" (
  echo.
  echo [ERROR] Inno Setup ^(ISCC.exe^) not found.
  echo   설치: winget install -e --id JRSoftware.InnoSetup
  echo   설치 후 다시 실행하세요.
  exit /b 1
)

"%ISCC%" installer.iss
if errorlevel 1 (
  echo.
  echo [ERROR] Inno Setup compile failed.
  exit /b 1
)

echo.
echo === DONE ===
echo   설치본: installer_dist\HoneySetup-0.1.0.exe
exit /b 0
