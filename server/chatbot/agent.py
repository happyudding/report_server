"""고정 워크플로 오케스트레이션 — QueryPlan → 툴 호출 → 근거가 붙은 답변.

LangGraph 를 쓰지 않는다: 1단계 흐름은 분기 5개와 1회 완화 재검색이 전부라 평범한
함수 호출로 충분하다. 재검색 루프·후보 확인 멀티턴이 실제로 필요해지면 그때 도입한다.

원칙:
- **지어내지 않는다.** 결과가 없으면 "없음 + 시도한 조건"을 그대로 말한다.
- **근거를 붙인다.** 모든 결과 줄에 session_id / product / lot / 평가일 중 있는 것을 단다.
- **툴 호출을 기록한다.** `steps` 에 (함수, 인자, 결과 수)가 남아 CLI --json 으로 검증 가능.
"""
from __future__ import annotations

import time

from . import planner, tools_eval, tools_report


class _Trace:
    """툴 호출 로그. 골든 세트 채점이 '어떤 툴을 어떤 인자로 불렀나'를 보게 한다."""

    def __init__(self):
        self.steps = []

    def call(self, fn, **kwargs):
        result = fn(**kwargs)
        self.steps.append({"tool": fn.__name__,
                           "args": {k: v for k, v in kwargs.items() if k != "viewer"},
                           "count": _count(result)})
        return result


