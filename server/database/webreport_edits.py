"""web_report 편집 상태 (세션 단위 — web_report/edits.py 가 소비)
(report_db facade 구현).

2026-08-14 — Note 시트(kind=note_sheet)의 **본문**은 객체 저장(session blob)으로 나갔다.
이 파일은 그 전환의 호환 계층이다: 저장은 blob + legacy 행 **dual-write**, 읽기는 blob
우선 + legacy 폴백. 어느 시점에 구 코드로 되돌려도 legacy 행만으로 완전히 동작한다.
"""
import gzip
import hashlib
import logging

from .core import get_conn, _now

_log = logging.getLogger(__name__)

NOTE_SHEET_KIND = "note_sheet"
NOTE_SHEET_ITEM_KEY = "sheet"


def note_base_token(blob):
    """Note 시트 blob 의 낙관적 잠금 base 토큰 (없으면 None).

    세션 전역 rev 는 다른 채널 저장에도 증가해 오탐이 나고, updated_at 은 초 단위라
    같은 1초 안의 stale write 를 놓친다. 내용 해시는 타이밍과 무관하며 유일한 미탐이
    '동일 내용 저장'(= 무손실)뿐이다."""
    if blob is None:
        return None
    return hashlib.sha1(str(blob).encode("utf-8")).hexdigest()[:16]


# report payload 계산에 **들어가지 않는** 편집 kind — /full 조립 단계에서만 붙는다
# (routes_session extras). 이것들만 저장하면 payload_rev 를 올리지 않아 report 캐시가
# 살아남는다. 여기 없는 kind 는 전부 payload 영향으로 간주한다(모르는 kind 가 생기면
# 캐시를 유지하는 쪽보다 무효화하는 쪽이 안전 — 틀린 화면을 서빙하지 않는다).
PAYLOAD_NEUTRAL_KINDS = ("chart_note", "note_sheet", "note_tag", "dist_composite",
                        "gap_chart")


# ── Note 본문 blob 헬퍼 (본문만 객체 저장으로 — 포인터는 report_session_blob) ──
#
# storage_gateway 는 `from database import report_db` 를 module 최상단에서 하므로 여기서
# 맞import 하면 순환이 된다. 전부 함수 안에서 지연 import 한다.

def _blob_bytes(blob):
    """본문 str → (gzip bytes, sha256 hex). 압축은 잠금 밖에서 미리 끝내둔다."""
    raw = str(blob).encode("utf-8")
    return gzip.compress(raw, 6), hashlib.sha256(raw).hexdigest()


def _load_note_blob(session_id):
    """포인터가 있으면 본문 str 을 돌려준다. 없거나 검증 실패면 None (legacy 폴백).

    본문 유실을 조용히 '빈 Note' 로 보이게 하지 않으려고 실패는 항상 로그로 남긴다."""
    from .session_blobs import get_session_blob
    ptr = get_session_blob(session_id, NOTE_SHEET_KIND)
    if not ptr:
        return None
    try:
        import storage_gateway
        data = storage_gateway.load_session_blob(ptr["backend"], ptr["object_key"])
        raw = gzip.decompress(data) if (ptr.get("content_encoding") == "gzip") else data
        if hashlib.sha256(raw).hexdigest() != ptr["content_hash"]:
            _log.error("note blob 해시 불일치 (session=%s key=%s) — legacy 폴백",
                       session_id, ptr["object_key"])
            return None
        return raw.decode("utf-8")
    except Exception:
        _log.warning("note blob 로드 실패 (session=%s) — legacy 폴백", session_id,
                     exc_info=True)
        return None


def _apply_note_blob(session_id, rows):
    """조회 결과의 note_sheet 행 본문을 blob 값으로 치환 (포인터가 있을 때만).

    legacy 행이 없어도(cutover 이후) 같은 모양의 행을 만들어 넣어, 소비자
    (web_report/edits.load_note_sheet 등)는 이 전환을 몰라도 된다."""
    from .session_blobs import get_session_blob
    ptr = get_session_blob(session_id, NOTE_SHEET_KIND)
    if not ptr:
        return rows
    body = _load_note_blob(session_id)
    if body is None:
        return rows  # legacy 행이 있으면 그것이 서빙된다 (무손실)
    for row in rows:
        if row.get("kind") == NOTE_SHEET_KIND and row.get("item_key") == NOTE_SHEET_ITEM_KEY:
            row["value"] = body
            return rows
    rows.append({"kind": NOTE_SHEET_KIND, "item_key": NOTE_SHEET_ITEM_KEY,
                 "value": body, "updated_at": ptr.get("updated_at") or 0,
                 "updated_by": ptr.get("updated_by") or ""})
    return rows


