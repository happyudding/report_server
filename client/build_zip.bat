@echo off
REM Honey ZIP release build: PyInstaller(onedir) -> Honey-<version>.zip
REM Usage: double-click or run from a command prompt.
setlocal
cd /d "%~dp0"

REM [임시/테스트] 버전을 3.0.0 으로 고정한다. 이 -Version 인자를 지우면 원래대로
REM CURRENT_VERSION 의 patch 자동 증가(3.0.0 -> 3.0.1 -> ...) 로 돌아간다.
powershell -NoProfile -ExecutionPolicy Bypass -File ".\release\release_honey.ps1" -Version "3.0.0"
if errorlevel 1 (
  echo.
  echo [ERROR] Honey ZIP release build failed.
  echo [ERROR] 로그: client\release\logs\ 의 최신 release_*.log 파일을 확인하세요.
  echo         ^(pip/PyInstaller 상세 에러는 위 콘솔 출력에서 확인^)
  pause
  exit /b 1
)

echo.
echo === DONE ===
pause
exit /b 0