def _count(result):
    if isinstance(result, dict):
        for key in ("items", "history", "similar", "comments", "hits", "issues",
                    "sessions", "products"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def answer(question, *, viewer, see_all_private=False, use_llm=True) -> dict:
    """질문 1건 처리 → {plan, steps, text, data}."""
    plan = planner.plan(question, use_llm=use_llm)
    trace = _Trace()
    ctx = {"viewer": viewer, "see_all_private": see_all_private, "trace": trace}

    handler = {
        "item_history": _item_history,
        "session_issue": _session_issue,
        "product_search": _product_search,
        "similar_case": _similar_case,
        "comment_search": _comment_search,
    }.get(plan.intent, _unknown)

    text, data = handler(plan, ctx)
    return {"plan": plan.to_dict(), "steps": trace.steps, "text": text, "data": data}


# ── intent 별 흐름 ───────────────────────────────────────────────────────────
def _item_history(plan, ctx):
    """item 축: eval.db 후보/이력 + report.db 세션 근거를 함께 본다.

    두 소스를 모두 부르는 이유 — eval.db 는 item 마스터·수치·선례를 갖고 있고,
    report.db 는 "어느 세션에서 누가 뭐라고 썼나"라는 근거를 갖고 있다. 어느 한쪽만으로는
    "예전에 어떻게 됐었지?" 에 답이 안 된다.
    """
    trace = ctx["trace"]
    keyword = plan.item_keywords[0] if plan.item_keywords else None
    if not keyword:
        return ("어떤 항목(item)을 찾을지 알려주세요. 예: \"SGM 들어가는 항목 이력\"", {})

    lines = []
    data = {}

    cand = trace.call(tools_eval.search_item_candidates, item_keyword=keyword,
                      product_type=plan.product_type, family_product=plan.family_product,
                      limit=20)
    data["candidates"] = cand
    items = cand.get("items") or []
    if items:
        lines.append(f"[eval DB] \"{keyword}\" 가 포함된 항목 {len(items)}개:")
        for it in items[:10]:
            products = ", ".join(it.get("products_sample") or []) or "-"
            lines.append(f"  - {it['item_canonical']} ({it.get('value_type') or '?'}) "
                         f"— case {it.get('cases', 0)}건 / 제품 {products}")
        if len(items) == 1:
            hist = trace.call(tools_eval.get_item_history,
                              item_canonical=items[0]["item_canonical"],
                              family_product=plan.family_product,
                              product_type=plan.product_type, limit=20)
            data["history"] = hist
            lines.append("")
            lines.extend(_format_history(items[0]["item_canonical"], hist))
        else:
            lines.append("  → 항목을 하나 지정하면 상세 이력을 보여드립니다.")
    elif not cand.get("db_available"):
        lines.append(f"[eval DB] 없음 — {cand.get('db_path')} (item 마스터 조회 건너뜀)")
    else:
        lines.append(f"[eval DB] \"{keyword}\" 로 찾은 항목 없음")

    hits = trace.call(tools_report.search_item_in_sessions, item_keyword=keyword,
                      viewer=ctx["viewer"], see_all_private=ctx["see_all_private"],
                      product_type=plan.product_type, family_product=plan.family_product,
                      limit=30)
    data["session_hits"] = hits
    lines.append("")
    if hits.get("hits"):
        lines.append(f"[세션 기록] \"{keyword}\" 관련 이슈 {len(hits['hits'])}건 "
                     f"({hits['sessions']}개 세션):")
        lines.extend(_format_hits(hits["hits"]))
    else:
        lines.append(f"[세션 기록] \"{keyword}\" 로 걸린 Issue Table 코멘트 없음")

    return "\n".join(lines), data


def _session_issue(plan, ctx):
    """제품 → 세션 → 그 세션의 이슈. 세션이 여럿이면 전부 훑어 item 으로 거른다."""
    trace = ctx["trace"]
    item_kw = plan.item_keywords[0] if plan.item_keywords else None

    found = trace.call(tools_report.search_sessions, viewer=ctx["viewer"],
                       see_all_private=ctx["see_all_private"],
                       product=plan.product, lot_id=plan.lot_id, limit=20)
    sessions = found.get("sessions") or []
    if not sessions and plan.product:
        # 1회 완화: 정확일치 product 를 자유 검색어로 바꿔 다시 본다.
        found = trace.call(tools_report.search_sessions, viewer=ctx["viewer"],
                           see_all_private=ctx["see_all_private"],
                           q=plan.product, limit=20)
        sessions = found.get("sessions") or []
    if not sessions:
        cond = plan.product or plan.lot_id or plan.normalized_question
        return (f"\"{cond}\" 로 찾은 평가 세션이 없습니다. "
                f"제품명을 다르게 알고 계실 수 있어 product_search 로도 확인해 보세요.", {})

    lines = [f"세션 {len(sessions)}건 확인:"]
    data = {"sessions": sessions, "issues": []}
    for s in sessions[:10]:
        detail = trace.call(tools_report.get_session_issues, session_id=s["session_id"],
                            viewer=ctx["viewer"], see_all_private=ctx["see_all_private"],
                            item_keyword=item_kw)
        data["issues"].append(detail)
        head = (f"  - {s.get('product') or '?'} / lot {s.get('lot_id') or '-'} / "
                f"{_date(s.get('created_at'))} / {s['session_id']}")
        if detail.get("error"):
            lines.append(head + f"  ({detail['error']})")
            continue
        issues = detail.get("issues") or []
        if not issues:
            lines.append(head + ("  (해당 item 이슈 없음)" if item_kw else "  (이슈 없음)"))
            continue
        lines.append(head)
        for issue in issues[:10]:
            lines.append("      " + _format_issue(issue))
        if detail.get("note"):
            lines.append(f"      · {detail['note']}")
    return "\n".join(lines), data


def _product_search(plan, ctx):
    trace = ctx["trace"]
    keyword = plan.product or plan.normalized_question
    res = trace.call(tools_report.search_products, keyword=keyword, viewer=ctx["viewer"],
                     see_all_private=ctx["see_all_private"], limit=20)
    products = res.get("products") or []
    if not products:
        return f"\"{keyword}\" 로 찾은 제품이 없습니다.", {"products": res}
    lines = [f"\"{keyword}\" 로 찾은 제품 {len(products)}개:"]
    for p in products:
        lots = ", ".join(p.get("lot_ids") or []) or "-"
        lines.append(f"  - {p['product']} ({p.get('product_type') or '?'}"
                     f"/{p.get('family_product') or '?'}) — 세션 {p['sessions']}건, "
                     f"최근 {_date(p['last_created_at'])}, lot: {lots}")
    if res.get("truncated"):
        lines.append(f"  ※ 최근 {res['scanned_sessions']}개 세션만 훑은 결과입니다.")
    return "\n".join(lines), {"products": res}


def _similar_case(plan, ctx):
    trace = ctx["trace"]
    keyword = plan.item_keywords[0] if plan.item_keywords else None
    if not keyword:
        return "어떤 항목과 비슷한 사례를 찾을지 알려주세요.", {}
    cand = trace.call(tools_eval.search_item_candidates, item_keyword=keyword,
                      product_type=plan.product_type, family_product=plan.family_product,
                      limit=5)
    items = cand.get("items") or []
    if not items:
        note = ("eval DB 없음" if not cand.get("db_available") else "일치 항목 없음")
        return f"\"{keyword}\" 의 유사 사례를 찾지 못했습니다 ({note}).", {"candidates": cand}
    canonical = items[0]["item_canonical"]
    res = trace.call(tools_eval.search_similar_cases, item_canonical=canonical,
                     family_product=plan.family_product, limit=10)
    rows = res.get("similar") or []
    if not rows:
        return (f"\"{canonical}\" 와 같은 유형(value_type "
                f"{items[0].get('value_type') or '?'})의 다른 사례가 없습니다.",
                {"similar": res})
    lines = [f"\"{canonical}\" 와 같은 유형의 사례 {len(rows)}건:"]
    for r in rows:
        lines.append(f"  - {r.get('product_name')} / {r.get('item_canonical')} / "
                     f"bin={r.get('bin')} / {_date(r.get('occurred_at'))}"
                     f"{_ref(r.get('session_id'))}")
        if r.get("human_comment"):
            lines.append(f"      · {_oneline(r['human_comment'])}")
    return "\n".join(lines), {"similar": res}


def _comment_search(plan, ctx):
    trace = ctx["trace"]
    keyword = plan.free_text or plan.normalized_question
    res = trace.call(tools_eval.search_comments, keyword=keyword,
                     family_product=plan.family_product, limit=20)
    rows = res.get("comments") or []
    if not rows:
        note = ("eval DB 없음" if not res.get("db_available") else "일치 코멘트 없음")
        return f"\"{keyword}\" 가 들어간 코멘트를 찾지 못했습니다 ({note}).", {"comments": res}
    lines = [f"\"{keyword}\" 가 들어간 코멘트 {len(rows)}건:"]
    for r in rows:
        lines.append(f"  - {r.get('product_name')} / {r.get('item_canonical')} / "
                     f"bin={r.get('bin')}{_ref(r.get('session_id'))}")
        lines.append(f"      · {_oneline(r.get('human_comment'))}")
    return "\n".join(lines), {"comments": res}


def _unknown(plan, ctx):
    return ("무엇을 찾을지 파악하지 못했습니다. 다음처럼 물어보세요:\n"
            "  - \"PMIC SOC 에 SGM 들어가는 항목 이력\"\n"
            "  - \"S3222 보고서에서 LDO 이슈 어떻게 close 됐어?\"\n"
            "  - \"S3222 라는 제품 있었어?\"", {})


# ── 포맷 ─────────────────────────────────────────────────────────────────────
def _format_history(canonical, hist):
    rows = hist.get("history") or []
    if not rows:
        if not hist.get("db_available"):
            return [f"[eval DB] 없음 — {hist.get('db_path')}"]
        return [f"\"{canonical}\" 의 과거 이력 없음"]
    lines = [f"\"{canonical}\" 과거 이력 {len(rows)}건:"]
    for r in rows:
        metrics = []
        if r.get("cpk") is not None:
            metrics.append(f"cpk={r['cpk']:.2f}")
        if r.get("yield") is not None:
            metrics.append(f"yield={r['yield']:.2f}%")
        if r.get("fail_count") is not None:
            metrics.append(f"fail={r['fail_count']}")
        lines.append(f"  - {r.get('product_name')} / lot {r.get('lot_id') or '-'} / "
                     f"bin={r.get('bin')} / {_date(r.get('occurred_at'))} "
                     f"{' '.join(metrics)}{_ref(r.get('session_id'))}")
        if r.get("engine_status"):
            lines.append(f"      · 엔진 판정: {r['engine_status']}"
                         + (f" — {_oneline(r['engine_comment'])}"
                            if r.get("engine_comment") else ""))
        if r.get("human_comment"):
            lines.append(f"      · 사람 코멘트: {_oneline(r['human_comment'])}")
    return lines


def _format_hits(hits):
    lines = []
    for h in hits[:20]:
        lines.append(f"  - {h.get('product') or '?'} / lot {h.get('lot_id') or '-'} / "
                     f"{h['category']}"
                     + (f" bin={h['bin']}" if h.get("bin") is not None else "")
                     + f" / {h['item']} / {h['status']} / {_date(h.get('created_at'))}"
                     + _ref(h.get("session_id")))
        for col, text in (h.get("comments") or {}).items():
            lines.append(f"      · [{col}] {_oneline(text)}")
    return lines


def _format_issue(issue):
    head = (f"{issue.get('category') or '?'}"
            + (f" bin={issue['bin']}" if issue.get("bin") is not None else "")
            + f" / {issue.get('item') or '?'}"
            + (f" / {issue['status']}" if issue.get("status") else ""))
    comments = issue.get("comments") or {}
    if not comments:
        return head
    body = " | ".join(f"[{col}] {text}" for col, text in comments.items())
    return f"{head}\n          · {body}"


def _oneline(text):
    """여러 줄 코멘트를 한 줄로 — 병합 코멘트는 "[PTE] ...\\n[개발] ..." 형태라
    그대로 찍으면 들여쓰기가 깨진다."""
    return " / ".join(part.strip() for part in str(text or "").splitlines() if part.strip())


def _date(epoch):
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(epoch)))
    except (TypeError, ValueError):
        return "-"


def _ref(session_id):
    return f"  ({session_id})" if session_id else ""
