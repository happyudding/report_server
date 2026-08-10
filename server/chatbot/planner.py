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
           "comment_search", "session_find", "session_metrics", "page_jump",
           "stats", "item_search", "session_meta", "help", "unknown")

METRICS = ("yield", "cpk", "raw")
JUMP_TARGETS = ("item_detail", "map")
# 집계 축 — 정본은 tools_eval.STATS_AXES (여기 목록이 그 키와 어긋나면 조용히 status 로 떨어진다)
STATS_AXES = ("status", "product", "product_type", "family_product", "item",
              "item_class", "bin")
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
    group_by: str | None = None           # stats: 집계 축 (STATS_AXES)
    status: str | None = None             # stats: 판정 필터 (CRITICAL/MAJOR/MINOR/MONITOR)
    date_from: int | None = None          # 세션 조회 기간 (epoch 초)
    date_to: int | None = None
    # 근거 없이 catch-all 로 정해진 계획인가 — 규칙이 "영문 토큰이 있으니 item 이겠지" 로
    # 찍은 경우 True. agent 가 이 비트를 보고 조회 결과로 재분류한다(LLM 계획은 항상 False).
    weak: bool = False
    llm_ms: int | None = None             # LLM 왕복 소요(관리자 탭 부하 분해용)

    def to_dict(self):
        return asdict(self)


# ── 어휘 (검증 화이트리스트) ─────────────────────────────────────────────────
def taxonomy() -> dict:
    """{product_type: [family_product...]} — eval 룰 taxonomy 가 정본.

    web_report.eval_debug 경유로 읽는다(eval_engine import 허용 3곳 중 하나라 새 import
    지점을 만들지 않는다). 실패하면 빈 dict — 그 경우 값 검증만 느슨해진다.

    실패를 **경고로** 남기는 이유: 조용히 {} 가 되면 제품군 스코프가 통째로 빠져 같은 질문의
    조회 범위가 달라지는데, 로그가 debug 면 아무도 모른다. 다만 질문마다 찍히면 시끄러우니
    프로세스당 1회만 올린다.
    """
    global _TAXONOMY_WARNED
    try:
        from web_report import eval_debug
        return eval_debug.taxonomy()
    except Exception:
        if not _TAXONOMY_WARNED:
            _TAXONOMY_WARNED = True
            _log.warning("taxonomy 로드 실패 — 제품군 스코프 없이 진행한다", exc_info=True)
        return {}


_TAXONOMY_WARNED = False


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
- stats          : 목록이 아니라 **건수/분포**를 묻는다 ("PMIC 에 MAJOR 몇 건", "제품별 몇 건")
- item_search    : 특정 제품군에 **어떤 항목들이 있는지** 목록을 묻는다 ("PMIC SOC 에 무슨 Item 있어")
- session_meta   : 열려 있는 세션의 메타를 묻는다 (누가/언제 올렸나, 온도·공정·설비·패키지 등)
- help           : 인사이거나 "뭐 할 수 있어?" 같은 기능 안내 요청이다
- unknown        : 위 어디에도 해당하지 않는다

session_metrics 면 metric 을 반드시 하나 고른다: "yield"(수율) / "cpk" / "raw"(실제 측정값).
page_jump 면 jump_target 을 반드시 하나 고른다: "item_detail"(항목 상세) / "map"(웨이퍼 맵).
stats 면 group_by 를 하나 고른다: "status" / "product" / "product_type" / "family_product" /
"item" / "item_class" / "bin". 판정을 특정했으면 status 에 CRITICAL|MAJOR|MINOR|MONITOR 중 하나.
질문에 기간 표현(오늘/어제/이번주/지난주/이번 달/올해)이 있으면 date_from·date_to 를 epoch 초로
채운다. "최근"·"요즘" 처럼 범위가 모호한 말은 **비워 둔다**(최신순 정렬로 충분하다).

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
 "jump_target": null 또는 문자열, "group_by": null 또는 문자열,
 "status": null 또는 문자열, "date_from": null 또는 정수, "date_to": null 또는 정수,
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
    """LLM 출력 → 검증된 QueryPlan. 모르는 값은 버린다(예외 아님).

    `weak` 은 채우지 않는다(항상 False) — 규칙 catch-all 전용 신호이고, LLM 은 근거를 갖고
    고른 계획이므로 agent 의 재분류 대상이 아니다.
    """
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
    group_by = _clean(data.get("group_by"))
    if group_by not in STATS_AXES:
        group_by = None
    status = (_clean(data.get("status")) or "").upper() or None
    if status not in _STATUS_VOCAB:
        status = None
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
        group_by=group_by,
        status=status,
        date_from=_int_or_none(data.get("date_from")),
        date_to=_int_or_none(data.get("date_to")),
        llm_ms=data.get("_llm_ms"))


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value):
    text = str(value or "").strip()
    return text or None


