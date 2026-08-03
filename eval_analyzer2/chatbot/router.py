"""LLM off 일 때의 규칙기반 fallback 라우터.

목적: config.EVAL_LLM_* 가 비어(LLM 미설정)도 파이프라인이 **실제 DB 결과를 반환**하도록.
정교한 의도파악은 하지 않는다(그건 LLM 계층 몫). 키워드로 조회 함수 1개를 고르고,
결과 dict/list 를 사람이 읽을 텍스트로 포맷한다. langchain 비의존.
"""
from . import queries
from .queries import STATUS_VOCAB

_STATS_KW = ("통계", "개수", "몇", "카운트", "count", "집계", "분포")
_PREC_KW = ("선례", "과거", "precedent", "이력", "비슷")


def route(question: str) -> str:
    q = question.strip()
    ql = q.lower()

    if any(k in q or k in ql for k in _STATS_KW):
        group_by = _pick_group_by(q)
        return format_stats(group_by, queries.stats_summary(group_by))

    if any(k in q or k in ql for k in _PREC_KW):
        item = _guess_item(q)
        if not item:
            return "선례를 찾을 item 이름을 함께 입력해 주세요. 예: \"vref 선례\""
        return format_precedents(item, queries.find_precedents(item))

    # 기본: fail_case 검색 (status 키워드 있으면 필터)
    status = next((s for s in STATUS_VOCAB if s in q.upper()), None)
    token = _guess_item(q)
    rows = queries.search_cases(item=token, status=status)
    if not rows and token:  # item 으로 못 찾으면 product 로 재시도(규칙기반 한계 보완)
        rows = queries.search_cases(product=token, status=status)
    return format_cases(rows)


# ── 파라미터 추출(단순) ────────────────────────────────────────────────
def _pick_group_by(q: str) -> str:
    if "제품타입" in q or "product_type" in q.lower():
        return "product_type"
    if "제품" in q or "product" in q.lower():
        return "product"
    if "클래스" in q or "item_class" in q.lower():
        return "item_class"
    return "status"


def _guess_item(q: str) -> str | None:
    """따옴표로 감싼 토큰 또는 영문/숫자 토큰을 item 후보로(가장 단순한 추정)."""
    import re
    m = re.search(r'["“‘]([^"”’]+)["”’]', q)
    if m:
        return m.group(1).strip()
    toks = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", q)
    return toks[0] if toks else None


# ── 결과 포맷(공용 — agent.py 도 재사용) ─────────────────────────────────
def format_stats(group_by, rows) -> str:
    if not rows:
        return f"({group_by}별 집계 결과 없음 — DB 가 비었거나 아직 적재 전)"
    lines = [f"[{group_by}별 case 수]"]
    lines += [f"  {r['key']}: {r['count']}" for r in rows]
    return "\n".join(lines)


def format_cases(rows) -> str:
    if not rows:
        return "(조건에 맞는 case 없음)"
    lines = [f"검색된 case {len(rows)}건:"]
    for r in rows:
        lines.append(
            f"  - {r['product_name']} / {r['item_canonical']} / bin={r['bin']} "
            f"/ status={r.get('status')} / {r['case_id'][:10]}…")
        if r.get("comment"):
            lines.append(f"      · {r['comment']}")
    return "\n".join(lines)


def format_precedents(item, rows) -> str:
    if not rows:
        return f"(\"{item}\" 관련 선례 없음)"
    lines = [f"\"{item}\" 선례 {len(rows)}건:"]
    for r in rows:
        parts = [r.get("item_canonical"), f"sim={r.get('similarity', 0):.2f}"]
        if r.get("human_comment"):
            parts.append(f"코멘트: {r['human_comment']}")
        if r.get("action"):
            parts.append(f"조치: {r['action']}→{r.get('result')}")
        lines.append("  - " + " / ".join(str(p) for p in parts if p))
    return "\n".join(lines)
