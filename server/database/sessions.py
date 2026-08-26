"""report_session CRUD + 검색결과 히스토리 + retention 조회 (report_db facade 구현)."""
from .core import get_conn, _now, _row, _PRODUCT_TYPE_NAMES
from .models import Session


# product_info.db lookup 으로 채우는 세션 기준정보 컬럼(화이트리스트 — 오타/미지 키 차단).
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
    # password(4자리 PIN)는 2026-08-14 폐지 — 접근제어에 쓰이지 않은 지 오래인데 평문으로
    # 남아 있었다. 인자는 호출부 호환을 위해 받되 **저장하지 않는다**(항상 NULL).
    password = None
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
        # 큰 본문 포인터 — 실제 객체 삭제는 호출부(storage_gateway.delete_session_blobs)가
        # 이 행을 읽어 먼저 수행한다. 여기서는 포인터만 지운다.
        conn.execute("DELETE FROM report_session_blob WHERE session_id=?", (session_id,))
        # 세션을 참조하는 사용자별 부가 테이블 — 안 지우면 purge 후 영구 고아로 남는다.
        conn.execute("DELETE FROM report_user_favorite WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM report_user_important WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM report_session_editor WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM report_session WHERE session_id=?", (session_id,))


def trash_session(session_id, deleted_by=None):
    """세션을 휴지통으로 이동(soft delete) — deleted_at/deleted_by 기록. 산출물·DB 행은
    유지한다. 실제 산출물/DB 정리는 30일(REPORT_TRASH_RETENTION_DAYS) 경과 후 purge —
    관리자 수동 실행 또는 cleanup 스케줄러(REPORT_CLEANUP_DRYRUN 존중)."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE report_session SET deleted_at=?, deleted_by=?, updated_at=? "
            "WHERE session_id=?",
            (now, deleted_by, now, session_id),
        )
    return now


def restore_session(session_id):
    """휴지통 세션 복원 — deleted_at/deleted_by 를 비운다."""
    now = _now()
    with get_conn() as conn:
        conn.execute(
            "UPDATE report_session SET deleted_at=NULL, deleted_by=NULL, updated_at=? "
            "WHERE session_id=?",
            (now, session_id),
        )


def get_trashed_sessions(before_epoch=None):
    """휴지통(deleted_at NOT NULL) 세션 목록 — 내부 관리용 조회(관리자 purge/복원).

    before_epoch 지정 시 deleted_at 이 그 이전인 것만(=purge 경과분 대상). deleted_at 오름차순."""
    sql = ("SELECT session_id, analysis_key, product_type, product, lot_id, file_name, "
           "       created_at, deleted_at, deleted_by "
           "FROM report_session WHERE deleted_at IS NOT NULL")
    params = []
    if before_epoch is not None:
        sql += " AND deleted_at <= ?"
        params.append(int(before_epoch))
    sql += " ORDER BY deleted_at ASC"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


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


def session_ids_for_analysis_key(analysis_key):
    """analysis_key 를 공유하는 세션 id 목록 (dedup 형제 포함).

    물리 원본(parquet)이 교체되면 그 원본을 가리키는 **모든** 세션의 행 위치 기반 상태
    (전처리 셀 패치)가 무효가 된다 — update_content_hash_for_analysis_key 와 같은 이유로
    형제 전체를 대상으로 잡아야 한다. 휴지통 세션도 복원 시 정합을 위해 포함한다.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id FROM report_session WHERE analysis_key=?",
            (analysis_key,)).fetchall()
    return [r[0] for r in rows]


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


_SESSION_META_UPDATABLE = ("file_name", "family_product", "product", "lot_id", "process")


