"""DB 공통 기반: 스키마·마이그레이션·커넥션·analysis lock.

report_db facade 의 구현 분리(2026-07-11 Phase 4). 다른 database/ 모듈들은
여기의 get_conn/_now/_row 를 공유한다.
"""
import sqlite3
import time
from contextlib import contextmanager

from config import REPORT_DB_PATH, REPORT_LOCK_TTL_SEC

SCHEMA = """
CREATE TABLE IF NOT EXISTS report_session (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT UNIQUE NOT NULL,
    analysis_key  TEXT,
    file_name     TEXT NOT NULL,
    file_path     TEXT,
    content_hash  TEXT,
    status        TEXT DEFAULT 'pending',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER,
    error_message TEXT,
    product_type  TEXT,
    family_product TEXT,
    process       TEXT,
    product       TEXT,
    part_id       TEXT,
    sub_part_id   TEXT,
    product_group TEXT,
    wf_size       TEXT,
    chip_size_x   TEXT,
    chip_size_y   TEXT,
    gross_die     TEXT,
    pkg_type      TEXT,
    e2f_fab_site  TEXT,
    step          TEXT,
    temperature   TEXT,
    equip         TEXT,
    para          TEXT,
    flat_zone     TEXT,
    revision      TEXT,
    edm_link      TEXT,
    dataset_id    TEXT,
    lot_id        TEXT,
    password      TEXT,
    is_debug      INTEGER DEFAULT 0,
    source        TEXT DEFAULT 'xlsx_upload',
    is_important  INTEGER DEFAULT 0,
    is_private    INTEGER DEFAULT 0,
    uploaded_by   TEXT,
    client_host   TEXT,
    webreport_options TEXT,
    mode          TEXT DEFAULT 'Normal',
    deleted_at    INTEGER,
    deleted_by    TEXT
);
CREATE INDEX IF NOT EXISTS idx_report_session_analysis_key
    ON report_session(analysis_key);
CREATE INDEX IF NOT EXISTS idx_report_session_status_created
    ON report_session(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_session_product_type
    ON report_session(product_type);
CREATE INDEX IF NOT EXISTS idx_report_session_deleted_at
    ON report_session(deleted_at);

CREATE TABLE IF NOT EXISTS report_analysis_summary (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_key  TEXT NOT NULL,
    session_id    TEXT,
    item_name     TEXT NOT NULL,
    bin_number    INTEGER,
    yield_percent REAL,
    fail_count    INTEGER,
    cpk_val       REAL,
    mean_val      REAL,
    stdev_val     REAL,
    lsl           REAL,
    usl           REAL,
    unit          TEXT,
    created_at    INTEGER NOT NULL,
    UNIQUE(analysis_key, item_name, bin_number)
);
CREATE INDEX IF NOT EXISTS idx_report_summary_analysis_key
    ON report_analysis_summary(analysis_key);

CREATE TABLE IF NOT EXISTS report_object_info (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_key  TEXT NOT NULL,
    object_type   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    options_json  TEXT NOT NULL,
    s3_bucket     TEXT,
    s3_key        TEXT NOT NULL,
    s3_uri        TEXT,
    created_at    INTEGER NOT NULL,
    last_accessed INTEGER,
    UNIQUE(analysis_key, object_type)
);
CREATE INDEX IF NOT EXISTS idx_report_object_content_hash
    ON report_object_info(content_hash);
CREATE INDEX IF NOT EXISTS idx_report_object_last_accessed
    ON report_object_info(last_accessed);

CREATE TABLE IF NOT EXISTS report_analysis_lock (
    analysis_key  TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    locked_at     INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS report_csv_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_key TEXT NOT NULL,
    filename     TEXT NOT NULL,
    s3_key       TEXT NOT NULL,
    s3_uri       TEXT,
    file_size    INTEGER,
    uploaded_at  INTEGER NOT NULL,
    UNIQUE(analysis_key, filename)
);
CREATE INDEX IF NOT EXISTS idx_report_csv_analysis_key
    ON report_csv_files(analysis_key);

CREATE TABLE IF NOT EXISTS report_annotation (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    analysis_key TEXT,
    target       TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_annotation_session
    ON report_annotation(session_id);

CREATE TABLE IF NOT EXISTS report_dashboard_comment (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    item_key     TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    UNIQUE(dataset_id, kind, item_key)
);
CREATE INDEX IF NOT EXISTS idx_report_dashboard_dataset
    ON report_dashboard_comment(dataset_id, kind);

CREATE TABLE IF NOT EXISTS report_sheet_data (
    analysis_key TEXT NOT NULL,
    sheet_name   TEXT NOT NULL,
    data_json    TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    PRIMARY KEY (analysis_key, sheet_name)
);

CREATE TABLE IF NOT EXISTS report_audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action         TEXT NOT NULL,        -- 'upload' | 'edit' | 'delete'
    session_id     TEXT,
    analysis_key   TEXT,
    -- 삭제 시 세션 행이 사라지므로 조회 가독성을 위해 메타 스냅샷을 함께 저장
    product_type   TEXT,
    product        TEXT,
    lot_id         TEXT,
    file_name      TEXT,
    changed_fields TEXT,                 -- edit 시 변경 필드명 콤마조인, 그 외 NULL
    client_ip      TEXT,
    user_agent     TEXT,
    client_user    TEXT,                 -- 클라이언트 신고 Windows 계정 (upload 만, 위조 가능)
    client_host    TEXT,                 -- 클라이언트 신고 PC 이름
    result         TEXT DEFAULT 'ok',    -- 'ok' | 'fail'
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_audit_created_at
    ON report_audit_log(created_at);
-- /pe/admin 대시보드 필터 조회용 (audit 행이 누적돼도 action/session_id 필터가 풀스캔 안 되게)
CREATE INDEX IF NOT EXISTS idx_report_audit_action
    ON report_audit_log(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_report_audit_session_id
    ON report_audit_log(session_id);

-- 웹 챗봇(관리자 전용) 질문/답변 + 부하 계측. report_audit_log 와 분리한 이유:
-- 답변 전문이 수 KB 라 audit 의 changed_fields 1500자 관례에 안 맞고, 질문 단위 행이라
-- 업로드/편집 단위인 감사 화면을 밀어낸다. 소요는 총/대기/LLM 3분해로 남긴다 —
-- "느린 게 LLM 탓인지 동시성 제한 탓인지" 를 총 소요만으로는 가릴 수 없다.
CREATE TABLE IF NOT EXISTS report_chatbot_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    INTEGER NOT NULL,
    user          TEXT,                 -- 질문한 신원 (HoneyUser 계정)
    client_ip     TEXT,
    context_session_id TEXT,            -- 세션 상세에서 물었으면 그 세션
    question      TEXT NOT NULL,
    answer        TEXT,                 -- 답변 전문
    intent        TEXT,
    planner       TEXT,                 -- 'llm' | 'rule'
    plan_json     TEXT,
    steps_json    TEXT,                 -- 호출된 조회 툴 기록
    total_ms      INTEGER,
    wait_ms       INTEGER,              -- 동시실행 세마포어 대기
    llm_ms        INTEGER,              -- LLM 왕복 (미사용이면 NULL)
    result        TEXT DEFAULT 'ok',    -- 'ok' | 'busy' | 'error:<예외클래스>'
    error_detail  TEXT                  -- 실패 시 예외 메시지 + traceback (관리자 탭에서 펼쳐 봄)
);
CREATE INDEX IF NOT EXISTS idx_report_chatbot_created_at
    ON report_chatbot_log(created_at DESC);

CREATE TABLE IF NOT EXISTS report_user_favorite (
    user_id    TEXT NOT NULL,        -- 웹 사용자 신고 Windows ID (소문자 정규화, 위조 가능)
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

CREATE TABLE IF NOT EXISTS report_user (
    user_id       TEXT PRIMARY KEY,  -- 소문자 정규화된 로그인 ID
    password_hash TEXT NOT NULL,     -- werkzeug generate_password_hash 결과
    created_at    INTEGER NOT NULL
);

-- 사용자 실명(표시용). report_user 의 컬럼이 아니라 별도 테이블인 이유는, 웹 로그인 계정이
-- 없는 Honey 전용 사용자(password_hash NOT NULL 이라 report_user 에 행을 만들 수 없다)도
-- 이름을 가져야 하기 때문이다. 키는 report_user·report_web_visitor 와 같은 소문자 singleID.
-- 이름은 **표시 전용**이며 접근제어·감사 식별에는 쓰지 않는다(신원 판단은 계속 user_id).
CREATE TABLE IF NOT EXISTS report_user_profile (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at   INTEGER NOT NULL,
    updated_by   TEXT                -- 'self' | 'admin:<uid>'
);

CREATE TABLE IF NOT EXISTS report_session_editor (
    session_id  TEXT NOT NULL,       -- 편집 권한을 위임한 세션
    editor_user TEXT NOT NULL,       -- 권한을 받은 PC 계정 (소문자 정규화, _current_user 규칙)
    granted_by  TEXT,                -- 부여한 업로더 계정
    granted_at  INTEGER NOT NULL,
    PRIMARY KEY (session_id, editor_user)
);

CREATE TABLE IF NOT EXISTS report_web_visitor (
    user_id    TEXT PRIMARY KEY,     -- web_report 를 연 적 있는 Honey 사용자 (편집자 후보 풀)
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS report_user_important (
    user_id    TEXT NOT NULL,        -- 사용자별 개인 중요표시 (전역 is_important 와 별개)
    session_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, session_id)
);

-- web_report 편집 상태 (comment/override) — 세션 단위 저장 (2026-07-11).
-- manifest 는 업로드 시점 불변 스냅샷으로 강등되고, 편집은 이 테이블에만 기록된다.
-- dedup(동일 analysis_key) 세션 간 편집을 공유하지 않는다. 표시 순서(etc_item)는 rowid.
CREATE TABLE IF NOT EXISTS report_webreport_edit (
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- issue_comment | etc_item | trim_override | summary_engr
    item_key   TEXT NOT NULL,        -- kind 별 키 (web_report/edits.py 규약)
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT,
    PRIMARY KEY (session_id, kind, item_key)
);

-- 세션별 편집 rev (단조 증가) — TRIM//full 캐시 키의 무효화 토큰.
-- payload_rev 는 **report payload 계산에 실제로 들어가는 편집**(comment/hidden/status/
-- etc_item/engr/signature/preprocess/yield_basis)에만 오르는 두 번째 카운터다. Note 시트·
-- 차트 주석·Note 태그는 /full 조립 단계에서만 붙는데도 rev 를 올려 report payload 를
-- 통째로 콜드로 만들었다(한 글자 고쳐도 전체 재계산). rev 는 그대로 두어 /full·note
-- 낙관 잠금 의미를 보존한다.
CREATE TABLE IF NOT EXISTS report_webreport_edit_rev (
    session_id  TEXT PRIMARY KEY,
    rev         INTEGER NOT NULL,
    payload_rev INTEGER NOT NULL DEFAULT 0
);

-- 접속 사용량 일별 집계 (관리자 통계 탭) — Honey 실행·웹페이지 방문 횟수.
-- 행 1개 = (날짜, 종류, 사용자) 카운터. 기록은 best-effort UPSERT (database/usage.py).
CREATE TABLE IF NOT EXISTS report_usage_daily (
    day     TEXT NOT NULL,           -- 'YYYY-MM-DD' (서버 localtime)
    kind    TEXT NOT NULL,           -- honey_run | web_index | web_view
    user_id TEXT NOT NULL,           -- 소문자 계정. 신원 없으면 'ip:<addr>'
    count   INTEGER NOT NULL DEFAULT 0,
    last_at INTEGER NOT NULL,
    PRIMARY KEY (day, kind, user_id)
);

-- 접속 사용량 시간별 집계 — 위 일별 테이블과 같은 이벤트를 시각(0~23) 축으로도 남긴다.
-- 일별 테이블의 day 는 'YYYY-MM-DD' 문자열이라 시간대 분포를 복원할 수 없고, 그 PK 를
-- 바꾸면 이미 쌓인 행이 무의미해지므로 별도 테이블을 둔다. 기록은 record_usage 가 함께 한다.
CREATE TABLE IF NOT EXISTS report_usage_hourly (
    day     TEXT NOT NULL,           -- 'YYYY-MM-DD' (서버 localtime)
    hour    INTEGER NOT NULL,        -- 0~23 (서버 localtime)
    kind    TEXT NOT NULL,           -- honey_run | web_index | web_view
    user_id TEXT NOT NULL,           -- 소문자 계정. 신원 없으면 'ip:<addr>'
    count   INTEGER NOT NULL DEFAULT 0,
    last_at INTEGER NOT NULL,
    PRIMARY KEY (day, hour, kind, user_id)
);

-- 일별 Peak 동시 접속자 수 — metrics.active_users() 의 사람 수는 프로세스 메모리에만 있어
-- 지금까지 이력이 남지 않았다. 리소스 샘플러(10초)가 그날 최대치를 갱신한다. 값은 절대
-- **낮아지지 않는다**(서버 재시작으로 메모리 최대치가 0 이 되어도 MAX 로 막는다).
-- window_sec 를 함께 남기는 이유는 '동시'의 정의(최근 N초 안에 요청)가 env 로 바뀔 수 있어
-- 나중에 과거 값을 해석하려면 그때의 기준이 필요하기 때문이다.
CREATE TABLE IF NOT EXISTS report_usage_peak_daily (
    day        TEXT PRIMARY KEY,     -- 'YYYY-MM-DD' (서버 localtime)
    peak_users INTEGER NOT NULL,     -- 그날 동시 접속자(사람) 최대값
    peak_at    INTEGER NOT NULL,     -- 최대값을 찍은 시각 (epoch)
    window_sec INTEGER NOT NULL,     -- 그때의 '동시' 판정 창 (ACTIVE_USER_WINDOW_SEC)
    updated_at INTEGER NOT NULL
);

-- ── 2026-08-14 세션 DB 개선 (Expand 단계 — 신규 테이블만 추가, 기존 경로 무변경) ──

-- analysis_key 단위 **물리 원본 상태**. dedup 형제 세션이 각자 report_session.content_hash
-- 를 들고 있어 같은 산출물인데 값이 갈리는 일이 있었다(실측 1건). 원본의 진실을 여기 한 곳에
-- 두고 형제가 공유한다. report_session.content_hash 는 rollback 대비로 계속 동기화한다.
CREATE TABLE IF NOT EXISTS report_analysis (
    analysis_key   TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL,        -- authoritative (parquet 실체 기준)
    source         TEXT,                 -- 'web_report' | 'xlsx_upload'
    source_count   INTEGER NOT NULL DEFAULT 0,
    artifact_status TEXT NOT NULL DEFAULT 'ok',  -- ok | missing | pending
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    last_access_at INTEGER
);

-- 세션 단위 **큰 본문**의 객체 저장 포인터. "조회·조인하지 않는 본문"만 여기로 나간다
-- (현재 kind=note_sheet — Luckysheet 시트 JSON, 이미지가 base64 로 들어와 최대 10MB).
-- 작은 편집값(comment/status/chart_note 등)은 계속 report_webreport_edit 에 둔다.
-- base_token 은 Note 낙관적 잠금 base(sha1 16자, webreport_edits.note_base_token)를
-- 그대로 보존한다 — 이 컬럼이 없으면 충돌 검사 때마다 본문 전체를 로드해야 한다.
CREATE TABLE IF NOT EXISTS report_session_blob (
    session_id      TEXT NOT NULL,
    kind            TEXT NOT NULL,        -- note_sheet
    backend         TEXT NOT NULL,        -- s3 | local | local_pending(S3 이관 대기)
    object_key      TEXT NOT NULL,        -- backend 공통 상대 키
    content_hash    TEXT NOT NULL,        -- 본문 sha256 (무결성 검증)
    base_token      TEXT,                 -- 본문 sha1 16자 (낙관적 잠금 base)
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    content_encoding TEXT,                -- gzip
    format_version  INTEGER NOT NULL DEFAULT 1,
    updated_at      INTEGER NOT NULL,
    updated_by      TEXT,
    PRIMARY KEY (session_id, kind)
);

-- 마이그레이션 진행 상태 — 대량 backfill 을 중단 후 **재개**할 수 있게 단계별 cursor 를
-- 남긴다. 부팅 시 대량 작업을 하지 않는 것이 원칙이라(서버 기동 지연 = 세션 미오픈),
-- backfill 은 server/tools/migrate_session_db.py 로 따로 돌리고 그 진행을 여기 기록한다.
CREATE TABLE IF NOT EXISTS report_schema_migration (
    step        TEXT PRIMARY KEY,
    status      TEXT NOT NULL,            -- pending | running | done | failed
    cursor      TEXT,                     -- 재개 지점 (단계별 의미)
    detail      TEXT,
    updated_at  INTEGER NOT NULL
);

-- Honey 클라이언트 버전 대장 — 사람 1명 = 1행, "마지막으로 실행한 Honey 버전".
-- 소스는 앱 시작 시 1회 오는 GET /honey/version 의 UA 토큰(HoneyVer/<버전>) 뿐이라
-- 쓰기는 사람당 하루 몇 건이다. 버전 토큰을 안 보내는 구버전 클라는 행이 생기지 않거나
-- 옛 값이 그대로 남는데, 그 자체가 '아직 업데이트하지 않은 사람' 신호다(last_at 으로 구분).
CREATE TABLE IF NOT EXISTS report_client_version (
    user_id      TEXT PRIMARY KEY,   -- 소문자 계정(identity_norm). 신원 없으면 'ip:<addr>'
    version      TEXT NOT NULL,      -- 마지막으로 확인된 클라 버전
    prev_version TEXT,               -- 직전 버전 (업데이트가 실제로 일어났는지 확인용)
    first_at     INTEGER NOT NULL,   -- 이 버전을 처음 본 시각 (버전이 바뀌면 갱신)
    last_at      INTEGER NOT NULL,   -- 마지막 실행 시각
    runs         INTEGER NOT NULL DEFAULT 0,  -- 이 버전으로 실행한 횟수
    updated_at   INTEGER NOT NULL
);

-- 챗봇 일별 비식별 집계 — 질문/답변 전문은 보존기간(기본 90일) 후 삭제하지만 사용 추이와
-- 부하 지표는 계속 필요하다. 원문 purge 직전에 이 표로 접어 넣는다.
CREATE TABLE IF NOT EXISTS report_chatbot_daily (
    day           TEXT NOT NULL,          -- 'YYYY-MM-DD'
    intent        TEXT NOT NULL DEFAULT '',
    planner       TEXT NOT NULL DEFAULT '',
    result        TEXT NOT NULL DEFAULT '',
    cnt           INTEGER NOT NULL DEFAULT 0,
    total_ms_sum  INTEGER NOT NULL DEFAULT 0,
    wait_ms_sum   INTEGER NOT NULL DEFAULT 0,
    llm_ms_sum    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, intent, planner, result)
);

-- eval 룰 엔진 일별 지표 (2026-08-19). 원재료는 eval.db 에 계속 남지만, 지금까지
-- 정확도·커버리지는 **관리자가 탭을 열 때 전체 누적 한 숫자**로만 나와 "좋아지고 있나"를
-- 볼 수 없었다. 여기 접어 두면 3개월 추이가 남는다.
-- ⚠ 집계는 **재계산 UPSERT**(누적 더하기가 아니다) — 원본이 남아 있어 같은 날을 몇 번
--   다시 집계해도 같은 값이 나와야 한다(report_chatbot_daily 와 반대 규약이니 주의).
-- engine_version 을 키에 두는 이유: 룰 버전이 다르면 UNKNOWN 비율·발화 분포가 달라
--   섞으면 추이가 거짓말을 한다. 판정과 무관한 집계(사람 코멘트 라벨 수)는 ''.
CREATE TABLE IF NOT EXISTS report_eval_daily (
    day             TEXT NOT NULL,          -- 'YYYY-MM-DD' (서버 localtime)
    engine_version  TEXT NOT NULL DEFAULT '',
    -- 스냅샷 축적 (ingest_run.created_at 기준)
    runs            INTEGER NOT NULL DEFAULT 0,
    cases           INTEGER NOT NULL DEFAULT 0,
    fail_cases      INTEGER NOT NULL DEFAULT 0,  -- fail_count>0 = UNKNOWN 비율의 분모
    unknown_cases   INTEGER NOT NULL DEFAULT 0,  -- primary 가 없거나 UNKNOWN
    -- signature 확정(✓, labeler='web-signature') vs 엔진 발화 대조
    sig_labeled     INTEGER NOT NULL DEFAULT 0,  -- 스냅샷 case 와 짝이 맞은 확정 라벨 수
    sig_exact       INTEGER NOT NULL DEFAULT 0,  -- 사람 지목 == 엔진 발화 집합
    sig_overlap     INTEGER NOT NULL DEFAULT 0,  -- 교집합 1개 이상(부분 일치 포함)
    -- 사람 코멘트(Status=Close) 라벨 — matched 는 엔진 판정과 case 가 이어진 수.
    -- 이 둘의 비가 곧 case_id 정합 건강도다(2026-08-19 이전에는 구조적으로 0 이었다).
    comment_labels  INTEGER NOT NULL DEFAULT 0,
    comment_matched INTEGER NOT NULL DEFAULT 0,
    -- status 정답 라벨 채점 (labeler='eval-panel')
    score_pairs     INTEGER NOT NULL DEFAULT 0,
    score_agree     INTEGER NOT NULL DEFAULT 0,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (day, engine_version)
);
"""

