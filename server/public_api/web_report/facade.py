"""web_report 조회 facade — Flask 무의존. REST·MCP·(후속)챗봇이 공유하는 함수층.

규약(전 함수 공통):
  · `viewer` 는 **키워드 필수 인자**다. `database/sessions.py:_history_where` 는
    `viewer=None` 이면 비공개 필터를 통째로 생략한다 — 기본값을 두지 않아 호출부가
    반드시 신원을 결정하게 만든다(`""` = 신원 없음 = 공개 세션만).
  · 예외를 던지지 않고 **분기 키가 든 dict** 를 돌려준다(`server/chatbot/tools_metrics.py`
    와 같은 규약). 라우트는 그 키를 HTTP 상태로만 옮긴다:
        {"ok": True, "data": ..., "meta": ...}
        {"error": "session_not_found"}        권한 없음도 같은 응답(존재 은닉)
        {"error": "not_web_report"}           수치 payload 자체가 없는 xlsx 세션
        {"error": "bad_request", "message"}
        {"building": True, "blocked": bool}   콜드 빌드 중
  · 콜드 빌드를 **동기 대기하지 않는다**(`build_if_cold=False`). waitress 스레드가
    13개뿐이라 외부 폴러 하나가 서버를 굶길 수 있다. 콜드면 백그라운드 빌드만 예약한다.
  · `load_webreport` 가 돌려주는 report 는 **캐시 공유 객체**다. 행을 고르거나 정렬하기
    전에 반드시 `dict(row)` 로 복사한다 — 안 하면 캐시가 오염돼 웹 화면까지 망가진다.

새 계산을 여기서 만들지 않는다(CLAUDE.md 규칙 13). 값은 전부 payload/service 에서
가져다 슬라이스만 한다 — 그래야 API 와 화면이 같은 숫자를 보인다.
"""
from __future__ import annotations

from pathlib import Path

from database import report_db

# 접근제어·세션 검색은 챗봇 툴의 검증된 구현을 그대로 위임한다(public_api README
# "향후 확장" 절이 지정한 정본 경로). 사본을 만들면 비공개 판정이 두 벌로 갈라진다.
from chatbot import tools_report
from web_report.comment_format import strip_format

# Issue Table 컬럼 이름 정본 — 저장 키이자 화면 헤더라 여기서 재정의하지 않는다.
from web_report.tabs.issue_table import (AI_COMMENT_COL, COMMENT_COLS, SIGNATURE_COL)

# 페이지 상한. 외부 폴러가 응답을 통째로 끌어가지 못하게 서버가 잘라 준다.
_LIMITS = {"sessions": 100, "yield": 2000, "fail_bins": 200, "cpk": 200,
           "items": 500, "issue": 2000, "temperature": 2000, "raw": 2000,
           "compare": 1000, "compare_sessions": 5}


def _upload_root() -> Path:
    import config
    return Path(config.REPORT_UPLOAD_DIR)


