"""Issue Table 사람 코멘트(PTE/개발) → eval_analyzer eval.db 스키마 export.

eval_engine import 의 **두 번째 허용 지점** (첫 번째는 ai_comment.py — docs/13).
세션 업로드(ingest 시드)와 코멘트 편집 저장 때마다 세션 전체 코멘트 상태를
report_server 소유 별도 SQLite(config.REPORT_EVAL_DB_PATH, eval.db 스키마 그대로)에
재적재한다(멱등). eval_analyzer 쪽은 EVAL_DB_PATH env 로 이 파일을 가리켜 읽는다.

- eval_engine.config.DB_PATH(운영 eval.db) 는 절대 건드리지 않는다 — 자체 커넥션에
  store.SCHEMA 를 적용하고 모든 store CRUD 를 conn= 주입으로 호출한다.
- 매핑: PTE+개발 comment 를 "[PTE] ...\n[개발] ..." 로 병합해 label 1행
  (labeler='web_report'), item 메타(unit/limit)는 honeyform tables 에서 best-effort.
- 실패는 어떤 경우에도 업로드/편집 저장을 죽이지 않는다 (safe_export 격리,
  훅은 export_async 데몬 스레드 사용).
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from pathlib import Path

from . import cache
from . import edits

logger = logging.getLogger(__name__)

_EVAL_DIR = Path(__file__).resolve().parent.parent / "eval_analyzer"

# 이 모듈이 만든 label 행의 식별자 — 갱신/정리 시 이 labeler 행만 만진다.
_LABELER = "web_report"
_COMMENT_PREFIX = {"PTE comment": "[PTE]", "개발 comment": "[개발]"}


def _engine():
    """eval_engine 지연 import (ai_comment._evaluate_fn 과 동일 sys.path 규약).

    store CRUD(conn= 주입)와 item 정규화 헬퍼(db_input/import_csv.py 가 쓰는 공인
    패턴)를 반환한다. import 실패는 호출자(safe_export)가 격리한다.
    """
    path = str(_EVAL_DIR)
    if path not in sys.path:
        sys.path.append(path)
    from eval_engine import store
    from eval_engine.pipeline import ingest as engine_ingest
    return store, engine_ingest


def db_path() -> Path:
    import config  # server/ 가 sys.path 에 있음 (ingest.py 의 product_info 와 동일)
    return Path(config.REPORT_EVAL_DB_PATH)


def open_conn(create: bool = True):
    """export 대상 eval DB 커넥션 (+스키마/마이그레이션/bin_taxonomy 시드, 멱등).

    store.get_conn(config.DB_PATH 고정)을 쓰지 않고 자체 파일에 연결한다.
    create=False 면 파일이 없을 때 만들지 않고 None 반환 (admin 조회용).
    """
    store, _ = _engine()
    path = db_path()
    if not create and not path.exists():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(store.SCHEMA)
    store._migrate(conn)          # 사설 API 의존 — docs/13 에 핀
    store._seed_bin_taxonomy(conn)
    conn.commit()
    return conn


def _merge_comment(cols: dict) -> str:
    """PTE/개발 comment 병합 — 있는 쪽만 "[PTE] ...\n[개발] ..." 로 연결."""
    parts = []
    for col, prefix in _COMMENT_PREFIX.items():
        text = str(cols.get(col) or "").strip()
        if text:
            parts.append(f"{prefix} {text}")
    return "\n".join(parts)


def _parse_row_key(row_key: str):
    """row_key(tabs/issue_table.py 규약) → (bin|None, item) | None(대상 아님).

    Yield|<bin>|<item> → 그 bin. 단 Pass 요약행(bin==1)은 fail-item 이 아니라 skip.
    CPK|<item> → bin=1 (엔진 PASS_BIN 관례 — cpk marginal case).
    ETC|<item> → bin=None (rawdata 에 없는 자유입력 item 가능).
    """
    if row_key.startswith("Yield|"):
        parts = row_key.split("|", 2)
        if len(parts) != 3 or not parts[2]:
            return None
        try:
            bin_ = int(float(parts[1]))
        except (TypeError, ValueError):
            logger.warning("eval_export: bin 파싱 실패 row_key=%r — skip", row_key)
            return None
        if bin_ == 1:
            return None  # Pass 요약행
        return (bin_, parts[2])
    if row_key.startswith("CPK|") and row_key[4:]:
        return (1, row_key[4:])
    if row_key.startswith("ETC|") and row_key[4:]:
        return (None, row_key[4:])
    return None


def _collect_comments(report_db, session, upload_root: Path) -> dict:
    """세션 issue_comment 상태 → {row_key: {"cols": {col: text}, "by": 최종 편집자}}.

    rev>0 이면 세션 편집 DB 가 진실. rev==0(legacy 미이전, admin 재적재 등)은
    manifest 폴백 — ensure_seeded 는 호출하지 않는다(rev 를 올려 REPORT_CACHE 를
    불필요하게 무효화하는 부작용 방지, 읽기 전용).
    """
    from .tabs.issue_table import COMMENT_COLS

    session_id = session["session_id"]
    per_key: dict[str, dict] = {}
    if report_db.get_webreport_edit_rev(session_id) > 0:
        rows = report_db.get_webreport_edits(session_id,
                                             kinds=(edits.KIND_ISSUE_COMMENT,))
        for row in rows:
            row_key, _, col = str(row["item_key"]).partition(edits._SEP)
            value = str(row["value"] or "").strip()
            if not row_key or col not in COMMENT_COLS or not value:
                continue
            ent = per_key.setdefault(row_key, {"cols": {}, "by": None, "_at": -1})
            ent["cols"][col] = value
            at = row.get("updated_at") or 0
            if at >= ent["_at"]:
                ent["_at"] = at
                ent["by"] = row.get("updated_by") or None
    else:
        manifest = cache.load_manifest_cached(session.get("analysis_key"), upload_root)
        state = edits.state_from_manifest(manifest)
        by = str(session.get("uploaded_by") or "") or None
        for row_key, cols in (state.get("issue_comments") or {}).items():
            kept = {c: str(v).strip() for c, v in (cols or {}).items()
                    if c in COMMENT_COLS and str(v or "").strip()}
            if kept:
                per_key[str(row_key)] = {"cols": kept, "by": by}
    return per_key


def _find_item_meta(tables, item):
    """item 의 unit/usl/lsl — 첫 매칭 소스 우선 (tabs/common.item_meta 관례)."""
    from .tabs.common import num
    for t in tables or []:
        if item in (t.units or {}) or item in t.item_columns:
            return {"unit": (t.units or {}).get(item),
                    "usl": num((t.hilim or {}).get(item)),
                    "lsl": num((t.lolim or {}).get(item))}
    return None


def _yield_metrics(tables, item, bin_) -> dict:
    """Yield 행 fail/total 집계 — FAILTNO==item TNO AND BIN==bin (yield_tab 규칙)."""
    from .tabs.common import bin_types
    from .tabs.yield_tab import _tno_norm, failtno_norms

    bin_s = str(bin_)
    fail = 0
    total = 0
    matched = False
    for t in tables or []:
        total += int(len(t.data))
        tno = _tno_norm((t.tno or {}).get(item))
        if tno is None:
            continue
        matched = True
        for b, ft in zip(bin_types(t), failtno_norms(t)):
            if ft == tno and b == bin_s:
                fail += 1
    if not matched or not total:
        return {}
    return {"fail_count": fail, "total_count": total, "yield": 1 - fail / total}


def _dist_metrics(tables, item) -> dict:
    """item 측정값 요약통계 — 소스별 cpk 중 worst(최저) 소스 채택 (Issue Table CPK 규약)."""
    from .tabs.cpk import _stats

    best = None
    for t in tables or []:
        if item not in t.item_columns:
            continue
        st = _stats(t.data[item], (t.lolim or {}).get(item), (t.hilim or {}).get(item))
        if not st.get("n"):
            continue
        if best is None:
            best = st
        elif st.get("cpk") is not None and (best.get("cpk") is None
                                            or st["cpk"] < best["cpk"]):
            best = st
    if best is None:
        return {}
    return {"cpk": best.get("cpk"), "cpl": best.get("cpl"), "cpu": best.get("cpu"),
            "cp": best.get("cp"), "mean": best.get("average"),
            "stdev": best.get("stdev"), "min": best.get("min"), "max": best.get("max")}


def _find_run_id(conn, session_id: str):
    row = conn.execute(
        "SELECT run_id FROM ingest_run WHERE session_id=? ORDER BY run_id DESC LIMIT 1",
        (session_id,)).fetchone()
    return row["run_id"] if row else None


def export_session_comments(session_id: str, *, report_db, upload_root,
                            tables=None) -> dict:
    """세션의 Issue Table 코멘트 상태 전체를 eval DB 로 재적재 (멱등).

    반환: {"cases": n, "labels": n, "removed": n} 또는 {"skipped": 사유}.
    tables 는 호출자가 이미 들고 있으면 주입(재로드 회피), 없으면 캐시 경유 로드.
    """
    upload_root = Path(upload_root)
    session = report_db.get_session(session_id)
    if not session or str(session.get("source") or "") != "web_report":
        return {"skipped": "not a web_report session"}
    if not session.get("analysis_key"):
        return {"skipped": "no analysis_key"}

    from . import ai_comment
    meta = ai_comment._session_meta(session, 0)
    if meta is None:
        return {"skipped": f"unsupported product_type: {session.get('product_type')!r}"}
    meta["wafer_number"] = None  # 코멘트는 행(세션) 단위 — lot 수준 case 로 적재

    parsed = []  # [(bin|None, item, 병합 comment, 최종 편집자)]
    for row_key, ent in _collect_comments(report_db, session, upload_root).items():
        pk = _parse_row_key(row_key)
        if pk is None:
            continue
        text = _merge_comment(ent["cols"])
        if text:
            parsed.append((pk[0], pk[1], text, ent.get("by")))

    # 코멘트 0건 + eval DB 미존재면 파일 생성조차 하지 않는다 (업로드마다 빈 DB 방지).
    if not parsed and not db_path().exists():
        return {"cases": 0, "labels": 0, "removed": 0}

    if tables is None and parsed:
        try:
            from . import loader
            tables = loader.load_tables(session_id, report_db=report_db,
                                        upload_root=upload_root, session=session)
        except Exception:
            logger.warning("eval_export: tables 로드 실패 — 코멘트만 적재 (session=%s)",
                           session_id, exc_info=True)
            tables = None

    store, engine_ingest = _engine()
    alias = engine_ingest._alias_map()

    with cache.keyed_lock(("eval_export", session_id)):
        conn = open_conn()
        try:
            run_id = _find_run_id(conn, session_id)
            if run_id is None:
                if not parsed:
                    return {"cases": 0, "labels": 0, "removed": 0}
                run_id = store.create_ingest_run({
                    "product_name": meta["product_name"], "lot_id": meta["lot_id"],
                    "source_file": str(session.get("file_name") or ""),
                    "analysis_key": session.get("analysis_key"),
                    "session_id": session_id, "ingested_by": _LABELER,
                }, conn=conn)

            store.upsert_product_master(meta, conn=conn)
            now_cases = set()
            for bin_, item, text, by in parsed:
                item_meta = _find_item_meta(tables, item)
                unit = item_meta["unit"] if item_meta else None
                item_canonical = alias.get(item, engine_ingest._canonicalize(item))
                category_major = engine_ingest._classify_category_major(item)
                value_type = engine_ingest._classify_value_type(unit, item)
                item_id = store.upsert_item_master(
                    item_canonical, item, None, None, category_major, None,
                    value_type, str(unit) if unit is not None else None, conn=conn)
                store.upsert_item_alias(item, item_id, conn=conn)
                if item_meta and (item_meta["lsl"] is not None
                                  or item_meta["usl"] is not None):
                    store.upsert_item_spec(item_id, meta["product_name"],
                                           meta["revision"], item_meta["lsl"],
                                           item_meta["usl"], conn=conn)

                case_id = store.make_case_id(meta["product_name"], meta["lot_id"],
                                             None, item_id, bin_, meta["revision"])
                item_class = (f"{category_major}|{value_type}|"
                              f"{bin_ if bin_ is not None else ''}")
                store.upsert_fail_case(case_id, meta["product_name"], meta["lot_id"],
                                       None, item_id, bin_, meta["revision"],
                                       item_class, conn=conn)
                store.link_run_case(run_id, case_id, conn=conn)

                metrics = {}
                try:  # 통계는 best-effort — 실패해도 코멘트 적재는 계속
                    metrics = _dist_metrics(tables, item)
                    if bin_ is not None and bin_ != 1:
                        metrics.update(_yield_metrics(tables, item, bin_))
                except Exception:
                    logger.warning("eval_export: metrics 계산 실패 item=%r — 생략",
                                   item, exc_info=True)
                if metrics:
                    store.save_raw_metrics(case_id, run_id, metrics, conn=conn)

                conn.execute("DELETE FROM label WHERE case_id=? AND labeler=?",
                             (case_id, _LABELER))
                store.insert_label(case_id, None, None, None, None, 0, 0,
                                   text, _LABELER, by or None, "manual", conn=conn)
                now_cases.add(case_id)

            # reconciliation — 이전 export(run_case)에 있었지만 이번 상태에 없는 case 의
            # 우리 label/metrics/링크 정리. fail_case 는 유지(다른 run·CSV 적재와 공유
            # 가능, label 없으면 선례검색 자동 후순위) — 완전 삭제는 admin 탭 몫.
            prev = {r["case_id"] for r in conn.execute(
                "SELECT case_id FROM run_case WHERE run_id=?", (run_id,))}
            removed = 0
            for case_id in prev - now_cases:
                conn.execute("DELETE FROM label WHERE case_id=? AND labeler=?",
                             (case_id, _LABELER))
                conn.execute("DELETE FROM raw_metrics WHERE case_id=? AND run_id=?",
                             (case_id, run_id))
                conn.execute("DELETE FROM run_case WHERE run_id=? AND case_id=?",
                             (run_id, case_id))
                removed += 1

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    result = {"cases": len(now_cases), "labels": len(now_cases), "removed": removed}
    logger.info("eval_export: session=%s → %s", session_id, result)
    return result


def safe_export(session_id: str, *, report_db, upload_root, tables=None) -> dict:
    """export 실패 격리 — 예외 시 warning 로그 + skipped (ai_comment.safe_build 관례)."""
    try:
        return export_session_comments(session_id, report_db=report_db,
                                       upload_root=upload_root, tables=tables)
    except Exception as exc:
        logger.warning("eval_export 실패 — 무시하고 진행 (session=%s)", session_id,
                       exc_info=True)
        try:  # 조용한 적재 누락 방지 — admin User Action Monitoring/Eval DB 탭에서 확인
            report_db.log_audit(action="eval_export", session_id=session_id,
                                result="error", changed_fields=repr(exc)[:500])
        except Exception:
            pass  # 감사 기록 실패가 업로드/편집을 죽이면 안 됨 (기존 격리 원칙)
        return {"skipped": "error"}


def export_async(session_id: str, *, report_db, upload_root) -> None:
    """훅 전용 — 데몬 스레드에서 safe_export (콜드 tables 로드가 응답을 늦추지 않게)."""
    threading.Thread(
        target=safe_export, args=(session_id,),
        kwargs={"report_db": report_db, "upload_root": Path(upload_root)},
        daemon=True, name=f"eval-export-{session_id}",
    ).start()
