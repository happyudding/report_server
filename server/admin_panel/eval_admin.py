"""관리자 Eval DB 뷰 — Issue Table 코멘트 export DB(eval.db 스키마) 조회/정리.

eval DB 는 report.db(세션 DB)와 분리된 report_server 소유 파일이다
(config.REPORT_EVAL_DB_PATH, 적재는 web_report/eval_export.py — docs/13).
eval_engine 은 여기서 import 하지 않는다(허용 지점 2곳 규약 유지) — 커넥션은
eval_export.open_conn 경유(스키마 보장), 조회/삭제는 직접 SELECT/DELETE.
"""
import csv
import io
import logging
import re
import sys
from pathlib import Path

# web_report 패키지(repo 루트)가 sys.path 에 없을 수 있어(작업 디렉토리 server/)
# report_routes.py 와 동일한 가드로 보강한다.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web_report import eval_export

_log = logging.getLogger(__name__)

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def db_path() -> Path:
    return eval_export.db_path()


def _stat(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _recent_export_failures(days: int = 7) -> int:
    """최근 N일 eval_export 실패 건수 (report.db 감사 로그, created_at=epoch) —
    Eval DB 탭 경고 배지용. 조회 실패는 0 (best-effort)."""
    try:
        import time
        from database import report_db
        cutoff = time.time() - days * 86400
        rows = report_db.get_audit_logs(action="eval_export", limit=1000)
        return sum(1 for r in rows
                   if r.get("result") == "error" and (r.get("created_at") or 0) >= cutoff)
    except Exception:
        _log.warning("eval_export 실패 카운트 조회 실패", exc_info=True)
        return 0


def overview() -> dict:
    """상단 카드용: 파일 경로/크기, user_version, 테이블별 건수, 세션/케이스/라벨 수."""
    path = db_path()
    recent_failures = _recent_export_failures()
    conn = eval_export.open_conn(create=False)
    if conn is None:
        # export 가 파일 생성 전에 실패할 수 있어 실패 카운트는 DB 부재 시에도 노출
        return {"exists": False, "path": str(path),
                "recent_failures": recent_failures}
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {}
        for name in names:
            if _TABLE_NAME_RE.match(name) and not name.startswith("sqlite_"):
                counts[name] = conn.execute(
                    f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
        sessions = conn.execute(
            "SELECT COUNT(DISTINCT session_id) AS n FROM ingest_run "
            "WHERE session_id IS NOT NULL AND session_id <> ''").fetchone()["n"]
    finally:
        conn.close()
    size = sum(_stat(p) for p in (path, path.with_name(path.name + "-wal"),
                                  path.with_name(path.name + "-shm")))
    return {
        "exists": True, "path": str(path), "bytes": size,
        "user_version": user_version, "table_counts": counts,
        "sessions": sessions,
        "cases": counts.get("fail_case", 0), "labels": counts.get("label", 0),
        "recent_failures": recent_failures,
    }


def list_labels(q=None, limit=100, offset=0) -> dict:
    """label(코멘트) 목록 — case/item 메타 + 최신 세션ID 역참조. LIKE 검색 + 페이지네이션."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    where = "1=1"
    params = []
    if q:
        where = ("(fc.product_name LIKE ? OR fc.lot_id LIKE ? OR im.item_name_raw LIKE ?"
                 " OR l.human_comment LIKE ? OR ir.session_id LIKE ?"
                 " OR pm.family_product LIKE ?)")
        params = [f"%{q}%"] * 6

    base = f"""
        FROM label l
        JOIN fail_case fc ON fc.case_id = l.case_id
        JOIN item_master im ON im.item_id = fc.item_id
        LEFT JOIN product_master pm ON pm.product_name = fc.product_name
        LEFT JOIN run_case rc ON rc.case_id = l.case_id
        LEFT JOIN ingest_run ir ON ir.run_id = rc.run_id
        WHERE {where}"""

    conn = eval_export.open_conn(create=False)
    if conn is None:
        return {"total": 0, "limit": limit, "offset": offset, "rows": [],
                "exists": False}
    try:
        total = conn.execute(
            f"SELECT COUNT(DISTINCT l.label_id) AS n {base}", params).fetchone()["n"]
        rows = conn.execute(f"""
            SELECT l.label_id, l.case_id, l.human_comment, l.labeler, l.reviewer,
                   l.label_quality, l.created_at,
                   fc.product_name, fc.lot_id, fc.bin,
                   pm.product_type, pm.family_product,
                   im.item_id, im.item_name_raw AS item, im.value_type, im.unit,
                   MAX(ir.session_id) AS session_id
            {base}
            GROUP BY l.label_id
            ORDER BY l.created_at DESC, l.label_id DESC
            LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "rows": [dict(r) for r in rows], "exists": True}
    finally:
        conn.close()


# ── Unit(value_type) 그룹 수정 ──────────────────────────────────────────────
# value_type 은 선례검색(store.search_precedents)이 등호 하드필터로 쓰는 값이라
# 오분류되면 그 item 의 선례가 통째로 안 잡힌다. item_master.value_type 과
# fail_case.item_class("<category_major>|<value_type>|<bin>") 를 **함께** 고친다.

VALUE_TYPES = eval_export.VALUE_TYPES


def _apply_value_type(conn, item_id: int, value_type: str) -> int:
    """item_master.value_type 갱신 + 그 item 의 fail_case.item_class 재구성 → 갱신 case 수."""
    import time
    row = conn.execute("SELECT category_major FROM item_master WHERE item_id=?",
                       (item_id,)).fetchone()
    if row is None:
        return 0
    conn.execute("UPDATE item_master SET value_type=? WHERE item_id=?",
                 (value_type, item_id))
    cat = row["category_major"] or ""
    now = int(time.time())
    cases = conn.execute("SELECT case_id, bin FROM fail_case WHERE item_id=?",
                         (item_id,)).fetchall()
    for c in cases:
        bin_ = c["bin"]
        item_class = f"{cat}|{value_type}|{'' if bin_ is None else bin_}"
        conn.execute("UPDATE fail_case SET item_class=?, updated_at=? WHERE case_id=?",
                     (item_class, now, c["case_id"]))
    return len(cases)


def set_item_value_type(item_ids, value_type: str) -> dict:
    """선택한 item 들의 Unit 그룹(value_type)을 수동 지정."""
    if value_type not in VALUE_TYPES:
        raise ValueError(f"unknown value_type: {value_type!r}")
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return {"updated": 0, "cases": 0, "exists": False}
    updated = cases = 0
    try:
        for iid in item_ids:
            n = _apply_value_type(conn, int(iid), value_type)
            updated += 1
            cases += n
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"updated": updated, "cases": cases, "exists": True}