_PRODUCT_TYPE_NAMES = ("MDDI", "PDDI", "PMIC", "SECURITY", "TCON")


def _now():
    return int(time.time())


def _row(row):
    return None if row is None else dict(row)


def _table_exists(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name, column_name):
    if not _table_exists(conn, table_name):
        return False
    return any(r[1] == column_name for r in conn.execute(f"PRAGMA table_info({table_name})"))


# 마이그레이션 스탬프. _migrate 는 존재-probe 방식이라 매번 재실행해도 안전하지만,
# 전량 UPDATE 처럼 "이미 끝났으면 스캔 자체가 낭비"인 단계는 이 버전으로 건너뛴다.
# 새 단계를 추가하면 이 값을 올리고 해당 단계를 버전 비교로 감싼다.
SCHEMA_USER_VERSION = 1


def _migrate_product_type_names(conn):
    for table_name in ("report_session", "report_audit_log"):
        if not _column_exists(conn, table_name, "product_type"):
            continue
        for name in _PRODUCT_TYPE_NAMES:
            conn.execute(
                f"UPDATE {table_name} SET product_type=? WHERE product_type=?",
                (name, name[:2]),
            )


def _migrate(conn):
    """기존 DB 스키마 업그레이드. 빈 DB(테이블 없음) 에서는 no-op — SCHEMA 가 새로 만든다."""

    # report_object_info: 옛 (analysis_key PK) → (id PK + UNIQUE(analysis_key, object_type))
    if _table_exists(conn, "report_object_info"):
        info = conn.execute("PRAGMA table_info(report_object_info)").fetchall()
        col_names = [r[1] for r in info]
        if col_names and "id" not in col_names:
            conn.execute("ALTER TABLE report_object_info RENAME TO _report_object_info_old")
            conn.execute("""
                CREATE TABLE report_object_info (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    analysis_key  TEXT NOT NULL,
                    object_type   TEXT NOT NULL,
                    content_hash  TEXT NOT NULL,
                    options_json  TEXT NOT NULL,
                    s3_bucket     TEXT,
                    s3_key        TEXT NOT NULL,
                    s3_uri        TEXT,
                    created_at    INTEGER NOT NULL,
                    last_accessed INTEGER,
                    UNIQUE(analysis_key, object_type)
                )
            """)
            conn.execute("""
                INSERT INTO report_object_info
                    (analysis_key, object_type, content_hash, options_json,
                     s3_bucket, s3_key, s3_uri, created_at, last_accessed)
                SELECT analysis_key, object_type, content_hash, options_json,
                       s3_bucket, s3_key, s3_uri, created_at, last_accessed
                FROM _report_object_info_old
            """)
            conn.execute("DROP TABLE _report_object_info_old")

    # report_session: 추가 컬럼들
    if _table_exists(conn, "report_session"):
        sess_info = conn.execute("PRAGMA table_info(report_session)").fetchall()
        sess_cols = {r[1] for r in sess_info}
        for col in (
            "analysis_key", "content_hash", "error_message",
            "product_type", "family_product", "process", "product", "revision", "edm_link",
            "dataset_id", "lot_id", "password", "uploaded_by", "client_host",
            "webreport_options",
            "part_id", "sub_part_id", "product_group", "wf_size", "chip_size_x",
            "chip_size_y", "gross_die", "pkg_type", "e2f_fab_site", "step",
            "temperature", "equip", "para", "flat_zone",
        ):
            if col not in sess_cols:
                conn.execute(f"ALTER TABLE report_session ADD COLUMN {col} TEXT")
        if "is_debug" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN is_debug INTEGER DEFAULT 0")
        if "source" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN source TEXT DEFAULT 'xlsx_upload'")
        if "is_important" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN is_important INTEGER DEFAULT 0")
        if "is_private" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN is_private INTEGER DEFAULT 0")
        if "mode" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN mode TEXT DEFAULT 'Normal'")
        # 휴지통(soft delete) 컬럼 — deleted_at 은 INTEGER(epoch), deleted_by 는 삭제자 계정.
        if "deleted_at" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN deleted_at INTEGER")
        if "deleted_by" not in sess_cols:
            conn.execute("ALTER TABLE report_session ADD COLUMN deleted_by TEXT")

    if not _table_exists(conn, "report_sheet_data"):
        conn.execute("""
            CREATE TABLE report_sheet_data (
                analysis_key TEXT NOT NULL,
                sheet_name   TEXT NOT NULL,
                data_json    TEXT NOT NULL,
                updated_at   INTEGER NOT NULL,
                PRIMARY KEY (analysis_key, sheet_name)
            )
        """)

    # report_audit_log: 클라이언트 신고 신원 컬럼 (기존 DB 는 ALTER 필요)
    if _table_exists(conn, "report_audit_log"):
        audit_cols = {r[1] for r in conn.execute("PRAGMA table_info(report_audit_log)")}
        for col in ("client_user", "client_host"):
            if col not in audit_cols:
                conn.execute(f"ALTER TABLE report_audit_log ADD COLUMN {col} TEXT")

    # report_chatbot_log: 실패 상세(traceback) 컬럼 — 관리자 Chatbot 탭이 예외 클래스만
    # 보여주던 것을 원인까지 보여주려고 추가(2026-08-10). 그 이전에 만들어진 표는 ALTER.
    if _table_exists(conn, "report_chatbot_log"):
        chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(report_chatbot_log)")}
        if "error_detail" not in chat_cols:
            conn.execute("ALTER TABLE report_chatbot_log ADD COLUMN error_detail TEXT")

    # report_webreport_edit_rev: payload 전용 rev 컬럼 (2026-08-14).
    # ⚠️ 기존 행은 payload_rev = rev 로 **초기화한다**. 0 으로 두면 이미 rev 1..N 을
    # 키로 저장된 옛 report 캐시가 payload_rev 가 그 값에 다시 도달할 때 되살아나
    # 현재 편집이 반영 안 된 화면이 나간다(그리고 배포 즉시 전 세션이 콜드가 된다).
    # rev 를 그대로 물려받으면 키가 안 바뀌어 무효화도, 부활도 없다.
    if _table_exists(conn, "report_webreport_edit_rev"):
        rev_cols = {r[1] for r in conn.execute("PRAGMA table_info(report_webreport_edit_rev)")}
        if "payload_rev" not in rev_cols:
            conn.execute("ALTER TABLE report_webreport_edit_rev "
                         "ADD COLUMN payload_rev INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE report_webreport_edit_rev SET payload_rev=rev")

    # 편집 권한 위임 / web_report 방문자 / 사용자별 개인 중요표시 (기존 DB 에도 생성)
    if not _table_exists(conn, "report_session_editor"):
        conn.execute("""
            CREATE TABLE report_session_editor (
                session_id  TEXT NOT NULL,
                editor_user TEXT NOT NULL,
                granted_by  TEXT,
                granted_at  INTEGER NOT NULL,
                PRIMARY KEY (session_id, editor_user)
            )
        """)
    if not _table_exists(conn, "report_web_visitor"):
        conn.execute("""
            CREATE TABLE report_web_visitor (
                user_id    TEXT PRIMARY KEY,
                first_seen INTEGER NOT NULL,
                last_seen  INTEGER NOT NULL
            )
        """)
    if not _table_exists(conn, "report_user_important"):
        conn.execute("""
            CREATE TABLE report_user_important (
                user_id    TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, session_id)
            )
        """)
    # 웹 로그인 계정 (singleID + PIN 4자리). SCHEMA 에는 있으나 구 DB 에는 없을 수 있어
    # 여기서도 생성한다 — 일반 브라우저 로그인이 이 테이블을 사용한다.
    if not _table_exists(conn, "report_user"):
        conn.execute("""
            CREATE TABLE report_user (
                user_id       TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                created_at    INTEGER NOT NULL
            )
        """)

    # 2자리 legacy product_type 치환은 1회성 전량 UPDATE 다. 인덱스가 없는 report_audit_log
    # (최대 365일치)를 코드당 1회 풀스캔하므로, 스탬프가 찍힌 DB 에서는 건너뛴다.
    if _user_version(conn) < 1:
        _migrate_product_type_names(conn)


