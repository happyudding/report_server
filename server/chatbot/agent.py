"""고정 워크플로 오케스트레이션 — QueryPlan → 툴 호출 → 근거가 붙은 답변.

LangGraph 를 쓰지 않는다: 1단계 흐름은 분기 5개와 1회 완화 재검색이 전부라 평범한
함수 호출로 충분하다. 재검색 루프·후보 확인 멀티턴이 실제로 필요해지면 그때 도입한다.

원칙:
- **지어내지 않는다.** 결과가 없으면 "없음 + 시도한 조건"을 그대로 말한다.
- **근거를 붙인다.** 모든 결과 줄에 session_id / product / lot / 평가일 중 있는 것을 단다.
- **툴 호출을 기록한다.** `steps` 에 (함수, 인자, 결과 수)가 남아 CLI --json 으로 검증 가능.
"""
from __future__ import annotations

import logging
import time

from . import planner, tools_eval, tools_metrics, tools_report

_log = logging.getLogger(__name__)


class AnswerFailed(Exception):
    """핸들러 실행 중 실패 — **어디까지 갔는지**(plan/steps)를 함께 들고 올라간다.

    예외 클래스 이름만 남기면 "AttributeError 3건" 까지는 알아도 어느 인텐트의 어느 툴에서
    터졌는지 몰라 재현부터 다시 해야 한다. 그 왕복을 없애려고 계획과 툴 호출 기록을 붙인다.
    원인 예외는 ``cause`` 로 보존한다(`raise ... from` 이라 traceback 도 이어진다).
    """

    def __init__(self, cause, plan, steps):
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause
        self.plan = plan
        self.steps = steps

# 세션 상세 패널에서 온 질문의 "이 세션" 을 붙일 intent — 규칙 폴백은 컨텍스트를 모르므로
# 여기서 사후 주입한다.
_SESSION_INTENTS = ("session_issue", "session_metrics", "page_jump")

# 세션 바로가기 URL. 같은 페이지 안에서의 이동은 url 이 아니라 action 으로 내보낸다.
_VIEW_URL = "/pe/report/view/{sid}"
_LINK_CAP = 10


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
                    "sessions", "products", "groups"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def _run(question, *, viewer, see_all_private, use_llm, context_session_id=None) -> dict:
    plan = planner.plan(question, use_llm=use_llm, context_session_id=context_session_id)
    if not plan.session_id and context_session_id and plan.intent in _SESSION_INTENTS:
        plan.session_id = context_session_id
    trace = _Trace()
    web = {"links": [], "choices": [], "building": False}
    ctx = {"viewer": viewer, "see_all_private": see_all_private, "trace": trace,
           "web": web, "question": question, "context_session_id": context_session_id}

    handler = {
        "item_history": _item_history,
        "session_issue": _session_issue,
        "product_search": _product_search,
        "similar_case": _similar_case,
        "comment_search": _comment_search,
        "session_find": _session_find,
        "session_metrics": _session_metrics,
        "page_jump": _page_jump,
        "stats": _stats,
    }.get(plan.intent, _unknown)

    try:
        text, data = handler(plan, ctx)
    except Exception as exc:
        raise AnswerFailed(exc, plan.to_dict(), trace.steps) from exc
    try:
        _derive_links(data, ctx)
    except Exception:
        # 링크는 답변의 덤이다 — 파생이 실패했다고 이미 만든 답을 버리지 않는다.
        _log.warning("chatbot 링크 파생 실패 (답변은 그대로 반환)", exc_info=True)
    return {"plan": plan.to_dict(), "steps": trace.steps, "text": text, "data": data,
            "web": web}


def answer(question, *, viewer, see_all_private=False, use_llm=True) -> dict:
    """질문 1건 처리 → {plan, steps, text, data}. (CLI 계약 — 키 4개 고정)"""
    result = _run(question, viewer=viewer, see_all_private=see_all_private,
                  use_llm=use_llm)
    return {k: result[k] for k in ("plan", "steps", "text", "data")}


def answer_web(question, *, viewer, see_all_private=False, use_llm=True,
               context_session_id=None) -> dict:
    """웹 챗 패널용 — answer() 결과에 `web{links, choices, building}` 을 얹는다.

    links  : 세션 바로가기 url 또는 같은 페이지 내 점프 action
    choices: 무상태 선택 버튼 — 각 항목의 `question` 을 그대로 다시 보내면 된다
             (서버에 대화 상태를 두지 않으려고 후속 질의문을 완성해 내려보낸다)
    """
    return _run(question, viewer=viewer, see_all_private=see_all_private,
                use_llm=use_llm, context_session_id=context_session_id)