def get_webreport_edit_rev(session_id, payload=False):
    """세션 편집 rev (없으면 0). 캐시 키의 무효화 토큰.

    payload=True 면 **report payload 에 영향을 준 편집만** 센 payload_rev 를 준다
    (Note 시트·차트 주석 편집으로 리포트 전체가 콜드가 되지 않게 — core.py 스키마 주석).
    """
    col = "payload_rev" if payload else "rev"
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {col} AS v FROM report_webreport_edit_rev WHERE session_id=?",
            (session_id,)).fetchone()
    return int(row["v"]) if row else 0


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
    rows = [dict(r) for r in rows]
    # note_sheet 를 실제로 요청한 조회에서만 본문을 붙인다 — 기본 조회(kinds=None)도
    # 대상이지만, exclude_kinds 로 걸러낸 표 상태 조회는 종전대로 본문을 건드리지 않는다.
    wants_note = (not kinds or NOTE_SHEET_KIND in kinds) and \
                 (not exclude_kinds or NOTE_SHEET_KIND not in exclude_kinds)
    if wants_note:
        rows = _apply_note_blob(session_id, rows)
    return rows


def get_webreport_edit_meta(session_id, kind):
    """kind 의 편집행 메타만 [(item_key, updated_at, updated_by)] — value 를 읽지 않는다.

    note_sheet(본문은 객체 저장) 존재 여부/최종 수정자를 /full extras 가 매 요청
    조회하는 용도. 본문이 blob 으로만 있는 세션도 같은 모양으로 답한다."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT item_key, updated_at, updated_by FROM report_webreport_edit "
            "WHERE session_id=? AND kind=? ORDER BY rowid",
            (session_id, kind)).fetchall()
    rows = [dict(r) for r in rows]
    if kind == NOTE_SHEET_KIND and not rows:
        from .session_blobs import get_session_blob
        ptr = get_session_blob(session_id, NOTE_SHEET_KIND)
        if ptr:
            rows = [{"item_key": NOTE_SHEET_ITEM_KEY,
                     "updated_at": ptr.get("updated_at") or 0,
                     "updated_by": ptr.get("updated_by") or ""}]
    return rows


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
        # payload_rev 는 report payload 를 실제로 바꾸는 kind 가 하나라도 있을 때만 올린다.
        bump = 1 if any(k not in PAYLOAD_NEUTRAL_KINDS for k, _, _ in changes) else 0
        conn.execute(
            "INSERT INTO report_webreport_edit_rev (session_id, rev, payload_rev) "
            "VALUES (?, 1, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET rev=rev+1, payload_rev=payload_rev+?",
            (session_id, bump, bump))
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
    묶는다. rev 증가는 /full 의 note_info 와 응답 캐시 무효화에 계속 필요하다.

    본문은 객체 저장(session blob)에 두고 포인터만 DB 에 남기지만, 전환 기간에는 legacy
    행(report_webreport_edit.value)에도 **함께 쓴다**(dual-write). 객체 저장이 통째로
    실패해도 legacy 행 저장은 그대로 진행한다 — 사용자 입력을 잃는 것보다 낫다."""
    from .session_blobs import delete_session_blob_row, upsert_session_blob
    now = _now()
    stored = None          # 이번에 객체 저장에 쓴 결과 (실패 시 None → legacy 만)
    content_hash = None
    if blob is not None and kind == NOTE_SHEET_KIND:
        try:
            import storage_gateway
            gz, content_hash = _blob_bytes(blob)
            stored = storage_gateway.save_session_blob(session_id, kind, content_hash, gz)
        except Exception:
            _log.warning("note blob 저장 실패 (session=%s) — legacy 행만 기록",
                         session_id, exc_info=True)
            stored = None
    prev = None            # 직전 포인터 (옛 객체 정리용) — 잠금 안에서 읽는다
    abandoned = None       # 잠금 검사에서 밀린 경우 방금 쓴 객체를 되돌린다
    conflict = None        # None 이 아니면 낙관적 잠금에 걸린 것 (쓰기 없음)
    result = None

    with get_conn() as conn:
        # python sqlite3 는 SELECT 앞에서 트랜잭션을 열지 않아, 그대로 두면 검사와 쓰기
        # 사이에 남의 저장이 끼어들 수 있다. BEGIN IMMEDIATE 로 쓰기 잠금을 먼저 잡는다.
        # 포인터도 **이 잠금 안에서** 읽는다 — 밖에서 읽으면 그 사이 남의 저장이 끼어들어
        # 낙관적 잠금 판정이 옛 값 기준이 된다.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value, updated_at, updated_by FROM report_webreport_edit "
            "WHERE session_id=? AND kind=? AND item_key=?",
            (session_id, kind, item_key)).fetchone()
        if kind == NOTE_SHEET_KIND:
            prev_row = conn.execute(
                "SELECT backend, object_key, base_token, updated_at, updated_by "
                "FROM report_session_blob WHERE session_id=? AND kind=?",
                (session_id, kind)).fetchone()
            prev = dict(prev_row) if prev_row else None
        # legacy 행이 있으면 종전과 완전히 같은 계산. cutover 로 legacy 행이 사라진 뒤에는
        # 포인터에 보관해 둔 base_token 을 쓴다(본문을 로드하지 않고 같은 값을 얻는다).
        if row:
            cur_base = note_base_token(row["value"])
        elif prev:
            cur_base = prev.get("base_token")
        else:
            cur_base = None
        if check and not force and cur_base != base:
            if stored and (not prev or prev.get("object_key") != stored["object_key"]):
                abandoned = stored
            conflict = {"base": cur_base,
                        "updated_at": (row["updated_at"] if row else
                                       (prev or {}).get("updated_at", 0)) or 0,
                        "updated_by": (row["updated_by"] if row else
                                       (prev or {}).get("updated_by", "")) or ""}
        elif blob is None:
            conn.execute(
                "DELETE FROM report_webreport_edit "
                "WHERE session_id=? AND kind=? AND item_key=?",
                (session_id, kind, item_key))
            if kind == NOTE_SHEET_KIND:
                delete_session_blob_row(session_id, kind, conn=conn)
        else:
            conn.execute(
                "INSERT INTO report_webreport_edit "
                "(session_id, kind, item_key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, kind, item_key) DO UPDATE SET "
                "  value=excluded.value, updated_at=excluded.updated_at, "
                "  updated_by=excluded.updated_by",
                (session_id, kind, item_key, str(blob), now, updated_by))
            if stored:
                upsert_session_blob(
                    session_id, kind, backend=stored["backend"],
                    object_key=stored["object_key"], content_hash=content_hash,
                    base_token=note_base_token(blob), size_bytes=stored["size_bytes"],
                    content_encoding="gzip", updated_by=updated_by, conn=conn)
        if conflict is None:
            conn.execute(
                "INSERT INTO report_webreport_edit_rev (session_id, rev) VALUES (?, 1) "
                "ON CONFLICT(session_id) DO UPDATE SET rev=rev+1",
                (session_id,))
            rev_row = conn.execute(
                "SELECT rev FROM report_webreport_edit_rev WHERE session_id=?",
                (session_id,)).fetchone()
            result = (True, {"rev": int(rev_row["rev"]) if rev_row else 0,
                             "base": note_base_token(blob)})

    # 커밋 이후에만 옛 객체를 지운다 — 순서를 바꾸면 커밋 실패 시 본문이 사라진다.
    if abandoned:
        _drop_blob(abandoned["backend"], abandoned["object_key"])
    if conflict is not None:
        return False, conflict
    if blob is None and prev:
        _drop_blob(prev["backend"], prev["object_key"])
    elif stored and prev and prev["object_key"] != stored["object_key"]:
        _drop_blob(prev["backend"], prev["object_key"])
    return result


def _drop_blob(backend, object_key):
    """옛 본문 객체 제거 (best-effort — 실패해도 조회는 새 포인터를 본다)."""
    try:
        import storage_gateway
        storage_gateway.delete_session_blob(backend, object_key)
    except Exception:
        _log.warning("note blob 삭제 실패 (%s)", object_key, exc_info=True)