# ── 규칙 폴백 ────────────────────────────────────────────────────────────────
# 이슈 처리 결과를 묻는 신호. "평가한/보고서" 같은 약한 단어를 여기 넣으면
# "S3222 평가한 적 있어?"(제품 존재 확인)까지 session_issue 로 빨려 들어간다.
# ⚠ 확장할 때 단독 음절/짧은 어간을 넣지 말 것 — "이상"은 "3건 이상", "튀"는 "튀던"(코멘트
#   질문)에 걸린다. 반드시 "이상동작"·"튐" 처럼 오탐이 없는 형태로만 넣는다.
_ISSUE_KW = ("이슈", "issue", "close", "클로즈", "종결", "조치", "불량", "fail",
             "문제", "에러", "error", "고장", "이상동작", "깨짐", "튐")
_REPORT_KW = ("보고서", "리포트", "세션")
_HISTORY_KW = ("히스토리", "이력", "예전", "과거", "선례", "전에")
_SIMILAR_KW = ("비슷", "유사", "닮은", "같은 유형")
# 코멘트 본문 검색 신호. 증상어를 함께 둔다 — "고온에서 문제된 적 있어?" 처럼 제품·item 토큰이
# 없는 순한글 질문은 이 분기(11번)에서만 건질 수 있다. 11번은 items·product 가 모두 없을 때만
# 도달하므로(정리 B) 확장의 회귀 위험이 구조적으로 낮다.
_COMMENT_KW = ("현상", "증상", "코멘트", "문제", "에러", "error", "고장", "이상동작")
# 수치 질문 신호. "수율/cpk" 는 기존 어느 키워드 세트에도 없어 기존 분기를 건드리지 않는다.
_METRIC_KW = ("수율", "yield", "cpk", "씨피케이")
_RAW_KW = ("측정값", "실측", "로우데이터", "원시값", "raw data", "rawdata")
# 화면 이동 신호. "보여줘" 단독은 item_history 질문("SGM 항목 이력 보여줘")에도 흔해서
# 반드시 이동 대상(_MAP_KW/_DETAIL_KW)과 함께 있을 때만 page_jump 로 본다.
_JUMP_KW = ("열어", "보여줘", "이동", "점프", "탭", "띄워")
_MAP_KW = ("맵", "map", "웨이퍼")
_DETAIL_KW = ("상세", "detail", "분포")
_FIND_KW = ("찾아", "검색", "목록", "리스트")
# 존재 확인("IW06 세션 있냐?"). ⚠ 7번(session_find)의 `has_report_like AND (…)` 안쪽에서만
# 쓴다 — 밖으로 빼면 "S3222 라는 제품 평가한 적 있어?"(product_search)를 삼킨다.
_EXISTS_KW = ("있냐", "있어", "있나", "있는지", "있을까", "존재")
# 업로드 표현. has_report 를 늘리지 않고 7번에서만 쓴다.
_UPLOAD_KW = ("올라온", "올라왔", "올린", "업로드")
# 시간 표현. ⚠ "예전"·"과거" 는 넣지 않는다 — 그건 _HISTORY_KW(item 이력) 소관이다.
_TIME_KW = ("최근", "요즘", "오늘", "어제", "이번주", "이번 주", "지난주", "지난 주",
            "이번 달", "이달", "올해")