# ── 웹 응답(링크/선택지) ─────────────────────────────────────────────────────
def _link(ctx, session_id, label, *, tab=None, item=None):
    """세션 이동 링크 1개 추가.

    대상이 지금 열려 있는 세션이면 페이지 안에서 이동(action)하고, 다른 세션이면
    딥링크 url 을 준다 — 판정을 여기 한 곳에만 둬서 호출부가 실수할 여지를 없앤다.
    """
    web = ctx["web"]
    if len(web["links"]) >= _LINK_CAP or not session_id:
        return
    if session_id == ctx.get("context_session_id"):
        if tab == "item_detail" and item:
            action, args = "open_item_detail", {"subject": item}
        elif tab == "map":
            action, args = "open_map", {"subject": item} if item else {}
        elif tab:
            action, args = "open_tab", {"tab": tab}
        else:
            return      # 이미 그 세션을 보고 있다 — "여기 열기" 링크는 의미가 없다
        web["links"].append({"label": label, "action": action, "args": args})
        return
    url = _VIEW_URL.format(sid=session_id)
    if tab:
        url += f"?tab={_q(tab)}"
        if item:
            url += f"&item={_q(item)}"
    if any(existing.get("url") == url for existing in web["links"]):
        return
    web["links"].append({"label": label, "url": url})


def _q(value):
    from urllib.parse import quote
    return quote(str(value or ""), safe="")


def _choice(ctx, label, question):
    ctx["web"]["choices"].append({"label": label, "question": question})


def _derive_links(data, ctx):
    """핸들러가 직접 링크를 안 붙였어도, 결과에 담긴 세션들로 바로가기를 만들어 준다.

    기존 intent 핸들러(item_history 등)를 건드리지 않고 웹 응답을 얹기 위한 지점이다.
    """
    if ctx["web"]["links"]:
        return
    seen = []
    for session in (data.get("sessions") or []):
        seen.append(session)
    for detail in (data.get("issues") or []):
        session = detail.get("session")
        if isinstance(session, dict):
            seen.append(session)
    for hit in ((data.get("session_hits") or {}).get("hits") or []):
        seen.append(hit)
    done = set()
    for s in seen:
        sid = s.get("session_id")
        if not sid or sid in done:
            continue
        done.add(sid)
        _link(ctx, sid, f"보고서 열기: {s.get('product') or '?'}"
                        f" / lot {s.get('lot_id') or '-'}")


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


def _session_find(plan, ctx):
    """조건에 맞는 평가 세션을 찾아 바로가기를 준다 — "S3222 보고서 찾아줘"."""
    trace = ctx["trace"]
    found = trace.call(tools_report.search_sessions, viewer=ctx["viewer"],
                       see_all_private=ctx["see_all_private"],
                       product=plan.product, lot_id=plan.lot_id, limit=20)
    sessions = found.get("sessions") or []
    if not sessions and (plan.product or plan.lot_id):
        found = trace.call(tools_report.search_sessions, viewer=ctx["viewer"],
                           see_all_private=ctx["see_all_private"],
                           q=plan.product or plan.lot_id, limit=20)
        sessions = found.get("sessions") or []
    if not sessions:
        cond = plan.product or plan.lot_id or plan.normalized_question
        return f"\"{cond}\" 로 찾은 평가 세션이 없습니다.", {}

    lines = [f"세션 {found.get('total', len(sessions))}건 중 {len(sessions[:10])}건:"]
    for s in sessions[:10]:
        lines.append(f"  - {_session_line(s)}")
        _link(ctx, s["session_id"],
              f"보고서 열기: {s.get('product') or '?'} / lot {s.get('lot_id') or '-'}")
    if len(sessions) > 1:
        for s in sessions[:5]:
            _choice(ctx, f"{s.get('product') or '?'} / {_date(s.get('created_at'))} 이슈",
                    f"세션 {s['session_id']} 의 이슈 알려줘")
    else:
        sid = sessions[0]["session_id"]
        _choice(ctx, "이슈 보기", f"세션 {sid} 의 이슈 알려줘")
        _choice(ctx, "수율 보기", f"세션 {sid} 의 수율 알려줘")
    return "\n".join(lines), {"sessions": sessions}