def update_session_meta(session_id, meta, product_info=None):
    """세션 메타(이름/Family/Product/LOT/Process) + 기준정보 14컬럼을 갱신한다.

    범용 update_session 의 화이트리스트를 넓히지 않고 별도 함수로 둔다 — 다른 호출부가
    실수로 메타를 덮어쓰는 경로를 만들지 않기 위해서다.

    product_info 는 product_info.lookup() 결과. **미매칭이면 14컬럼을 비운다** — 옛 제품의
    Wafer Size/Gross Die 가 남아 있으면 상단바가 잘못된 기준정보를 계속 보여준다.
    """
    fields = {k: meta[k] for k in _SESSION_META_UPDATABLE if k in meta}
    info = product_info or {}
    cols = list(fields.items()) + [(c, info.get(c) or None) for c in _PRODUCT_INFO_COLUMNS]
    set_sql = ", ".join(f"{c}=?" for c, _ in cols) + ", updated_at=?"
    params = [v for _, v in cols] + [_now(), session_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE report_session SET {set_sql} WHERE session_id=?", params)


def rename_session(session_id, file_name):
    """세션 **표시 이름만** 갱신 (검색결과 목록의 이름 칸 = report_session.file_name).

    ⚠️ 이름 하나 고치려고 ``update_session_meta`` 를 쓰면 안 된다 — 그쪽은 기준정보
    14컬럼을 **항상** 덮어쓰므로(product_info 미지정이면 전부 NULL) Wafer Size/Gross Die
    가 통째로 날아간다. ``update_session`` 도 안 된다(화이트리스트에 file_name 이 없고,
    넓히면 다른 호출부가 실수로 메타를 덮는 경로가 생긴다 — 그 주석 참조).

    이름은 표시 전용이라 analysis_key·산출물·기준정보와 무관하다.
    """
    with get_conn() as conn:
        conn.execute("UPDATE report_session SET file_name=?, updated_at=? WHERE session_id=?",
                     (file_name, _now(), session_id))


def update_content_hash_for_analysis_key(analysis_key, content_hash):
    """같은 analysis_key 를 공유하는 모든 세션의 content_hash 를 일괄 갱신. 반환: 갱신 행 수.

    raw_data 편집은 analysis_key 단위 물리 원본(parquet)을 교체하므로 편집한 세션 1건만
    갱신하면 dedup 형제 세션이 옛 hash 로 남아 disk_cache 의 stale payload 를 계속 서빙한다.
    휴지통(deleted_at) 세션도 같은 원본을 가리키므로 함께 갱신한다(복원 시 정합).
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE report_session SET content_hash=?, updated_at=? WHERE analysis_key=?",
            (content_hash, _now(), analysis_key))
        return cur.rowcount


def get_session(session_id):
    """세션 1건 — models.Session (Mapping 호환: .get/[]/dict() 그대로 동작 + 속성 접근)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM report_session WHERE session_id=?", (session_id,)
        ).fetchone()
    return Session.from_row(row)


# 업로더 비교는 is_uploader(auth_identity)의 'DOMAIN\\user' 뒷부분·소문자 규칙을 SQL 로
# 근사한다 — 실데이터는 단일 '\' 라 INSTR(첫 위치) 기준과 동치. 비공개 필터와 '내 업로드'
# 필터가 같은 규칙을 써야 해서 한 곳에 둔다.
_UPLOADER_MATCH = ("LOWER(TRIM(CASE WHEN INSTR(s.uploaded_by, '\\') > 0"
                   " THEN SUBSTR(s.uploaded_by, INSTR(s.uploaded_by, '\\') + 1)"
                   " ELSE s.uploaded_by END)) = ?")

# 정렬 화이트리스트. 어떤 키를 골라도 session_id 를 마지막에 붙여 offset 페이지 간
# 순서가 흔들리지 않게 한다.
# 목록 열 머리글 클릭 정렬이 이 표를 그대로 쓴다 — 화면은 한 페이지만 들고 있어
# (서버 페이지네이션) JS 배열 정렬로는 전체를 정렬할 수 없기 때문이다. 열마다
# 오름(<key>)/내림(<key>_desc) 두 벌을 둔다. 'new'/'old' 는 Date 열의 두 방향이며
# 종전 URL·저장된 링크 호환을 위해 이름을 그대로 둔다.
_HISTORY_SORTS = {
    "new": "s.created_at DESC",
    "old": "s.created_at ASC",
    "product": "s.product COLLATE NOCASE ASC",
    "product_desc": "s.product COLLATE NOCASE DESC",
    "lot": "s.lot_id COLLATE NOCASE ASC",
    "lot_desc": "s.lot_id COLLATE NOCASE DESC",
    "ptype": "s.product_type COLLATE NOCASE ASC",
    "ptype_desc": "s.product_type COLLATE NOCASE DESC",
    "mode": "COALESCE(s.mode, 'Normal') COLLATE NOCASE ASC",
    "mode_desc": "COALESCE(s.mode, 'Normal') COLLATE NOCASE DESC",
    "fname": "s.file_name COLLATE NOCASE ASC",
    "fname_desc": "s.file_name COLLATE NOCASE DESC",
    "owner": "s.uploaded_by COLLATE NOCASE ASC",
    "owner_desc": "s.uploaded_by COLLATE NOCASE DESC",
}

