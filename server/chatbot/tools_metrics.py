"""세션 수치 조회 툴 — 수율(yield) / CPK / 실제 측정값(raw).

tools_report 와 분리한 이유: 이 모듈만 `web_report.service`(캐시·ProcessPool 오프로드)에
의존한다. tools_report 는 report_db 만 보므로 임시 DB 만으로 단위 테스트가 되는데, 거기에
service 의존을 섞으면 그 격리가 깨진다.

**콜드 정책**: 리포트가 아직 계산되지 않은 세션에서 `build_if_cold=True` 로 부르면 요청
스레드가 수십 초 묶인다 — waitress 스레드가 13개뿐이라 챗봇 한 명이 서버를 마비시킬 수
있다. 그래서 항상 `build_if_cold=False` 로 부르고, 콜드면 백그라운드 빌드만 예약한 뒤
`{"building": True}` 를 돌려준다(라우트의 202 규약과 동일한 태도).

**권한**: tools_report 와 같은 규약 — `viewer` 는 키워드 필수 인자이고, 조회 전에
`tools_report._can_view` 를 통과해야 한다. 권한 없음과 미존재는 같은 응답
(`session_not_found`)으로 합친다(세션 존재 여부를 흘리지 않는다).
"""
from __future__ import annotations

from pathlib import Path

from database import report_db

from . import tools_report

# CPK 나쁜 항목 기본 표시 개수. 챗 답변은 스크롤 없이 읽히는 길이여야 한다.
_CPK_LIMIT = 10
# 측정값 표본 표시 개수(상위/하위 각각). 전량은 절대 반환하지 않는다 — 챗 페이로드는
# 사람이 읽는 요약이지 데이터 덤프가 아니다.
_VALUES_TOP_N = 10


def _upload_root() -> Path:
    import config
    return Path(config.REPORT_UPLOAD_DIR)


def _load(session_id, *, viewer, see_all_private):
    """(session, report, err) — err 가 있으면 그걸 그대로 반환하면 된다."""
    session = report_db.get_session(session_id)
    if not session or not tools_report._can_view(session, viewer, see_all_private):
        return None, None, {"error": "session_not_found", "session_id": session_id}
    if str(session.get("source") or "") != "web_report":
        return session, None, {"error": "not_web_report", "session_id": session_id,
                               "source": session.get("source")}

    from web_report import build_status, compute, service
    try:
        _, report = service.load_webreport(session_id, report_db=report_db,
                                           upload_root=_upload_root(), session=session,
                                           build_if_cold=False)
    except service.ColdBuildRequired:
        blocked = build_status.failure_blocked(session_id, "report")
        if blocked:
            return session, None, {"building": True, "blocked": True,
                                   "session_id": session_id}
        compute.request_build(session_id, str(_upload_root()), "report")
        return session, None, {"building": True, "blocked": False,
                               "session_id": session_id}
    except (FileNotFoundError, KeyError):
        return session, None, {"error": "session_not_found", "session_id": session_id}
    return session, report, None


def _session_ref(session):
    """답변에 붙일 세션 근거(제품/lot/평가일)."""
    return {"session_id": session.get("session_id"),
            "product": session.get("product"),
            "product_type": session.get("product_type"),
            "lot_id": session.get("lot_id"),
            "created_at": session.get("created_at")}