def remap_unit_aliases(dry_run: bool = False) -> dict:
    """저장된 unit 원문에 별칭 규칙(VOLT→V / AMP→A / HERTZ→Hz)을 일괄 재적용.

    규칙에 걸리지 않는 unit 은 손대지 않는다(수동 지정값 보존). dry_run 이면
    바뀔 목록만 돌려준다.
    """
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return {"changed": 0, "cases": 0, "items": [], "exists": False}
    changes = []
    cases = 0
    try:
        rows = conn.execute(
            "SELECT item_id, item_name_raw, unit, value_type FROM item_master").fetchall()
        for r in rows:
            want = eval_export.unit_group(r["unit"])
            if not want or want == r["value_type"]:
                continue
            changes.append({"item_id": r["item_id"], "item": r["item_name_raw"],
                            "unit": r["unit"], "from": r["value_type"], "to": want})
            if not dry_run:
                cases += _apply_value_type(conn, r["item_id"], want)
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"changed": len(changes), "cases": cases,
            "items": changes[:50], "exists": True}


# ── 코멘트 CSV export (db_input 단순 5컬럼 포맷 — run_import.bat 재적재용) ────

_CSV_COLUMNS = ("Product type", "Family Product", "unit", "Item", "comment")
_CSV_CHUNK = 1000
_CSV_MAX_ROWS = 100000  # 폭주 방지 상한


def labels_csv_iter():
    """코멘트 라벨 → db_input 단순 포맷 CSV generator (첫 청크에 UTF-8 BOM + 헤더).

    unit 은 화면에 보이는 `im.unit`(원문, 예 "mV") 이 아니라 `im.value_type`(엔진 어휘
    V/A/Hz/CODE/Ohm/Sec/PF)을 내보낸다 — 어휘값은 전부 import_csv 의 alias 표에 있어
    받은 파일을 그대로 재적재할 수 있다. 코멘트가 빈 라벨은 제외(단순 포맷 필수값).
    """
    def _line(values):
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\r\n").writerow(values)
        return buf.getvalue()

    yield "\ufeff" + _line(_CSV_COLUMNS)
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return
    sql = """
        SELECT pm.product_type, pm.family_product, im.value_type,
               im.item_name_raw AS item, l.human_comment
        FROM label l
        JOIN fail_case fc ON fc.case_id = l.case_id
        JOIN item_master im ON im.item_id = fc.item_id
        LEFT JOIN product_master pm ON pm.product_name = fc.product_name
        WHERE l.human_comment IS NOT NULL AND l.human_comment <> ''
        ORDER BY l.label_id
        LIMIT ? OFFSET ?"""
    try:
        offset = 0
        while offset < _CSV_MAX_ROWS:
            rows = conn.execute(sql, (_CSV_CHUNK, offset)).fetchall()
            if not rows:
                break
            for r in rows:
                yield _line([r["product_type"] or "", r["family_product"] or "",
                             r["value_type"] or "", r["item"] or "", r["human_comment"]])
            if len(rows) < _CSV_CHUNK:
                break
            offset += _CSV_CHUNK
    finally:
        conn.close()


# ── 채점 (엔진 판정 vs 사람 정답) ────────────────────────────────────────────
# 원재료는 eval 패널 트레이스의 "정답 라벨"(label.eval_id ⨝ evaluation) — eval_id 가
# 없는 코멘트 export 라벨은 채점에 안 잡힌다(정답 status 가 없으므로).