def _session_metrics(plan, ctx):
    """세션의 수율 / CPK / 측정값. metric 이 없으면 가장 흔한 질문인 수율로 본다."""
    metric = plan.metric or "yield"
    sid, pending = _resolve_session(plan, ctx, f"{_METRIC_LABEL[metric]} 알려줘")
    if pending is not None:
        return pending, {"sessions": ctx.get("_candidates") or []}

    keyword = plan.item_keywords[0] if plan.item_keywords else None
    res = ctx["trace"].call(tools_metrics.get_session_metrics, session_id=sid,
                            viewer=ctx["viewer"], see_all_private=ctx["see_all_private"],
                            item_keyword=keyword if metric != "yield" else None)
    guard = _metrics_guard(res, ctx, sid)
    if guard is not None:
        return guard, {"metrics": res}

    session = res["session"]
    head = f"{session.get('product') or '?'} / lot {session.get('lot_id') or '-'}"
    if metric == "yield":
        return _format_yield(head, res, ctx, sid), {"metrics": res}
    if metric == "cpk":
        return _format_cpk(head, res, keyword, ctx, sid), {"metrics": res}
    return _format_raw(head, res, keyword, ctx, sid)


def _page_jump(plan, ctx):
    """화면 이동만 — "맵 열어줘" / "VDD_INT 상세 보여줘"."""
    target = plan.jump_target or "item_detail"
    label = "웨이퍼 맵" if target == "map" else "항목 상세"
    sid, pending = _resolve_session(plan, ctx, f"{label} 열어줘")
    if pending is not None:
        return pending, {"sessions": ctx.get("_candidates") or []}

    subject = plan.item_keywords[0] if plan.item_keywords else None
    exact = None
    if subject:
        listing = ctx["trace"].call(tools_metrics.list_items, session_id=sid,
                                    viewer=ctx["viewer"],
                                    see_all_private=ctx["see_all_private"],
                                    keyword=subject, limit=20)
        items = listing.get("items") or []
        if len(items) == 1:
            exact = items[0]
        elif len(items) > 1:
            for name in items[:8]:
                _choice(ctx, name,
                        f"세션 {sid} 의 {name} {'맵' if target == 'map' else '상세'} 열어줘")
            return (f"\"{subject}\" 로 걸린 항목이 {len(items)}개입니다 — 하나 고르세요.",
                    {"items": items})
        elif not listing.get("building") and not listing.get("error"):
            return (f"\"{subject}\" 로 걸린 항목이 이 세션에 없습니다.", {"items": []})

    if target == "map":
        _link(ctx, sid, f"Map Analysis 열기{f' — {exact}' if exact else ''}",
              tab="map", item=exact)
        return "아래 버튼으로 Map Analysis 를 엽니다.", {"session_id": sid, "item": exact}
    if not exact:
        return ("어떤 항목의 상세를 열지 알려주세요. 예: \"VDD_INT 상세 보여줘\"",
                {"session_id": sid})
    _link(ctx, sid, f"Item Detail 열기 — {exact}", tab="item_detail", item=exact)
    return f"아래 버튼으로 \"{exact}\" 상세를 엽니다.", {"session_id": sid, "item": exact}


def _stats(plan, ctx):
    """"몇 건인가" — eval.db fail_case 를 축 하나로 세어 준다."""
    axis = plan.group_by or "status"
    try:
        res = ctx["trace"].call(tools_eval.stats_summary, group_by=axis,
                                product_type=plan.product_type,
                                family_product=plan.family_product,
                                status=plan.status, limit=20)
    except ValueError as exc:
        return str(exc), {}
    groups = res.get("groups") or []
    if not groups:
        if not res.get("db_available"):
            return (f"[eval DB] 없음 — {res.get('db_path')} (집계할 데이터가 없습니다)",
                    {"stats": res})
        return "조건에 맞는 case 가 없습니다.", {"stats": res}

    scope = " / ".join(x for x in (plan.product_type, plan.family_product,
                                   plan.status) if x)
    head = (f"{_AXIS_LABEL.get(axis, axis)} 집계 — 총 {res['total']}건"
            + (f" ({scope})" if scope else ""))
    lines = [head]
    for row in groups:
        lines.append(f"  - {row.get('key') or '(없음)'}: {row.get('count')}건"
                     f" (최근 {_date(row.get('last_at'))})")
    if axis != "status" and not plan.status:
        _choice(ctx, "판정별로 보기", "판정별 통계 알려줘")
    if axis != "product":
        _choice(ctx, "제품별로 보기", "제품별 건수 알려줘")
    return "\n".join(lines), {"stats": res}


