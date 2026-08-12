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

from . import conversation, planner, tools_eval, tools_help, tools_metrics, tools_report

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
_SESSION_INTENTS = ("session_issue", "session_metrics", "page_jump", "session_meta")
# 직전 항목을 이어받아도 되는 intent — 항목이 질문의 주어인 것들만.
_ITEM_CARRY_INTENTS = ("item_history", "session_issue", "similar_case")

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
                    "sessions", "products", "groups", "features"):
            value = result.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def _run(question, *, viewer, see_all_private, use_llm, context_session_id=None,
         conversation_id=None) -> dict:
    state = conversation.recall(conversation_id)
    plan = planner.plan(question, use_llm=use_llm, context_session_id=context_session_id)

    # ── 직전 대화에서 이어받기 (사실만 이어받는다) ──────────────────────────
    # "1번 세션 Yield 알려줘" — 직전 답변의 목록에서 N번째를 집는다.
    if plan.ordinal and not plan.session_id:
        listed = state.get("sessions") or []
        idx = plan.ordinal - 1 if plan.ordinal > 0 else len(listed) - 1
        if 0 <= idx < len(listed):
            plan.session_id = listed[idx].get("session_id")
    # 세션을 안 말했으면: 열어 둔 세션 > 직전에 고른 세션 순으로 잇는다.
    if not plan.session_id and plan.intent in _SESSION_INTENTS:
        plan.session_id = context_session_id or state.get("session_id")
    # "이 제품 예전에 …" — 제품을 안 말했으면 직전 제품을 잇는다.
    if not plan.product and plan.intent in ("item_history", "session_issue", "session_find"):
        plan.product = state.get("product")
    # "그 항목 코멘트 뭐야" — 항목을 안 말했으면 직전 항목을 잇는다.
    # ⚠ page_jump 는 제외한다: "맵 링크 알려줘" 에 직전 항목이 딸려 들어가면 탭을 여는 대신
    #    "그 항목이 20개인데 고르세요" 로 되묻게 된다(항목을 안 말한 것이 곧 "탭만 열어줘"다).
    if not plan.item_keywords and state.get("item") and plan.intent in _ITEM_CARRY_INTENTS:
        plan.item_keywords = [state["item"]]

    trace = _Trace()
    web = {"links": [], "choices": [], "building": False}
    ctx = {"viewer": viewer, "see_all_private": see_all_private, "trace": trace,
           "web": web, "question": question, "context_session_id": context_session_id,
           "conversation_id": conversation_id, "state": state}

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
        "item_search": _item_search,
        "session_meta": _session_meta,
        "feature_help": _feature_help,
        "help": _help,
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
    _remember(conversation_id, plan, data)
    return {"plan": plan.to_dict(), "steps": trace.steps, "text": text, "data": data,
            "web": web}


def _remember(conversation_id, plan, data):
    """이번 턴에서 확정된 **사실만** 대화 상태에 남긴다 (질문 원문·추론은 담지 않는다)."""
    if not conversation_id:
        return
    listed = None
    rows = data.get("sessions")
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        listed = [{"session_id": r.get("session_id"), "product": r.get("product"),
                   "lot_id": r.get("lot_id")} for r in rows if r.get("session_id")]
    conversation.remember(conversation_id,
                          sessions=listed,
                          session_id=plan.session_id,
                          product=plan.product,
                          item=plan.item_keywords[0] if plan.item_keywords else None)


def answer(question, *, viewer, see_all_private=False, use_llm=True) -> dict:
    """질문 1건 처리 → {plan, steps, text, data}. (CLI 계약 — 키 4개 고정)"""
    result = _run(question, viewer=viewer, see_all_private=see_all_private,
                  use_llm=use_llm)
    return {k: result[k] for k in ("plan", "steps", "text", "data")}