def get_session_metrics(session_id, *, viewer, see_all_private=False,
                        item_keyword=None, cpk_limit=_CPK_LIMIT):
    """세션의 수율 요약 + CPK 행을 읽는다.

    언제 쓰나: "이 세션 수율 어때?" / "cpk 나쁜 항목 뭐야?" 처럼 **세션이 특정된** 수치 질문.
    item_keyword 를 주면 그 문자열이 든 항목만, 없으면 CPK 나쁜 순 상위 cpk_limit 개.

    반환(예외 대신 분기 키):
      정상   {session, yield_summary, cpk_rows[], items_matched[], cpk_worst, building:False}
      미존재 {"error": "session_not_found"}   (권한 없음도 같은 응답)
      xlsx   {"error": "not_web_report"}      (수율/CPK payload 자체가 없는 세션)
      콜드   {"building": True, "blocked": bool}
    """
    session, report, err = _load(session_id, viewer=viewer, see_all_private=see_all_private)
    if err:
        return err

    rows = (report.get("sheets") or {}).get("CPK") or []
    keyword = str(item_keyword or "").strip().lower()
    # report 는 캐시 공유 객체다 — 정렬/슬라이스 전에 반드시 새 리스트로 복사한다.
    if keyword:
        picked = [dict(r) for r in rows if keyword in str(r.get("subject") or "").lower()]
    else:
        picked = [dict(r) for r in rows if r.get("cpk") is not None]
        picked.sort(key=lambda r: r["cpk"])
        picked = picked[:int(cpk_limit)]

    matched = []
    for r in picked:
        subject = str(r.get("subject") or "")
        if subject and subject not in matched:
            matched.append(subject)

    worst = None
    for r in rows:
        if r.get("cpk") is None:
            continue
        if worst is None or r["cpk"] < worst["cpk"]:
            worst = {"subject": r.get("subject"), "source": r.get("source"),
                     "cpk": r["cpk"]}

    return {"session": _session_ref(session),
            "yield_summary": report.get("yield_summary") or {},
            "cpk_rows": picked,
            "items_matched": matched,
            "cpk_worst": worst,
            "building": False}


def list_items(session_id, *, viewer, see_all_private=False, keyword=None, limit=20):
    """세션의 측정 항목 이름 목록 — 점프/측정값 질문에서 정확한 항목명을 확정할 때.

    CPK 시트의 subject 를 쓴다(측정 항목 전체가 여기 한 번씩은 등장한다).
    """
    res = get_session_metrics(session_id, viewer=viewer, see_all_private=see_all_private,
                              item_keyword=keyword, cpk_limit=10_000)
    if res.get("error") or res.get("building"):
        return res
    return {"session": res["session"], "items": res["items_matched"][:int(limit)],
            "building": False}


def get_item_values(session_id, subject, *, viewer, see_all_private=False,
                    top_n=_VALUES_TOP_N):
    """특정 항목의 실제 측정값 — 소스별 통계 + 최소/최대 쪽 표본 몇 개.

    전량(수만 점)은 돌려주지 않는다. 값 전체를 보려면 Item Detail 화면으로 보내는 게 맞다
    (챗 답변에 그 링크가 함께 붙는다).
    """
    session = report_db.get_session(session_id)
    if not session or not tools_report._can_view(session, viewer, see_all_private):
        return {"error": "session_not_found", "session_id": session_id}
    if str(session.get("source") or "") != "web_report":
        return {"error": "not_web_report", "session_id": session_id}

    from web_report import build_status, compute, service
    try:
        detail = service.scatter_item(session_id, subject, report_db=report_db,
                                      upload_root=_upload_root(), session=session)
    except KeyError:
        return {"error": "item_not_found", "session_id": session_id, "subject": subject}
    except service.ColdBuildRequired:
        blocked = build_status.failure_blocked(session_id, "report")
        if not blocked:
            compute.request_build(session_id, str(_upload_root()), "report")
        return {"building": True, "blocked": bool(blocked), "session_id": session_id}
    except (FileNotFoundError,):
        return {"error": "session_not_found", "session_id": session_id}

    sources = []
    for src in detail.get("sources") or []:
        values = src.get("values") or []
        ordered = sorted(values)
        sources.append({
            "source": src.get("name"),
            "count": len(values),
            "min_values": ordered[:int(top_n)],
            "max_values": ordered[-int(top_n):][::-1] if ordered else [],
        })
    return {"session": _session_ref(session),
            "subject": detail.get("subject"),
            "units": detail.get("units"),
            "lower_limit": detail.get("lower_limit"),
            "upper_limit": detail.get("upper_limit"),
            "cpk": detail.get("cpk"),
            "status": detail.get("status"),
            "fail_total": detail.get("fail_total"),
            "stats": detail.get("stats") or [],
            "sources": sources,
            "building": False}
