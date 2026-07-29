@echo off
rem 콘솔을 UTF-8 로 맞춘다. 이 파일은 UTF-8(BOM 없음)이라 이 줄이 없으면
rem 한국어 Windows 기본 코드페이지(949)에서 한글이 깨져 보인다.
chcp 65001 >nul
rem ============================================================================
rem report-server 포트 점유 진단 — 그냥 더블클릭하면 된다. 관리자 권한 불필요.
rem
rem 읽기 전용이다: 프로세스를 죽이거나 설정을 바꾸지 않는다. 서비스에 영향 없음.
rem
rem 하는 일: 서비스 포트(8080)를 누가 쥐고 있는지 / 진단 포트(8081)가 답하는지 /
rem          둘의 주인이 같은 프로세스인지 / watchdog 최근 기록을 한 파일로 모은다.
rem
rem 결과: server\log\diagnose_port_<시각>.txt 로 저장되고 메모장으로 열린다.
rem       그 내용을 그대로 복사해 전달하면 된다.
rem ============================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose_port.ps1"
echo.
pause
