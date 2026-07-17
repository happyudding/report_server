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

# Issue Table 사람 코멘트(PTE/개발) export 대상 — eval_analyzer eval.db 스키마의
# report_server 소유 별도 파일. session DB(report.db)와 분리. eval_analyzer 쪽은
# EVAL_DB_PATH env 로 이 파일을 가리켜 읽는다 (docs/13_eval_analyzer_integration.md).
REPORT_EVAL_DB_PATH = _path_env("REPORT_EVAL_DB_PATH", ROOT_DIR / "DB" / "pe" / "report" / "eval" / "eval.db")

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

# ── 휴지통(soft delete) 보관 기간 ─────────────────────────────────────────────
# 사용자 DELETE 는 즉시 영구삭제가 아니라 report_session.deleted_at 을 찍어 휴지통으로
# 보낸다. deleted_at 이후 이 일수가 지난 세션만 관리자 purge 에서 산출물+DB 를 정리한다
# (자동 purge 없음 — 관리자 수동 실행). 복원은 관리자 패널 또는 업로더/삭제자 권한.
REPORT_TRASH_RETENTION_DAYS   = int(os.getenv("REPORT_TRASH_RETENTION_DAYS", "30"))

# report_audit_log 롤오프 — 감사 로그는 세션 삭제 후에도 보존되어 무한 증가하므로
# created_at 이 AUDIT_RETENTION_DAYS(기본 365일) 이전인 행을 cleanup 주기마다 삭제한다.
# 0 이하 = 비활성(무기한 보존). 세션 cleanup 의 DRYRUN 과 무관하게 동작한다.
REPORT_AUDIT_RETENTION_DAYS   = int(os.getenv("REPORT_AUDIT_RETENTION_DAYS", "365"))

# ── 로컬 hot 캐시 → S3 자동 티어링 (report_tiering.py) ────────────────────────
# 바이너리 산출물(web_report parquet·manifest, issue/dist PNG)을 로컬에 hot 캐시로
# 두다가, ① 6개월(REPORT_TIER_AGE_DAYS) 이상 됐거나 ② 로컬 티어링대상 총량이
# REPORT_TIER_LOCAL_MAX_GB 를 넘으면 오래된 순으로 S3 로 이동하고 로컬 원본을 삭제한다.
# S3 미설정(REPORT_S3_BUCKET 공란)이면 no-op. cleanup 스케줄러 주기에 얹혀 실행된다.
# disk_cache 의 WEB_REPORT_DISK_CACHE_MAX_GB(계산캐시 500GB LRU)와는 별개다.
# DRYRUN 이 참(기본)이면 실제 이동 없이 대상만 로그에 남긴다(첫 도입 안전판) —
# 실이동하려면 REPORT_TIER_DRYRUN=0 으로 명시. 6개월 만료 세션을 삭제하던 종전
# retention 은 폐지되고(데이터 유실 방지) 이 티어링(S3 아카이브)이 대체한다.
REPORT_TIER_ENABLED      = _bool_env("REPORT_TIER_ENABLED", True)
REPORT_TIER_LOCAL_MAX_GB = float(os.getenv("REPORT_TIER_LOCAL_MAX_GB", "1024") or 1024)
REPORT_TIER_AGE_DAYS     = int(os.getenv("REPORT_TIER_AGE_DAYS", "180"))
REPORT_TIER_DRYRUN       = _bool_env("REPORT_TIER_DRYRUN", True)

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

# admin 패널 접속 비밀번호. 아무나 못 들어오게 하는 간단한 게이트 — 맞으면 쿠키를
# 발급하고 이후 admin 경로 접근을 허용한다. 바꾸려면 REPORT_ADMIN_PASSWORD 지정.
REPORT_ADMIN_PASSWORD = os.getenv("REPORT_ADMIN_PASSWORD", "0023")
