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

import collections
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import cache
from . import edits
from .comment_format import strip_format

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


def _engine_eval():
    """평가 스냅샷 수집용 (evaluate + engine_version). _engine 과 같은 sys.path 규약.

    ai_comment.py 가 아니라 여기서 evaluate 를 부르는 이유: 이 모듈이 report_server
    소유 eval DB(REPORT_EVAL_DB_PATH)의 주인이고, eval_engine import 지점을 3곳으로
    유지해야 하기 때문이다(docs/13 §2). ai_comment 의 호출은 여전히 persist=False 다.
    """
    path = str(_EVAL_DIR)
    if path not in sys.path:
        sys.path.append(path)
    from eval_engine import config as eval_config, evaluate
    return evaluate, eval_config.ENGINE_VERSION


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


def open_conn_ro():
    """**조회 전용** 커넥션 — 스키마 보장·마이그레이션·시드를 하지 않는다.

    open_conn 은 호출마다 `SCHEMA` executescript + `_migrate` + bin_taxonomy UPSERT +
    커밋(fsync)을 한다 = 사실상 **쓰기 트랜잭션**이다. 관리자 전용 진입점일 때는 무해했지만,
    Issue Table Signature 의 판정근거 팝업(`?`)은 편집 권한 없는 조회자 전원이 누르는
    읽기 경로다 — 클릭마다 쓰기 잠금을 다투게 되고, 업로드 스냅샷 적재와 겹치면
    busy_timeout(5초)만큼 waitress 스레드를 물고 있다 실패한다.

    파일이 없거나 스키마가 아직 없으면 None (호출부는 "eval DB 없음" 폴백).
    `mode=ro` URI 를 쓰지 않는 이유 — WAL DB 를 읽기전용으로 열면 -shm 이 없을 때
    (기동 직후 아직 쓴 적 없는 상태) 열기 자체가 실패한다. 여기서는 SELECT 만 하므로
    보통 커넥션으로 열되 **쓰기 작업을 하지 않는 것**으로 같은 효과를 낸다.
    """
    path = db_path()
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    try:
        got = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evaluation'"
        ).fetchone()
    except Exception:
        got = None
    if not got:
        conn.close()
        return None
    return conn


def _merge_comment(cols: dict) -> str:
    """PTE/개발 comment 병합 — 있는 쪽만 "[PTE] ...\n[개발] ..." 로 연결.

    화면 전용 서식 토큰(*[..]/*r[..])은 여기서 벗긴다. 이 함수가 eval.db
    label.human_comment 로 가는 유일한 관문이라, 여기 한 곳만 막으면 챗봇 코멘트 검색·
    AI Comment 선례 인용·관리자 패널·CSV 내보내기가 전부 평문을 보게 된다.
    """
    parts = []
    for col, prefix in _COMMENT_PREFIX.items():
        text = strip_format(cols.get(col)).strip()
        if text:
            parts.append(f"{prefix} {text}")
    return "\n".join(parts)