# 자유 검색어(q)가 훑는 컬럼 — 종전 클라이언트 필터의 haystack 과 동일하게 맞춘다.
_HISTORY_Q_COLUMNS = ("s.source", "s.product_type", "s.family_product", "s.product",
                      "s.lot_id", "s.process", "s.file_name", "s.session_id",
                      "s.uploaded_by", "s.client_host")


def _history_where(product_type=None, process=None, product=None, revision=None,
                   lot_id=None, source=None, viewer=None, q=None, mode=None,
                   date_from=None, date_to=None, mine=False, visibility=None,
                   see_all_private=False):
    """get_history / count_history 공용 WHERE 절 + 파라미터.

    viewer: None=비공개 필터 없음(하위호환·관리자용) / ""=신원 없음(비공개 전부 숨김) /
    "<uid>"=공개 OR legacy(업로더 기록 없음 — is_uploader 규칙) OR 업로더 본인 OR 위임 편집자.
    q: 위 _HISTORY_Q_COLUMNS 전체 대상 부분일치.
    mode: 'Normal'|'Compare'|'DUT'|'Commonality' — web_report 세션에만 의미가 있어
          source='web_report' 조건이 함께 붙는다(종전 클라 필터와 동일).
    date_from/date_to: epoch 초 (to 는 그 시각 이하 포함).
    mine: 참이면 viewer 가 업로더인 세션만. visibility: 'public'|'private'.
    see_all_private: 참이면(master PC) 비공개 숨김 조건을 적용하지 않는다 — viewer 는
                     mine/favorite 판별에만 쓰이고 비공개 세션도 목록에 노출된다.
    """
    # 휴지통(soft delete)된 세션은 일반 목록 조회에서 항상 제외한다.
    conditions = ["s.status IN ('done', 'reused')", "s.deleted_at IS NULL"]
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
    if q:
        like = f"%{q}%"
        conditions.append(
            "(" + " OR ".join(f"{col} LIKE ?" for col in _HISTORY_Q_COLUMNS) + ")")
        params.extend([like] * len(_HISTORY_Q_COLUMNS))
    if mode:
        conditions.append("s.source = 'web_report' AND COALESCE(s.mode, 'Normal') = ?")
        params.append(mode)
    if date_from is not None:
        conditions.append("s.created_at >= ?")
        params.append(int(date_from))
    if date_to is not None:
        conditions.append("s.created_at <= ?")
        params.append(int(date_to))
    if visibility == "public":
        conditions.append("COALESCE(s.is_private, 0) = 0")
    elif visibility == "private":
        conditions.append("COALESCE(s.is_private, 0) = 1")
    if mine:
        # 신원이 없으면 '내 업로드'는 공집합이다 (전체 표시로 흘리지 않는다).
        conditions.append(_UPLOADER_MATCH if viewer else "1=0")
        if viewer:
            params.append(viewer)
    if see_all_private:
        pass  # master PC: 비공개 숨김 미적용 (viewer 는 mine/favorite 에만 사용)
    elif viewer is not None:
        if viewer:
            conditions.append(
                "(COALESCE(s.is_private, 0) = 0"
                " OR s.uploaded_by IS NULL OR s.uploaded_by = ''"
                " OR " + _UPLOADER_MATCH +
                " OR EXISTS (SELECT 1 FROM report_session_editor e"
                " WHERE e.session_id = s.session_id AND e.editor_user = ?))")
            params.extend([viewer, viewer])
        else:
            conditions.append("COALESCE(s.is_private, 0) = 0")
    return " AND ".join(conditions), params