def answer_web(question, *, viewer, see_all_private=False, use_llm=True,
               context_session_id=None, conversation_id=None) -> dict:
    """웹 챗 패널용 — answer() 결과에 `web{links, choices, building}` 을 얹는다.

    links  : 세션 바로가기 url 또는 같은 페이지 내 점프 action
    choices: 선택 버튼 — 각 항목의 `question` 을 그대로 다시 보내면 된다(후속 질의문 완성형)
    conversation_id: 주면 직전 턴의 **사실**(보여준 세션 목록·고른 세션·항목·제품)을 이어받아
             "1번 세션 Yield 알려줘", "그 항목 코멘트 뭐야" 같은 후속 질문이 성립한다.
    """
    return _run(question, viewer=viewer, see_all_private=see_all_private,
                use_llm=use_llm, context_session_id=context_session_id,
                conversation_id=conversation_id)


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
                      product=plan.product, limit=30)
    data["session_hits"] = hits

    # 근거 기반 재분류 — 규칙이 "영문 토큰이 있으니 item 이겠지"로 찍었는데(weak) item 축에
    # 아무것도 없으면, 그 토큰을 세션 자유검색에 던져 본다. "IW06 있어?" 처럼 제품/lot/파일명은
    # 맞는데 item 은 아닌 경우가 여기서 바로잡힌다. 모양이 아니라 데이터가 판정한다.
    if plan.weak and not items and not hits.get("hits"):
        alt = trace.call(tools_report.search_sessions, q=keyword, viewer=ctx["viewer"],
                         see_all_private=ctx["see_all_private"], limit=20)
        if alt.get("sessions"):
            plan.intent = "session_find"      # 응답·관리자 로그가 사실과 일치하게
            return _render_sessions(alt, ctx, plan)

    lines.append("")
    if hits.get("hits"):
        lines.append(f"[세션 기록] \"{keyword}\" 관련 이슈 {len(hits['hits'])}건 "
                     f"({hits['sessions']}개 세션):")
        lines.extend(_format_hits(hits["hits"]))
    else:
        lines.append(f"[세션 기록] \"{keyword}\" 로 걸린 Issue Table 코멘트 없음")

    return "\n".join(lines), data


def _session_issue(plan, ctx):
    """제품 → 세션 → 그 세션의 이슈. 세션이 여럿이면 전부 훑어 item 으로 거른다.

    세션이 하나로 정해져 있으면(컨텍스트·"1번"·직전 대화) 그 세션만 보고, Open/fail·카테고리·
    상위 N·이름 일치율 같은 추림 조건을 적용한다.
    """
    trace = ctx["trace"]
    item_kw = plan.item_keywords[0] if plan.item_keywords else None

    if plan.session_id:
        return _one_session_issues(plan, ctx, plan.session_id, item_kw)

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


def _one_session_issues(plan, ctx, sid, item_kw):
    """세션 1건의 Issue Table 을 조건대로 추려 보여준다.

    조건: Open/Close/fail 필터 · 카테고리(CPK/Yield/TEMP/ETC) · 상위 N · item 이름 일치율 순.
    item 이름은 부분일치가 아니라 **일치율 순 정렬**이라, 정확한 이름을 몰라도 가까운 것부터
    나온다("CPK 에 xxx 아이템 있어?").
    """
    detail = ctx["trace"].call(tools_report.get_session_issues, session_id=sid,
                               viewer=ctx["viewer"],
                               see_all_private=ctx["see_all_private"])
    if detail.get("error") == "session_not_found":
        return "해당 세션을 찾을 수 없습니다(또는 조회 권한이 없습니다).", {"issues": [detail]}
    if detail.get("error"):
        return "삭제되었거나 아직 처리 중인 세션입니다.", {"issues": [detail]}

    issues = list(detail.get("issues") or [])
    total = len(issues)
    if plan.issue_category:
        issues = [i for i in issues
                  if str(i.get("category") or "").upper() == plan.issue_category.upper()]
    if plan.issue_filter == "open":
        issues = [i for i in issues if str(i.get("status") or "Open") != "Close"]
    elif plan.issue_filter == "close":
        issues = [i for i in issues if str(i.get("status") or "") == "Close"]
    elif plan.issue_filter == "fail":
        # Yield 이슈 = 특정 bin 으로 떨어진 die — "fail 된 거"에 해당하는 축이다.
        issues = [i for i in issues if str(i.get("category") or "").upper() == "YIELD"]

    if item_kw:
        scored = [(_match_score(item_kw, i.get("item")), i) for i in issues]
        scored = [(s, i) for s, i in scored if s > 0]
        scored.sort(key=lambda si: -si[0])
        issues = [i for _, i in scored]
        ranked_by = "이름 일치율"
    else:
        # 이름 조건이 없으면 Open 을 위로(미해결이 먼저 눈에 띄어야 한다).
        issues.sort(key=lambda i: (str(i.get("status") or "Open") == "Close",))
        ranked_by = "Open 우선"

    shown = issues[:plan.top_n] if plan.top_n else issues[:15]
    session = detail.get("session") or {}
    head = (f"{session.get('product') or '?'} / lot {session.get('lot_id') or '-'} "
            f"— 이슈 {len(issues)}건" + (f" (전체 {total}건 중)" if len(issues) != total else ""))
    cond = " · ".join(x for x in (
        {"open": "Open 만", "close": "Close 만", "fail": "Yield(fail) 만"}.get(plan.issue_filter),
        f"{plan.issue_category} 카테고리" if plan.issue_category else None,
        f"\"{item_kw}\" {ranked_by}" if item_kw else None,
        f"상위 {plan.top_n}" if plan.top_n else None) if x)
    lines = [head + (f"  [{cond}]" if cond else "")]
    if not issues:
        lines.append("  조건에 맞는 이슈가 없습니다.")
    for issue in shown:
        lines.append("  - " + _format_issue(issue))
        if issue.get("item"):
            _link(ctx, sid, f"상세: {issue['item']}", tab="item_detail", item=issue["item"])
    if detail.get("note"):
        lines.append(f"  · {detail['note']}")
    _link(ctx, sid, "보고서 열기")
    return "\n".join(lines), {"issues": [detail], "shown": shown}


