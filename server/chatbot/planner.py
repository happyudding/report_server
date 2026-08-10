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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

_log = logging.getLogger(__name__)

INTENTS = ("item_history", "session_issue", "product_search", "similar_case",
           "comment_search", "session_find", "session_metrics", "page_jump", "unknown")

METRICS = ("yield", "cpk", "raw")
JUMP_TARGETS = ("item_detail", "map")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")   # security.py 와 동일 패턴


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
    session_id: str | None = None         # 질문이 특정 세션을 지목할 때
    metric: str | None = None             # session_metrics: 'yield' | 'cpk' | 'raw'
    jump_target: str | None = None        # page_jump: 'item_detail' | 'map'
    llm_ms: int | None = None             # LLM 왕복 소요(관리자 탭 부하 분해용)

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


def chat_url(endpoint=None) -> str:
    """EVAL_LLM_ENDPOINT → 실제로 POST 할 chat completions URL.

    base URL(`http://host:8000/v1`)과 완성 경로(`.../v1/chat/completions`)를 둘 다 받는다.
    엔진 쪽 짝은 `eval_analyzer/eval_engine/llm_client.chat_url` 이며 **판정 규칙이 같아야
    한다** — 같은 env 하나로 두 소비자가 서로 다른 URL 을 때리면 "챗봇은 되는데 AI Comment
    는 404" 같은 반쪽 배선이 된다. 코드를 공유하지 않는 이유는 불변규칙 #8(eval_engine
    import 는 web_report 3파일만)이라 여기서 import 할 수 없기 때문이다.
    """
    url = str(endpoint if endpoint is not None else _env("EVAL_LLM_ENDPOINT")).strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return url + "/chat/completions"
    return url


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
- session_find   : 조건(제품/lot/기간)에 맞는 평가 세션(보고서) 자체를 찾아 열려 한다
- session_metrics: 특정 세션의 수율(yield)·CPK·측정값(raw) 수치를 묻는다
- page_jump      : 화면 이동만 원한다 (항목 상세 보기 / 웨이퍼 맵 보기)
- unknown        : 위 어디에도 해당하지 않는다

session_metrics 면 metric 을 반드시 하나 고른다: "yield"(수율) / "cpk" / "raw"(실제 측정값).
page_jump 면 jump_target 을 반드시 하나 고른다: "item_detail"(항목 상세) / "map"(웨이퍼 맵).

규칙:
- 사용자가 말하지 않은 제품명·항목명·날짜를 만들어내지 않는다.
- 약어를 임의로 확장하지 않는다. item_keywords 에는 사용자가 쓴 문자열을 그대로 넣는다.
- product_type 은 MDDI/PDDI/PMIC/SECURITY/TCON 중 하나만.
- 후보가 여럿일 수 있으면 ambiguity=true.
- 질문의 "이 세션 / 여기 / 현재 보고서 / 이번 평가" 는 위에 주어진 컨텍스트 세션을 가리킨다.
  그 경우 session_id 에 컨텍스트 세션 id 를 그대로 넣는다. 컨텍스트가 없으면 null.

