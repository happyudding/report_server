"""AI Comment — eval_analyzer(eval_engine) 통합의 유일한 접점.

report_server → eval_analyzer 는 단방향 의존이며, eval_engine import 는 이 모듈
안에서만 허용된다 (docs/13_eval_analyzer_integration.md). 세션 옵션
webreport_options.ai_comment 가 참인 세션의 콜드 빌드(service.load_webreport)
시점에 evaluate() 를 호출해 IssueTable row_key → 셀 텍스트 사전을 만든다 —
캐시 키(content_hash)에 묶이므로 rawdata 편집 시 자동 재평가된다.
evaluate 는 persist=False(미리보기) 로만 호출한다 — eval.db 무기록이라 컴퓨트
워커 동시 실행에도 안전하고, 선례검색(sql)은 DB 파일이 없으면 빈 목록이다.
실패는 어떤 경우에도 IssueTable 빌드를 죽이지 않는다 (safe_build 빈 dict 폴백).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

from .honeyform import META_COLUMNS, META_ROW_LABELS

logger = logging.getLogger(__name__)

_EVAL_DIR = Path(__file__).resolve().parent.parent / "eval_analyzer"

# 세션에는 family_product 가 없다 — product_taxonomy.yaml 허용 조합 중 범용(*_ETC) 폴백.
_FAMILY_FALLBACK = {
    "MDDI": "MDDI_ETC",
    "PDDI": "PDDI_ETC",
    "PMIC": "PMIC_ETC",
    "SECURITY": "SECU_ETC",
    "TCON": "TCON_ETC",
}

_SEVERITY = {"OK": 0, "MONITOR": 1, "MINOR": 2, "MAJOR": 3, "CRITICAL": 4}
_PASS_BIN = 1


def _evaluate_fn():
    """eval_engine.evaluate 지연 import (컴퓨트 워커 프로세스에서도 호출 시점 성립).

    append(후순위 삽입)라 report_server 쪽 top-level 패키지가 항상 우선한다.
    """
    path = str(_EVAL_DIR)
    if path not in sys.path:
        sys.path.append(path)
    from eval_engine import evaluate
    return evaluate


def _table_to_raw_df(table, items):
    """HoneyformTable(읽기 경로, df=None) → 엔진 정본 raw_df 레이아웃 재조립.

    columns = META 7개 + item, row0~5 = TSEQ/TNO/STEP/UNIT/HILIM/LOLIM(라벨은 SERIAL
    컬럼), row6+ = 측정 (eval_analyzer/CLAUDE.md 불변 규칙 7). 엔진 _is_num 은
    파이썬 int/float 만 인정해 np.int64 정수 컬럼이 통째로 무시되므로 item 블록을
    float64 로 강제한다 (np.float64 는 float 서브클래스라 통과).
    """
    meta_rows = {"TSEQ": table.tseq, "TNO": table.tno, "STEP": table.step,
                 "UNIT": table.units, "HILIM": table.hilim, "LOLIM": table.lolim}
    head = []
    for label in META_ROW_LABELS:
        src = meta_rows[label] or {}
        row = {col: "" for col in META_COLUMNS}
        row["SERIAL"] = label
        for item in items:
            row[item] = src.get(item)
        head.append(row)
    head_df = pd.DataFrame(head, columns=META_COLUMNS + items)
    body = table.data.loc[:, META_COLUMNS + items].copy()
    body[items] = body[items].astype("float64")
    return pd.concat([head_df, body], ignore_index=True)


def _session_meta(session, wafer_number):
    """세션 dict → evaluate meta. product_type 이 허용 5종 밖이면 None(평가 스킵).

    family_product 는 세션 실제값을 우선 사용하고, 비어 있으면(구세션) *_ETC 폴백.
    그 외 세션에 없는 필수 필드도 폴백으로 채운다: revision 숫자 변환 실패=0.0,
    wafer_number=소스 순번(실제 wafer 번호 아님).
    """
    product_type = str(session.get("product_type") or "").strip()
    family = str(session.get("family_product") or "").strip() or _FAMILY_FALLBACK.get(product_type)
    if not family:
        return None
    try:
        revision = float(str(session.get("revision") or "").strip() or 0)
    except ValueError:
        revision = 0.0
    return {
        "product_name": str(session.get("product") or "").strip() or "UNKNOWN",
        "product_type": product_type,
        "family_product": family,
        "revision": revision,
        "lot_id": str(session.get("lot_id") or "").strip(),
        "wafer_number": int(wafer_number),
    }


def _cell_text(case):
    status = str(case.get("status") or "").strip()
    comment = str(case.get("comment") or "").strip()
    return f"[{status}] {comment}" if status else comment


def _sev(case):
    return _SEVERITY.get(str(case.get("status") or ""), -1)


def _to_row_keys(cases_by_key):
    """(item_raw, bin) case 들 → IssueTable row_key 사전.

    row_key 규약(tabs/issue_table.py): fail bin(≠1) 케이스는 Yield|<bin>|<item>,
    item 별 worst 케이스를 CPK|<item> / ETC|<item> 폴백으로도 채운다 — CPK/ETC
    섹션 행은 bin 이 없어 item 만으로 매칭하고, 미사용 키는 그냥 버려진다.
    """
    out = {}
    worst_by_item = {}
    for (item, bin_), case in cases_by_key.items():
        if not item:
            continue
        if bin_ is not None and bin_ != _PASS_BIN:
            out[f"Yield|{bin_}|{item}"] = _cell_text(case)
        prev = worst_by_item.get(item)
        if prev is None or _sev(case) > _sev(prev):
            worst_by_item[item] = case
    for item, case in worst_by_item.items():
        out.setdefault(f"CPK|{item}", _cell_text(case))
        out.setdefault(f"ETC|{item}", _cell_text(case))
    return out


def build_ai_comments(tables, session, selected_items=None):
    """tables(모드 변형 후) 를 소스별로 evaluate 해 row_key → 셀 텍스트 사전 반환.

    selected_items 필터는 build_report_payload 의 in-place 필터와 동일 집합으로
    적용한다(미선택 item 평가 회피). 여러 소스에서 같은 (item, bin) 케이스가 나오면
    severity 높은 쪽이 남는다.
    """
    evaluate = _evaluate_fn()
    selected = {str(v) for v in (selected_items or []) if str(v)}
    best = {}
    for idx, table in enumerate(tables):
        meta = _session_meta(session, idx + 1)
        if meta is None:
            logger.warning("ai_comment: product_type=%r 는 평가 대상이 아님 — 건너뜀",
                           session.get("product_type"))
            return {}
        items = [c for c in table.item_columns if not selected or c in selected]
        if not items:
            continue
        raw_df = _table_to_raw_df(table, items)
        result = evaluate({"meta": meta, "raw_df": raw_df}, persist=False)
        for case in result.get("cases") or []:
            key = (str(case.get("item_raw") or ""), case.get("bin"))
            prev = best.get(key)
            if prev is None or _sev(case) > _sev(prev):
                best[key] = case
    return _to_row_keys(best)


def safe_build(tables, session, selected_items=None):
    """build_ai_comments 실패 격리 — 예외 시 warning 로그 + 빈 dict.

    반환이 dict 인 한(빈 dict 포함) IssueTable 에 AI Comment 컬럼은 표시된다
    (값만 비어 있음). None 을 반환하지 않는다 — 컬럼 표시 여부는 호출자(service)의
    옵션 판정이 결정한다.
    """
    try:
        return build_ai_comments(tables, session, selected_items)
    except Exception:
        logger.warning("ai_comment 빌드 실패 — 빈 값으로 진행 (session=%s)",
                       session.get("session_id"), exc_info=True)
        return {}