def _match_score(keyword, name):
    """item 이름 일치율 0~1 — 정확일치 > 접두 > 부분 > 문자 유사도 순."""
    kw = str(keyword or "").strip().lower()
    nm = str(name or "").strip().lower()
    if not kw or not nm:
        return 0.0
    if kw == nm:
        return 1.0
    if nm.startswith(kw):
        return 0.9
    if kw in nm:
        return 0.8
    import difflib
    ratio = difflib.SequenceMatcher(None, kw, nm).ratio()
    # 너무 먼 것까지 늘어놓으면 "없다"는 사실이 가려진다.
    return ratio if ratio >= 0.55 else 0.0


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
    """조건에 맞는 평가 세션을 찾아 바로가기를 준다 — "S3222 보고서 찾아줘".

    제품·lot 이 없으면 남은 토큰을 자유검색(q)으로 넘긴다 — `q` 는 lot_id·file_name·
    session_id·uploaded_by 등 10컬럼을 훑으므로 "IW06 세션 있냐?" 처럼 제품 코드처럼 안 생긴
    문자열도 여기서 걸린다(규칙이 모양으로 못 알아본 것을 DB 가 알아본다).
    """
    trace = ctx["trace"]
    free = None
    if not plan.product and not plan.lot_id:
        free = (plan.item_keywords[0] if plan.item_keywords else None) or plan.session_id
    found = trace.call(tools_report.search_sessions, viewer=ctx["viewer"],
                       see_all_private=ctx["see_all_private"],
                       product=plan.product, lot_id=plan.lot_id, q=free,
                       date_from=plan.date_from, date_to=plan.date_to, limit=20)
    sessions = found.get("sessions") or []
    if not sessions and (plan.product or plan.lot_id):
        found = trace.call(tools_report.search_sessions, viewer=ctx["viewer"],
                           see_all_private=ctx["see_all_private"],
                           q=plan.product or plan.lot_id,
                           date_from=plan.date_from, date_to=plan.date_to, limit=20)
        sessions = found.get("sessions") or []
    if not sessions:
        cond = plan.product or plan.lot_id or free or plan.normalized_question
        period = _period_text(plan)
        return f"\"{cond}\" 로 찾은 평가 세션이 없습니다{period}.", {}
    return _render_sessions(found, ctx, plan)


def _render_sessions(found, ctx, plan=None):
    """세션 목록 렌더 — `_session_find` 와 근거 재분류(`_item_history`)가 공유한다."""
    sessions = found.get("sessions") or []
    lines = [f"세션 {found.get('total', len(sessions))}건 중 {len(sessions[:10])}건"
             f"{_period_text(plan)}:"]
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


def _period_text(plan):
    if plan is None or plan.date_from is None:
        return ""
    if plan.date_to:
        return f" ({_date(plan.date_from)} ~ {_date(plan.date_to)})"
    return f" ({_date(plan.date_from)} 이후)"


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
        # "링크 알려줘" 처럼 주소 자체를 원하면 눌러야 보이는 버튼만으로는 부족하다 —
        # 복사해 쓸 수 있게 URL 을 본문에도 적는다.
        url = _view_url(sid, tab="map", item=exact)
        return (f"Map Analysis 링크입니다:\n  {url}\n(아래 버튼으로 바로 열 수도 있습니다.)",
                {"session_id": sid, "item": exact, "url": url})
    if not exact:
        return ("어떤 항목의 상세를 열지 알려주세요. 예: \"VDD_INT 상세 보여줘\"",
                {"session_id": sid})
    _link(ctx, sid, f"Item Detail 열기 — {exact}", tab="item_detail", item=exact)
    url = _view_url(sid, tab="item_detail", item=exact)
    return (f"\"{exact}\" 상세 링크입니다:\n  {url}\n(아래 버튼으로 바로 열 수도 있습니다.)",
            {"session_id": sid, "item": exact, "url": url})


