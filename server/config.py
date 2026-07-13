import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

def _path_env(name, default):
    v = os.getenv(name)
    return Path(v).expanduser().resolve() if v else default


REPORT_ANALYSIS_INDEX_HTML = ROOT_DIR / "server" / "report" / "report_analysis_index.html"
REPORT_VIEW_HTML           = ROOT_DIR / "server" / "report" / "report_view.html"
ADMIN_DASHBOARD_HTML       = ROOT_DIR / "server" / "report" / "admin_dashboard.html"

_HOST = os.getenv("HOST", "127.0.0.1")
_PORT = os.getenv("PORT", "8000")
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", f"http://{_HOST}:{_PORT}")

REPORT_DB_PATH = _path_env("REPORT_DB_PATH", ROOT_DIR / "DB" / "pe" / "report" / "report.db")
REPORT_UPLOAD_DIR = _path_env("REPORT_UPLOAD_DIR", ROOT_DIR / "uploads" / "report")

STDINFO_DB_PATH = _path_env("STDINFO_DB_PATH", ROOT_DIR / "DB" / "INFORMATION" / "stdinfo_20260511.db")
PRODUCT_INFO_CSV_PATH = _path_env("PRODUCT_INFO_CSV_PATH", ROOT_DIR / "server" / "product_info.csv")

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
REPORT_S3_ISSUE_IMG_PREFIX    = os.getenv("REPORT_S3_ISSUE_IMG_PREFIX",   "pe/report_server/issue_img")
REPORT_S3_CHART_PREFIX        = os.getenv("REPORT_S3_CHART_PREFIX",       "pe/report_server/chart_png")

REPORT_THUMB_WORKERS = int(os.getenv("REPORT_THUMB_WORKERS", "8"))
REPORT_S3_MAX_POOL_CONNECTIONS = int(os.getenv("REPORT_S3_MAX_POOL_CONNECTIONS", "30"))

REPORT_LOCK_TTL_SEC = 300
REPORT_LOCK_POLL_SEC = 0.5
REPORT_LOCK_MAX_WAIT_SEC = 60

# ── 오래된 세션 자동정리 (retention) ─────────────────────────────────────────
# created_at 이 RETENTION_DAYS 이전인 세션을 주기적으로 삭제(S3 산출물 + DB 행).
# 단, is_important=1(중요 표시) 세션은 제외. DRYRUN 이 참이면 실제 삭제 없이
# 대상만 로그/감사로그에 남긴다 — 실삭제하려면 REPORT_CLEANUP_DRYRUN=0 으로 명시.
def _bool_env(name, default):
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

REPORT_RETENTION_DAYS         = int(os.getenv("REPORT_RETENTION_DAYS", "180"))
REPORT_CLEANUP_DRYRUN         = _bool_env("REPORT_CLEANUP_DRYRUN", True)
REPORT_CLEANUP_INTERVAL_HOURS = float(os.getenv("REPORT_CLEANUP_INTERVAL_HOURS", "24"))
REPORT_CLEANUP_ENABLED        = _bool_env("REPORT_CLEANUP_ENABLED", True)

# report_audit_log 롤오프 — 감사 로그는 세션 삭제 후에도 보존되어 무한 증가하므로
# created_at 이 AUDIT_RETENTION_DAYS(기본 365일) 이전인 행을 cleanup 주기마다 삭제한다.
# 0 이하 = 비활성(무기한 보존). 세션 cleanup 의 DRYRUN 과 무관하게 동작한다.
REPORT_AUDIT_RETENTION_DAYS   = int(os.getenv("REPORT_AUDIT_RETENTION_DAYS", "365"))

# ── report.db 주기 백업 (db_backup.py) ──────────────────────────────────────
# WAL 모드라 파일 복사가 아닌 sqlite3 backup API 로 온라인 백업. KEEP 초과분 자동 삭제.
REPORT_DB_BACKUP_ENABLED        = _bool_env("REPORT_DB_BACKUP_ENABLED", True)
REPORT_DB_BACKUP_INTERVAL_HOURS = float(os.getenv("REPORT_DB_BACKUP_INTERVAL_HOURS", "24"))
REPORT_DB_BACKUP_KEEP           = int(os.getenv("REPORT_DB_BACKUP_KEEP", "7"))
REPORT_DB_BACKUP_DIR            = _path_env("REPORT_DB_BACKUP_DIR", REPORT_DB_PATH.parent / "backup")

HONEY_RELEASES_DIR = _path_env("HONEY_RELEASES_DIR", ROOT_DIR / "server" / "releases")
HONEY_VERSION_JSON = HONEY_RELEASES_DIR / "version.json"

# ── admin 대시보드 (admin_panel/) ────────────────────────────────────────────
# admin URL 경로 조각. 기본값 'pte' → /pe/admin-pte/ 로 항상 접속 가능.
# 경로를 숨기고 싶으면 REPORT_ADMIN_SECRET 에 임의 문자열(영숫자/_/- 3~64자) 지정.
REPORT_ADMIN_SECRET = os.getenv("REPORT_ADMIN_SECRET", "pte").strip()