def get_history(product_type=None, process=None, product=None, revision=None, lot_id=None,
                source=None, limit=500, offset=0, viewer=None, q=None, mode=None,
                date_from=None, date_to=None, mine=False, visibility=None, sort="new",
                see_all_private=False):
    where, params = _history_where(product_type, process, product, revision, lot_id, source,
                                   viewer=viewer, q=q, mode=mode, date_from=date_from,
                                   date_to=date_to, mine=mine, visibility=visibility,
                                   see_all_private=see_all_private)
    order_by = _HISTORY_SORTS.get(sort or "new", _HISTORY_SORTS["new"])
    # 즐겨찾기는 어떤 정렬을 골라도 최상단 고정 — 클라이언트가 전량을 들고 있을 때
    # 하던 일을 서버 페이지네이션에서도 유지하려면 정렬 자체에 넣어야 한다
    # (뒤 페이지의 즐겨찾기가 1페이지로 올라오는 종전 동작 보존).
    fav_params = [viewer or ""]
    params = fav_params + params + [limit, offset]
    # session_id 를 마지막 정렬키로 두어 offset 페이지 간 순서가 안정되게 한다
    sql = f"""
        SELECT s.session_id, s.file_name, s.product_type, s.family_product, s.process, s.product,
               s.revision, s.edm_link, s.lot_id, s.created_at, s.status, s.dataset_id,
               s.is_debug, s.source, s.uploaded_by, s.client_host,
               COALESCE(s.mode, 'Normal') AS mode,
               COALESCE(s.is_important, 0) AS is_important,
               COALESCE(s.is_private, 0) AS is_private,
               0 AS has_password,   -- PIN 폐지 (2026-08-14) — 컬럼은 보존, 표기는 항상 false
               CASE WHEN f.session_id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite,
               COALESCE(SUM(c.file_size), 0) AS total_file_size
        FROM report_session s
        LEFT JOIN report_user_favorite f
               ON f.session_id = s.session_id AND f.user_id = ?
        LEFT JOIN report_csv_files c ON c.analysis_key = s.analysis_key
        WHERE {where}
        GROUP BY s.session_id
        ORDER BY is_favorite DESC, COALESCE(s.is_important, 0) DESC, {order_by}, s.session_id
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_history(product_type=None, process=None, product=None, revision=None,
                  lot_id=None, source=None, viewer=None, q=None, mode=None,
                  date_from=None, date_to=None, mine=False, visibility=None,
                  see_all_private=False):
    """get_history 와 동일 필터의 전체 세션 수 (서버 페이지네이션 total 용)."""
    where, params = _history_where(product_type, process, product, revision, lot_id, source,
                                   viewer=viewer, q=q, mode=mode, date_from=date_from,
                                   date_to=date_to, mine=mine, visibility=visibility,
                                   see_all_private=see_all_private)
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM report_session s WHERE {where}", params).fetchone()
    return int(row[0]) if row else 0


def count_by_product_type():
    """제품군별 세션 수 -> {"MDDI": n, ..., "TCON": n}. /pe 랜딩의 현황 수치 전용.

    **카운트 전용이다 — 목록 조회에 쓰지 말 것.** viewer 를 넘기지 않아
    (_history_where 의 viewer=None) 비공개 필터가 걸리지 않는다: 랜딩은 누가 보든
    같은 숫자를 보여야 한다는 요구라 비공개 세션도 수에 포함한다. 숫자만 나가고
    세션 메타는 나가지 않으므로 유출이 아니지만, 같은 값을 목록에 쓰면 비공개
    세션이 그대로 노출된다.

    완료상태·휴지통 제외 규칙은 _history_where 정본을 그대로 따른다.
    DB 에 0건인 제품군도 키를 0 으로 채워 돌려준다 (타일에 '0' 이 떠야 한다).
    """
    where, params = _history_where()
    counts = {name: 0 for name in _PRODUCT_TYPE_NAMES}
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT s.product_type AS pt, COUNT(*) AS n FROM report_session s "
            f"WHERE {where} GROUP BY s.product_type", params).fetchall()
    for r in rows:
        pt = (r["pt"] or "").strip()
        if pt in counts:                 # enum 밖 값(구 데이터)은 제품군 타일에 자리가 없다
            counts[pt] = int(r["n"])
    return counts


def count_recent_activity(days=7):
    """최근 N일 활동 -> {"days": n, "created": x, "updated": y}. /pe 랜딩의 현황 수치 전용.

    created — 그 기간에 새로 만들어진 세션.
    updated — 그 **이전에** 만들어졌는데 기간 안에 내용이 편집된 세션.
    둘은 정의상 겹치지 않으므로 합계를 내도 이중집계가 아니다.

    '내용 편집' 의 기준은 report_webreport_edit.updated_at 이다 — 코멘트·ETC·trim
    override·ENGR·차트주석·Note 시트·전처리 등 web_report 편집 전부가 이 테이블에
    쌓인다(편집 상태의 진실 저장소). 편집 1건만 있어도 잡힌다.

    count_by_product_type 과 같은 이유로 비공개 필터를 걸지 않는다 — 카운트 전용이다.
    """
    cutoff = _now() - int(days) * 86400
    where, params = _history_where()
    with get_conn() as conn:
        created = conn.execute(
            f"SELECT COUNT(*) FROM report_session s WHERE {where} AND s.created_at >= ?",
            params + [cutoff]).fetchone()
        updated = conn.execute(
            f"SELECT COUNT(*) FROM report_session s WHERE {where} AND s.created_at < ? "
            f"AND EXISTS (SELECT 1 FROM report_webreport_edit e "
            f"            WHERE e.session_id = s.session_id AND e.updated_at >= ?)",
            params + [cutoff, cutoff]).fetchone()
    return {"days": int(days),
            "created": int(created[0]) if created else 0,
            "updated": int(updated[0]) if updated else 0}


def get_history_page(product_type=None, process=None, product=None, revision=None,
                     lot_id=None, source=None, limit=500, offset=0, viewer=None, q=None,
                     mode=None, date_from=None, date_to=None, mine=False, visibility=None,
                     sort="new", see_all_private=False):
    """서버 페이지네이션 전용: 목록 + 전체 건수를 커넥션 1개로 조회. -> (rows, total)

    get_history 와 결과 동일하되 total_file_size 는 CSV JOIN/GROUP BY 대신
    idx_report_csv_files_analysis_key 를 타는 상관 집계로 구한다 (favorite JOIN 은
    PK(user_id,session_id)라 팬아웃이 없어 GROUP BY 없이도 세션당 1행)."""
    where, params = _history_where(product_type, process, product, revision, lot_id, source,
                                   viewer=viewer, q=q, mode=mode, date_from=date_from,
                                   date_to=date_to, mine=mine, visibility=visibility,
                                   see_all_private=see_all_private)
    order_by = _HISTORY_SORTS.get(sort or "new", _HISTORY_SORTS["new"])
    sql = f"""
        SELECT s.session_id, s.file_name, s.product_type, s.family_product, s.process, s.product,
               s.revision, s.edm_link, s.lot_id, s.created_at, s.status, s.dataset_id,
               s.is_debug, s.source, s.uploaded_by, s.client_host,
               COALESCE(s.mode, 'Normal') AS mode,
               COALESCE(s.is_important, 0) AS is_important,
               COALESCE(s.is_private, 0) AS is_private,
               0 AS has_password,   -- PIN 폐지 (2026-08-14) — 컬럼은 보존, 표기는 항상 false
               CASE WHEN f.session_id IS NOT NULL THEN 1 ELSE 0 END AS is_favorite,
               (SELECT COALESCE(SUM(c.file_size), 0) FROM report_csv_files c
                 WHERE c.analysis_key = s.analysis_key) AS total_file_size
        FROM report_session s
        LEFT JOIN report_user_favorite f
               ON f.session_id = s.session_id AND f.user_id = ?
        WHERE {where}
        ORDER BY is_favorite DESC, COALESCE(s.is_important, 0) DESC, {order_by}, s.session_id
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, [viewer or ""] + params + [limit, offset]).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM report_session s WHERE {where}", params).fetchone()
    return [dict(r) for r in rows], int(total[0]) if total else 0


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
            "  AND status IN ('done', 'reused') AND deleted_at IS NULL "
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
            "WHERE created_at < ? AND status = 'pending' AND deleted_at IS NULL "
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
        WHERE s.dataset_id = ? AND s.deleted_at IS NULL
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