def _view_url(session_id, *, tab=None, item=None):
    """복사해 쓸 수 있는 절대 경로. `_link` 의 action/url 판정과 달리 **항상 url** 이다."""
    url = _VIEW_URL.format(sid=session_id)
    if tab:
        url += f"?tab={_q(tab)}"
        if item:
            url += f"&item={_q(item)}"
    return url


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


def _item_search(plan, ctx):
    """"이 제품군에 무슨 항목이 있나" — eval.db 의 item 목록.

    ⚠ eval.db 에는 **이슈 코멘트가 달린 item 만** 쌓인다(적재 경로가 코멘트 export 뿐).
    그래서 "전체 측정항목"이 아니라는 범위를 답변에 반드시 밝힌다 — 지어내지 않는다 원칙.
    """
    scope = " / ".join(x for x in (plan.product_type, plan.family_product) if x) or "전체"
    res = ctx["trace"].call(tools_eval.search_item_candidates, item_keyword="",
                            product_type=plan.product_type,
                            family_product=plan.family_product, limit=30)
    items = res.get("items") or []
    if not items:
        if not res.get("db_available"):
            # eval.db 가 없고 세션이 열려 있으면 그 세션의 측정항목으로 답한다(범위가 다르다).
            if plan.session_id:
                return _item_search_from_session(plan, ctx, scope)
            return (f"[eval DB] 없음 — {res.get('db_path')} (항목 목록을 낼 데이터가 없습니다)",
                    {"items": res})
        return f"{scope} 에서 이슈 이력이 있는 항목을 찾지 못했습니다.", {"items": res}

    lines = [f"{scope} — 이슈 코멘트가 남은 항목 {len(items)}개 (사례 많은 순):"]
    for it in items:
        products = ", ".join(it.get("products_sample") or []) or "-"
        lines.append(f"  - {it['item_canonical']} ({it.get('value_type') or '?'}) "
                     f"— case {it.get('cases', 0)}건 / 제품 {products}")
    lines.append("  ※ 이슈 코멘트가 달린 항목 기준입니다 — 측정만 되고 이슈가 없던 항목은 "
                 "여기 없습니다.")
    for it in items[:8]:
        _choice(ctx, it["item_canonical"], f"{it['item_canonical']} 항목 이력 알려줘")
    return "\n".join(lines), {"items": res}


def _item_search_from_session(plan, ctx, scope):
    """eval.db 가 없을 때의 폴백 — 열려 있는 세션의 측정항목(CPK subject)."""
    listing = ctx["trace"].call(tools_metrics.list_items, session_id=plan.session_id,
                                viewer=ctx["viewer"],
                                see_all_private=ctx["see_all_private"], limit=50)
    guard = _metrics_guard(listing, ctx, plan.session_id)
    if guard is not None:
        return guard, {"items": listing}
    names = listing.get("items") or []
    if not names:
        return "이 세션에서 측정 항목을 찾지 못했습니다.", {"items": listing}
    lines = [f"eval DB 가 없어 **열려 있는 세션 기준**으로 답합니다 — 측정 항목 {len(names)}개:"]
    lines += [f"  - {n}" for n in names]
    return "\n".join(lines), {"items": listing}


