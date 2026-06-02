import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

def _path_env(name, default):
    v = os.getenv(name)
    return Path(v).expanduser().resolve() if v else default


REPORT_ANALYSIS_INDEX_HTML = ROOT_DIR / "server" / "report" / "report_analysis_index.html"
REPORT_VIEW_HTML           = ROOT_DIR / "server" / "report" / "report_view.html"

_HOST = os.getenv("HOST", "127.0.0.1")
_PORT = os.getenv("PORT", "8000")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", f"http://{_HOST}:{_PORT}")

REPORT_DB_PATH = _path_env("REPORT_DB_PATH", ROOT_DIR / "DB" / "pe" / "report" / "report.db")
REPORT_UPLOAD_DIR = _path_env("REPORT_UPLOAD_DIR", ROOT_DIR / "uploads" / "report")

REPORT_S3_ENDPOINT   = os.getenv("REPORT_S3_ENDPOINT", "")
REPORT_S3_BUCKET     = os.getenv("REPORT_S3_BUCKET", "")
REPORT_S3_REGION     = os.getenv("REPORT_S3_REGION", "us-east-1")
REPORT_S3_ACCESS_KEY = os.getenv("REPORT_S3_ACCESS_KEY", "")
REPORT_S3_SECRET_KEY = os.getenv("REPORT_S3_SECRET_KEY", "")

REPORT_S3_PREFIX            = os.getenv("REPORT_S3_PREFIX",            "pe/report_server/plotly")
REPORT_S3_CSV_PREFIX        = os.getenv("REPORT_S3_CSV_PREFIX",        "pe/report_server/origin_csv_files")
REPORT_S3_FAIL_PREFIX       = os.getenv("REPORT_S3_FAIL_PREFIX",       "pe/report_server/fail_items")
REPORT_S3_ISSUE_PREFIX      = os.getenv("REPORT_S3_ISSUE_PREFIX",      "pe/report_server/issue_table")
REPORT_S3_THUMB_PREFIX      = os.getenv("REPORT_S3_THUMB_PREFIX",      "pe/report_server/thumbs")
REPORT_S3_SOURCE_XLSX_PREFIX = os.getenv("REPORT_S3_SOURCE_XLSX_PREFIX","pe/report_server/source_xlsx")
REPORT_S3_SUMMARY_TEXT_PREFIX = os.getenv("REPORT_S3_SUMMARY_TEXT_PREFIX","pe/report_server/summary_text")
REPORT_S3_ISSUE_TEXT_PREFIX   = os.getenv("REPORT_S3_ISSUE_TEXT_PREFIX",  "pe/report_server/issue_table_text")
REPORT_S3_YIELD_TEXT_PREFIX   = os.getenv("REPORT_S3_YIELD_TEXT_PREFIX",  "pe/report_server/yield_text")
REPORT_S3_ISSUE_IMG_PREFIX    = os.getenv("REPORT_S3_ISSUE_IMG_PREFIX",   "pe/report_server/issue_img")
REPORT_S3_CHART_PREFIX        = os.getenv("REPORT_S3_CHART_PREFIX",       "pe/report_server/chart_png")

REPORT_THUMB_WORKERS = int(os.getenv("REPORT_THUMB_WORKERS", "8"))
REPORT_S3_MAX_POOL_CONNECTIONS = int(os.getenv("REPORT_S3_MAX_POOL_CONNECTIONS", "30"))

REPORT_LOCK_TTL_SEC = 300
REPORT_LOCK_POLL_SEC = 0.5
REPORT_LOCK_MAX_WAIT_SEC = 60

HONEY_RELEASES_DIR = _path_env("HONEY_RELEASES_DIR", ROOT_DIR / "server" / "releases")
HONEY_VERSION_JSON = HONEY_RELEASES_DIR / "version.json"
