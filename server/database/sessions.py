"""report_session CRUD + 검색결과 히스토리 + retention 조회 (report_db facade 구현)."""
from .core import get_conn, _now, _row
from .models import Session


# product_info.csv lookup 으로 채우는 세션 기준정보 컬럼(화이트리스트 — 오타/미지 키 차단).
_PRODUCT_INFO_COLUMNS = (
    "part_id", "sub_part_id", "product_group", "wf_size", "chip_size_x",
    "chip_size_y", "gross_die", "pkg_type", "e2f_fab_site", "step",
    "temperature", "equip", "para", "flat_zone",
)


def create_session(session_id, file_name, file_path, product_type=None, dataset_id=None,
                   lot_id=None, password=None, is_debug=0, product=None,
                   process=None, revision=None, edm_link=None, source='xlsx_upload',
                   uploaded_by=None, client_host=None, mode='Normal', product_info=None,
                   family_product=None):
    now = _now()
    file_path_str = str(file_path) if file_path is not None else None
    # 기준정보(product_info)는 화이트리스트 컬럼 중 값이 있는 것만 동적 병합한다. 컬럼명은
    # 코드 상수에서만 오므로 안전하고, 값만 파라미터 바인딩한다. 없으면 기존 INSERT 와 동일.
    info = product_info or {}
    extra_cols = [c for c in _PRODUCT_INFO_COLUMNS if info.get(c)]
    extra_col_sql = "".join(f"{c}, " for c in extra_cols)
    extra_ph = "?, " * len(extra_cols)
    extra_vals = [info[c] for c in extra_cols]
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO report_session "
            "(session_id, file_name, file_path, product_type, family_product, process, product, revision, "
            " edm_link, dataset_id, lot_id, password, is_debug, source, uploaded_by, client_host, "
            f" mode, {extra_col_sql}status, created_at, updated_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {extra_ph}'pending', ?, ?)",
            (session_id, file_name, file_path_str, product_type, family_product, process, product, revision,
             edm_link, dataset_id, lot_id, password, is_debug, source, uploaded_by, client_host,
             mode or 'Normal', *extra_vals, now, now),
        )


_SESSION_UPDATABLE = {"analysis_key", "content_hash", "status", "error_message", "file_path",
                      "is_important", "is_private", "webreport_options"}


def delete_session(session_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM report_annotation WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM report_webreport_edit WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM report_webreport_edit_rev WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM report_session WHERE session_id=?", (session_id,))


def count_sessions_for_analysis_key(analysis_key, exclude_session_id=None):
    """analysis_key 를 참조하는 세션 수. 삭제 시 산출물 공유 여부 판단용.

    동일 데이터 재업로드는 같은 analysis_key 를 공유하므로, 산출물(S3/로컬 파일·메타 행)은
    마지막 참조 세션을 지울 때만 정리해야 한다.
    """
    with get_conn() as conn:
        if exclude_session_id:
            row = conn.execute(
                "SELECT COUNT(*) FROM report_session WHERE analysis_key=? AND session_id<>?",
                (analysis_key, exclude_session_id)).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM report_session WHERE analysis_key=?",
                (analysis_key,)).fetchone()
    return int(row[0])


def delete_analysis_rows(analysis_key):
    """analysis_key 에 매달린 산출물 메타 행 삭제 (마지막 참조 세션 삭제 시에만 호출).

    report_audit_log 는 의도적으로 보존한다 (삭제 이력 추적용 메타 스냅샷 포함).
    """
    with get_conn() as conn:
        conn.execute("DELETE FROM report_object_info WHERE analysis_key=?", (analysis_key,))
        conn.execute("DELETE FROM report_analysis_summary WHERE analysis_key=?", (analysis_key,))
        conn.execute("DELETE FROM report_sheet_data WHERE analysis_key=?", (analysis_key,))
        conn.execute("DELETE FROM report_csv_files WHERE analysis_key=?", (analysis_key,))