def _session_meta(plan, ctx):
    """세션 자체의 메타 — 누가·언제 올렸나, 온도·공정·설비·패키지 등."""
    sid, pending = _resolve_session(plan, ctx, "세션 정보 알려줘")
    if pending is not None:
        return pending, {"sessions": ctx.get("_candidates") or []}
    res = ctx["trace"].call(tools_report.get_session_detail, session_id=sid,
                            viewer=ctx["viewer"], see_all_private=ctx["see_all_private"])
    if res.get("error") == "session_not_found":
        return "해당 세션을 찾을 수 없습니다(또는 조회 권한이 없습니다).", {"detail": res}
    if res.get("error"):
        return "삭제되었거나 아직 처리 중인 세션입니다.", {"detail": res}

    fields = res.get("fields") or []
    by_key = {f["key"]: f for f in fields}
    lines = []
    # 질문이 특정 필드를 짚었으면 그 한 줄을 먼저 — "온도 몇 도야?" 에 표부터 들이밀지 않는다.
    asked = [k for k, words in _META_FIELD_HINTS
             if any(w in plan.normalized_question for w in words) and k in by_key]
    for key in asked:
        lines.append(f"{by_key[key]['label']}: {_meta_value(by_key[key])}")
    if asked:
        lines.append("")
    lines.append("세션 정보:")
    for f in fields:
        lines.append(f"  - {f['label']}: {_meta_value(f)}")
    _link(ctx, sid, "보고서 열기")
    _choice(ctx, "이슈 보기", f"세션 {sid} 의 이슈 알려줘")
    _choice(ctx, "수율 보기", f"세션 {sid} 의 수율 알려줘")
    return "\n".join(lines), {"detail": res}


# 질문 어휘 → 세션 메타 필드. 짚은 필드를 답변 맨 앞에 올리는 용도일 뿐이라 빗나가도 무해하다.
_META_FIELD_HINTS = (
    ("uploaded_by", ("누가", "올렸", "업로더", "작성자")),
    ("created_at", ("언제", "날짜", "시각")),
    ("temperature", ("온도", "몇 도", "몇도")),
    ("process", ("공정",)),
    ("equip", ("설비", "장비")),
    ("pkg_type", ("패키지",)),
    ("gross_die", ("gross die", "die 수")),
    ("revision", ("리비전", "revision")),
    ("step", ("step",)),
    ("file_name", ("파일명",)),
)


def _meta_value(field):
    if field["key"] == "created_at":
        return _date(field["value"])
    return field["value"]


def _help(plan, ctx):
    """인사·도움말 — 무엇을 물을 수 있는지 보여주고 예시를 클릭 가능하게 준다."""
    for label, question in _EXAMPLES:
        _choice(ctx, label, question)
    return ("\n".join([
        "web_report 세션과 과거 평가 이력을 자연어로 찾아 드립니다. 이런 걸 물어보세요:",
        "  · 보고서 찾기      \"S3222 보고서 찾아줘\" · \"어제 올라온 세션 목록\"",
        "  · 세션 정보        \"이 세션 누가 올렸어?\" · \"온도 몇 도야?\"",
        "  · 수치             \"이 세션 수율 어때?\" · \"cpk 안 좋은 항목\" · \"VDD_INT 측정값\"",
        "  · 이슈·코멘트      \"S3222 이슈 어떻게 close 됐어?\" · \"고온에서 문제된 적 있어?\"",
        "  · 항목 이력        \"PMIC SOC 에 무슨 Item 있어\" · \"SGM 항목 이력\"",
        "  · 집계             \"PMIC 에 MAJOR 몇 건?\" · \"제품별 건수\"",
        "  · 화면 이동        \"맵 열어줘\" · \"VDD_INT 상세 보여줘\"",
        "",
        "아직 못 하는 것: 세션 A 와 B 를 직접 비교하는 것(각각 물어보셔야 합니다), "
        "데이터 수정·삭제(조회 전용입니다).",
    ]), {})


_FEATURE_STATUS = {
    "available": "사용 가능",
    "conditional": "조건부 사용 가능",
    "coming_soon": "준비 중",
}


def _feature_help(plan, ctx):
    """공개 기능 카탈로그만 조회해 제공 상태·사용법·도움말 링크를 답한다."""
    result = ctx["trace"].call(
        tools_help.search_help_features, query=ctx["question"], limit=8)
    features = result.get("features") or []
    if not features:
        ctx["web"]["links"].append(
            {"label": "HONEY 전체 도움말", "url": "/pe/report/help"})
        return (
            "공개 기능 카탈로그에서 확인되지 않습니다. 기능명이 다르거나 아직 공개되지 "
            "않았을 수 있습니다. 전체 도움말에서 현재 제공 기능을 확인해 주세요.", result)

    if result.get("generic"):
        lines = ["HONEY에서 제공하는 대표 기능입니다:"]
        for feature in features:
            lines.append(
                f"  · {feature['title']} — {_FEATURE_STATUS[feature['status']]} · "
                f"{feature['summary']}")
        lines.append("\n기능명을 넣어 ‘Temperature 모드 있어?’, ‘DUT 제외 어떻게 해?’처럼 물어보세요.")
        ctx["web"]["links"].append(
            {"label": "HONEY 전체 도움말", "url": "/pe/report/help"})
        return "\n".join(lines), result

    feature = features[0]
    status = _FEATURE_STATUS[feature["status"]]
    lines = [f"{feature['title']}: {status}", feature["summary"],
             f"사용 조건: {feature['availability']}"]
    if feature["usage"]:
        lines.append("사용 방법:")
        lines.extend(f"  {idx}. {step}" for idx, step in enumerate(feature["usage"], 1))
    if feature["cautions"]:
        lines.append("주의: " + " ".join(feature["cautions"]))
    url = f"/pe/report/help#{feature['help_anchor']}"
    ctx["web"]["links"].append({"label": f"도움말: {feature['title']}", "url": url})
    return "\n".join(lines), {"features": [feature],
                               "catalog_version": result.get("catalog_version")}


