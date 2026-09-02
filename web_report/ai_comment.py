"""AI Comment — eval_analyzer(eval_engine) 통합의 유일한 접점.

report_server → eval_analyzer 는 단방향 의존이며, eval_engine import 는 이 모듈
안에서만 허용된다 (docs/13_eval_analyzer_integration.md). 세션 옵션
webreport_options.ai_comment 가 참인 세션의 콜드 빌드(service.load_webreport)
시점에 evaluate() 를 호출해 IssueTable row_key → 셀 텍스트 사전을 만든다 —
캐시 키(content_hash)에 묶이므로 rawdata 편집 시 자동 재평가된다.
evaluate 는 persist=False(미리보기) 로만 호출한다 — eval.db 무기록이라 컴퓨트
워커 동시 실행에도 안전하고, 선례검색(sql)은 DB 파일이 없으면 빈 목록이다.
실패는 어떤 경우에도 IssueTable 빌드를 죽이지 않는다 (safe_build 빈 dict 폴백).

엔진 사설 계약 핀(엔진 변경 시 함께 확인):
  present.to_result 의 case["signatures"][].evidence[] — BIMODALITY 의 note 포맷
  "modality_v2 <label>" 을 _modality_tag 가 파싱한다 (signatures._evaluate_subpop_gap).
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import pandas as pd

from .honeyform import META_COLUMNS, META_ROW_LABELS
from .validation import (validate_mode, webreport_ai_no_suggest,
                         webreport_eval_overrides, webreport_temperature_groups)

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

# 평가 범위 — 1(기본)=Issue Table 에 행이 생기는 item 만: fail item(FAILTNO 1chip 이상,
# Yield 섹션) ∪ CPK 섹션 후보(worst Bin1 cpk<1.33, 2026-09-01 — eval_fail_scope).
# 0=전체 item(종전 동작). 토글은 server.env 수정 + 서버 재기동.
# ⚠ 이 필터는 **item 컬럼만** 줄인다. 선택된 item 의 chip 행(측정 행)은 전량 유지해야
# 엔진 L1/L2 가 전체 분포(cpk·이봉·outlier) 대비 fail 을 볼 수 있다 — fail chip 만
# 남기는 행 필터는 만들지 말 것.
_FAIL_ONLY = (os.getenv("WEB_REPORT_EVAL_FAIL_ONLY", "1") or "1").strip().lower() \
    in ("1", "true", "yes", "on")

# 평가 스킵/실패 시 반환 형태 — 키 구성은 정상 반환과 동일해야 한다(호출부 분기 없음).
# 키를 늘릴 때 여기도 같이 늘려야 예외 폴백에서 KeyError 로 빌드가 죽지 않는다.
# prompts (2026-08-28): 클라 LLM 대행용 {item_raw: {"prompt","sha"}} — docs/23.
# precedents / precedent_counts (2026-09-02): 사례 상세 팝오버(라우트 조회)와 셀 아래
# 「📋 사례 N건 상세」 링크(payload) 의 재료 — docs/23.
# llm_enabled (2026-09-02): 서버 LLM 배선 상태 — 화면 처리 주체 아이콘의 기본값 재료.
_EMPTY_RESULT = {"comments": {}, "etc_auto_items": [], "row_signatures": {},
                 "signature_options": [], "prompts": {},
                 "precedents": {}, "precedent_counts": {}, "llm_enabled": False}

# 사례 팝오버에 실을 선례 필드 — 엔진 present._precedent_result 계약의 부분집합.
# 전량(metrics/features 전체)을 싣지 않는 이유: 화면이 읽는 것만 남겨 캐시 파일과 응답을
# 가볍게 유지한다(선례 5건 × 세션 수백 item).
# session_id (2026-09-02): 팝오버의 "세션 열기 ↗" 링크 — 그 코멘트가 저장됐던 세션으로
# 바로 간다. 엔진은 계약 dict 에 담아 주는데(present._precedent_result) 여기서 빠져 있어
# 화면 코드(sig_reason.js aicPrecRowHtml)가 링크를 그릴 재료를 못 받고 있었다.
_PREC_VIEW_KEYS = ("product_name", "lot_id", "item_canonical", "unit",
                   "status", "signature", "similarity", "comment", "session_id")
_PREC_VIEW_METRICS = ("cpk", "yield", "mean", "stdev", "fail_count", "total_count")

# ENGR 가 "해당 없음/새 유형" 으로 지목할 때 쓰는 값.
# 2026-08-12 부터 **엔진도 같은 id 로 자동 발화한다** — fail 인데 어떤 룰도 안 뜨면
# signature 0건(화면 "미분류")으로 두지 않고 UNKNOWN 을 명시 발화해 사유까지 남긴다
# (eval_analyzer signatures._evaluate_unknown). 사람이 확정한 라벨과는 편집행 유무
# (`_sigrev`)로 계속 구분되고, **커버율 집계에서는 UNKNOWN 을 빼고 센다** — 자동 발화를
# 성과로 세면 커버율이 가짜로 100% 가 되기 때문(eval_debug._coverage).
UNKNOWN_SIGNATURE = "UNKNOWN"

# 이봉(BIMODALITY) 배지 — 엔진은 primary_signature 일 때만 코멘트 본문에 이봉 문구를
# 쓰는데(recommend._phenomenon_text), BIMODALITY 는 specificity 순위가 낮아 같은 MAJOR 인
# 공간 룰 등에 밀리기 쉽다. 그래서 발화 사실 자체를 case["signatures"] 에서
# 직접 읽어 status 뒤에 붙인다 — primary/secondary 를 구분하지 않는다.
# (2026-08-12 개명: SUBPOP_GAP → BIMODALITY)
_SUBPOP_SIG_ID = "BIMODALITY"
_MODALITY_SIGNAL = "MODALITY_V2"
_MODALITY_RE = re.compile(r"modality_v2\s+(\w+)")
# "제안 제외" 세션에서 [제안] 섹션을 통째로 걷어내는 패턴 — 토큰부터 끝까지.
# 신·구 토큰 둘 다(캐시에 굳은 옛 코멘트도 같은 화면을 내야 한다). 교대는 왼쪽 우선이라
# 긴 옛 토큰(점검제안)을 앞에 둔다.
_SUGG_SEC_RE = re.compile(r"\s*\[(?:점검제안|제안)\].*$", re.S)
# multimodal 은 **배지를 붙이지 않는다**(2026-08-13 사용자 요청) — 값이 빈 문자열이라
# 아래 폴백([분포분리])도 타지 않는다. 판정·목록에는 그대로 남고 셀 표기만 생략한다.
_MODALITY_TAG = {"bimodal": "[이봉]", "multimodal": "", "separated": "[분리]"}
# note 포맷이 바뀌어도 "발화했다" 는 사실은 잃지 않는다 (조용한 미표시 방지).
_MODALITY_TAG_FALLBACK = "[분포분리]"


def fail_only_enabled() -> bool:
    """서버 기본 평가 범위가 fail item 만인가 (env WEB_REPORT_EVAL_FAIL_ONLY)."""
    return _FAIL_ONLY


def eval_fail_scope(tables, session=None, selected=None):
    """평가 대상 item 집합 = fail item ∪ Issue Table CPK 섹션 후보. 소스 합집합.

    - fail item: fail(FAILTNO==TNO) 이 1chip 이상 — Yield 탭 / Issue Table 의 fail 귀속
      규칙 그대로(tabs.distribution.fail_items → yield_tab.tno_to_item_map). 모드 구분 없이
      같은 기준을 쓴다 — Temperature 의 CT/HT RT-limit 재판정은 저장된 FAILTNO 와 다르지만,
      그 표(Issue Table Temp)는 AI Comment 대상에서 제외했으므로 어긋나지 않는다.
    - CPK 섹션 후보(2026-09-01): fail 은 없지만 worst Bin1 cpk 가 임계값 미만이라 Issue
      Table CPK 섹션에 **행이 생기는** item — 행 멤버십과 같은 함수
      (tabs.issue_table.cpk_issue_subjects)를 쓴다. 여기는 **누구를 평가할지** 만 정한다.
      LOW_CPK 가 뜨는지는 엔진이 자기 threshold(/pe/eval 오버레이 + 세션 민감도 override)로
      판정하며, 기준을 바꿔 안 뜨면 화면은 "미분류" 다 — 탭 임계값으로 서버가 덧붙이지
      않는다(사용자 설정을 무시하게 된다).
    - `session` 이 있으면 Temperature 세션은 RT source 만으로 cpk 후보를 잰다(Issue Table 과
      같은 테이블 — metrics.temperature_yield_tables). `selected` 는 selected_items 집합 —
      service 경로의 tables 는 build_report_payload 의 in-place 필터 **이전** 객체라 여기서
      걸러야 미선택 item 의 cpk 를 헛계산하지 않는다. 둘 다 None 이면 전 테이블·전 item.
    """
    from .tabs.distribution import fail_items
    scope = set(fail_items(tables))
    scope |= _cpk_issue_scope(tables, session, selected, exclude=scope)
    return scope


def _cpk_issue_scope(tables, session, selected, exclude) -> set:
    """Issue Table CPK 섹션 후보 item 집합 (eval_fail_scope 의 cpk 쪽 절반).

    cpk 는 CPK 탭 정본(tabs.cpk.build_cpk_rows, Bin1)으로만 계산한다 — 공식 사본 금지
    (CLAUDE.md 규칙 13). `exclude`(이미 스코프인 fail item)는 후보에서 빼 계산량을 줄인다.
    """
    from .tabs.cpk import build_cpk_rows
    from .tabs.issue_table import cpk_issue_subjects
    cpk_tables = list(tables or ())
    if session is not None:
        from .metrics import temperature_yield_tables
        groups = webreport_temperature_groups(session.get("webreport_options") or "",
                                              [t.source for t in cpk_tables])
        cpk_tables = temperature_yield_tables(
            cpk_tables, validate_mode(session.get("mode")), groups)
    candidates = sorted({c for t in cpk_tables for c in t.item_columns
                         if (not selected or c in selected) and c not in exclude})
    if not candidates:
        return set()
    rows = build_cpk_rows(cpk_tables, candidates)
    return {subject for subject, _ in
            cpk_issue_subjects(rows, [t.source for t in cpk_tables])}


def _eval_items(table, selected, fail_set):
    """이 소스에서 엔진에 넘길 item 컬럼 — selected_items 필터 + (옵션) fail 필터.

    ai_comment / eval_debug / eval 스냅샷이 같은 식을 쓰도록 여기로 모은다.
    fail_set 이 None 이면 종전(전체 item) 동작.
    """
    return [c for c in table.item_columns
            if (not selected or c in selected) and (fail_set is None or c in fail_set)]


def _evaluate_fn():
    """eval_engine.evaluate 지연 import (컴퓨트 워커 프로세스에서도 호출 시점 성립).

    append(후순위 삽입)라 report_server 쪽 top-level 패키지가 항상 우선한다.
    """
    path = str(_EVAL_DIR)
    if path not in sys.path:
        sys.path.append(path)
    from eval_engine import evaluate
    return evaluate


def llm_status(*, ping=False):
    """엔진 LLM 배선 상태 — 설정 출처·해석된 URL·활성 여부(+선택적 실호출 1회).

    엔진 접근은 이 파일을 통해서만 한다는 규약(불변규칙 #8) 때문에, 배선 점검 도구
    ([tools/llm_check.py](../tools/llm_check.py))도 eval_engine 을 직접 열지 않고 여기를 부른다.

    ``ping=True`` 면 실제로 한 번 호출해 왕복을 확인한다 — 설정만 맞고 서버가 안 뜬 경우를
    "켜짐" 으로 오독하지 않기 위해서다. 실패는 예외가 아니라 ``error`` 문자열로 담는다.
    """
    out = {"enabled": False, "endpoint_raw": "", "endpoint_resolved": "", "model": "",
           "timeout": None, "api_key_set": False, "error": None, "reply": None}
    try:
        path = str(_EVAL_DIR)
        if path not in sys.path:
            sys.path.append(path)
        from eval_engine import config as eval_config, llm_client
    except Exception as exc:                      # 엔진 폴더가 없거나 import 실패
        out["error"] = f"engine import 실패: {exc}"
        return out

    out.update(enabled=bool(llm_client.is_enabled()),
               endpoint_raw=eval_config.EVAL_LLM_ENDPOINT,
               endpoint_resolved=llm_client.chat_url(),
               model=eval_config.EVAL_LLM_MODEL,
               timeout=eval_config.EVAL_LLM_TIMEOUT,
               api_key_set=bool(eval_config.EVAL_LLM_API_KEY))
    if ping and out["enabled"]:
        try:
            out["reply"] = (llm_client.complete("ping. 한 단어로만 답하세요.") or "")[:200]
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def signature_catalog() -> list:
    """ENGR 가 고를 수 있는 signature 목록 — [{"id", "enabled"}, ...] + UNKNOWN.

    사람 정답 라벨용이므로 **현재 비활성(enabled:false)인 룰도 포함**한다 — 발화는 안
    하지만 "이게 맞는 유형"으로 지목할 수는 있어야 한다. enabled 는 기준값
    (signatures.yaml) 기준이고 제품군 오버레이는 반영하지 않는다(화면 안내용 표시).
    """
    path = str(_EVAL_DIR)
    if path not in sys.path:
        sys.path.append(path)
    from eval_engine.pipeline._rules import signatures_doc
    out = [{"id": str(s.get("id")), "enabled": s.get("enabled") is not False}
           for s in (signatures_doc().get("signatures") or []) if s.get("id")]
    # UNKNOWN 은 2026-08-12 부터 엔진 룰로도 선언돼 있다(미분류 명시 발화) — 카탈로그에
    # 두 번 실리지 않게 이미 있으면 덧붙이지 않는다. 사람 라벨용 선택지로는 항상 있어야
    # 하므로 없을 때만 보탠다(구 룰 파일 호환).
    if not any(s["id"] == UNKNOWN_SIGNATURE for s in out):
        out.append({"id": UNKNOWN_SIGNATURE, "enabled": True})
    return out


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
        # 선례검색 자기 세션 제외용(시간 누출 차단) — 엔진이 case_ctx 로 넘긴다
        "session_id": str(session.get("session_id") or "") or None,
        "analysis_key": str(session.get("analysis_key") or "") or None,
    }


def _modality_tag(case):
    """BIMODALITY 발화 시 이봉/다봉/분리 배지 문자열, 아니면 "".

    primary 인지 secondary 인지 보지 않는다 — 발화 사실만으로 붙인다.
    """
    for sig in case.get("signatures") or []:
        if sig.get("id") != _SUBPOP_SIG_ID:
            continue
        for ev in sig.get("evidence") or []:
            if ev.get("signal_code") != _MODALITY_SIGNAL:
                continue
            m = _MODALITY_RE.search(str(ev.get("note") or ""))
            if m:
                return _MODALITY_TAG.get(m.group(1), _MODALITY_TAG_FALLBACK)
        return _MODALITY_TAG_FALLBACK
    return ""


def _drop_suggestion(comment: str) -> str:
    """[제안] 섹션을 통째로 뺀 코멘트 — "제안 제외" 세션 전용 (2026-09-02).

    사용자가 "사례만 보고 판단하겠다" 고 켠 옵션이라 조치 문장을 화면에 두지 않는다.
    ⚠ 섹션 **토큰 자체를 지운다**(값만 비우지 않는다) — 빈 `[제안]` 이 남으면 화면에
    라벨만 덩그러니 뜨고, 사용자에게는 "제안이 만들어지다 말았다" 로 보인다.
    [현상]/[사례] 는 그대로 두어 3섹션 파서(신·구 토큰)가 계속 앞 두 섹션을 읽는다.
    """
    return _SUGG_SEC_RE.sub("", comment).rstrip()


def _cell_text(case, no_suggest: bool = False):
    status = str(case.get("status") or "").strip()
    comment = str(case.get("comment") or "").strip()
    if no_suggest and comment:
        comment = _drop_suggestion(comment)
    tag = _modality_tag(case)
    prefix = f"[{status}]{tag}" if status else tag
    return f"{prefix} {comment}".strip() if prefix else comment


def rank_key(status, has_modality):
    """소스 간 대표 케이스 선택 순위 — severity 동률이면 이봉 발화 쪽을 남긴다.

    `_rank` 에서 분리해 공개한 이유: Signature 근거 팝업이 eval DB 행(evaluation.status +
    case_signature 에 BIMODALITY 존재)에서 **같은 대표 케이스**를 골라야 화면 코멘트와
    팝업 근거가 어긋나지 않는다. `_modality_tag` 는 BIMODALITY 발화 시에만 비지 않으므로
    DB 쪽 has_modality = case_signature 에 BIMODALITY 행 존재 와 1:1 대응한다.
    """
    return (_SEVERITY.get(str(status or ""), -1), 1 if has_modality else 0)


def _rank(case):
    return rank_key(case.get("status"), bool(_modality_tag(case)))


def _case_sig_ids(case) -> list:
    """이 케이스에서 발화한 signature id — primary 먼저, 그다음 secondary(발화 순).

    엔진은 발화 목록 전체를 case["signatures"](role 포함)로 돌려주는데 코멘트 본문은
    primary 하나로만 쓰인다. Issue Table Signature 컬럼은 전부 보여주기 위해 여기서
    목록을 그대로 꺼낸다 (억제된 룰은 엔진이 이미 목록에서 뺐다 — suppressed_by).
    """
    rows = case.get("signatures") or []
    primary = [str(s.get("id")) for s in rows if s.get("role") == "primary" and s.get("id")]
    rest = [str(s.get("id")) for s in rows
            if s.get("role") != "primary" and s.get("id") and str(s.get("id")) not in primary]
    return primary + rest


def fail_bins_by_item(tables) -> dict:
    """{item: [fail bin…]} — Issue Table 에 Yield 행이 생기는 (bin, item) 조합.

    엔진 case 가 **item 당 1개**가 된 뒤로(2026-08-19) case 에서는 그 item 이 어느 bin 행에
    걸리는지 알 수 없다. 그래서 화면 행 구성의 정본인 `yield_tab.fail_counts_by_source`
    (Issue Table Yield 행을 만드는 바로 그 집계)에서 가져온다 — 다른 데서 세면 코멘트가
    붙는 행과 실제로 그려지는 행이 갈린다(CLAUDE.md 규칙 13, 재계산 금지).
    Pass bin(1)은 fail 행이 아니므로 제외한다.
    """
    from .tabs.yield_tab import fail_counts_by_source
    out: dict = {}
    for table in tables or ():
        for (bin_value, item), cnt in fail_counts_by_source(table).items():
            if not cnt or not item:
                continue
            try:
                b = int(float(bin_value))
            except (TypeError, ValueError):
                continue
            if b == _PASS_BIN:
                continue
            bins = out.setdefault(str(item), [])
            if b not in bins:
                bins.append(b)
    return {k: sorted(v) for k, v in out.items()}


def _to_row_keys(cases_by_item, fail_bins=None, with_comments: bool = True,
                 no_suggest: bool = False):
    """item 별 case → {"comments": row_key 사전, "etc_auto_items": [...]}.

    row_key 규약(tabs/issue_table.py): 그 item 이 걸리는 **모든 fail bin 행**
    `Yield|<bin>|<item>` 에 같은 셀을 채우고(fan-out), CPK|<item> / ETC|<item> 도 채운다.
    미사용 키는 그냥 버려진다.

    **왜 fan-out 인가** (2026-08-19): 엔진 case 가 item 당 1개가 되면서 판정도 item 단위가
    됐다 — 그 item 의 fail 전체를 보고 낸 결론이므로 어느 bin 행에 놓든 같은 값이 맞다.
    대표 bin 행에만 넣으면 나머지 Yield 행이 빈 셀이 되어 명백한 회귀가 된다.
    `fail_bins` 는 `fail_bins_by_item(tables)` 결과이며, 없으면(구 호출부·테스트) case 의
    대표 bin 한 행에만 채운다.

    etc_auto_items = fail 이 하나도 없는데(=Issue Table 에 Yield 행이 생기지 않는데)
    signature 가 발화한 item. 수율·cpk 는 정상인데 분포만 이상한 항목이 표 어디에도
    안 나오던 공백을 ETC 섹션 자동 행으로 메운다.
    cpk<1.33 항목과의 중복 제외는 cpk_rows 를 가진 issue_table 쪽이 한다.

    row_signatures = 같은 row_key 규약으로 담은 **발화 signature id 목록**. Issue Table
    Signature 컬럼의 엔진 제안값이며, 발화가 없으면 키 자체를 만들지 않는다(그 행은
    화면에서 "미분류"로 보인다).
    """
    out = {}
    sigs = {}
    fail_bin_items, fired_items = set(), set()
    for item, case in cases_by_item.items():
        if not item:
            continue
        ids = _case_sig_ids(case)
        # with_comments=False = Signature 만 먼저 내는 1단계 빌드. 셀 텍스트를 빈 문자열로
        # 두면 아래 대입이 전부 "" 를 쓰고 호출부가 그 dict 를 그대로 화면에 태울 때
        # "Loading 중…" 이 유지된다 (build_ai_comments docstring 의 ⚠ 참조).
        text = _cell_text(case, no_suggest) if with_comments else ""
        bins = (fail_bins or {}).get(item)
        if bins is None:
            # 폴백 — case 의 대표 bin 한 행만(구 호출부 호환).
            rep = case.get("bin")
            bins = [rep] if (rep is not None and rep != _PASS_BIN) else []
        for b in bins:
            if with_comments:
                out[f"Yield|{b}|{item}"] = text
            if ids:
                sigs[f"Yield|{b}|{item}"] = ids
            fail_bin_items.add(item)
        # UNKNOWN(미분류 명시 발화)만 있는 케이스는 ETC 자동 행을 만들지 않는다 —
        # 자동 행의 취지는 "표 어디에도 안 나오는데 룰이 뭔가 잡아낸 항목" 이라,
        # 설명하지 못했다는 표시만으로 표를 늘리면 취지에 반한다.
        if any(str(s.get("id")) != UNKNOWN_SIGNATURE for s in (case.get("signatures") or [])):
            fired_items.add(item)
        if with_comments:
            out.setdefault(f"CPK|{item}", text)
            out.setdefault(f"ETC|{item}", text)
        if ids:
            sigs.setdefault(f"CPK|{item}", ids)
            sigs.setdefault(f"ETC|{item}", ids)
    return {"comments": out, "etc_auto_items": sorted(fired_items - fail_bin_items),
            "row_signatures": sigs, "signature_options": signature_catalog()}


def _precedent_views(best: dict) -> dict:
    """{item_raw: [사례 dict…]} — 사례 상세 팝오버가 읽는 화면용 목록.

    재료는 엔진이 case 에 실어 준 `precedents[]`(present._precedent_result 계약) 그대로다.
    **코멘트가 없는 선례는 뺀다** — 프롬프트 재료 기준(`ai_prompt._precedent_count`)과 같아야
    화면의 "N건" 과 LLM 이 받은 사례 수가 어긋나지 않는다.
    """
    out = {}
    for item, case in (best or {}).items():
        rows = []
        for p in case.get("precedents") or []:
            comment = str((p or {}).get("comment") or "").strip()
            if not comment:
                continue
            row = {k: p.get(k) for k in _PREC_VIEW_KEYS if p.get(k) is not None}
            row["comment"] = comment
            metrics = {k: (p.get("metrics") or {}).get(k)
                       for k in _PREC_VIEW_METRICS
                       if (p.get("metrics") or {}).get(k) is not None}
            if metrics:
                row["metrics"] = metrics
            rows.append(row)
        if rows:
            out[str(item)] = rows
    return out


def _precedent_counts(prec_views: dict, comments: dict) -> dict:
    """{row_key: 사례 건수} — payload 로 나가 셀 아래 링크를 그린다.

    row_key fan-out 규약은 `_to_row_keys` 와 **같다**(Yield|bin|item / CPK|item / ETC|item).
    이미 만들어진 comments 의 키를 재사용해 규약 사본을 늘리지 않는다(규칙 13) — 어느
    행에 코멘트가 붙었는지가 곧 어느 행에 링크가 붙어야 하는지다.
    """
    if not prec_views or not comments:
        return {}
    out = {}
    for key in comments:
        for item, rows in prec_views.items():
            if key.endswith("|" + item):
                out[key] = len(rows)
                break
    return out


def _prompt_enrich(best: dict, tables) -> dict:
    """클라 대행 프롬프트의 **현재 케이스 재료** — {item_raw: ai_prompt 의 enrich}.

    unit/limit 과 L1 통계를 담는다. 계산은 `eval_export` 의 것을 그대로 쓴다 — 선례의
    `raw_metrics` 를 만든 바로 그 산식이라 "그때 cpk vs 지금 cpk" 가 같은 자로 잰 값이
    된다(CLAUDE.md 규칙 13, 재계산 금지).

    여기서 채우지 않는 것 2가지:
    - 현재 L2 — 발화 signature 의 evidence 값이 이미 case dict 에 있다(ai_prompt 가 쓴다).
    - **과거 선례 상세** — 엔진이 case["precedents"][] 에 실어 준다
      (store.search_precedents JOIN → present._precedent_result).

    프롬프트 보강은 부가 기능이라 **실패해도 코멘트 생성은 계속돼야 한다** — 어떤
    예외도 밖으로 내보내지 않고 빈 dict(= 현재 통계 줄 없는 프롬프트)로 떨어진다.
    """
    try:
        from . import eval_export
        out = {}
        for item, case in best.items():
            entry = {}
            meta = eval_export._find_item_meta(tables, item)
            if meta:
                entry.update(unit=meta.get("unit"), lsl=meta.get("lsl"),
                             usl=meta.get("usl"))
            stats = dict(eval_export._dist_metrics(tables, item))
            rep_bin = case.get("bin")
            if rep_bin is not None:
                stats.update(eval_export._yield_metrics(tables, item, rep_bin))
            if stats:
                entry["stats"] = stats
            out[item] = entry
        return out
    except Exception:
        logger.warning("ai_comment: 프롬프트 보강 재료 조립 실패 — 기본 프롬프트로 진행",
                       exc_info=True)
        return {}


def _ai_prompt_rules() -> dict:
    """운영자 지시문(`/pe/eval` AI 지시문 탭) — 실패해도 프롬프트는 만들어야 한다.

    `eval_debug` 는 엔진 import 허용 3곳 중 하나이자 룰 파일 읽기 창구다. 그쪽이 이미
    예외를 삼키지만(빈 목록 반환), 모듈 부재 같은 import 단계 실패까지 여기서 막는다.
    """
    try:
        from . import eval_debug
        return eval_debug.ai_prompt_rules()
    except Exception:                                   # noqa: BLE001
        logger.warning("ai_comment: 지시문 로딩 실패 — 기본 프롬프트로 진행", exc_info=True)
        return {}


def build_ai_comments(tables, session, selected_items=None, fail_only=None,
                      generate_comment: bool = True):
    """tables(모드 변형 후) 를 소스별로 evaluate 해 IssueTable 입력 dict 반환.

    반환 = {"comments": {row_key: 셀 텍스트}, "etc_auto_items": [item...],
            "row_signatures": {row_key: [signature id...]},
            "signature_options": [{"id","enabled"}...]}.
    selected_items 필터는 build_report_payload 의 in-place 필터와 동일 집합으로
    적용한다(미선택 item 평가 회피). 여러 소스에서 같은 **item** 케이스가 나오면
    severity 높은 쪽이 남고, 동률이면 이봉(BIMODALITY) 발화 쪽이 남는다(_rank).
    (2026-08-19: 엔진 case 가 item 당 1개가 되어 키에서 bin 이 빠졌다 — 소스 간 대표
    선정이라는 _rank 의 역할 자체는 그대로다.)

    fail_only=None 이면 서버 기본(env). 참이면 Issue Table 에 행이 생기는 item 만 평가한다
    (fail 1chip 이상 ∪ CPK 섹션 후보 — eval_fail_scope) — 그 결과 **수율·cpk 가 둘 다
    정상인데 룰만 위반한 item(etc_auto_items)은 생기지 않는다**. 의도된 동작이며, 되돌리려면
    WEB_REPORT_EVAL_FAIL_ONLY=0.

    세션에 민감도 게이지 설정(webreport_options.eval_sensitivity)이 있으면 그 구체값을
    `thresholds_override` 로 넘긴다 — 단계표 해석은 저장 시점에 끝나 있고 여기는 숫자만
    전달한다. 이 값은 `cache_policy.ai_comment_key` 에도 digest 로 들어가 있어야 한다
    (없으면 같은 rawdata·다른 민감도인 dedup 형제 세션이 캐시를 공유해 조용한 오답이 된다).

    `generate_comment=False` (2026-08-28) 는 **Signature 만 먼저 내보내는 1단계 빌드**다.
    엔진 L5(선례검색 + LLM 코멘트 합성)를 건너뛰므로 LLM 이 켜진 세션에서 판정(L1~L4)이
    끝나는 즉시 Signature 컬럼을 채울 수 있다.
    ⚠ 이 모드의 반환 `comments` 는 **항상 빈 dict** 다. 엔진이 `comment=None` 을 주므로
    `_cell_text` 를 태우면 status 배지만 남은 껍데기 문자열이 나오는데, 그게 셀에 박히면
    사용자에게는 "AI Comment 가 배지만 있고 내용이 없다"로 보인다(완성본이 도착하기 전까지
    그 상태로 굳는다). 빈 dict 면 화면은 종전대로 "Loading 중…" 을 유지한다 — 이 함수를
    호출하는 쪽이 최종본 캐시와 **다른 키**(ai_comment_key stage="sig")에 담아야 하는
    이유이기도 하다.
    """
    evaluate = _evaluate_fn()
    th_override = webreport_eval_overrides(session.get("webreport_options") or "") or None
    selected = {str(v) for v in (selected_items or []) if str(v)}
    fail_set = eval_fail_scope(tables, session, selected) \
        if (fail_only if fail_only is not None else _FAIL_ONLY) else None
    from . import build_log
    best = {}
    for idx, table in enumerate(tables):
        # 실행 중 체크포인트 — 타임아웃으로 워커가 죽어도 실패 레코드의 last_source 에
        # "몇 번째 소스에서 멎었나"가 남는다(docs/20 §3). stage("ai_comment") 는 호출부
        # (service)가 이미 잡고 있으므로 여기서 stage 를 중첩하면 소요가 2배가 된다 —
        # 반드시 checkpoint 를 쓴다 (build_log 모듈 주석).
        build_log.checkpoint("ai_comment",
                             f"{idx + 1}/{len(tables)} {table.source or ''}")
        meta = _session_meta(session, idx + 1)
        if meta is None:
            logger.warning("ai_comment: product_type=%r 는 평가 대상이 아님 — 건너뜀",
                           session.get("product_type"))
            return _EMPTY_RESULT.copy()
        items = _eval_items(table, selected, fail_set)
        if not items:
            continue
        raw_df = _table_to_raw_df(table, items)
        result = evaluate({"meta": meta, "raw_df": raw_df}, persist=False,
                          generate_comment=generate_comment,
                          thresholds_override=th_override)
        for case in result.get("cases") or []:
            key = str(case.get("item_raw") or "")
            prev = best.get(key)
            if prev is None or _rank(case) > _rank(prev):
                best[key] = case
    no_suggest = webreport_ai_no_suggest(session.get("webreport_options") or "")
    out = _to_row_keys(best, fail_bins_by_item(tables),
                       with_comments=generate_comment, no_suggest=no_suggest)
    # 클라 LLM 대행용 프롬프트(docs/23) — 대표 case 기준, 키는 item_raw(comments 의
    # row_key 꼬리와 동일). Signature 1단계 빌드(generate_comment=False)는 comment 가
    # None 이라 재료가 없다 — 빈 dict 로 계약 키만 유지한다.
    if generate_comment:
        from .ai_prompt import build_prompts
        # "제안 제외"(Honey 체크) 세션은 프롬프트를 **아예 만들지 않는다** — 클라 워커가
        # 받을 목록이 비어 조용히 끝나므로 LLM 호출·토큰이 0 이다(2026-09-02 사용자 요청).
        # 셀에는 코드가 만든 [사례] 나열과 룰 조치가 그대로 남는다.
        out["prompts"] = ({} if no_suggest
                          else build_prompts(best, _prompt_enrich(best, tables),
                                             _ai_prompt_rules()))
        # 사례 상세(팝오버 라우트) + 행별 건수(payload 링크). Signature 1단계 빌드는
        # 선례 검색 자체를 안 하므로(엔진 generate_comment=False) 빈 dict 다.
        prec_views = _precedent_views(best)
        out["precedents"] = prec_views
        out["precedent_counts"] = _precedent_counts(prec_views, out.get("comments") or {})
    else:
        out["prompts"] = {}
        out["precedents"] = {}
        out["precedent_counts"] = {}
    # 이 평가가 서버 LLM 을 쓸 수 있는 상태였나 — 화면의 처리 주체 아이콘 재료(2026-09-02).
    # ⚠ **행 단위 정밀도가 아니다**: 엔진이 case 별로 "이 문장은 LLM 이 썼다"를 돌려주지
    # 않으므로 배선 상태(설정)로 근사한다. 정확한 것은 클라 대행분뿐이며, 그건 세션 편집
    # DB 에 저장 행이 있는지로 서버가 확실히 안다(service._session_ai_overlay).
    # 콜드 빌드 1회당 1번만 부른다 — 위 evaluate 가 이미 엔진을 로드한 뒤라 값싸다.
    try:
        out["llm_enabled"] = bool(llm_status().get("enabled"))
    except Exception:                                    # noqa: BLE001 — 아이콘은 부가 정보
        out["llm_enabled"] = False
    return out


def safe_build_ex(tables, session, selected_items=None, fail_only=None,
                  generate_comment: bool = True):
    """safe_build + 성공 여부 — (result dict, ok). ok=False 는 **예외 폴백**(빈 결과).

    캐시 경로(service)가 폴백을 캐시하지 않기 위한 구분이다 — 일시 오류(메모리·룰 파일
    잠금 등)의 빈 결과를 캐시하면 오류가 영구화된다. product_type 미허용 등 '정상적으로
    빈 결과'는 결정적이므로 ok=True 다(캐시해도 안전).
    """
    try:
        return build_ai_comments(tables, session, selected_items, fail_only,
                                 generate_comment=generate_comment), True
    except Exception:
        logger.warning("ai_comment 빌드 실패 — 빈 값으로 진행 (session=%s)",
                       session.get("session_id"), exc_info=True)
        return _EMPTY_RESULT.copy(), False


def safe_build(tables, session, selected_items=None, fail_only=None):
    """build_ai_comments 실패 격리 — 예외 시 warning 로그 + 빈 결과.

    반환이 dict 인 한(빈 comments 포함) IssueTable 에 AI Comment 컬럼은 표시된다
    (값만 비어 있음). None 을 반환하지 않는다 — 컬럼 표시 여부는 호출자(service)의
    옵션 판정이 결정한다.
    """
    return safe_build_ex(tables, session, selected_items, fail_only)[0]
