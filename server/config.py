import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

def _path_env(name, default):
    v = os.getenv(name)
    return Path(v).expanduser().resolve() if v else default


def _server_env_file(name):
    """env/server.env 에서 name 값을 읽는다 (KEY=VALUE, '#' 은 주석).

    start.bat / watchdog.ps1 은 이 파일을 읽어 환경변수로 export 하므로 그 경로로
    기동하면 os.getenv 로 이미 잡힌다. 이 함수는 `python wsgi.py` 처럼 bat 없이
    직접 기동할 때도 같은 파일이 정본이 되게 하는 폴백이다. 없으면 None.
    """
    path = ROOT_DIR / "server" / "env" / "server.env"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip() or None
    return None


REPORT_ANALYSIS_INDEX_HTML = ROOT_DIR / "server" / "report" / "report_analysis_index.html"
REPORT_VIEW_HTML           = ROOT_DIR / "server" / "report" / "report_view.html"
ADMIN_DASHBOARD_HTML       = ROOT_DIR / "server" / "report" / "admin_dashboard.html"
REPORT_LANDING_HTML        = ROOT_DIR / "server" / "landing" / "landing.html"

# 절대 URL 생성 기준 — bind 주소(HOST=0.0.0.0)가 아니라 클라이언트가 실제로 접속하는
# 운영 서버 주소. 정본은 env/server.env 의 SERVER_BASE_URL 이고, 환경변수로 덮을 수 있다.
# 맨 끝 하드코딩 값은 env 파일이 없을 때만 쓰이는 최후 폴백이다.
SERVER_BASE_URL = (
    os.getenv("SERVER_BASE_URL")
    or _server_env_file("SERVER_BASE_URL")
    or "http://12.81.220.117:8080"
)

REPORT_DB_PATH = _path_env("REPORT_DB_PATH", ROOT_DIR / "DB" / "pe" / "report" / "report.db")
REPORT_UPLOAD_DIR = _path_env("REPORT_UPLOAD_DIR", ROOT_DIR / "uploads" / "report")

# Issue Table 사람 코멘트(PTE/개발) export 대상 — eval_analyzer eval.db 스키마의
# report_server 소유 별도 파일. session DB(report.db)와 분리. eval_analyzer 쪽은
# EVAL_DB_PATH env 로 이 파일을 가리켜 읽는다 (docs/13_eval_analyzer_integration.md).
REPORT_EVAL_DB_PATH = _path_env("REPORT_EVAL_DB_PATH", ROOT_DIR / "DB" / "pe" / "report" / "eval" / "eval.db")

# Honey 'DB Input'(선례 CSV 적재)이 돌리는 db_input/import_csv.py 의 인터프리터.
# 기본은 서버 자신 — waitress 가 python.exe 안에서 도는 현 구성에선 이게 맞다.
# 서버가 파이썬이 아닌 호스트(frozen 등) 아래 돌 때만 env 로 지정한다.
# in-process import 가 아니라 별도 프로세스인 이유: import_csv._import_group 이
# eval_engine.config.DB_PATH 를 모듈 전역에 대입한다 (docs/13 §10).
REPORT_EVAL_IMPORT_PYTHON = os.getenv("REPORT_EVAL_IMPORT_PYTHON", "") or sys.executable

# VOC 게시판 DB — 세션 DB(report.db)·eval DB 와 분리된 report_server 소유 별도 파일
# (database/voc_db.py 가 자체 커넥션으로 관리, 이미지 파일은 storage_gateway 에 별도 저장).
REPORT_VOC_DB_PATH = _path_env("REPORT_VOC_DB_PATH", ROOT_DIR / "DB" / "pe" / "report" / "voc" / "voc.db")

STDINFO_DB_PATH = _path_env("STDINFO_DB_PATH", ROOT_DIR / "DB" / "INFORMATION" / "stdinfo_20260511.db")