def update_session(session_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _SESSION_UPDATABLE}
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields) + ", updated_at=?"
    params = list(fields.values()) + [_now(), session_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE report_session SET {cols} WHERE session_id=?", params)


def get_session(session_id):
    """세션 1건 — models.Session (Mapping 호환: .get/[]/dict() 그대로 동작 + 속성 접근)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM report_session WHERE session_id=?", (session_id,)
        ).fetchone()
    return Session.from_row(row)


def _history_where(product_type=None, process=None, product=None, revision=None,
                   lot_id=None, source=None, viewer=None):
    """get_history / count_history 공용 WHERE 절 + 파라미터.

    viewer: None=비공개 필터 없음(하위호환·관리자용) / ""=신원 없음(비공개 전부 숨김) /
    "<uid>"=공개 OR legacy(업로더 기록 없음 — is_uploader 규칙) OR 업로더 본인 OR 위임 편집자."""
    conditions = ["s.status IN ('done', 'reused')"]
    params = []
    if product_type:
        conditions.append("s.product_type = ?")
        params.append(product_type)
    if process:
        conditions.append("s.process = ?")
        params.append(process)
    if product:
        conditions.append("s.product = ?")
        params.append(product)
    if revision:
        conditions.append("s.revision = ?")
        params.append(revision)
    if lot_id:
        conditions.append("s.lot_id LIKE ?")
        params.append(f"%{lot_id}%")
    if source:
        conditions.append("s.source = ?")
        params.append(source)
    if viewer is not None:
        if viewer:
            # 업로더 비교는 is_uploader(auth_identity)의 'DOMAIN\user' 뒷부분·소문자
            # 규칙을 SQL 로 근사 — 실데이터는 단일 '\' 라 INSTR(첫 위치) 기준과 동치.
            conditions.append(
                "(COALESCE(s.is_private, 0) = 0"
                " OR s.uploaded_by IS NULL OR s.uploaded_by = ''"
                " OR LOWER(TRIM(CASE WHEN INSTR(s.uploaded_by, '\\') > 0"
                " THEN SUBSTR(s.uploaded_by, INSTR(s.uploaded_by, '\\') + 1)"
                " ELSE s.uploaded_by END)) = ?"
                " OR EXISTS (SELECT 1 FROM report_session_editor e"
                " WHERE e.session_id = s.session_id AND e.editor_user = ?))")
            params.extend([viewer, viewer])
        else:
            conditions.append("COALESCE(s.is_private, 0) = 0")
    return " AND ".join(conditions), params


def get_history(product_type=None, process=None, product=None, revision=None, lot_id=None,
                source=None, limit=500, offset=0, viewer=None):
    where, params = _history_where(product_type, process, product, revision, lot_id, source,
                                   viewer=viewer)
    params.extend([limit, offset])
    # session_id 를 마지막 정렬키로 두어 offset 페이지 간 순서가 안정되게 한다
    sql = f"""
        SELECT s.session_id, s.file_name, s.product_type, s.process, s.product,
               s.revision, s.edm_link, s.lot_id, s.created_at, s.status, s.dataset_id,
               s.is_debug, s.source, s.uploaded_by, s.client_host,
               COALESCE(s.mode, 'Normal') AS mode,
               COALESCE(s.is_important, 0) AS is_important,
               COALESCE(s.is_private, 0) AS is_private,
               CASE WHEN s.password IS NOT NULL THEN 1 ELSE 0 END AS has_password,
               COALESCE(SUM(c.file_size), 0) AS total_file_size
        FROM report_session s
        LEFT JOIN report_csv_files c ON c.analysis_key = s.analysis_key
        WHERE {where}
        GROUP BY s.session_id
        ORDER BY COALESCE(s.is_important, 0) DESC, s.created_at DESC, s.session_id
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_history(product_type=None, process=None, product=None, revision=None,
                  lot_id=None, source=None, viewer=None):
    """get_history 와 동일 필터의 전체 세션 수 (서버 페이지네이션 total 용)."""
    where, params = _history_where(product_type, process, product, revision, lot_id, source,
                                   viewer=viewer)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM report_session s WHERE {where}", params).fetchone()
    return int(row[0]) if row else 0


# ── retention / cleanup ───────────────────────────────────────────────────────

def get_expired_sessions(cutoff_epoch):
    """created_at 이 cutoff 이전이고 중요표시가 없는 세션. 자동정리 대상.

    전역 is_important(legacy) 또는 사용자별 개인 중요표시(report_user_important)가
    하나라도 있으면 보존한다 — 누군가 중요하다고 표시한 데이터는 지우지 않는다."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, analysis_key, product_type, product, lot_id, "
            "       file_name, created_at "
            "FROM report_session "
            "WHERE created_at < ? AND COALESCE(is_important, 0) = 0 "
            "  AND status IN ('done', 'reused') "
            "  AND session_id NOT IN (SELECT session_id FROM report_user_important) "
            "ORDER BY created_at ASC",
            (cutoff_epoch,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_orphan_pending_sessions(cutoff_epoch):
    """ingest 크래시 잔존물 — status='pending' 이고 analysis_key 가 없는 세션.

    create_session 과 update_session(status='done') 사이에서 죽으면 남는다.
    get_expired_sessions 는 status IN ('done','reused') 만 보므로 여기서 별도 회수한다.
    보수적으로 analysis_key 미기록(산출물 참조 없음 — 세션 행만 삭제)만 대상."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, analysis_key, product_type, product, lot_id, "
            "       file_name, created_at "
            "FROM report_session "
            "WHERE created_at < ? AND status = 'pending' "
            "  AND (analysis_key IS NULL OR analysis_key = '') "
            "ORDER BY created_at ASC",
            (cutoff_epoch,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_session_by_dataset_id(dataset_id):
    """dataset_id 로 가장 최근 세션 1건과 총 CSV 크기를 함께 반환."""
    sql = """
        SELECT s.session_id, s.file_name, s.product_type, s.process, s.product,
               s.revision, s.edm_link, s.lot_id, s.created_at, s.status, s.dataset_id, s.analysis_key,
               COALESCE(SUM(c.file_size), 0) AS total_file_size
        FROM report_session s
        LEFT JOIN report_csv_files c ON c.analysis_key = s.analysis_key
        WHERE s.dataset_id = ?
        GROUP BY s.session_id
        ORDER BY s.created_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (dataset_id,)).fetchone()
    return _row(row)


def get_session_path_by_analysis_key(analysis_key):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT file_path FROM report_session "
            "WHERE analysis_key=? AND file_path IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1",
            (analysis_key,),
        ).fetchone()
    return row["file_path"] if row else None