# item 목록 질문("PMIC SOC 에 무슨 Item 있어") — 둘 다 있어야 성립한다.
_ITEMWORD_KW = ("항목", "아이템", "item")
_LIST_KW = ("무슨", "어떤", "뭐 있", "뭐가", "목록", "리스트", "종류")
# 세션 메타(누가/언제/온도/공정…). session_scope 가 있을 때만 쓰이므로 오탐 위험이 낮다.
_META_KW = ("누가", "올렸", "업로더", "작성자", "언제", "온도", "몇 도", "몇도", "파일명",
            "공정", "설비", "장비", "패키지", "gross die", "칩 사이즈", "리비전",
            "revision", "step")
# 인사·도움말. **전체 매치**만 인정한다 — 부분일치면 긴 질문 중간의 "도움"에도 걸린다.
_HELP_RE = re.compile(r"(안녕|안녕하세요|하이|hi|hello|헬로|도움말|도움|help|"
                      r"뭐할수있어|뭐할수있니|뭐가능해|기능|사용법|어떻게써|어떻게사용)",
                      re.I)
# 집계 신호. "몇 건/건수/통계/분포" 는 목록 질문에는 거의 안 쓰이는 말이라 오분류 위험이 낮다.
# ⚠ "분포" 는 _DETAIL_KW 에도 있다 — page_jump 가 앞 분기라 "분포 보여줘" 는 화면 이동이 이긴다
#    (그게 맞다: 사용자는 차트를 보려는 것이다). 집계는 "몇 건/통계" 같은 수량 표현이 있을 때다.
_STATS_KW = ("몇 건", "몇건", "건수", "통계", "집계", "몇 개", "몇개", "얼마나")
_STATUS_VOCAB = ("CRITICAL", "MAJOR", "MINOR", "MONITOR")
# 집계 축을 말에서 고른다(먼저 걸리는 것이 이긴다 — 좁은 축을 앞에 둔다).
_STATS_AXIS_KW = (
    ("item_class", ("아이템 클래스", "item_class", "항목 분류")),
    ("family_product", ("family", "패밀리", "제품군")),
    ("product_type", ("product_type", "제품 타입", "제품타입")),
    ("product", ("제품별", "제품 별", "product")),
    ("item", ("항목별", "항목 별", "아이템별", "item별")),
    ("bin", ("bin별", "bin 별", "빈별")),
    ("status", ("판정", "status", "등급")),
)
# 세션 id 는 "<epoch>_<hex6>" 형태(web_report.ingest) — 짧은 제품코드와 섞이지 않게 8자 이상만.
_SESSION_REF_RE = re.compile(r"세션\s*([A-Za-z0-9_-]{8,80})")
# 제품 코드처럼 보이는 토큰: 영문 1~4자 (+ 구분자) + 숫자 3자 이상
# — S3222 / KTD2026 / SOC-000016 형태를 모두 받는다. \b 덕분에 POR_TH_0131 같은 item
# 이름 중간(TH_0131)에는 걸리지 않는다(밑줄이 단어 문자라 경계가 없다).
_PRODUCT_RE = re.compile(r"\b([A-Za-z]{1,4}[-_]?\d{3,}[A-Za-z0-9_-]*)\b")
# item 후보 토큰: 영문 2자 이상으로 시작하는 식별자 (SGM, LDO, PLL_VCO 등)
_ITEM_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{1,})\b")
# item 후보에서 뺄 일반 명사. cpk/yield 를 넣는 이유: "S3222 cpk 안 좋은 항목?" 이
# item_keywords=['cpk'] 를 만들어 get_session_metrics 가 이름에 'cpk' 가 든 항목만 찾다
# 0건을 답했다(조용한 오답). 지표 이름은 항목 이름이 아니다.
_STOPWORDS = {"close", "issue", "item", "lot", "db", "id", "ok", "ng", "pte",
              "cpk", "yield"}