_HIGH = ("MAJOR", "CRITICAL")
_STATUS_ORDER = ("OK", "MONITOR", "MINOR", "MAJOR", "CRITICAL")


def scoring() -> dict:
    """evaluation ⨝ label(eval_id) 쌍 집계 — 혼동행렬 + high-severity precision/recall.

    precision = 엔진이 MAJOR+ 라고 한 것 중 사람도 MAJOR+ 라고 한 비율(오탐 반대).
    recall    = 사람이 MAJOR+ 라고 한 것 중 엔진이 MAJOR+ 로 잡은 비율(미탐 반대).
    """
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return {"exists": False, "pairs": 0}
    try:
        rows = conn.execute("""
            SELECT ev.status AS engine_status, l.human_status,
                   l.engine_comment_accepted, l.human_comment,
                   cs.signature AS primary_signature,
                   pm.product_type, pm.family_product
            FROM label l
            JOIN evaluation ev ON ev.eval_id = l.eval_id
            JOIN fail_case fc ON fc.case_id = l.case_id
            LEFT JOIN product_master pm ON pm.product_name = fc.product_name
            LEFT JOIN case_signature cs ON cs.eval_id = ev.eval_id AND cs.role='primary'
            WHERE l.human_status IS NOT NULL AND ev.status IS NOT NULL""").fetchall()
    finally:
        conn.close()

    pairs = len(rows)
    confusion: dict = {}          # engine_status → human_status → n
    per_sig: dict = {}            # primary_signature → 집계
    agree = accepted = 0
    eng_high = hum_high = both_high = 0
    for r in rows:
        e, h = r["engine_status"], r["human_status"]
        confusion.setdefault(e, {})[h] = confusion.setdefault(e, {}).get(h, 0) + 1
        if e == h:
            agree += 1
        if r["engine_comment_accepted"]:
            accepted += 1
        e_hi, h_hi = e in _HIGH, h in _HIGH
        eng_high += e_hi
        hum_high += h_hi
        both_high += e_hi and h_hi
        sig = r["primary_signature"] or "(없음)"
        s = per_sig.setdefault(sig, {"n": 0, "agree": 0, "eng_high": 0,
                                     "hum_high": 0, "both_high": 0})
        s["n"] += 1
        s["agree"] += e == h
        s["eng_high"] += e_hi
        s["hum_high"] += h_hi
        s["both_high"] += e_hi and h_hi

    def _ratio(num, den):
        return round(num / den, 4) if den else None

    sig_rows = [{"signature": sig, "n": s["n"],
                 "agree_rate": _ratio(s["agree"], s["n"]),
                 "precision_high": _ratio(s["both_high"], s["eng_high"]),
                 "recall_high": _ratio(s["both_high"], s["hum_high"])}
                for sig, s in sorted(per_sig.items(), key=lambda kv: -kv[1]["n"])]
    return {
        "exists": True, "pairs": pairs,
        "statuses": list(_STATUS_ORDER),
        "confusion": confusion,
        "agree_rate": _ratio(agree, pairs),
        "accepted_rate": _ratio(accepted, pairs),
        "high": {"engine": eng_high, "human": hum_high, "both": both_high,
                 "precision": _ratio(both_high, eng_high),
                 "recall": _ratio(both_high, hum_high)},
        "per_signature": sig_rows,
    }


def delete_cases(case_ids) -> dict:
    """case 단위 완전 삭제 — 자식(label/metrics/outcome/evaluation 계열/링크)까지.

    item_master/item_alias/item_spec/product_master 는 다른 case 와 공유하는 마스터라
    유지한다 (선례 매칭 일관성)."""
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return {"deleted": 0, "exists": False}
    deleted = 0
    try:
        for cid in case_ids:
            eval_sub = "SELECT eval_id FROM evaluation WHERE case_id=?"
            conn.execute(f"DELETE FROM eval_evidence WHERE eval_id IN ({eval_sub})", (cid,))
            conn.execute(f"DELETE FROM case_signature WHERE eval_id IN ({eval_sub})", (cid,))
            conn.execute(
                f"DELETE FROM eval_precedent WHERE eval_id IN ({eval_sub}) "
                "OR precedent_case_id=?", (cid, cid))
            conn.execute("DELETE FROM evaluation WHERE case_id=?", (cid,))
            conn.execute("DELETE FROM features WHERE case_id=?", (cid,))
            conn.execute("DELETE FROM raw_metrics WHERE case_id=?", (cid,))
            conn.execute("DELETE FROM case_outcome WHERE case_id=?", (cid,))
            conn.execute("DELETE FROM label WHERE case_id=?", (cid,))
            conn.execute("DELETE FROM run_case WHERE case_id=?", (cid,))
            cur = conn.execute("DELETE FROM fail_case WHERE case_id=?", (cid,))
            deleted += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"deleted": deleted, "exists": True}


def reexport(session_id: str) -> dict:
    """세션 코멘트 상태를 eval DB 로 동기 재적재 (admin 은 결과를 봐야 하므로 async 아님)."""
    import config
    from database import report_db
    return eval_export.export_session_comments(
        session_id, report_db=report_db, upload_root=Path(config.REPORT_UPLOAD_DIR))
