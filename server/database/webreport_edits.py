"""web_report 편집 상태 (세션 단위 — web_report/edits.py 가 소비)
(report_db facade 구현)."""
import hashlib

from .core import get_conn, _now


def note_base_token(blob):
    """Note 시트 blob 의 낙관적 잠금 base 토큰 (없으면 None).

    세션 전역 rev 는 다른 채널 저장에도 증가해 오탐이 나고, updated_at 은 초 단위라
    같은 1초 안의 stale write 를 놓친다. 내용 해시는 타이밍과 무관하며 유일한 미탐이
    '동일 내용 저장'(= 무손실)뿐이다."""
    if blob is None:
        return None
    return hashlib.sha1(str(blob).encode("utf-8")).hexdigest()[:16]


def get_webreport_edit_rev(session_id):
    """세션 편집 rev (없으면 0). 캐시 키의 무효화 토큰."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT rev FROM report_webreport_edit_rev WHERE session_id=?",
            (session_id,)).fetchone()
    return int(row["rev"]) if row else 0


def get_webreport_edits(session_id, kinds=None, exclude_kinds=None):
    """세션의 편집행 [(kind, item_key, value, updated_at, updated_by)] — rowid(삽입) 순서 보존.

    kinds: 지정 시 해당 kind 만 조회. exclude_kinds: 지정 시 해당 kind 제외 —
    대용량 값(note_sheet 시트 JSON 등)을 표 상태 조회가 매번 끌어오지 않게 한다
    (web_report/edits.py 가 소비). 기본(둘 다 None)은 종전과 동일하게 전부."""
    sql = ("SELECT kind, item_key, value, updated_at, updated_by "
           "FROM report_webreport_edit WHERE session_id=?")
    params = [session_id]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        params.extend(kinds)
    if exclude_kinds:
        sql += " AND kind NOT IN (%s)" % ",".join("?" * len(exclude_kinds))
        params.extend(exclude_kinds)
    sql += " ORDER BY rowid"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_webreport_edit_meta(session_id, kind):
    """kind 의 편집행 메타만 [(item_key, updated_at, updated_by)] — value 를 읽지 않는다.

    note_sheet(최대 10MB) 존재 여부/최종 수정자를 /full extras 가 매 요청 조회하는 용도."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item_key, updated_at, updated_by FROM report_webreport_edit "
            "WHERE session_id=? AND kind=? ORDER BY rowid",
            (session_id, kind)).fetchall()
    return [dict(r) for r in rows]


def apply_webreport_edits(session_id, changes, updated_by=None):
    """changes: [(kind, item_key, value|None)] — None 은 삭제. 단일 트랜잭션으로
    적용하고 rev 를 1 증가시킨다 (빈 changes 는 no-op, rev 유지). 새 rev 반환.

    upsert 는 UPDATE 경로에서 rowid 를 유지하므로 etc_item 표시 순서가 보존된다."""
    if not changes:
        return get_webreport_edit_rev(session_id)
    now = _now()
    with get_conn() as conn:
        for kind, item_key, value in changes:
            if value is None:
                conn.execute(
                    "DELETE FROM report_webreport_edit "
                    "WHERE session_id=? AND kind=? AND item_key=?",
                    (session_id, kind, item_key))
            else:
                conn.execute(
                    "INSERT INTO report_webreport_edit "
                    "(session_id, kind, item_key, value, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(session_id, kind, item_key) DO UPDATE SET "
                    "  value=excluded.value, updated_at=excluded.updated_at, "
                    "  updated_by=excluded.updated_by",
                    (session_id, kind, item_key, str(value), now, updated_by))
        conn.execute(
            "INSERT INTO report_webreport_edit_rev (session_id, rev) VALUES (?, 1) "
            "ON CONFLICT(session_id) DO UPDATE SET rev=rev+1",
            (session_id,))
        row = conn.execute(
            "SELECT rev FROM report_webreport_edit_rev WHERE session_id=?",
            (session_id,)).fetchone()
        return int(row["rev"]) if row else 0


def save_note_sheet_checked(session_id, kind, item_key, blob, base,
                            updated_by=None, check=True, force=False):
    """Note 시트(통째 치환)를 낙관적 잠금과 함께 저장 — (ok, info) 반환.

    check 이고 force 가 아니면, 현재 저장본의 base 토큰이 호출자가 들고 있던 base 와
    다를 때 **쓰기 없이** (False, {"updated_by","updated_at","base"}) 를 반환한다
    (rev 도 올리지 않는다). 현재 행이 없는데 base 가 있거나 그 반대인 경우(신규 작성
    경합)도 같은 규칙으로 잡힌다.

    통과하면 upsert + rev+1 을 **같은 트랜잭션**에서 수행하고 (True, {"rev","base"}) 를
    반환한다. 검사와 쓰기를 분리하면 그 사이에 남의 저장이 끼어들 수 있어 한 트랜잭션에
    묶는다. rev 증가는 /full 의 note_info 와 응답 캐시 무효화에 계속 필요하다."""
    now = _now()
    with get_conn() as conn:
        # python sqlite3 는 SELECT 앞에서 트랜잭션을 열지 않아, 그대로 두면 검사와 쓰기
        # 사이에 남의 저장이 끼어들 수 있다. BEGIN IMMEDIATE 로 쓰기 잠금을 먼저 잡는다.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value, updated_at, updated_by FROM report_webreport_edit "
            "WHERE session_id=? AND kind=? AND item_key=?",
            (session_id, kind, item_key)).fetchone()
        cur_base = note_base_token(row["value"]) if row else None
        if check and not force and cur_base != base:
            return False, {"base": cur_base,
                           "updated_at": (row["updated_at"] if row else 0) or 0,
                           "updated_by": (row["updated_by"] if row else "") or ""}
        if blob is None:
            conn.execute(
                "DELETE FROM report_webreport_edit "
                "WHERE session_id=? AND kind=? AND item_key=?",
                (session_id, kind, item_key))
        else:
            conn.execute(
                "INSERT INTO report_webreport_edit "
                "(session_id, kind, item_key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, kind, item_key) DO UPDATE SET "
                "  value=excluded.value, updated_at=excluded.updated_at, "
                "  updated_by=excluded.updated_by",
                (session_id, kind, item_key, str(blob), now, updated_by))
        conn.execute(
            "INSERT INTO report_webreport_edit_rev (session_id, rev) VALUES (?, 1) "
            "ON CONFLICT(session_id) DO UPDATE SET rev=rev+1",
            (session_id,))
        rev_row = conn.execute(
            "SELECT rev FROM report_webreport_edit_rev WHERE session_id=?",
            (session_id,)).fetchone()
        return True, {"rev": int(rev_row["rev"]) if rev_row else 0,
                      "base": note_base_token(blob)}
