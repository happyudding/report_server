"""자연어 질문 → QueryPlan(구조화) 변환.

LLM 은 **검색 계획만** 세운다 — SQL 을 만들지도, DB 를 직접 읽지도 않는다. 실제 조회는
tools_report/tools_eval 의 파라미터 바인딩 SELECT 가 전담한다.

LangChain 을 쓰지 않는 이유(1단계 한정): 필요한 건 OpenAI 호환 chat/completions POST 1개와
JSON 스키마 강제뿐이고, 운영 venv 가 Python 3.14 라 무거운 의존을 새로 얹는 위험이 이득보다
크다. 멀티턴·재검색 루프가 실제로 필요해지면 그때 LangGraph 와 함께 재검토한다.

LLM 이 없거나 실패하면 **규칙 기반으로 폴백**한다(빈손으로 죽지 않는다).
설정은 기존 eval_analyzer 관례를 그대로 재사용한다:
    EVAL_LLM_ENABLED / EVAL_LLM_ENDPOINT / EVAL_LLM_MODEL / EVAL_LLM_API_KEY / EVAL_LLM_TIMEOUT
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

_log = logging.getLogger(__name__)

INTENTS = ("item_history", "session_issue", "product_search", "similar_case",
           "comment_search", "unknown")


@dataclass
class QueryPlan:
    intent: str = "unknown"
    product: str | None = None
    product_type: str | None = None
    family_product: str | None = None
    item_keywords: list[str] = field(default_factory=list)
    lot_id: str | None = None
    free_text: str | None = None          # 코멘트 본문 검색용 표현
    ambiguity: bool = False
    normalized_question: str = ""
    planner: str = "rule"                 # 'llm' | 'rule' — 어디서 나온 계획인지

    def to_dict(self):
        return asdict(self)


# ── 어휘 (검증 화이트리스트) ─────────────────────────────────────────────────
def taxonomy() -> dict:
    """{product_type: [family_product...]} — eval 룰 taxonomy 가 정본.

    web_report.eval_debug 경유로 읽는다(eval_engine import 허용 3곳 중 하나라 새 import
    지점을 만들지 않는다). 실패하면 빈 dict — 그 경우 값 검증만 느슨해진다.
    """
    try:
        from web_report import eval_debug
        return eval_debug.taxonomy()
    except Exception:
        _log.debug("taxonomy 로드 실패 — 검증 없이 진행", exc_info=True)
        return {}


def _valid_scope(product_type, family_product):
    """(product_type, family_product) 를 taxonomy 로 검증해 정규화."""
    tax = taxonomy()
    pt = str(product_type or "").strip().upper() or None
    fam = str(family_product or "").strip() or None
    if tax:
        if pt and pt not in tax:
            pt = None
        if fam:
            pools = tax.get(pt) if pt else [f for v in tax.values() for f in v]
            match = next((f for f in (pools or []) if f.lower() == fam.lower()), None)
            fam = match
    return pt, fam


# ── LLM ──────────────────────────────────────────────────────────────────────
def _env(name, default=""):
    value = os.getenv(name)
    if value:
        return value
    try:
        import config
        return config._server_env_file(name) or default
    except Exception:
        return default


def llm_enabled() -> bool:
    return (str(_env("EVAL_LLM_ENABLED", "false")).strip().lower() == "true"
            and bool(_env("EVAL_LLM_ENDPOINT")) and bool(_env("EVAL_LLM_MODEL")))


_SYSTEM_PROMPT = """당신은 반도체 평가 이력 검색 시스템의 Query Planner 다.
사용자 질문에 직접 답하지 않는다. 검색 계획만 JSON 으로 반환한다.

사용할 수 있는 데이터:
- 평가 세션(보고서): 제품명(product), product_type, family_product, lot_id, 평가일
- Issue Table: 항목(item)별 이슈, Status(Open/Close), PTE/개발 코멘트
- item 이력: item 별 과거 제품/lot/bin/cpk/수율/사람 코멘트

intent 는 다음 중 하나다.
- item_history   : 항목(item) 이름 일부/전체로 과거 이력을 묻는다 (여러 제품에 걸친 선례)
- session_issue  : 특정 제품/보고서의 이슈가 어떻게 처리·close 됐는지 묻는다
- product_search : 제품이 있었는지/어떤 평가가 있었는지 묻는다
- similar_case   : 비슷한 유형의 다른 사례를 묻는다
- comment_search : 항목이 아니라 현상·조치 표현으로 찾는다
- unknown        : 위 어디에도 해당하지 않는다

규칙:
- 사용자가 말하지 않은 제품명·항목명·날짜를 만들어내지 않는다.
- 약어를 임의로 확장하지 않는다. item_keywords 에는 사용자가 쓴 문자열을 그대로 넣는다.
- product_type 은 MDDI/PDDI/PMIC/SECURITY/TCON 중 하나만.
- 후보가 여럿일 수 있으면 ambiguity=true.