# 기준정보 DB — 원본 CSV 가 DRM(NASCA)으로 암호화돼 서버가 평문으로 못 읽는다(서버는 Excel 을
# 쓰지 않는다, CLAUDE.md 규칙 #1). Excel 이 설치된 별도 PC 에서 tools/product_info_import 로
# 만들어 이 경로에 손으로 복사한다. 서버는 읽기 전용으로만 연다 — product_info.py 참조.
PRODUCT_INFO_DB_PATH = _path_env("PRODUCT_INFO_DB_PATH",
                                 ROOT_DIR / "DB" / "pe" / "report" / "product_info.db")

# web_report 업로드 parquet **합계** 상한(MB). 개별 파일 상한(512MB)만으로는 파일 수만큼
# 웹 프로세스 메모리에 그대로 쌓인다(전량 메모리 적재 후 디코드). MAX_CONTENT_LENGTH_MB
# (요청 전체, 기본 2048)보다 작게 두는 것이 목적.
REPORT_WEBREPORT_TOTAL_MB = int(os.getenv("REPORT_WEBREPORT_TOTAL_MB", "1024") or 1024)

# web_report 업로드 **동시 처리 건수**. 업로드 1건은 parquet bytes 전량 + 디코드된 tables
# 를 동시에 들고 있어 대형 세션(2000항목×1500행×24소스)이면 건당 RAM 피크가 GB 급이다.
# 제한이 없으면 waitress 스레드 수만큼 그 피크가 겹쳐 웹 프로세스가 죽는다.
# 초과분은 즉시 거절하지 않고 WAIT_SEC 까지 대기한다 — 대기 중에는 werkzeug 가 요청 본문을
# 디스크에 스풀해 둔 상태라 RAM 을 거의 쓰지 않는다.
WEB_REPORT_UPLOAD_CONCURRENCY = int(os.getenv("WEB_REPORT_UPLOAD_CONCURRENCY", "2") or 2)
WEB_REPORT_UPLOAD_WAIT_SEC = float(os.getenv("WEB_REPORT_UPLOAD_WAIT_SEC", "180") or 180)

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
# 보낸다. deleted_at 이후 이 일수가 지난 세션만 purge(산출물+DB 정리) 대상이 된다 —
# 관리자 수동 실행과 cleanup 스케줄러(REPORT_CLEANUP_DRYRUN 존중) 양쪽에서 걷어간다.
# 복원은 관리자 패널 또는 업로더/삭제자 권한.
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
# 외부(다른 디스크/UNC) 백업 복제 경로. 지정하면 integrity 통과본만 여기로도 복사한다
# (같은 디스크가 통째로 죽는 경우 대비). 미설정이면 로컬 백업만.
REPORT_DB_BACKUP_EXTERNAL_DIR   = _path_env("REPORT_DB_BACKUP_EXTERNAL_DIR", None)

HONEY_RELEASES_DIR = _path_env("HONEY_RELEASES_DIR", ROOT_DIR / "server" / "releases")
HONEY_VERSION_JSON = HONEY_RELEASES_DIR / "version.json"
# 릴리스 공지 원문. 운영자가 직접 편집하며 /honey/announcement 가 그대로 서빙한다.
HONEY_ANNOUNCEMENT_TXT = HONEY_RELEASES_DIR / "announcement.txt"

# ── admin 대시보드 (admin_panel/) ────────────────────────────────────────────
# admin URL 경로 조각. 기본값 'pte' → /pe/admin-pte/ 로 항상 접속 가능.
# 경로를 숨기고 싶으면 REPORT_ADMIN_SECRET 에 임의 문자열(영숫자/_/- 3~64자) 지정.
REPORT_ADMIN_SECRET = os.getenv("REPORT_ADMIN_SECRET", "pte").strip()

# admin 패널 접속 비밀번호. 아무나 못 들어오게 하는 간단한 게이트 — 맞으면 쿠키를
# 발급하고 이후 admin 경로 접근을 허용한다. 바꾸려면 REPORT_ADMIN_PASSWORD 지정.
REPORT_ADMIN_PASSWORD = os.getenv("REPORT_ADMIN_PASSWORD", "1031")
