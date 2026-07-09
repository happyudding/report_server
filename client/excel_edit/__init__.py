"""Honey rawdata Excel 편집 (내장 브라우저 세션 → 별도 Excel 창 왕복).

Honey 사이드바의 'Rawdata 수정' 버튼이 현재 열린 web_report 세션의 parquet 원본을
서버에서 내려받아 Excel 로 열고, 사용자가 저장·닫으면 재인코딩해 서버 원본을 덮어쓴다.

- excel_session.run_excel_edit(): Qt 비의존 순수 로직 (다운로드→xlsx→감시→재인코딩→업로드)
- worker.ExcelEditWorker(QThread): honey_main 이 쓰는 백그라운드 래퍼 (상태 시그널)
"""
