@echo off
chcp 65001 >nul
REM ============================================================================
REM  Honey 내장 브라우저 그래픽 우회 (문제가 있는 PC 에서만 한 번 실행)
REM
REM  이런 증상일 때 씁니다:
REM   - 세션 화면에서 마우스를 움직일 때마다 화면이 심하게 깜빡인다
REM   - 화면이 갑자기 하얘지거나 버튼이 사라진다
REM   - 웹 브라우저(Edge/Chrome)로 같은 주소를 열면 멀쩡하다
REM
REM  하는 일: 내장 브라우저가 그래픽카드 가속 대신 소프트웨어 렌더링을 쓰도록
REM  Windows 사용자 환경변수를 설정합니다. Honey 를 다시 설치할 필요는 없습니다.
REM
REM  되돌리려면 아래 한 줄을 명령 프롬프트에 붙여넣고 실행하세요:
REM    reg delete "HKCU\Environment" /v QTWEBENGINE_CHROMIUM_FLAGS /f
REM ============================================================================
setlocal

echo.
echo  [Honey] 내장 브라우저를 소프트웨어 렌더링으로 전환합니다.
echo.

setx QTWEBENGINE_CHROMIUM_FLAGS "--disable-gpu" >nul
if errorlevel 1 (
  echo  [실패] 환경변수를 설정하지 못했습니다.
  echo         이 창을 닫고, 이 파일을 마우스 오른쪽 클릭 - "관리자 권한으로 실행" 해보세요.
  echo.
  pause
  exit /b 1
)

echo  [완료] 설정했습니다.
echo.
echo  다음 순서로 진행하세요:
echo    1) 실행 중인 Honey 를 완전히 종료합니다.
echo    2) Honey 를 다시 실행합니다.
echo    3) 세션 화면에서 깜빡임이 사라졌는지 확인합니다.
echo.
echo  (그래도 그대로면 담당자에게 알려주세요.)
echo.
pause
exit /b 0