# `help` 와 광역 폴백이 공유하는 예시 — 한 곳에서만 고친다.
_EXAMPLES = (
    ("보고서 찾기", "S3222 보고서 찾아줘"),
    ("최근 세션", "최근에 올라온 보고서 보여줘"),
    ("항목 목록", "PMIC SOC 에 무슨 Item 있어"),
    ("항목 이력", "SGM 항목 이력 알려줘"),
    ("집계", "제품별 건수 알려줘"),
    ("코멘트 검색", "고온에서 문제된 적 있어?"),
)

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
    """분류 실패 — **"모르겠다"로 끝내지 않는다.**

    질문에서 건진 토큰을 세션·item·코멘트 세 축에 던져 "무엇으로 걸리는지"를 보여준다.
    의도를 못 맞혀도 사용자는 다음 클릭을 할 수 있다. 토큰도 기간도 없으면 그때만 안내문.
    """
    token = (plan.item_keywords[0] if plan.item_keywords else None) or plan.product \
        or plan.lot_id
    if not token and plan.date_from is None:
        return _help(plan, ctx)

    trace = ctx["trace"]
    data, found = {}, []

    sess = trace.call(tools_report.search_sessions, q=token, viewer=ctx["viewer"],
                      see_all_private=ctx["see_all_private"],
                      date_from=plan.date_from, date_to=plan.date_to, limit=5)
    # `sessions` 키에는 **행 리스트**를 담는다 — _derive_links 가 그 규약으로 읽는다
    # (봉투 dict 를 그대로 넣으면 키 문자열을 순회하게 된다).
    data["sessions"] = sess.get("sessions") or []
    n_sess = len(data["sessions"])
    if n_sess:
        found.append(f"세션 {sess.get('total', n_sess)}건")

    n_item = n_comment = 0
    if token:
        cand = trace.call(tools_eval.search_item_candidates, item_keyword=token, limit=5)
        data["candidates"] = cand
        n_item = len(cand.get("items") or [])
        if n_item:
            found.append(f"항목 {n_item}개")
        com = trace.call(tools_eval.search_comments, keyword=token, limit=5)
        data["comments"] = com
        n_comment = len(com.get("comments") or [])
        if n_comment:
            found.append(f"코멘트 {n_comment}건")

    label = f"\"{token}\"" if token else _period_text(plan).strip(" ()")
    if not found:
        lines = [f"{label} 로는 세션·항목·코멘트 어디에서도 찾지 못했습니다.",
                 "다르게 물어보시려면 아래를 눌러 보세요."]
        for text, question in _EXAMPLES:
            _choice(ctx, text, question)
        return "\n".join(lines), data

    lines = [f"{label} 로 찾은 것: {' / '.join(found)}"]
    for s in data["sessions"][:5]:
        lines.append(f"  - {_session_line(s)}")
        _link(ctx, s["session_id"],
              f"보고서 열기: {s.get('product') or '?'} / lot {s.get('lot_id') or '-'}")
    for it in (data.get("candidates", {}).get("items") or [])[:5]:
        lines.append(f"  - [항목] {it['item_canonical']} — case {it.get('cases', 0)}건")
    # 걸린 축만 후속 버튼으로 — 없는 축을 눌러 빈 결과를 보게 하지 않는다.
    if n_sess:
        _choice(ctx, "세션 더 보기", f"{token} 보고서 찾아줘")
    if n_item:
        _choice(ctx, "항목 이력 보기", f"{token} 항목 이력 알려줘")
    if n_comment:
        _choice(ctx, "코멘트 보기", f"{token} 코멘트 찾아줘")
    return "\n".join(lines), data


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