_AXIS_LABEL = {"status": "판정", "product": "제품", "product_type": "제품 타입",
               "family_product": "제품군", "item": "항목", "item_class": "항목 분류",
               "bin": "bin"}
_METRIC_LABEL = {"yield": "수율", "cpk": "CPK", "raw": "측정값"}


def _resolve_session(plan, ctx, followup_suffix):
    """(session_id, None) 또는 (None, 되물음 텍스트).

    질문이 세션을 지목하지 않았으면 조건으로 후보를 찾고, 하나면 확정, 여럿이면 선택
    버튼을 만들어 되묻는다(서버에 대화 상태를 두지 않으므로 후속 질의문을 완성해 준다).
    """
    if plan.session_id:
        return plan.session_id, None
    if not (plan.product or plan.lot_id):
        return None, ("어느 보고서인지 알려주세요. 세션을 연 상태에서 물으시거나 "
                      "제품명을 함께 적어 주세요. 예: \"S3222 수율 알려줘\"")
    found = ctx["trace"].call(tools_report.search_sessions, viewer=ctx["viewer"],
                              see_all_private=ctx["see_all_private"],
                              product=plan.product, lot_id=plan.lot_id, limit=10)
    sessions = found.get("sessions") or []
    if not sessions and plan.product:
        found = ctx["trace"].call(tools_report.search_sessions, viewer=ctx["viewer"],
                                  see_all_private=ctx["see_all_private"],
                                  q=plan.product, limit=10)
        sessions = found.get("sessions") or []
    ctx["_candidates"] = sessions
    if not sessions:
        cond = plan.product or plan.lot_id
        return None, f"\"{cond}\" 로 찾은 평가 세션이 없습니다."
    if len(sessions) == 1:
        return sessions[0]["session_id"], None
    for s in sessions[:8]:
        _choice(ctx, f"{s.get('product') or '?'} / lot {s.get('lot_id') or '-'} / "
                     f"{_date(s.get('created_at'))}",
                f"세션 {s['session_id']} 의 {followup_suffix}")
    return None, f"해당 조건의 세션이 {len(sessions)}건입니다 — 하나 고르세요."


def _metrics_guard(res, ctx, sid):
    """콜드/권한/xlsx 분기를 사람 문장으로. 정상이면 None."""
    if res.get("building"):
        ctx["web"]["building"] = True
        if res.get("blocked"):
            return ("이 세션은 리포트 계산이 반복 실패해 지금은 수치를 낼 수 없습니다. "
                    "보고서 화면에서 직접 확인해 주세요.")
        _choice(ctx, "다시 시도", ctx["question"])
        return ("리포트가 아직 계산되지 않아 백그라운드 빌드를 시작했습니다 "
                "(수 초~수십 초). 잠시 후 다시 시도해 주세요.")
    error = res.get("error")
    if error == "session_not_found":
        return "해당 세션을 찾을 수 없습니다(또는 조회 권한이 없습니다)."
    if error == "not_web_report":
        return "이 세션은 xlsx 업로드 세션이라 수율/CPK 수치를 계산해 두지 않습니다."
    if error == "item_not_found":
        return f"\"{res.get('subject')}\" 항목을 이 세션에서 찾지 못했습니다."
    return None


def _format_yield(head, res, ctx, sid):
    y = res.get("yield_summary") or {}
    if not y:
        return f"{head} — 수율 요약이 비어 있습니다."
    lines = [f"{head} 수율 {y.get('yield_pct')}% "
             f"(pass {y.get('pass')} / fail {y.get('fail')} / 측정 {y.get('tested')} / "
             f"분모 {y.get('total')})"]
    for s in (y.get("by_source") or [])[:12]:
        lines.append(f"  - {s.get('source')}: {s.get('yield_pct')}% "
                     f"(pass {s.get('pass')} / fail {s.get('fail')})")
    worst = res.get("cpk_worst")
    if worst and worst.get("cpk") is not None:
        lines.append(f"  · 최저 CPK: {worst.get('subject')} = {worst['cpk']:.2f} "
                     f"({worst.get('source')})")
    _link(ctx, sid, "보고서 열기")
    _choice(ctx, "CPK 워스트 보기", f"세션 {sid} 의 cpk 알려줘")
    return "\n".join(lines)