def _parse_row_key(row_key: str):
    """row_key(tabs/issue_table.py 규약) → (bin|None, item, condition) | None(대상 아님).

    ⚠ **돌려주는 bin 은 표시·참고용이다 — case_id 재료가 아니다** (2026-08-19).
    엔진 case 가 item 당 1개가 되면서 case_id 의 bin 자리는 항상 None 이다
    (pipeline/ingest.py). 그래서 어느 섹션에 코멘트를 썼든 같은 item 이면 **같은 case**
    로 모인다 — 종전에는 `Yield|20|X` 와 `CPK|X` 가 서로 다른 case_id 로 갈려 사람 라벨이
    엔진 판정과 만나지 못했다(운영 라벨 1건이 실제로 100% 고아였다).

    Yield|<bin>|<item> → 그 bin. 단 Pass 요약행(bin==1)은 fail-item 이 아니라 skip.
    CPK|<item> / ETC|<item> → bin=None (섹션 자체에 bin 개념이 없다).
    TEMP|<item> → bin=None + condition='TEMP' (Temperature 모드 RT limit 이탈 항목).
      condition 은 **case_id 재료로 계속 쓴다** — 온도 재판정은 엔진이 평가하지 않는
      별개 축이라, 같은 item 의 일반 코멘트와 한 case 로 붕괴하면 서로 덮어쓴다.
    CMPDIST|<item> / CMPETC|<item> → bin=None + condition='COMPARE'
      (Compare 모드 Issue Table Compare 탭, 2026-08-20). TEMP 와 **같은 이유**로 조건을
      붙인다 — Before/After 비교는 엔진이 평가하지 않는 별개 축이고, 같은 item 의 일반
      코멘트와 한 case 로 모이면 둘이 서로 덮어쓴다. Distribution/ETC 두 섹션을 같은
      'COMPARE' 로 묶는 이유는 그 구분이 화면 배치일 뿐 판단 축이 아니기 때문이다.
      ※ Compare 탭 **하단 별도 표**(동일 좌표 Bin 비교 bm:, Log 비교 gl:)의 코멘트는
        issue_comment 가 아니라 compare_note kind 라 애초에 이 경로로 오지 않는다.
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
        return (bin_, parts[2], "")
    if row_key.startswith("CPK|") and row_key[4:]:
        return (None, row_key[4:], "")
    if row_key.startswith("TEMP|") and row_key[5:]:
        return (None, row_key[5:], "TEMP")
    if row_key.startswith("ETC|") and row_key[4:]:
        return (None, row_key[4:], "")
    if row_key.startswith("CMPDIST|") and row_key[8:]:
        return (None, row_key[8:], "COMPARE")
    if row_key.startswith("CMPETC|") and row_key[7:]:
        return (None, row_key[7:], "COMPARE")
    return None


def _group_by_case(parsed):
    """[(bin, item, cond, text, by, at)…] → 같은 case 로 갈 행끼리 묶어 텍스트 병합.

    case_id 재료는 (item, cond) 뿐이므로(bin 은 2026-08-19 부터 키에서 빠졌다) 같은 item 의
    Yield|2 / Yield|5 / CPK 행이 한 case 로 모인다. 행마다 따로 저장하면 마지막 것만 남아
    **앞 코멘트가 사라지므로**(CLAUDE.md 규칙 12) 여기서 미리 합친다.

    병합 규칙: 텍스트는 입력 순서대로 줄바꿈 join(중복 문구는 1회만 — 같은 코멘트를 두 행에
    똑같이 써 둔 흔한 경우에 같은 문장이 두 번 나오지 않게). 대표 bin 은 **가장 작은 fail
    bin**(엔진 대표 bin 규칙과 같은 방향), 편집자는 **가장 최근에 고친 사람**.
    반환 튜플은 종전과 같은 5개(bin, item, cond, text, by) — 시각은 선정에만 쓴다.
    """
    groups: dict = {}
    order: list = []
    for bin_, item, cond, text, by, at in parsed:
        key = (item, cond)
        if key not in groups:
            groups[key] = {"bins": [], "texts": [], "by": None, "at": None}
            order.append(key)
        g = groups[key]
        if bin_ is not None:
            g["bins"].append(bin_)
        if text and text not in g["texts"]:
            g["texts"].append(text)
        if by and (g["at"] is None or at >= g["at"]):
            g["by"], g["at"] = by, at
    out = []
    for item, cond in order:
        g = groups[(item, cond)]
        out.append((min(g["bins"]) if g["bins"] else None, item, cond,
                    "\n".join(g["texts"]), g["by"]))
    return out


def _status_key(row_key: str) -> str:
    """comment row_key → 그 행이 속한 이슈의 Status 키 (tabs/issue_table.py 규약).

    Status/숨김은 이슈 단위라 Yield 는 bin 까지만("Yield|<bin>"), CPK/TEMP/ETC 는
    row_key 와 같다. 프런트 sheets.js issueHideStatusKey 와 같은 규칙이다.
    """
    if row_key.startswith("Yield|"):
        parts = row_key.split("|", 2)
        if len(parts) == 3:
            return f"Yield|{parts[1]}"
    return row_key


def _close_keys(report_db, session_id: str) -> set:
    """Status 가 Close 인 이슈 키 집합 (부재=Open — edits.KIND_ISSUE_STATUS 규약)."""
    rows = report_db.get_webreport_edits(session_id,
                                         kinds=(edits.KIND_ISSUE_STATUS,))
    return {str(r["item_key"]) for r in rows if str(r["value"] or "") == "Close"}


def _collect_comments(report_db, session) -> dict:
    """세션 issue_comment 상태 → {row_key: {"cols": {col: text}, "by": 최종 편집자}}.

    **Status 가 Close 인 이슈의 코멘트만** 돌려준다 (2026-08-04). Open 은 아직
    조사 중인 미확정 코멘트라 선례로 쓰면 안 된다 — Close 로 바뀌는 순간 편집 훅이
    다시 export 하고, Close→Open 으로 되돌리면 reconciliation 이 그 case 의 라벨을
    지운다(멱등 재적재라 상태 변화가 그대로 반영된다).

    rev>0 이면 세션 편집 DB 가 진실. rev==0(legacy 미이전, admin 재적재 등)은
    manifest 폴백 — manifest 에는 Status 가 존재한 적이 없어(신규 kind) 전부 Open =
    적재 대상 없음이다. ensure_seeded 는 호출하지 않는다(rev 를 올려 REPORT_CACHE 를
    불필요하게 무효화하는 부작용 방지, 읽기 전용).
    """
    from .tabs.issue_table import COMMENT_COLS

    session_id = session["session_id"]
    per_key: dict[str, dict] = {}
    if report_db.get_webreport_edit_rev(session_id) > 0:
        closed = _close_keys(report_db, session_id)
        if not closed:
            return per_key
        rows = report_db.get_webreport_edits(session_id,
                                             kinds=(edits.KIND_ISSUE_COMMENT,))
        for row in rows:
            row_key, _, col = str(row["item_key"]).partition(edits._SEP)
            value = str(row["value"] or "").strip()
            if not row_key or col not in COMMENT_COLS or not value:
                continue
            if _status_key(row_key) not in closed:
                continue   # Open 이슈 — 확정 전이라 선례로 적재하지 않는다
            ent = per_key.setdefault(row_key, {"cols": {}, "by": None, "_at": -1})
            ent["cols"][col] = value
            at = row.get("updated_at") or 0
            if at >= ent["_at"]:
                ent["_at"] = at
                ent["by"] = row.get("updated_by") or None
    # rev==0 = 편집 DB 이전 세션 — Status 를 저장한 적이 없으므로 전부 Open 이다.
    # (manifest 폴백에는 issue_status 가 없다 — edits.state_from_manifest 참조.)
    return per_key


# ── 단위 원문 → 엔진 어휘 그룹 (report_server 쪽 선보정) ─────────────────────
# 엔진 UNIT_TO_VALUE_TYPE(pipeline/ingest.py)은 정확매칭 표라 "VOLTS"/"HERTZ"/
# "mAMP" 같은 표기를 놓치고 조용히 PF 로 떨어뜨린다. 도입 당시 eval_analyzer 가 무수정
# 영역이라 여기서 부분문자열 규칙으로 먼저 매핑했고, 이미 적재된 데이터와의 정합 때문에
# 엔진이 자유 수정이 된 지금도 이 2단 구조를 유지한다 (../docs/13 §10).
# 엔진 표와 충돌하지 않는다 — "v"/"hz"/"amp" 등 짧은 표기는 이 규칙에 안 걸려
# 그대로 엔진 표로 내려간다.
_UNIT_SUBSTR_RULES = (("VOLT", "V"), ("AMP", "A"), ("HERTZ", "Hz"))

# item_master.value_type 어휘 (엔진 UNIT_TO_VALUE_TYPE 의 값 집합) — 관리자 수정 UI 용.
# '%' 는 2026-08-12 에 엔진 표로 승격됐다(종전엔 db_input 선례 적재에만 있던 어휘).
VALUE_TYPES = ("V", "A", "Hz", "CODE", "Ohm", "Sec", "%", "PF")


def unit_group(unit):
    """단위 원문에 VOLT/AMP/HERTZ 가 포함되면 V/A/Hz. 아니면 None(엔진 표에 위임)."""
    text = str(unit or "").upper()
    for token, value_type in _UNIT_SUBSTR_RULES:
        if token in text:
            return value_type
    return None


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


def _find_run_id(conn, session_id: str, ingested_by: str | None = None):
    """세션의 ingest_run — ingested_by 를 주면 그 용도의 run 만 찾는다.

    한 세션에 용도가 다른 run 이 여럿 있다(코멘트 export / 스냅샷 수집 / signature 라벨).
    ingested_by 없이 최근 run 을 집으면 남의 run 에 라벨이 달려 세션 역참조가 흐려진다.
    """
    if ingested_by:
        row = conn.execute(
            "SELECT run_id FROM ingest_run WHERE session_id=? AND ingested_by=? "
            "ORDER BY run_id DESC LIMIT 1", (session_id, ingested_by)).fetchone()
    else:
        row = conn.execute(
            "SELECT run_id FROM ingest_run WHERE session_id=? ORDER BY run_id DESC LIMIT 1",
            (session_id,)).fetchone()
    return row["run_id"] if row else None


def export_session_comments(session_id: str, *, report_db, upload_root,
                            tables=None) -> dict:
    """세션의 Issue Table 코멘트 상태 전체를 eval DB 로 재적재 (멱등).

    적재 대상은 **Status 가 Close 인 이슈의 코멘트만**이다 (2026-08-04) — Open 은
    조사 중인 미확정 코멘트라 선례로 쓰지 않는다. Close→Open 으로 되돌린 case 는
    아래 reconciliation 이 우리 label 을 지운다.

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

    # [(bin|None, item, condition, 병합 comment, 최종 편집자, 편집시각)]
    # 편집시각은 여러 행이 한 case 로 합쳐질 때 **가장 최근 편집자**를 고르는 데만 쓴다.
    parsed = []
    for row_key, ent in _collect_comments(report_db, session).items():
        pk = _parse_row_key(row_key)
        if pk is None:
            continue
        text = _merge_comment(ent["cols"])
        if text:
            parsed.append((pk[0], pk[1], pk[2], text, ent.get("by"), ent.get("_at") or 0))

    # 코멘트 0건 + eval DB 미존재면 파일 생성조차 하지 않는다 (업로드마다 빈 DB 방지).
    if not parsed and not db_path().exists():
        return {"cases": 0, "labels": 0, "removed": 0}

    if tables is None and parsed:
        try:
            from . import loader
            _, tables, _ = loader.load_tables(session_id, report_db=report_db,
                                              upload_root=upload_root, session=session)
        except Exception:
            logger.warning("eval_export: tables 로드 실패 — 코멘트만 적재 (session=%s)",
                           session_id, exc_info=True)
            tables = None

    store, engine_ingest = _engine()
    alias = engine_ingest._alias_map()

    with cache.keyed_lock_ctx(("eval_export", session_id)):
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
            # 같은 item 의 여러 섹션 행(Yield|2|X · Yield|5|X · CPK|X …)이 **한 case** 로
            # 모인다(2026-08-19 — case_id 에서 bin 제거). 행마다 delete→insert 하면
            # **마지막 행만 남아 앞 코멘트가 조용히 사라진다**(CLAUDE.md 규칙 12 —
            # 사용자 입력은 무슨 일이 있어도 잃지 않는다). 그래서 먼저 묶어 텍스트를
            # 병합한 뒤 case 당 1회만 쓴다. 대표 bin/편집자는 마지막 편집분을 따른다.
            for group in _group_by_case(parsed):
                bin_, item, cond, text, by = group
                item_meta = _find_item_meta(tables, item)
                unit = item_meta["unit"] if item_meta else None
                item_canonical = alias.get(item, engine_ingest._canonicalize(item))
                category_major = engine_ingest._classify_category_major(item)
                value_type = (unit_group(unit)
                              or engine_ingest._classify_value_type(unit, item))
                item_id = store.upsert_item_master(
                    item_canonical, item, None, None, category_major, None,
                    value_type, str(unit) if unit is not None else None, conn=conn)
                store.upsert_item_alias(item, item_id, conn=conn)
                if item_meta and (item_meta["lsl"] is not None
                                  or item_meta["usl"] is not None):
                    store.upsert_item_spec(item_id, meta["product_name"],
                                           meta["revision"], item_meta["lsl"],
                                           item_meta["usl"], conn=conn)

                # case_id 의 bin 자리는 **항상 None** — 엔진(pipeline/ingest.py)과 같은
                # 규약이라야 사람 라벨이 엔진 판정과 같은 case 에 붙는다. bin 은 아래
                # fail_case.bin 컬럼(대표 bin)으로만 남는다.
                case_id = store.make_case_id(meta["product_name"], meta["lot_id"],
                                             None, item_id, None, meta["revision"],
                                             cond)
                item_class = f"{category_major}|{value_type}"
                store.upsert_fail_case(case_id, meta["product_name"], meta["lot_id"],
                                       None, item_id, bin_, meta["revision"],
                                       item_class, cond, conn=conn)
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

                store.delete_label_with_signatures("case_id=? AND labeler=?",
                                                   (case_id, _LABELER), conn=conn)
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
                store.delete_label_with_signatures("case_id=? AND labeler=?",
                                                   (case_id, _LABELER), conn=conn)
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


