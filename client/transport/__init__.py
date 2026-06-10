"""Server communication helpers for Honey.

- uploader      : multipart upload to /pe/report/upload_xlsx
- chart_export  : local chart/image export helpers
- version_check : /honey/version check and release ZIP download
- updater       : apply downloaded release ZIP packages
- config        : SERVER_BASE_URL, REQUEST_TIMEOUT_SEC, CURRENT_VERSION

Keep this package independent from report_generator except where a module
explicitly receives local file paths from the UI flow.
"""