def _clamp(value, default, maximum, minimum=1):
    """쿼리 파라미터 → 정수(범위 강제). 잘못된 값은 에러가 아니라 기본값이다."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(n, maximum))


def _session_ref(session):
    """응답에 붙일 세션 근거 — 어느 평가의 값인지 외부에서 식별할 최소 정보."""
    return {"session_id": session.get("session_id"),
            "product": session.get("product"),
            "product_type": session.get("product_type"),
            "family_product": session.get("family_product"),
            "lot_id": session.get("lot_id"),
            "mode": session.get("mode"),
            "created_at": session.get("created_at")}


def _meta(session, **extra):
    """캐시 일관성 판단 근거. 외부가 '이 값이 언제 것인지' 를 알 수 있어야 한다."""
    meta = {"session_id": session.get("session_id"),
            "content_hash": session.get("content_hash"),
            "analysis_key": session.get("analysis_key")}
    try:
        meta["edits_rev"] = report_db.get_webreport_edit_rev(session.get("session_id"))
    except Exception:                                    # noqa: BLE001 - 계측용, 실패해도 본문은 유효
        meta["edits_rev"] = None
    meta.update(extra)
    return meta


def _ok(session, data, **meta_extra):
    return {"ok": True, "data": data, "meta": _meta(session, **meta_extra)}


def _bad(message):
    return {"error": "bad_request", "message": str(message)}


# ── 세션 로드 ────────────────────────────────────────────────────────────────
def _get_session(session_id, *, viewer, see_all_private):
    """(session, err). 권한 없음과 미존재는 같은 응답으로 합친다(존재를 흘리지 않는다)."""
    session = report_db.get_session(session_id)
    if not session or not tools_report._can_view(session, viewer, see_all_private):
        return None, {"error": "session_not_found", "session_id": session_id}
    if str(session.get("source") or "") != "web_report":
        return session, {"error": "not_web_report", "session_id": session_id,
                         "source": session.get("source")}
    return session, None


def _load_report(session_id, *, viewer, see_all_private, request_build=True):
    """(session, report, err) — tools_metrics._load 와 같은 콜드 정책."""
    session, err = _get_session(session_id, viewer=viewer, see_all_private=see_all_private)
    if err:
        return session, None, err

    from web_report import build_status, compute, service
    try:
        _, report = service.load_webreport(session_id, report_db=report_db,
                                           upload_root=_upload_root(), session=session,
                                           build_if_cold=False)
    except service.ColdBuildRequired:
        blocked = build_status.failure_blocked(session_id, "report")
        if not blocked and request_build:
            compute.request_build(session_id, str(_upload_root()), "report")
        return session, None, {"building": True, "blocked": bool(blocked),
                               "session_id": session_id}
    except (FileNotFoundError, KeyError):
        return session, None, {"error": "session_not_found", "session_id": session_id}
    return session, report, None


# ── 1. 세션 검색 ─────────────────────────────────────────────────────────────
def list_sessions(*, viewer, see_all_private=False, product=None, product_type=None,
                  lot_id=None, q=None, date_from=None, date_to=None,
                  limit=20, offset=0, sort="new"):
    """평가 세션 검색. 비공개 필터·soft delete 제외는 위임 함수가 이미 처리한다."""
    limit = _clamp(limit, 20, _LIMITS["sessions"])
    offset = _clamp(offset, 0, 1_000_000, minimum=0)
    res = tools_report.search_sessions(
        viewer=viewer, see_all_private=see_all_private, product=product,
        product_type=product_type, lot_id=lot_id, q=q, source="web_report",
        date_from=date_from, date_to=date_to, limit=limit, offset=offset, sort=sort)
    return {"ok": True, "data": res,
            "meta": {"limit": limit, "offset": offset, "total": res.get("total")}}


# ── 2. 개요 ──────────────────────────────────────────────────────────────────
def get_overview(session_id, *, viewer, see_all_private=False):
    """세션 개요 — 모드·source·수율 요약(+STEP)·분모 기준·ENGR 결론."""
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    pending = [k for k in ("ai_comment_pending", "compare_pending") if report.get(k)]
    return _ok(session, {
        "session": _session_ref(session),
        "mode": report.get("mode"),
        "sources": list(report.get("sources") or []),
        "yield_summary": report.get("yield_summary") or {},
        "yield_basis": report.get("yield_basis") or {},
        "summary_engr": dict(report.get("summary_engr") or {}),
        "selected_items": list(report.get("selected_items") or []),
        "has_temperature": bool(report.get("temperature")),
        "has_compare": bool(report.get("compare")),
        "pending": pending,
    })


def get_build_status(session_id, *, viewer, see_all_private=False):
    """리포트 계산 상태. 202 를 받은 호출자가 폴링하는 자리 — 빌드를 예약하지 않는다."""
    session, err = _get_session(session_id, viewer=viewer, see_all_private=see_all_private)
    if err:
        return err
    from web_report import build_status, service
    snap = dict(build_status.snapshot(session_id) or {})
    snap["blocked"] = bool(build_status.failure_blocked(session_id, "report"))
    snap["cold"] = bool(service.report_is_cold(session_id, report_db=report_db,
                                               upload_root=_upload_root(), session=session))
    if not snap.get("state"):
        snap["state"] = "building" if snap.get("stage") else "idle"
    return _ok(session, snap)


# ── 3. Yield / Fail Bin ──────────────────────────────────────────────────────
def get_yield(session_id, *, viewer, see_all_private=False, limit=200):
    """Yield 표 + STEP(P1/P2/P3) 분해. Issue Table 의 Yield 섹션과 같은 목록이다."""
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    limit = _clamp(limit, 200, _LIMITS["yield"])
    rows = (report.get("sheets") or {}).get("Yield") or []
    return _ok(session, {
        "session": _session_ref(session),
        "yield_summary": report.get("yield_summary") or {},
        "yield_basis": report.get("yield_basis") or {},
        "rows": [dict(r) for r in rows[:limit]],
        "step_groups": report.get("yield_step_groups") or [],
        "bin_groups": report.get("yield_bin_groups") or [],
        "truncated": len(rows) > limit,
    }, total=len(rows), returned=min(len(rows), limit))


def get_fail_bins(session_id, *, viewer, see_all_private=False, limit=20):
    """Fail Bin 랭킹 — 어떤 bin 이 가장 많이 떨어졌나."""
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    limit = _clamp(limit, 20, _LIMITS["fail_bins"])
    rows = (report.get("sheets") or {}).get("Fail Bin") or []
    return _ok(session, {
        "session": _session_ref(session),
        "fail_bins": [dict(r) for r in rows[:limit]],
        "bin_summary": report.get("issue_bin_summary") or {},
    }, total=len(rows), returned=min(len(rows), limit))


# ── 4. CPK ───────────────────────────────────────────────────────────────────
def get_cpk(session_id, *, viewer, see_all_private=False, item=None, source=None,
            worst_n=50, offset=0):
    """CPK 전표. item 미지정이면 나쁜 순 — CPK 탭이 쓰는 행을 그대로 돌려준다."""
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    worst_n = _clamp(worst_n, 50, _LIMITS["cpk"])
    offset = _clamp(offset, 0, 1_000_000, minimum=0)

    rows = (report.get("sheets") or {}).get("CPK") or []
    keyword = str(item or "").strip().lower()
    src = str(source or "").strip()
    picked = [dict(r) for r in rows
              if (not keyword or keyword in str(r.get("subject") or "").lower())
              and (not src or str(r.get("source") or "") == src)]
    if not keyword:
        # 필터가 없으면 "나쁜 순" 이 기본 — cpk 없는 행(측정 불가)은 뒤로 뺀다.
        picked = [r for r in picked if r.get("cpk") is not None]
        picked.sort(key=lambda r: r["cpk"])

    worst = None
    for r in rows:
        if r.get("cpk") is None:
            continue
        if worst is None or r["cpk"] < worst["cpk"]:
            worst = {"subject": r.get("subject"), "source": r.get("source"), "cpk": r["cpk"]}

    page = picked[offset:offset + worst_n]
    return _ok(session, {
        "session": _session_ref(session),
        "cpk_rows": page,
        "cpk_worst": worst,
    }, total=len(picked), returned=len(page), offset=offset)


# ── 5. Issue Table ───────────────────────────────────────────────────────────
_ISSUE_SHEETS = {"main": "Issue Table", "temp": "Issue Table Temp",
                 "compare": "Issue Table Compare"}


def _is_section_row(row):
    """섹션 머리행·빈 자리행인가 — 값이 아니라 화면 구분선이라 결과에서 뺀다.

    CPK subhead 는 `Item="item name", avg="cpk"` 조합이 서명이다(issue_table.py 생성부).
    Item 만 보고 자르면 실제 항목명이 우연히 겹칠 때 데이터를 잃는다.
    """
    item = str(row.get("Item") or "").strip()
    if not item:
        return True                                   # ETC 헤더 / cpk 없을 때의 빈 행
    return item == "item name" and str(row.get("avg") or "") == "cpk"


def _issue_row_key(category, row):
    """렌더된 행 → 저장 키(row_key). issue_table.py 생성 규칙의 읽기 방향이다.

    Category 셀은 섹션 첫 행에만 채워지므로(시각적 병합) 호출부가 직전 값을 이어 준다.
    """
    item = str(row.get("Item") or "")
    if not item:
        return ""
    if category == "Yield":
        bin_value = row.get("Bin")
        return f"Yield|{bin_value}|{item}" if bin_value not in (None, "") else ""
    if category in ("CPK", "TEMP", "ETC"):
        return f"{category}|{item}"
    return ""


def get_issue_table(session_id, *, viewer, see_all_private=False, table="main",
                    item=None, limit=None):
    """Issue Table 계산본 — 편집이 없는 자동 생성 행까지 전부.

    화면과 같은 목록이라 사용자가 숨긴 행은 여기에도 없다(빌드 시점에 빠진다).
    comment 는 화면 전용 서식 토큰을 벗겨 평문으로 준다.
    """
    key = str(table or "main").strip().lower()
    if key not in _ISSUE_SHEETS:
        return _bad(f"unknown table: {table} (main|temp|compare)")
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    limit = _clamp(limit, _LIMITS["issue"], _LIMITS["issue"])

    sheet = (report.get("sheets") or {}).get(_ISSUE_SHEETS[key]) or []
    keyword = str(item or "").strip().lower()
    comment_cols = list(COMMENT_COLS)

    out, category = [], ""
    for raw in sheet:
        row = dict(raw)
        if str(row.get("Category") or ""):
            category = str(row["Category"])
        if _is_section_row(row):
            continue                                   # 섹션 머리행 — 데이터가 아니다
        row_key = _issue_row_key(category, row)
        if keyword and keyword not in str(row.get("Item") or "").lower() \
                and keyword not in row_key.lower():
            continue
        for col in comment_cols:
            if col in row:
                row[col] = strip_format(row[col])
        row["row_key"] = row_key
        row["category"] = category
        out.append(row)

    page = out[:limit]
    return _ok(session, {
        "session": _session_ref(session),
        "table": key,
        "rows": page,
        "columns": {"comment": comment_cols, "ai_comment": AI_COMMENT_COL,
                    "signature": SIGNATURE_COL},
    }, total=len(out), returned=len(page))


# ── 6. 항목 카탈로그 / 통계 ──────────────────────────────────────────────────
def list_items(session_id, *, viewer, see_all_private=False, keyword=None,
               limit=100, offset=0):
    """측정 항목 카탈로그 — distribution_index(표본수·limit·cpk)를 그대로 쓴다."""
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    limit = _clamp(limit, 100, _LIMITS["items"])
    offset = _clamp(offset, 0, 1_000_000, minimum=0)

    index = report.get("distribution_index") or []
    text = str(keyword or "").strip().lower()
    items = [dict(r) for r in index
             if not text or text in str(r.get("subject") or r.get("name") or "").lower()]
    page = items[offset:offset + limit]
    return _ok(session, {"session": _session_ref(session), "items": page},
               total=len(items), returned=len(page), offset=offset)


def get_item_stats(session_id, subject, *, viewer, see_all_private=False):
    """항목 1개의 source 별 기초 통계 + cpk + limit. 측정값 배열은 싣지 않는다.

    값 전량이 필요하면 대용량 경로(`/items/<subject>/values`)를 쓴다 — 이 함수는
    응답 크기가 항상 작아 폴링·요약에 안전하다.
    """
    text = str(subject or "").strip()
    if not text:
        return _bad("subject is required")
    session, err = _get_session(session_id, viewer=viewer, see_all_private=see_all_private)
    if err:
        return err

    from web_report import build_status, compute, service
    try:
        detail = service.scatter_item(session_id, text, report_db=report_db,
                                      upload_root=_upload_root(), session=session)
    except KeyError:
        return {"error": "item_not_found", "session_id": session_id, "subject": text}
    except service.ColdBuildRequired:
        blocked = build_status.failure_blocked(session_id, "report")
        if not blocked:
            compute.request_build(session_id, str(_upload_root()), "report")
        return {"building": True, "blocked": bool(blocked), "session_id": session_id}
    except FileNotFoundError:
        return {"error": "session_not_found", "session_id": session_id}

    sources = []
    for src in detail.get("sources") or []:
        values = src.get("values") or []
        sources.append({"source": src.get("name"), "count": len(values),
                        "min": min(values) if values else None,
                        "max": max(values) if values else None})
    return _ok(session, {
        "session": _session_ref(session),
        "subject": detail.get("subject") or text,
        "units": detail.get("units"),
        "lower_limit": detail.get("lower_limit"),
        "upper_limit": detail.get("upper_limit"),
        "cpk": detail.get("cpk"),
        "status": detail.get("status"),
        "fail_total": detail.get("fail_total"),
        "is_fail": detail.get("is_fail"),
        "stats": detail.get("stats") or [],
        "sources": sources,
    })


# ── 7. Compare / 세션 간 비교 ────────────────────────────────────────────────
_COMPARE_SECTIONS = ("summary", "dist_shift", "equivalence", "bin_delta",
                     "bin_matrix", "goodlog", "new_items")


def get_compare(session_id, *, viewer, see_all_private=False, section="summary",
                limit=100):
    """Compare 모드 세션의 source 간 비교 결과."""
    name = str(section or "summary").strip().lower()
    if name not in _COMPARE_SECTIONS:
        return _bad(f"unknown section: {section} ({'|'.join(_COMPARE_SECTIONS)})")
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    if report.get("compare_pending"):
        # 계산이 분리 캐시라 payload 는 있어도 compare 만 아직 없을 수 있다.
        return {"building": True, "blocked": False, "kind": "compare",
                "session_id": session_id}
    compare = report.get("compare")
    if not compare:
        return _bad("session is not a Compare report")

    limit = _clamp(limit, 100, _LIMITS["compare"])
    if name == "summary":
        # 섹션별 건수만 — 어디에 볼 게 있는지 먼저 알려 준다.
        data = {k: (len(v) if isinstance(v, (list, dict)) else v)
                for k, v in compare.items() if k not in ("sources", "groups")}
    else:
        value = compare.get(name)
        data = [dict(r) if isinstance(r, dict) else r for r in value[:limit]] \
            if isinstance(value, list) else value
    return _ok(session, {
        "session": _session_ref(session),
        "section": name,
        "data": data,
        "sources": compare.get("sources"),
        "groups": compare.get("groups"),
        "before_sources": compare.get("before_sources"),
        "after_sources": compare.get("after_sources"),
    })


def compare_sessions(*, viewer, see_all_private=False, sids=None, items=None):
    """세션 여러 건의 수율·CPK 를 나란히 — lot 간 추이 비교.

    새 계산은 하지 않는다. 각 세션의 기존 payload 값을 나열만 한다(규칙 13).
    콜드 세션은 값 대신 building 목록에 담아 알린다 — 여기서 기다리면 응답이 수십 초가 된다.
    """
    ids = [s.strip() for s in str(sids or "").split(",") if s.strip()]
    if not ids:
        return _bad("sids is required (comma separated session_id)")
    if len(ids) > _LIMITS["compare_sessions"]:
        return _bad(f"too many sids (max {_LIMITS['compare_sessions']})")
    wanted = [s.strip() for s in str(items or "").split(",") if s.strip()]

    out, missing, building = [], [], []
    for sid in ids:
        session, report, err = _load_report(sid, viewer=viewer,
                                            see_all_private=see_all_private)
        if err and err.get("building"):
            building.append(sid)
            continue
        if err:
            missing.append(sid)
            continue
        rows = (report.get("sheets") or {}).get("CPK") or []
        worst = None
        for r in rows:
            if r.get("cpk") is None:
                continue
            if worst is None or r["cpk"] < worst["cpk"]:
                worst = {"subject": r.get("subject"), "source": r.get("source"),
                         "cpk": r["cpk"]}
        picked = {}
        for name in wanted:
            low = name.lower()
            hit = [dict(r) for r in rows if low in str(r.get("subject") or "").lower()]
            if hit:
                picked[name] = hit
        out.append({"session": _session_ref(session),
                    "yield_summary": report.get("yield_summary") or {},
                    "cpk_worst": worst,
                    "items": picked})
    return {"ok": True,
            "data": {"sessions": out, "missing": missing, "building": building},
            "meta": {"requested": len(ids), "returned": len(out)}}


# ── 8. Temperature / Map / 입력정보 ──────────────────────────────────────────
def get_temperature(session_id, *, viewer, see_all_private=False, limit=500):
    """Temperature 세션의 RT/CT/HT 구성 + 온도별 재판정 이슈 행."""
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    limit = _clamp(limit, 500, _LIMITS["temperature"])
    rows = (report.get("sheets") or {}).get("Issue Table Temp") or []
    return _ok(session, {
        "session": _session_ref(session),
        "groups": report.get("temperature"),
        "rows": [dict(r) for r in rows[:limit]],
    }, total=len(rows), returned=min(len(rows), limit))


def get_map_summary(session_id, *, viewer, see_all_private=False):
    """Map Analysis 경량 메타(die 좌표 제외). 좌표 전량은 대용량 경로."""
    session, report, err = _load_report(session_id, viewer=viewer,
                                        see_all_private=see_all_private)
    if err:
        return err
    maps = (report.get("sheets") or {}).get("Map Analysis") or []
    return _ok(session, {"session": _session_ref(session),
                         "maps": [dict(m) for m in maps]}, total=len(maps))


def get_input_info(session_id, *, viewer, see_all_private=False):
    """source 별 입력 파일·STDF 메타. manifest 만 읽어 콜드 빌드와 무관하다."""
    session, err = _get_session(session_id, viewer=viewer, see_all_private=see_all_private)
    if err:
        return err
    from web_report import service
    try:
        info = service.input_info(session_id, report_db=report_db,
                                  upload_root=_upload_root())
    except (FileNotFoundError, KeyError):
        return {"error": "session_not_found", "session_id": session_id}
    data = dict(info)
    data["session"] = _session_ref(session)
    return _ok(session, data)


# ── 9. Raw Data ──────────────────────────────────────────────────────────────
def get_raw_data_columns(session_id, *, viewer, see_all_private=False):
    """Raw Data 컬럼 메타 + source 목록 + 전체 die 수."""
    session, err = _get_session(session_id, viewer=viewer, see_all_private=see_all_private)
    if err:
        return err
    from web_report import service
    try:
        cols = service.get_raw_data_columns(session_id, report_db=report_db,
                                            upload_root=_upload_root())
    except (FileNotFoundError, KeyError):
        return {"error": "session_not_found", "session_id": session_id}
    data = dict(cols)
    data["session"] = _session_ref(session)
    return _ok(session, data)


def get_raw_data(session_id, *, viewer, see_all_private=False, columns=None,
                 search="", bin_filter="", source_filter="", limit=200, offset=0):
    """Raw Data 저장값 페이지 조회.

    페이지 자르기는 여기서 한다 — `query_raw_data` 는 화면이 쓰는 함수라 손대지 않는다
    (운영 중 경로에 인자를 늘리는 것보다 소비자 쪽에서 자르는 편이 안전하다).
    """
    session, err = _get_session(session_id, viewer=viewer, see_all_private=see_all_private)
    if err:
        return err
    limit = _clamp(limit, 200, _LIMITS["raw"])
    offset = _clamp(offset, 0, 1_000_000, minimum=0)
    cols = [c.strip() for c in str(columns or "").split(",") if c.strip()]

    from web_report import service
    try:
        res = service.query_raw_data(session_id, report_db=report_db,
                                     upload_root=_upload_root(), columns=cols,
                                     search=search or "", bin_filter=bin_filter or "",
                                     source_filter=source_filter or "")
    except ValueError as exc:                    # 컬럼 상한 초과 — 호출자 잘못
        return _bad(str(exc))
    except (FileNotFoundError, KeyError):
        return {"error": "session_not_found", "session_id": session_id}

    rows = res.get("rows") or []
    page = rows[offset:offset + limit]
    next_offset = offset + len(page)
    return _ok(session, {
        "session": _session_ref(session),
        "columns": cols,
        "rows": page,
        "total_matched": res.get("total_matched"),
        "next_offset": next_offset if next_offset < len(rows) else None,
        "truncated": bool(res.get("truncated")),
    }, returned=len(page), offset=offset)