반드시 아래 키만 가진 JSON 객체 하나만 출력한다(설명·코드펜스 금지):
{"intent": "...", "product": null 또는 문자열, "product_type": null 또는 문자열,
 "family_product": null 또는 문자열, "item_keywords": [문자열...],
 "lot_id": null 또는 문자열, "free_text": null 또는 문자열,
 "ambiguity": true/false, "normalized_question": "무엇을 찾는지 한 문장"}"""


def _call_llm(question: str) -> dict | None:
    """OpenAI 호환 chat/completions 호출 → 파싱된 dict. 실패하면 None."""
    endpoint = _env("EVAL_LLM_ENDPOINT")
    payload = {
        "model": _env("EVAL_LLM_MODEL"),
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": question}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    api_key = _env("EVAL_LLM_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    timeout = float(_env("EVAL_LLM_TIMEOUT", "30") or 30)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError, IndexError,
            json.JSONDecodeError, ValueError) as exc:
        _log.warning("planner LLM 호출 실패 — 규칙 폴백: %s", exc)
        return None
    return _loads_lenient(text)


def _loads_lenient(text):
    """응답이 코드펜스로 감싸여 와도 JSON 객체를 뽑아낸다."""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            value = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _plan_from_dict(data: dict, question: str) -> QueryPlan:
    """LLM 출력 → 검증된 QueryPlan. 모르는 값은 버린다(예외 아님)."""
    intent = str(data.get("intent") or "").strip()
    if intent not in INTENTS:
        intent = "unknown"
    keywords = data.get("item_keywords")
    if isinstance(keywords, str):
        keywords = [keywords]
    keywords = [str(k).strip() for k in (keywords or []) if str(k or "").strip()]
    pt, fam = _valid_scope(data.get("product_type"), data.get("family_product"))
    return QueryPlan(
        intent=intent,
        product=_clean(data.get("product")),
        product_type=pt,
        family_product=fam,
        item_keywords=keywords[:5],
        lot_id=_clean(data.get("lot_id")),
        free_text=_clean(data.get("free_text")),
        ambiguity=bool(data.get("ambiguity")),
        normalized_question=str(data.get("normalized_question") or question).strip(),
        planner="llm")


def _clean(value):
    text = str(value or "").strip()
    return text or None


# ── 규칙 폴백 ────────────────────────────────────────────────────────────────
# 이슈 처리 결과를 묻는 신호. "평가한/보고서" 같은 약한 단어를 여기 넣으면
# "S3222 평가한 적 있어?"(제품 존재 확인)까지 session_issue 로 빨려 들어간다.
_ISSUE_KW = ("이슈", "issue", "close", "클로즈", "종결", "조치", "불량", "fail")
_REPORT_KW = ("보고서", "리포트", "세션")
_HISTORY_KW = ("히스토리", "이력", "예전", "과거", "선례", "전에")
_SIMILAR_KW = ("비슷", "유사", "닮은", "같은 유형")
_COMMENT_KW = ("현상", "증상", "코멘트")
# 제품 코드처럼 보이는 토큰: 영문 1~4자 (+ 구분자) + 숫자 3자 이상
# — S3222 / KTD2026 / SOC-000016 형태를 모두 받는다. \b 덕분에 POR_TH_0131 같은 item
# 이름 중간(TH_0131)에는 걸리지 않는다(밑줄이 단어 문자라 경계가 없다).
_PRODUCT_RE = re.compile(r"\b([A-Za-z]{1,4}[-_]?\d{3,}[A-Za-z0-9_-]*)\b")
# item 후보 토큰: 영문 2자 이상으로 시작하는 식별자 (SGM, LDO, PLL_VCO 등)
_ITEM_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{1,})\b")
_STOPWORDS = {"close", "issue", "item", "lot", "db", "id", "ok", "ng", "pte"}


def rule_plan(question: str) -> QueryPlan:
    """LLM 없이 키워드로 계획을 세운다 — LLM 미설정/장애 시의 폴백.

    정교한 의도 파악은 하지 않는다. 목적은 "그래도 실제 DB 결과를 돌려준다" 이다.
    """
    q = str(question or "").strip()
    tax = taxonomy()

    product_type = next((pt for pt in tax if pt.lower() in q.lower()), None)

    product = None
    m = _PRODUCT_RE.search(q)
    if m:
        product = m.group(1)

    family = None
    pools = tax.get(product_type) if product_type else [f for v in tax.values() for f in v]
    for fam in (pools or []):
        # 제품명 안의 조각(SOC-000016 의 'SOC')을 family 로 오인하면 조회가 과도하게
        # 좁아진다 — product 를 먼저 뽑아 두고 그 안에 든 이름은 건너뛴다.
        if product and fam.lower() in product.lower():
            continue
        if re.search(rf"\b{re.escape(fam)}\b", q, re.I):
            family = fam
            break
    product_type, family = _valid_scope(product_type, family)

    items = []
    for token in _ITEM_RE.findall(q):
        if token.lower() in _STOPWORDS:
            continue
        # 제품명 조각(SOC-000016 의 'SOC')을 item 후보로 오인하지 않는다.
        if product and token.lower() in product.lower():
            continue
        if product_type and token.upper() == product_type:
            continue
        if family and token.lower() == family.lower():
            continue
        if token not in items:
            items.append(token)

    ql = q.lower()
    has_issue = any(k in q or k in ql for k in _ISSUE_KW)
    has_report = any(k in q or k in ql for k in _REPORT_KW)
    if any(k in q or k in ql for k in _SIMILAR_KW):
        intent = "similar_case"
    elif product and (has_issue or (items and has_report)):
        intent = "session_issue"
    elif items and any(k in q or k in ql for k in _HISTORY_KW):
        intent = "item_history"
    elif items:
        intent = "item_history"
    elif product:
        intent = "product_search"
    elif any(k in q or k in ql for k in _COMMENT_KW):
        intent = "comment_search"
    else:
        intent = "unknown"

    return QueryPlan(intent=intent, product=product, product_type=product_type,
                     family_product=family, item_keywords=items[:5],
                     free_text=q if intent == "comment_search" else None,
                     ambiguity=len(items) > 1, normalized_question=q, planner="rule")


# ── 진입점 ───────────────────────────────────────────────────────────────────
def plan(question: str, *, use_llm=True) -> QueryPlan:
    """질문 → QueryPlan. LLM 이 꺼져 있거나 실패하면 규칙 계획을 돌려준다."""
    if use_llm and llm_enabled():
        data = _call_llm(question)
        if data is not None:
            return _plan_from_dict(data, question)
    return rule_plan(question)