반드시 아래 키만 가진 JSON 객체 하나만 출력한다(설명·코드펜스 금지):
{"intent": "...", "product": null 또는 문자열, "product_type": null 또는 문자열,
 "family_product": null 또는 문자열, "item_keywords": [문자열...],
 "lot_id": null 또는 문자열, "free_text": null 또는 문자열,
 "session_id": null 또는 문자열, "metric": null 또는 문자열,
 "jump_target": null 또는 문자열,
 "ambiguity": true/false, "normalized_question": "무엇을 찾는지 한 문장"}"""


def _call_llm(question: str, context_session_id=None) -> dict | None:
    """OpenAI 호환 chat/completions 호출 → 파싱된 dict. 실패하면 None.

    반환 dict 에 `_llm_ms`(왕복 소요)를 얹는다 — 관리자 탭이 "느린 게 LLM 탓인지"를
    가리려면 총 소요만으로는 부족하다.
    """
    endpoint = chat_url()
    user_content = question
    if context_session_id:
        user_content = f"(현재 열려 있는 세션: {context_session_id})\n{question}"
    payload = {
        "model": _env("EVAL_LLM_MODEL"),
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": user_content}],
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
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        text = body["choices"][0]["message"]["content"]
    except (urllib.error.URLError, OSError, KeyError, IndexError,
            json.JSONDecodeError, ValueError) as exc:
        _log.warning("planner LLM 호출 실패 — 규칙 폴백: %s", exc)
        return None
    data = _loads_lenient(text)
    if data is not None:
        data["_llm_ms"] = int((time.perf_counter() - started) * 1000)
    return data


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
    metric = _clean(data.get("metric"))
    if metric not in METRICS:
        metric = None
    jump = _clean(data.get("jump_target"))
    if jump not in JUMP_TARGETS:
        jump = None
    sid = _clean(data.get("session_id"))
    if sid and not _SESSION_ID_RE.match(sid):
        sid = None
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
        planner="llm",
        session_id=sid,
        metric=metric,
        jump_target=jump,
        llm_ms=data.get("_llm_ms"))


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
# 수치 질문 신호. "수율/cpk" 는 기존 어느 키워드 세트에도 없어 기존 분기를 건드리지 않는다.
_METRIC_KW = ("수율", "yield", "cpk", "씨피케이")
_RAW_KW = ("측정값", "실측", "로우데이터", "원시값", "raw data", "rawdata")
# 화면 이동 신호. "보여줘" 단독은 item_history 질문("SGM 항목 이력 보여줘")에도 흔해서
# 반드시 이동 대상(_MAP_KW/_DETAIL_KW)과 함께 있을 때만 page_jump 로 본다.
_JUMP_KW = ("열어", "보여줘", "이동", "점프", "탭", "띄워")
_MAP_KW = ("맵", "map", "웨이퍼")
_DETAIL_KW = ("상세", "detail", "분포")
_FIND_KW = ("찾아", "검색", "목록", "리스트")
# 세션 id 는 "<epoch>_<hex6>" 형태(web_report.ingest) — 짧은 제품코드와 섞이지 않게 8자 이상만.
_SESSION_REF_RE = re.compile(r"세션\s*([A-Za-z0-9_-]{8,80})")
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
    has_metric = any(k in q or k in ql for k in _METRIC_KW)
    has_raw = any(k in q or k in ql for k in _RAW_KW)
    has_map = any(k in q or k in ql for k in _MAP_KW)
    has_detail = any(k in q or k in ql for k in _DETAIL_KW)
    wants_jump = any(k in q or k in ql for k in _JUMP_KW) and (has_map or has_detail)

    session_ref = _SESSION_REF_RE.search(q)
    session_id = session_ref.group(1) if session_ref else None
    if session_id and product and product.lower() in session_id.lower():
        product = None      # 세션 id 조각을 제품명으로 오인하지 않는다

    metric = jump_target = None
    if any(k in q or k in ql for k in _SIMILAR_KW):
        intent = "similar_case"
    elif wants_jump:
        intent = "page_jump"
        jump_target = "map" if has_map else "item_detail"
    elif has_metric or has_raw:
        intent = "session_metrics"
        metric = "raw" if has_raw else ("cpk" if "cpk" in ql or "씨피케이" in q else "yield")
    elif product and (has_issue or (items and has_report)):
        intent = "session_issue"
    elif session_id and has_issue:
        intent = "session_issue"
    elif has_report and (product or session_id
                         or any(k in q or k in ql for k in _FIND_KW)):
        intent = "session_find"
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
                     ambiguity=len(items) > 1, normalized_question=q, planner="rule",
                     session_id=session_id, metric=metric, jump_target=jump_target)


# ── 진입점 ───────────────────────────────────────────────────────────────────
def plan(question: str, *, use_llm=True, context_session_id=None) -> QueryPlan:
    """질문 → QueryPlan. LLM 이 꺼져 있거나 실패하면 규칙 계획을 돌려준다.

    context_session_id 는 "지금 열려 있는 세션" — 웹 챗 패널이 세션 상세에서 보낸 질문의
    "이 세션" 을 해석하는 데 쓴다. 규칙 폴백은 이걸 모르므로 agent 가 사후 주입한다.
    """
    if use_llm and llm_enabled():
        data = _call_llm(question, context_session_id)
        if data is not None:
            return _plan_from_dict(data, question)
    return rule_plan(question)
