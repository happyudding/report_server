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
from .validation import webreport_eval_overrides

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

# 평가 범위 — 1(기본)=fail item 만(FAILTNO 1chip 이상, Yield/IssueTable 과 같은 기준),
# 0=전체 item(종전 동작). 토글은 server.env 수정 + 서버 재기동.
# ⚠ 이 필터는 **item 컬럼만** 줄인다. 선택된 item 의 chip 행(측정 행)은 전량 유지해야
# 엔진 L1/L2 가 전체 분포(cpk·이봉·outlier) 대비 fail 을 볼 수 있다 — fail chip 만
# 남기는 행 필터는 만들지 말 것.
_FAIL_ONLY = (os.getenv("WEB_REPORT_EVAL_FAIL_ONLY", "1") or "1").strip().lower() \
    in ("1", "true", "yes", "on")

# 평가 스킵/실패 시 반환 형태 — 키 구성은 정상 반환과 동일해야 한다(호출부 분기 없음).
# 키를 늘릴 때 여기도 같이 늘려야 예외 폴백에서 KeyError 로 빌드가 죽지 않는다.
_EMPTY_RESULT = {"comments": {}, "etc_auto_items": [], "row_signatures": {},
                 "signature_options": []}

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
# multimodal 은 **배지를 붙이지 않는다**(2026-08-13 사용자 요청) — 값이 빈 문자열이라
# 아래 폴백([분포분리])도 타지 않는다. 판정·목록에는 그대로 남고 셀 표기만 생략한다.
_MODALITY_TAG = {"bimodal": "[이봉]", "multimodal": "", "separated": "[분리]"}
# note 포맷이 바뀌어도 "발화했다" 는 사실은 잃지 않는다 (조용한 미표시 방지).
_MODALITY_TAG_FALLBACK = "[분포분리]"


def fail_only_enabled() -> bool:
    """서버 기본 평가 범위가 fail item 만인가 (env WEB_REPORT_EVAL_FAIL_ONLY)."""
    return _FAIL_ONLY


def eval_fail_scope(tables):
    """평가 대상 item 집합 — fail(FAILTNO==TNO) 이 1chip 이상인 항목. 소스 합집합.

    Yield 탭 / Issue Table 의 fail 귀속 규칙을 그대로 재사용한다
    (tabs.distribution.fail_items → yield_tab.tno_to_item_map). 모드 구분 없이 같은
    기준을 쓴다 — Temperature 의 CT/HT RT-limit 재판정은 저장된 FAILTNO 와 다르지만,
    그 표(Issue Table Temp)는 AI Comment 대상에서 제외했으므로 어긋나지 않는다.
    """
    from .tabs.distribution import fail_items
    return fail_items(tables)


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


def _cell_text(case):
    status = str(case.get("status") or "").strip()
    comment = str(case.get("comment") or "").strip()
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


def _to_row_keys(cases_by_item, fail_bins=None, with_comments: bool = True):
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
        text = _cell_text(case) if with_comments else ""
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

    fail_only=None 이면 서버 기본(env). 참이면 fail 이 1chip 이상인 item 만 평가한다 —
    그 결과 **수율·cpk 는 정상인데 룰만 위반한 item(etc_auto_items)이 생기지 않는다**.
    의도된 동작이며, 되돌리려면 WEB_REPORT_EVAL_FAIL_ONLY=0.

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
    fail_set = eval_fail_scope(tables) \
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
    return _to_row_keys(best, fail_bins_by_item(tables),
                        with_comments=generate_comment)


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
