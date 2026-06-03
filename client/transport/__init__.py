"""서버 연동 글루 패키지.

Honey 엔진(report_generator/)과 헤드리스 서버 사이의 모든 통신·배포 코드를 모은다:
- uploader      : /pe/report/upload_xlsx 로 xlsx + 차트 PNG 전송
- chart_export  : 로컬 Excel(COM)로 차트 → PNG 렌더 (서버가 헤드리스라 클라가 담당)
- version_check : /honey/version 폴링 + 설치본 다운로드
- updater       : 다운로드한 설치본 silent 재설치 실행
- config        : 서버/전송/버전 상수 (SERVER_BASE_URL, REQUEST_TIMEOUT_SEC, CURRENT_VERSION)

불변식: 이 패키지는 report_generator/ 를 import 하지 않는다 (chart_export 는 xlsx
파일 경로만 받음). 엔진↔전송 배선은 honey_main.py 한 곳에서만 한다.
"""
