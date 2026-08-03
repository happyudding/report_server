@echo off
rem 콘솔을 UTF-8 로 맞춘다. 이 파일은 UTF-8(BOM 없음)이라 이 줄이 없으면
rem 한국어 Windows 기본 코드페이지(949)에서 한글이 깨져 보인다.
chcp 65001 >nul
rem ============================================================================
rem bind 주소 사고 복구 — env\server.env 의 HOST 를 0.0.0.0 으로 되돌린다.
rem
rem 증상: watchdog 이 재기동을 하루 수십 번 반복하는데 사용자는 아무 문제 없이 쓴다.
rem       diagnose_port.bat 의 [4] 판정이 "바인딩 주소 문제" 로 나온다.
rem
rem 이 배치는 수정 전에 원본을 백업하고, 적용 여부를 물어본다.
rem 서버 재기동 여부도 따로 물어본다. 그냥 더블클릭하면 된다.
rem ============================================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_bind_host.ps1"
echo.
pause