def _format_cpk(head, res, keyword, ctx, sid):
    rows = res.get("cpk_rows") or []
    if not rows:
        cond = f"\"{keyword}\" 로 걸린 " if keyword else ""
        return f"{head} — {cond}CPK 항목이 없습니다."
    title = (f"{head} — \"{keyword}\" CPK {len(rows)}건:" if keyword
             else f"{head} — CPK 낮은 순 {len(rows)}건:")
    lines = [title]
    for r in rows:
        lines.append(f"  - {r.get('subject')} ({r.get('source')}) "
                     f"cpk={_num(r.get('cpk'))} / avg={_num(r.get('average'))} "
                     f"/ limit {_num(r.get('lower_limit'))}~{_num(r.get('upper_limit'))}")
    for subject in (res.get("items_matched") or [])[:5]:
        _link(ctx, sid, f"상세 보기 — {subject}", tab="item_detail", item=subject)
    return "\n".join(lines)


def _format_raw(head, res, keyword, ctx, sid):
    matched = res.get("items_matched") or []
    if not keyword:
        return ("어떤 항목의 측정값을 볼지 알려주세요. 예: \"VDD_INT 측정값 보여줘\"",
                {"metrics": res})
    if not matched:
        return f"{head} — \"{keyword}\" 로 걸린 항목이 없습니다.", {"metrics": res}
    if len(matched) > 1:
        for name in matched[:8]:
            _choice(ctx, name, f"세션 {sid} 의 {name} 측정값 알려줘")
        return (f"\"{keyword}\" 로 걸린 항목이 {len(matched)}개입니다 — 하나 고르세요.",
                {"metrics": res})

    subject = matched[0]
    values = ctx["trace"].call(tools_metrics.get_item_values, session_id=sid,
                               subject=subject, viewer=ctx["viewer"],
                               see_all_private=ctx["see_all_private"])
    guard = _metrics_guard(values, ctx, sid)
    if guard is not None:
        return guard, {"metrics": res, "values": values}

    lines = [f"{head} — {subject} ({values.get('units') or '-'}) "
             f"limit {_num(values.get('lower_limit'))}~{_num(values.get('upper_limit'))} "
             f"/ cpk={_num(values.get('cpk'))} / {values.get('status') or '-'}"]
    for st in values.get("stats") or []:
        lines.append(f"  - {st.get('source')}: n={st.get('n')} "
                     f"min={_num(st.get('min'))} avg={_num(st.get('average'))} "
                     f"max={_num(st.get('max'))} stdev={_num(st.get('stdev'))}")
    for src in values.get("sources") or []:
        low = ", ".join(_num(v) for v in (src.get("min_values") or [])[:5])
        high = ", ".join(_num(v) for v in (src.get("max_values") or [])[:5])
        lines.append(f"  · {src.get('source')} 최소 {low} / 최대 {high} "
                     f"(총 {src.get('count')}점)")
    if values.get("fail_total"):
        lines.append(f"  · 이 항목으로 fail 한 die {values['fail_total']}개")
    _link(ctx, sid, f"Item Detail 열기 — {subject}", tab="item_detail", item=subject)
    return "\n".join(lines), {"metrics": res, "values": values}


def _session_line(s):
    return (f"{s.get('product') or '?'} / lot {s.get('lot_id') or '-'} / "
            f"{_date(s.get('created_at'))} / {s.get('file_name') or '-'} "
            f"({s['session_id']})")


def _num(value):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _unknown(plan, ctx):
    return ("무엇을 찾을지 파악하지 못했습니다. 다음처럼 물어보세요:\n"
            "  - \"PMIC SOC 에 SGM 들어가는 항목 이력\"\n"
            "  - \"S3222 보고서에서 LDO 이슈 어떻게 close 됐어?\"\n"
            "  - \"S3222 라는 제품 있었어?\"\n"
            "  - \"S3222 보고서 찾아줘\" / \"이 세션 수율 알려줘\"\n"
            "  - \"VDD_INT 상세 보여줘\" / \"맵 열어줘\"\n"
            "  - \"PMIC 에 MAJOR 몇 건이야?\" / \"제품별 건수 알려줘\"", {})


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
