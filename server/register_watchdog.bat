@echo off
rem 콘솔을 UTF-8 로 맞춘다. 이 파일은 UTF-8(BOM 없음)이라 이 줄이 없으면
rem 한국어 Windows 기본 코드페이지(949)에서 한글이 깨져 보인다. BOM 을 붙이면
rem 대신 cmd 가 첫 줄(@echo off)을 못 읽어 에러를 내므로, BOM 없이 이 방식을 쓴다.
chcp 65001 >nul
setlocal
rem ============================================================================
rem report-server watchdog 등록 — 관리자 권한으로 1회 실행 (우클릭 > 관리자 권한 실행)
rem
rem 등록되는 작업 2개 (Windows 작업 스케줄러):
rem   report-server-watchdog : 5분 주기로 watchdog.ps1 실행 (죽었으면 자동 재기동)
rem   report-server-boot     : 부팅 1분 후 1회 실행 (부팅 직후 최대 5분 공백 제거)
rem
rem 주의:
rem   - /RU 없이 등록하므로 "현재 사용자가 로그온 중일 때만" 실행된다.
rem     서버 PC 가 자동 로그인으로 상시 로그온 상태라는 전제. 무로그인 실행이
rem     필요하면 아래 두 명령에  /RU %%USERNAME%% /RP *  를 붙여 재등록할 것.
rem   - 수동 점검(terminate.bat 으로 서버를 내려두는 시간)에는 먼저 일시 정지:
rem       schtasks /Change /TN report-server-watchdog /DISABLE
rem     점검이 끝나면:
rem       schtasks /Change /TN report-server-watchdog /ENABLE
rem   - 등록 해제:
rem       schtasks /Delete /TN report-server-watchdog /F
rem       schtasks /Delete /TN report-server-boot /F
rem ============================================================================

set "PS_CMD=powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%~dp0watchdog.ps1\""

echo [register] watchdog 5분 주기 작업 등록 ...
schtasks /Create /F /TN "report-server-watchdog" /SC MINUTE /MO 5 /RL HIGHEST /TR "%PS_CMD%"
if errorlevel 1 goto :fail

echo [register] 부팅 시 1회 작업 등록 ...
schtasks /Create /F /TN "report-server-boot" /SC ONSTART /DELAY 0001:00 /RL HIGHEST /TR "%PS_CMD%"
if errorlevel 1 goto :fail

echo.
echo [register] 등록 완료.
echo [register]   확인      : schtasks /Query /TN report-server-watchdog
echo [register]   즉시 실행 : schtasks /Run /TN report-server-watchdog
echo [register]   일시 정지 : schtasks /Change /TN report-server-watchdog /DISABLE
echo [register] 재기동 이력은 admin 대시보드(현황 탭) 또는 server\log\watchdog_events.log 에서 확인.
pause
exit /b 0

:fail
echo.
echo [register] ERROR: 작업 등록 실패 — 관리자 권한 cmd 에서 실행했는지 확인하세요.
pause
exit /b 1