def _date_range(question: str):
    """질문의 시간 표현 → (date_from, date_to) epoch. 못 읽으면 (None, None).

    **"최근"·"요즘" 은 일부러 범위를 만들지 않는다** — 세션 검색이 이미 최신순(sort="new")
    이라 정렬로 충족되고, 임의로 7일을 걸면 3개월 전 자료를 조용히 숨긴다.
    """
    q = str(question or "")
    now = time.localtime()
    midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    day = 86400
    if "오늘" in q:
        return int(midnight), None
    if "어제" in q:
        return int(midnight - day), int(midnight - 1)
    if "이번주" in q or "이번 주" in q:
        return int(midnight - now.tm_wday * day), None
    if "지난주" in q or "지난 주" in q:
        monday = midnight - now.tm_wday * day
        return int(monday - 7 * day), int(monday - 1)
    if "이번 달" in q or "이달" in q:
        return int(time.mktime((now.tm_year, now.tm_mon, 1, 0, 0, 0, 0, 0, -1))), None
    if "올해" in q:
        return int(time.mktime((now.tm_year, 1, 1, 0, 0, 0, 0, 0, -1))), None
    return None, None


def rule_plan(question: str, context_session_id=None) -> QueryPlan:
    """LLM 없이 키워드로 계획을 세운다 — LLM 미설정/장애 시의 폴백.

    정교한 의도 파악은 하지 않는다. 목적은 "그래도 실제 DB 결과를 돌려준다" 이다.

    context_session_id 는 **분류에만** 쓴다(반환 session_id 에 넣지 않는다 — 주입은 agent 가
    세션 관련 intent 에만 한다). 세션 상세를 열어 둔 채 "이슈 알려줘" 라고 하면 제품명이
    없어 unknown 으로 빠지던 것을, 열린 세션이 있으면 그 세션 질문으로 읽게 하려는 것이다.
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
        # 짧은 family 이름(IF·TV·MX·ESE)은 일반 단어와 충돌한다 — "if 조건 뭐야" 가
        # family_product='IF' 로 조회 범위를 잘못 좁혔다. 3자 이하는 **원문(대문자) 매칭**
        # 으로만 인정한다. 제품군을 말할 땐 대문자로 쓰므로("PMIC SOC", "TV 제품군") 실사용
        # 손실은 소문자로 친 경우뿐이고, 그마저 product_type 을 함께 쓰면 복구된다.
        flags = 0 if len(fam) <= 3 else re.I
        if re.search(rf"\b{re.escape(fam)}\b", q, flags):
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
    # 분류용 — 질문이 세션을 지목했거나, 지금 세션을 열어 두고 물었거나.
    session_scope = session_id or context_session_id

    has_comment = any(k in q or k in ql for k in _COMMENT_KW)
    has_history = any(k in q or k in ql for k in _HISTORY_KW)
    # 7번 전용 신호 — has_report 자체를 늘리면 5번(items and has_report)까지 번진다.
    has_report_like = has_report or any(k in q or k in ql for k in _UPLOAD_KW)
    has_exists = any(k in q or k in ql for k in _EXISTS_KW)
    has_time = any(k in q or k in ql for k in _TIME_KW)
    date_from, date_to = _date_range(q)

    metric = jump_target = group_by = None
    weak = False
    status = next((s for s in _STATUS_VOCAB if s in q.upper()), None)
    if any(k in q or k in ql for k in _STATS_KW):
        intent = "stats"
        group_by = next((axis for axis, words in _STATS_AXIS_KW
                         if any(w in q or w in ql for w in words)), None)
        if group_by is None:
            # 판정을 특정했으면 그 안에서 제품별로 세는 게 더 유용하다.
            group_by = "product" if status else "status"
    elif any(k in q or k in ql for k in _SIMILAR_KW):
        intent = "similar_case"
    elif wants_jump:
        intent = "page_jump"
        jump_target = "map" if has_map else "item_detail"
    elif has_metric or has_raw:
        intent = "session_metrics"
        metric = "raw" if has_raw else ("cpk" if "cpk" in ql or "씨피케이" in q else "yield")
    elif product and (has_issue or (items and has_report)):
        intent = "session_issue"
    elif session_scope and has_issue:
        intent = "session_issue"
    elif session_scope and not has_comment and any(k in q or k in ql for k in _META_KW):
        # 열어 둔 세션의 메타(누가/언제/온도/공정…). not has_comment 가드가 없으면
        # "온도 올라갈 때 튀던 현상 코멘트 찾아줘" 가 온도 때문에 여기로 샌다.
        intent = "session_meta"
    elif has_report_like and (product or session_scope or has_exists or has_time
                              or any(k in q or k in ql for k in _FIND_KW)):
        intent = "session_find"
    elif ((product_type or family) and not items and not has_history
            and any(k in q or k in ql for k in _ITEMWORD_KW)
            and any(k in q or k in ql for k in _LIST_KW)):
        # "PMIC SOC 에 무슨 Item 있어" — 스코프만 있고 특정 item 토큰이 없는 목록 질문.
        # not items / not has_history 가 item_history 질문("SGM 항목 이력")과 갈라 준다.
        intent = "item_search"
    elif items and has_history:
        intent = "item_history"          # 이력 어휘가 근거 — 재분류 대상 아님
    elif items:
        intent = "item_history"          # catch-all: 영문 토큰만 보고 찍은 것
        weak = True                      # → agent 가 조회 결과로 재검증한다
    elif product:
        intent = "product_search"
    elif has_comment:
        intent = "comment_search"
    elif _HELP_RE.fullmatch(re.sub(r"[\s?!.,~]+", "", q)):
        intent = "help"
    else:
        intent = "unknown"

    return QueryPlan(intent=intent, product=product, product_type=product_type,
                     family_product=family, item_keywords=items[:5],
                     free_text=q if intent == "comment_search" else None,
                     ambiguity=len(items) > 1, normalized_question=q, planner="rule",
                     session_id=session_id, metric=metric, jump_target=jump_target,
                     group_by=group_by, status=status if intent == "stats" else None,
                     date_from=date_from, date_to=date_to, weak=weak)


# ── 진입점 ───────────────────────────────────────────────────────────────────
def _needs_llm(plan_: QueryPlan) -> bool:
    """규칙 계획이 약해서 LLM 을 부를 값어치가 있는가.

    규칙이 근거를 갖고 분류한 질문은 LLM 을 부르지 않는다 — 챗 동시 처리는 3슬롯이고 LLM 왕복은
    최대 30초라, 전부 LLM 을 태우면 최악 처리량이 3건/30초가 되고 네 번째 사용자는 429 를 본다.
    또 골든셋이 지키는 것은 규칙 경로이므로, 규칙을 1차로 두면 **테스트되는 경로가 곧 운영
    경로**가 된다. LLM 의 강점(lot_id·날짜 같은 슬롯 추출)은 정확히 아래 '약한' 케이스에 몰려 있다.
    """
    return (plan_.intent in ("unknown", "help") or plan_.weak
            or (plan_.intent == "session_find"
                and not (plan_.product or plan_.lot_id or plan_.date_from)))


def plan(question: str, *, use_llm=True, context_session_id=None) -> QueryPlan:
    """질문 → QueryPlan. **규칙을 먼저** 돌리고, 약할 때만 LLM 에 물어본다.

    LLM 이 꺼져 있거나 실패하면 규칙 계획을 그대로 쓴다(빈손으로 죽지 않는다).
    context_session_id 는 "지금 열려 있는 세션" — 웹 챗 패널이 세션 상세에서 보낸 질문의
    "이 세션" 을 해석하는 데 쓴다.
    """
    rule = rule_plan(question, context_session_id)
    if use_llm and llm_enabled() and _needs_llm(rule):
        data = _call_llm(question, context_session_id)
        if data is not None:
            return _plan_from_dict(data, question)
    return rule