# ── 관리자 정답 라벨 (eval 패널 트레이스 → 예측·정답 쌍) ─────────────────────
# 엔진 판정(evaluation)과 사람 정답(label.eval_id 연결)을 같은 case_id 로 저장한다 —
# 채점(precision/recall)과 이후 보정 검증의 원재료. 코멘트 export(labeler='web_report')와
# 구분하기 위해 labeler='eval-panel' 을 쓰고, 같은 case 재검수 시 이전 패널 라벨을 교체한다.
_PANEL_LABELER = "eval-panel"
_STATUS_VOCAB = ("OK", "MONITOR", "MINOR", "MAJOR", "CRITICAL")


def save_human_label(session: dict, *, item: str, bin_, item_class: str,
                     engine: dict, human: dict) -> dict:
    """트레이스 케이스 1건에 대한 (엔진 판정, 사람 정답) 쌍 저장.

    engine: {engine_version, status, confidence, data_completeness, comment,
             primary_signature, secondary_signatures}  — 트레이스 스냅샷 그대로.
    human:  {accepted(bool), human_status(정정 시), human_comment(선택),
             root_cause_category(선택), reviewer(선택)}
    case 매핑은 코멘트 export 와 동일(lot 수준, wafer_number=None) — 같은 case 공간에서
    코멘트 라벨과 조인된다. value_type 은 트레이스의 item_class 에서 취해 실측 unit 과
    일치시킨다(기존 item_master 가 있으면 재사용 — value_type 덮어쓰기 방지).
    """
    from . import ai_comment
    meta = ai_comment._session_meta(session, 0)
    if meta is None:
        raise ValueError(f"product_type={session.get('product_type')!r} 는 평가 대상이 아님")
    meta["wafer_number"] = None

    # item_class 는 2단(category_major|value_type) — 2026-08-19. 구 3단(…|bin) 값도
    # 그대로 읽을 수 있게 길이를 고정하지 않는다(누적 트레이스·옛 라벨 호환).
    parts = (item_class or "").split("|")
    category_major = parts[0] if len(parts) >= 2 and parts[0] else "NON_TRIM"
    value_type = parts[1] if len(parts) >= 2 and parts[1] else "PF"

    accepted = bool(human.get("accepted"))
    engine_status = str(engine.get("status") or "").strip() or None
    human_status = engine_status if accepted else str(human.get("human_status") or "").strip()
    if human_status not in _STATUS_VOCAB:
        raise ValueError(f"human_status 는 {_STATUS_VOCAB} 중 하나여야 함: {human_status!r}")
    human_comment = str(human.get("human_comment") or "").strip() or None

    store, engine_ingest = _engine()
    alias = engine_ingest._alias_map()
    session_id = str(session.get("session_id") or "")

    with cache.keyed_lock_ctx(("eval_export", session_id)):
        conn = open_conn()
        try:
            run_id = _find_run_id(conn, session_id)
            if run_id is None:
                run_id = store.create_ingest_run({
                    "product_name": meta["product_name"], "lot_id": meta["lot_id"],
                    "source_file": str(session.get("file_name") or ""),
                    "analysis_key": session.get("analysis_key"),
                    "session_id": session_id, "ingested_by": _PANEL_LABELER,
                }, conn=conn)
            store.upsert_product_master(meta, conn=conn)
            # 기존 item 은 재사용 — 없을 때만 트레이스의 value_type 으로 생성
            # (upsert 로 덮으면 코멘트 export 가 넣은 unit/value_type 이 훼손된다).
            item_canonical = alias.get(item, engine_ingest._canonicalize(item))
            item_id = store.resolve_item_id(item, conn=conn)
            if item_id is None:
                item_id = store.upsert_item_master(
                    item_canonical, item, None, None, category_major, None,
                    value_type, None, conn=conn)
                store.upsert_item_alias(item, item_id, conn=conn)

            # bin 은 case_id 재료가 아니다(2026-08-19) — fail_case.bin 컬럼에만 남는다.
            case_id = store.make_case_id(meta["product_name"], meta["lot_id"],
                                         None, item_id, None, meta["revision"])
            store.upsert_fail_case(case_id, meta["product_name"], meta["lot_id"],
                                   None, item_id, bin_, meta["revision"],
                                   f"{category_major}|{value_type}", conn=conn)
            store.link_run_case(run_id, case_id, conn=conn)

            eval_id = store.save_evaluation(
                case_id, run_id, str(engine.get("engine_version") or "ev1"), None,
                engine_status, engine.get("confidence"),
                engine.get("data_completeness"), engine.get("comment"), conn=conn)
            sig_rows = []
            if engine.get("primary_signature"):
                sig_rows.append({"id": engine["primary_signature"],
                                 "role": "primary", "score": 1.0})
            sig_rows += [{"id": s, "role": "secondary", "score": None}
                         for s in engine.get("secondary_signatures") or []]
            if sig_rows:
                store.save_case_signature(eval_id, sig_rows, conn=conn)

            # 같은 case 의 이전 패널 검수는 교체 (case 당 최신 정답 1건 유지)
            store.delete_label_with_signatures("case_id=? AND labeler=?",
                                               (case_id, _PANEL_LABELER), conn=conn)
            label_id = store.insert_label(
                case_id, eval_id, human_status,
                str(human.get("root_cause_category") or "").strip() or None, None,
                1 if accepted else 0,
                1 if human_comment else 0,
                human_comment, _PANEL_LABELER,
                str(human.get("reviewer") or "").strip() or None, "manual", conn=conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return {"case_id": case_id, "eval_id": eval_id, "label_id": label_id,
            "human_status": human_status, "accepted": accepted}


def get_panel_label(session: dict, *, item: str, bin_) -> dict | None:
    """이 케이스에 이미 달아 둔 패널 정답 라벨 1건 (없으면 None) — 폼 프리필용.

    case_id 산식은 save_human_label 과 같아야 한다(lot 수준, wafer_number=None) —
    다르면 방금 저장한 라벨을 못 찾는다. 조회 전용이라 DB 를 만들지 않는다.
    """
    from . import ai_comment
    meta = ai_comment._session_meta(session, 0)
    if meta is None:
        return None
    conn = open_conn(create=False)
    if conn is None:                                  # eval DB 미생성 = 라벨 없음
        return None
    try:
        store, _ = _engine()
        item_id = store.resolve_item_id(item, conn=conn)
        if item_id is None:
            return None
        case_id = store.make_case_id(meta["product_name"], meta["lot_id"], None,
                                     item_id, None, meta["revision"])
        row = conn.execute(
            "SELECT label_id, human_status, human_comment, root_cause_category, "
            "engine_comment_accepted, reviewer, created_at "
            "FROM label WHERE case_id=? AND labeler=? "
            "ORDER BY label_id DESC LIMIT 1", (case_id, _PANEL_LABELER)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── ENGR 확정 Signature 라벨 (Issue Table Signature 컬럼 → eval DB) ───────────
# 왜 별도 run 을 쓰나: case_id 는 (제품, lot, wafer=None, item, bin, revision) 자연키라
# **세션이 들어가지 않는다**. 같은 lot 을 여러 세션에서 열어 각각 확정하면 case_id 가
# 겹쳐 서로의 라벨을 덮어쓴다. 그래서 세션마다 전용 ingest_run 을 만들고 그 run 의
# evaluation 에 label.eval_id 를 매달아, 삭제·조회를 **세션 단위**로 한다.
_SIGNATURE_LABELER = "web-signature"
_SIGNATURE_ENGINE_VERSION = "engr-label"   # 사람 라벨용 run — 엔진 판정 스냅샷이 아니다


def sync_session_signatures(session_id: str, *, report_db, upload_root=None) -> dict:
    """세션 편집 DB 의 ENGR 확정 signature 전체를 eval DB 로 멱등 재적재.

    요청 payload 가 아니라 **편집 DB 의 현재 상태**를 읽는다 — 연속 편집이 몰려도 마지막
    상태로 수렴하고, 서버 재시작 뒤 수동 재동기화도 같은 함수 하나로 된다.
    해제(편집행 삭제)된 행은 이 세션 run 의 라벨을 통째로 지운 뒤 다시 넣는 방식으로
    자연히 사라진다.
    """
    session = report_db.get_session(session_id)
    if not session or str(session.get("source") or "") != "web_report":
        return {"skipped": "not a web_report session"}

    from . import ai_comment, edits
    meta = ai_comment._session_meta(session, 0)
    if meta is None:
        return {"skipped": f"unsupported product_type: {session.get('product_type')!r}"}
    meta["wafer_number"] = None            # 코멘트/패널 라벨과 같은 lot 수준 case 공간

    state = edits.load_issue_signatures(report_db, session_id)
    # 같은 item 의 여러 섹션 행이 한 case 로 모이므로(2026-08-19) 미리 묶는다 — 안 묶으면
    # `save_evaluation` UNIQUE 로 같은 eval_id 를 재사용하면서 라벨만 겹쳐 쌓이고,
    # 사람이 지목한 signature 가 행마다 서로 덮인다. 확정값은 **합집합**(둘 다 사람이
    # 지목한 것이라 하나를 버릴 근거가 없다), 대표 bin 은 최소 fail bin.
    merged: dict = {}
    order: list = []
    for row_key, ids in state.items():
        pk = _parse_row_key(row_key)
        if pk is None or not ids:
            continue
        bin_, item, cond = pk
        key = (item, cond)
        if key not in merged:
            merged[key] = {"bins": [], "ids": []}
            order.append(key)
        g = merged[key]
        if bin_ is not None:
            g["bins"].append(bin_)
        for sid in ids:
            if sid not in g["ids"]:
                g["ids"].append(sid)
    parsed = [(min(merged[k]["bins"]) if merged[k]["bins"] else None,
               k[0], k[1], merged[k]["ids"]) for k in order]

    # 확정 0건 + eval DB 미존재면 파일조차 만들지 않는다 (빈 DB 양산 방지 — export 와 동일).
    if not parsed and not db_path().exists():
        return {"labels": 0, "removed": 0}

    store, engine_ingest = _engine()
    alias = engine_ingest._alias_map()
    labels = 0
    with cache.keyed_lock_ctx(("eval_export", session_id)):
        conn = open_conn()
        try:
            run_id = _find_run_id(conn, session_id, ingested_by=_SIGNATURE_LABELER)
            if run_id is None:
                run_id = store.create_ingest_run({
                    "product_name": meta["product_name"], "lot_id": meta["lot_id"],
                    "source_file": str(session.get("file_name") or ""),
                    "analysis_key": session.get("analysis_key"),
                    "session_id": session_id, "ingested_by": _SIGNATURE_LABELER,
                }, conn=conn)
            store.upsert_product_master(meta, conn=conn)

            # 이 세션 run 의 기존 확정 라벨 전량 제거 후 현재 상태로 재작성(멱등).
            removed = store.delete_label_with_signatures(
                "labeler=? AND eval_id IN (SELECT eval_id FROM evaluation WHERE run_id=?)",
                (_SIGNATURE_LABELER, run_id), conn=conn)

            for bin_, item, cond, ids in parsed:
                item_canonical = alias.get(item, engine_ingest._canonicalize(item))
                item_id = store.resolve_item_id(item, conn=conn)
                if item_id is None:
                    item_id = store.upsert_item_master(
                        item_canonical, item, None, None, "NON_TRIM", None, "PF",
                        None, conn=conn)
                    store.upsert_item_alias(item, item_id, conn=conn)
                # cond 는 코멘트 export 와 같은 값을 줘야 한다 — 안 그러면 같은 TEMP 행의
                # 코멘트 라벨과 signature 라벨이 서로 다른 case 로 갈라진다.
                case_id = store.make_case_id(meta["product_name"], meta["lot_id"], None,
                                             item_id, None, meta["revision"], cond)
                store.upsert_fail_case(case_id, meta["product_name"], meta["lot_id"],
                                       None, item_id, bin_, meta["revision"], None,
                                       cond, conn=conn)
                store.link_run_case(run_id, case_id, conn=conn)
                # 사람 라벨을 매달 자리(eval_id)를 만든다 — status 는 비운다.
                # scoring(관리자 채점)은 human_status 가 있는 라벨만 세므로 오염되지 않는다.
                eval_id = store.save_evaluation(case_id, run_id, _SIGNATURE_ENGINE_VERSION,
                                                None, None, None, None, None, conn=conn)
                label_id = store.insert_label(case_id, eval_id, None, None, None,
                                              None, 0, None, _SIGNATURE_LABELER,
                                              None, "manual", conn=conn)
                store.save_label_signatures(label_id, ids, conn=conn)
                labels += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"labels": labels, "removed": removed}


def safe_sync_signatures(session_id: str, *, report_db, upload_root=None) -> dict:
    """sync_session_signatures 실패 격리 — 편집 저장은 이미 끝났으므로 죽이지 않는다."""
    try:
        return sync_session_signatures(session_id, report_db=report_db,
                                       upload_root=upload_root)
    except Exception:
        logger.warning("signature 라벨 동기화 실패 (session=%s)", session_id, exc_info=True)
        return {"skipped": "error"}


# ── 평가 스냅샷 수집 (L1~L4 를 report_server 소유 DB 에 적재) ─────────────────
# 왜 필요한가: 운영 조회 경로는 evaluate(persist=False) 라 엔진이 콜드 빌드마다 L1/L2 를
# 전부 계산해 놓고 버린다. 그래서 features/evaluation/case_signature 가 0행이고,
# 룰 채점·표본 검수·임계값 what-if 의 재료가 통째로 없다. 업로드 직후 1회만 판단 근거를
# 남긴다(코멘트는 만들지 않는다 — generate_comment=False 로 LLM·선례검색 비용 0).

_SNAPSHOT_INGESTED_BY = "eval-snapshot"     # 코멘트 export run(web_report)과 구분하는 표식


def _snapshot_source_file(source_index: int) -> str:
    """ingest_run.source_file 에 소스 번호를 실어 세션×소스 단위 중복 판정을 가능하게 한다.

    ingest_run 에는 source_index 컬럼이 없다(스키마 무변경 방침). 기존 코멘트 export 가
    이 컬럼에 'web_report' 를 쓰므로 접두를 달리해 서로 침범하지 않는다.
    """
    return f"{_SNAPSHOT_INGESTED_BY}#{int(source_index)}"


def _snapshot_done(conn, session_id: str, source_file: str, engine_version: str) -> bool:
    """이 (세션, 소스, engine_version) 스냅샷이 이미 있나 — 재업로드·재조회 중복 방지.

    run 만 보지 않고 evaluation 까지 확인한다. run 은 만들어졌는데 도중에 실패한 경우를
    "수집됨" 으로 읽으면 그 소스가 영영 비게 된다.
    """
    row = conn.execute(
        """SELECT 1 FROM ingest_run ir
             JOIN evaluation ev ON ev.run_id = ir.run_id
            WHERE ir.session_id=? AND ir.source_file=? AND ev.engine_version=?
            LIMIT 1""",
        (session_id, source_file, engine_version)).fetchone()
    return row is not None


def collect_session_snapshot(session_id: str, *, report_db, upload_root,
                             tables=None, force: bool = False) -> dict:
    """세션의 평가 판단 근거(L1~L4)를 report_server 소유 eval DB 에 1회 적재.

    운영 조회(service.load_webreport)·AI Comment 와 **같은 변형 경로**를 거친다
    (loader.load_tables → mode_tables → ai_comment._table_to_raw_df) — 그래야 표본함에서
    보는 근거가 사용자가 보는 판정과 같은 것이 된다.

    - 저장 게이트(`present.should_store`)를 통과한 case 만 쌓인다. per-DUT 원본값은
      저장하지 않는다(불변 규칙 3).
    - `EVAL_DB_PATH`(엔진 소유 eval.db)는 건드리지 않는다 — `db_path()` 를 인자로 넘긴다.
    - `force=False` 면 이미 수집된 (세션, 소스, engine_version) 은 건너뛴다. `force=True`
      는 **지우지 않고 새 run 으로 다시 쌓는다** — 기존 evaluation 을 지우면 거기 달린
      사람 라벨까지 잃기 때문이다. 표본 조회는 case 별 최신 evaluation 을 본다.

    반환: {"sources": n, "collected": n, "skipped": n, "cases": n} 또는 {"skipped": 사유}.
    """
    upload_root = Path(upload_root)
    session = report_db.get_session(session_id)
    if not session or str(session.get("source") or "") != "web_report":
        return {"skipped": "not a web_report session"}
    if not session.get("analysis_key"):
        return {"skipped": "no analysis_key"}

    from . import ai_comment
    if ai_comment._session_meta(session, 1) is None:
        return {"skipped": f"unsupported product_type: {session.get('product_type')!r}"}

    evaluate, engine_version = _engine_eval()

    if tables is None:
        from . import loader
        from .validation import mode_tables
        _, tables, manifest = loader.load_tables(session_id, report_db=report_db,
                                                 upload_root=upload_root, session=session)
        tables = mode_tables(tables, str(session.get("mode") or "Normal"))
        selected = {str(v) for v in (manifest.get("selected_items") or []) if str(v)}
    else:
        selected = set()

    collected = skipped = n_cases = 0
    # 코멘트 export 와 같은 세션 키로 직렬화 — 같은 DB 파일을 두 경로가 함께 쓴다.
    with cache.keyed_lock_ctx(("eval_export", session_id)):
        conn = open_conn()
        try:
            for idx, table in enumerate(tables):
                source_file = _snapshot_source_file(idx)
                if not force and _snapshot_done(conn, session_id, source_file,
                                                engine_version):
                    skipped += 1
                    continue
                # 표본함(룰 튜닝) 모집단은 **항상 전체 item** 이다 — 운영 평가의
                # fail-only 플래그(WEB_REPORT_EVAL_FAIL_ONLY)를 여기에는 적용하지
                # 않는다(2026-08-11 결정). fail 이 없는 항목에서만 뜨는 룰의 표본이
                # 마르면 임계값 강화안이 한쪽으로 치우친다.
                items = [c for c in table.item_columns if not selected or c in selected]
                if not items:
                    continue
                meta = dict(ai_comment._session_meta(session, idx + 1))
                # case_id 는 코멘트/패널/signature 라벨과 **같은 lot 수준 공간**이어야 한다
                # (2026-08-19). 종전엔 여기만 wafer_number=소스 순번(1,2,3…)이 들어가
                # 라벨(전부 None)과 case_id 가 100% 어긋났고, 그래서 채점 표본과 선례의
                # signature 부스트가 한 건도 성립하지 않았다. ingest_run.source_file
                # (_snapshot_source_file)이 소스를 구분하므로 wafer 축은 필요 없다 —
                # 소스가 달라도 run_id 가 다르니 raw_metrics/evaluation/features 키는
                # 그대로 소스별로 갈린다(겹치는 것은 fail_case 한 행뿐, 그게 의도다).
                meta["wafer_number"] = None
                meta["session_id"] = session_id
                meta["analysis_key"] = session.get("analysis_key")
                meta["source_file"] = source_file
                meta["ingested_by"] = _SNAPSHOT_INGESTED_BY
                raw_df = ai_comment._table_to_raw_df(table, items)
                result = evaluate({"meta": meta, "raw_df": raw_df},
                                  persist=True, db_path=str(db_path()),
                                  generate_comment=False)
                n_cases += len(result.get("cases") or [])
                collected += 1
        finally:
            conn.close()
    return {"sources": len(tables), "collected": collected, "skipped": skipped,
            "cases": n_cases}


def safe_collect_snapshot(session_id: str, *, report_db, upload_root, force=False) -> dict:
    """수집 실패 격리 — 업로드 응답·리포트 조회를 절대 죽이지 않는다(safe_export 관례)."""
    try:
        return collect_session_snapshot(session_id, report_db=report_db,
                                        upload_root=upload_root, force=force)
    except Exception as exc:
        logger.warning("eval 스냅샷 수집 실패 — 무시하고 진행 (session=%s)", session_id,
                       exc_info=True)
        try:
            report_db.log_audit(action="eval_snapshot", session_id=session_id,
                                result="error", changed_fields=repr(exc)[:500])
        except Exception:
            pass
        return {"skipped": "error"}


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


# ── eval export 큐 (단일 소비자) ──────────────────────────────────────────────
# 종전엔 export_async 가 호출마다 데몬 스레드를 띄웠다 — 코멘트 자동저장/연속 편집
# 버스트에서 세션마다 스레드가 쌓이고, keyed_lock(("eval_export", sid)) 대기 스레드가
# 적체됐다. compute 의 prewarm/distpack 과 같은 단일 소비자 + pending dedup 으로 바꾼다.
# dedup 은 "큐 대기 중"만 막는다 — 실행을 시작하면 pending 에서 빼, 실행 중 들어온 새
# 편집이 다시 큐에 올라 최신 상태로 재-export 되게 한다(eval_export 는 세션 편집 상태를
# 읽어 멱등 재적재하므로 last-write 반영이 필요하다).
_EXPORT_QUEUE = collections.deque()
_EXPORT_PENDING: set = set()        # (kind, session_id) — 큐 대기 중만
_EXPORT_LOCK = threading.Lock()
_EXPORT_WAKE = threading.Event()
_EXPORT_THREAD = None

# 큐가 처리하는 작업 2종. 스냅샷 수집을 별도 스레드로 띄우지 않고 같은 단일 소비자에
# 합류시키는 이유 — 둘 다 같은 eval DB 파일에 쓰므로 여기서 직렬화하면 파일 경합이 없다.
_JOB_EXPORT = "export"
_JOB_SNAPSHOT = "snapshot"
_JOB_SIGNATURE = "signature"


def _export_loop() -> None:
    while True:
        with _EXPORT_LOCK:
            item = _EXPORT_QUEUE.popleft() if _EXPORT_QUEUE else None
            if item is None:
                _EXPORT_WAKE.clear()
        if item is None:
            _EXPORT_WAKE.wait()
            continue
        kind, session_id, report_db, upload_root = item
        with _EXPORT_LOCK:
            _EXPORT_PENDING.discard((kind, session_id))   # 실행 시작 → 대기 해제(실행 중 새 편집 재큐 허용)
        # safe_* 가 자체 예외 격리를 하지만, 소비자 스레드가 죽지 않게 한 번 더 감싼다.
        try:
            if kind == _JOB_SNAPSHOT:
                safe_collect_snapshot(session_id, report_db=report_db,
                                      upload_root=upload_root)
            elif kind == _JOB_SIGNATURE:
                safe_sync_signatures(session_id, report_db=report_db,
                                     upload_root=upload_root)
            else:
                safe_export(session_id, report_db=report_db, upload_root=upload_root)
        except Exception:
            logger.warning("eval %s 소비자 처리 실패 (session=%s)", kind, session_id,
                           exc_info=True)


def _supervised_export_loop() -> None:
    """_export_loop 가 어떤 예외로도 끝나지 않게 감싼다.

    루프 안에 잡별 try/except 가 있지만 그 **except 블록 자체**(로깅 등)가 실패하면
    스레드가 조용히 죽는다. 그러면 코멘트 편집이 eval DB 로 영영 나가지 않는데,
    에러가 아니라 "동기화가 안 된 상태"로만 보여 발견이 늦는다.
    """
    while True:
        try:
            _export_loop()
            return
        except Exception:
            logger.error("eval export 소비자 스레드가 예외로 종료됨 — 재시작합니다",
                         exc_info=True)
            try:
                import diagnostics
                diagnostics.emit("critical", "build", "consumer_thread_died",
                                 error_type="eval_export", message="eval-export loop died")
            except Exception:
                pass
            time.sleep(1.0)


def _enqueue(kind: str, session_id: str, report_db, upload_root) -> None:
    global _EXPORT_THREAD
    with _EXPORT_LOCK:
        if (kind, session_id) in _EXPORT_PENDING:
            return   # 이미 대기 중 — 소비자가 최신 상태를 읽으므로 재등록 불요
        _EXPORT_PENDING.add((kind, session_id))
        _EXPORT_QUEUE.append((kind, session_id, report_db, Path(upload_root)))
        # is_alive 까지 보는 이유 — 죽은 스레드 핸들이 남으면 `is None` 검사가 계속
        # 통과해 소비자 없는 큐에 요청만 쌓인다(_supervised 가 있어도 최후 방어).
        if _EXPORT_THREAD is None or not _EXPORT_THREAD.is_alive():
            _EXPORT_THREAD = threading.Thread(target=_supervised_export_loop,
                                              name="eval-export", daemon=True)
            _EXPORT_THREAD.start()
    _EXPORT_WAKE.set()


def export_async(session_id: str, *, report_db, upload_root) -> None:
    """훅 전용 — 단일 소비자 스레드가 순차로 safe_export 한다 (콜드 tables 로드가 응답을
    늦추지 않게, 그리고 편집 버스트에 스레드가 무한히 쌓이지 않게)."""
    _enqueue(_JOB_EXPORT, session_id, report_db, upload_root)


def collect_async(session_id: str, *, report_db, upload_root) -> None:
    """훅 전용 — 평가 스냅샷 수집을 같은 큐에 올린다 (업로드 응답을 늦추지 않게)."""
    _enqueue(_JOB_SNAPSHOT, session_id, report_db, upload_root)


def sync_signatures_async(session_id: str, *, report_db, upload_root) -> None:
    """훅 전용 — ENGR 확정 signature 동기화를 같은 큐에 올린다.

    소비자가 편집 DB 의 최신 전체 상태를 다시 읽으므로, 빠른 연속 편집에서 큐가
    dedup 돼도 마지막 상태가 반영된다(요청값을 큐에 싣지 않는 이유)."""
    _enqueue(_JOB_SIGNATURE, session_id, report_db, upload_root)