def _user_version(conn):
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def init_report_db():
    REPORT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(REPORT_DB_PATH) as conn:
        # PRAGMA 를 DDL 앞에 건다 — busy_timeout 없이 ALTER/CREATE INDEX 가 돌면 부팅 시
        # 다른 커넥션과 겹칠 때 대기 없이 "database is locked" 로 실패한다. journal_mode 는
        # 트랜잭션 중 변경 불가라 첫 DML 이전이 유일하게 안전한 위치.
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        _migrate(conn)
        conn.executescript(SCHEMA)
        # 스키마·마이그레이션이 모두 끝난 뒤에만 스탬프를 올린다(중간 실패 시 다음 기동에서 재시도).
        if _user_version(conn) < SCHEMA_USER_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_USER_VERSION}")


@contextmanager
def get_conn(busy_timeout_ms=5000):
    """요청별 DB 커넥션.

    busy_timeout_ms 는 기본 쓰기 경합 대기 상한이다. 응답을 막으면 안 되는 best-effort
    기록(VOC 감사 등)은 짧은 값을 넘겨 빠르게 포기할 수 있다.
    """
    busy_timeout_ms = max(0, int(busy_timeout_ms))
    conn = sqlite3.connect(REPORT_DB_PATH, timeout=busy_timeout_ms / 1000.0)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    # synchronous/temp_store 는 커넥션 단위 설정이라 init_report_db 만으로는 적용되지 않는다
    # (WAL 은 DB 파일 영속). 미설정 시 요청 커넥션이 synchronous=FULL 로 동작해 쓰기가 느려짐.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── analysis lock ─────────────────────────────────────────────────────────────

def try_acquire_analysis_lock(analysis_key, owner):
    now = _now()
    expires = now + REPORT_LOCK_TTL_SEC
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM report_analysis_lock WHERE expires_at <= ?", (now,)
        )
        try:
            conn.execute(
                "INSERT INTO report_analysis_lock "
                "(analysis_key, owner, locked_at, expires_at) VALUES (?, ?, ?, ?)",
                (analysis_key, owner, now, expires),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def release_analysis_lock(analysis_key, owner):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM report_analysis_lock WHERE analysis_key=? AND owner=?",
            (analysis_key, owner),
        )
