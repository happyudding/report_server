"""Server communication helpers for Honey.

- uploader      : multipart upload to /pe/report/upload_xlsx
- version_check : /honey/version check and release ZIP download
- updater       : apply downloaded release ZIP packages
- update_policy : 자동/수동(manual) 설치 상수 + manual 다운로드 폴더/탐색기 헬퍼
- config        : SERVER_BASE_URL, REQUEST_TIMEOUT_SEC, CURRENT_VERSION

Keep this package independent from report_generator except where a module
explicitly receives local file paths from the UI flow.
"""
