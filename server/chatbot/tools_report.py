"""report.db 기반 조회 툴 — 세션 / 제품 / Issue Table.

전부 read-only 다. 새 SQL 을 거의 쓰지 않고 기존 조회 함수를 위임한다
(`database/sessions.py` 의 `get_history_page` 는 비공개 필터·soft delete 제외·
정렬 규칙이 이미 들어 있는 검증된 경로다).

**권한**: `viewer` 는 키워드 **필수** 인자다. `database/sessions.py:_history_where` 는
`viewer=None` 이면 비공개 필터를 아예 붙이지 않아 비공개 세션이 전부 노출된다 —
기본값을 두지 않아 호출부가 반드시 신원을 결정하게 만든다.
    viewer=""      신원 없음(일반 브라우저) → 공개 세션만
    viewer="<uid>" 공개 + legacy(업로더 기록 없음) + 본인 업로드 + 위임 편집자
    see_all_private=True  master PC (비공개 숨김 미적용)
"""
from __future__ import annotations

from pathlib import Path

from database import report_db
from database.core import get_conn

from . import rowkey

# tabs/issue_table.py 의 편집 가능 컬럼. 이 두 개가 곧 "조치/close 사유" 텍스트다
# (Issue Table 에 별도 '조치' 컬럼은 없다).
COMMENT_COLS = ("PTE comment", "개발 comment")

# 목록을 훑어 집계하는 함수(search_products)의 스캔 상한. 세션 수가 이보다 많아지면
# 집계가 최근 N건 기준이 된다 — 그 사실을 결과에 함께 담는다(조용한 절단 금지).
_SCAN_LIMIT = 1000


# ── 1. 세션 검색 ──────────────────────────────────────────────────────────────
def search_sessions(*, viewer, see_all_private=False, product=None, product_type=None,
                    lot_id=None, q=None, source=None, date_from=None, date_to=None,
                    limit=20):
    """평가 세션(보고서)을 조건으로 검색한다.

    언제 쓰나: 사용자가 제품명·lot·기간·"예전에 올린 보고서" 를 말하며 **어떤 평가가
    있었는지** 물을 때. 특정 세션의 이슈 내용을 물으면 이 함수로 session_id 를 먼저 찾고
    `get_session_issues` 를 호출한다.
    언제 쓰지 않나: item 이름(SGM/LDO 등)으로 과거 이력을 물을 때 — 그건
    `tools_eval.search_item_candidates` → `get_item_history` 경로다.

    product: 정확일치(대소문자 구분 없음이 아니라 SQL '=' 이다) — 확실할 때만.
    q: 자유 검색어. product/lot/file_name/process/uploader 등을 부분일치로 훑는다.
       제품명이 정확한지 모르면 product 대신 q 를 쓴다.
    date_from/date_to: epoch 초.
    """
    rows, total = report_db.get_history_page(
        product=product, product_type=product_type, lot_id=lot_id, source=source,
        q=q, date_from=date_from, date_to=date_to, limit=int(limit), offset=0,
        viewer=viewer, see_all_private=bool(see_all_private), sort="new")
    return {"sessions": rows, "total": total, "returned": len(rows)}


# ── 2. 제품 후보 검색 ────────────────────────────────────────────────────────
def search_products(keyword, *, viewer, see_all_private=False, limit=20):
    """제품명 후보를 찾는다 — 사용자가 기억하는 이름이 실제 DB 값과 다를 때.

    언제 쓰나: "S3222 라는 제품이 있었던 것 같은데" 처럼 제품명이 불확실할 때 먼저 호출한다.
    반환: [{product, product_type, family_product, sessions, last_created_at}] — 세션 수가
    많은 순.

    구현 노트: 전용 DISTINCT SQL 을 새로 만들지 않고 `search_sessions` 결과를 집계한다.
    비공개 필터·soft delete 제외 규칙이 한 곳(`_history_where`)에만 있게 하려는 의도다.
    """
    rows, total = report_db.get_history_page(
        q=str(keyword or "").strip() or None, limit=_SCAN_LIMIT, offset=0,
        viewer=viewer, see_all_private=bool(see_all_private), sort="new")
    agg: dict[tuple, dict] = {}
    for r in rows:
        product = str(r.get("product") or "").strip()
        if not product:
            continue
        key = (product, r.get("product_type") or "", r.get("family_product") or "")
        ent = agg.setdefault(key, {
            "product": product,
            "product_type": r.get("product_type") or "",
            "family_product": r.get("family_product") or "",
            "sessions": 0, "last_created_at": 0, "lot_ids": set()})
        ent["sessions"] += 1
        ent["last_created_at"] = max(ent["last_created_at"], int(r.get("created_at") or 0))
        if r.get("lot_id"):
            ent["lot_ids"].add(str(r["lot_id"]))
    out = sorted(agg.values(), key=lambda e: (-e["sessions"], -e["last_created_at"]))
    for ent in out:
        ent["lot_ids"] = sorted(ent["lot_ids"])[:10]
    return {"products": out[:int(limit)],
            "scanned_sessions": len(rows),
            "truncated": total > len(rows)}


# ── 3. 세션의 Issue Table ────────────────────────────────────────────────────
def get_session_issues(session_id, *, viewer, see_all_private=False, item_keyword=None):
    """특정 세션의 Issue Table 에서 이슈 행·Status(Open/Close)·PTE/개발 코멘트를 읽는다.

    언제 쓰나: "S3222 보고서에서 LDO 이슈 어떻게 close 됐어?" 처럼 **세션이 특정된** 질문.
    언제 쓰지 않나: session_id 를 모를 때 — `search_sessions` 를 먼저 호출한다.

    item_keyword: 주면 item 이름 부분일치(대소문자 무시)로 거른다.

    반환 dict: {session, issues: [...], source, note}
      issues[i] = {category, bin, item, status, comments{}, updated_by, updated_at}
      status 는 "Close" 또는 "Open" (편집 DB 에 Close 만 저장되고 부재가 Open 이다).
    """
    session = report_db.get_session(session_id)
    if session is None:
        return {"error": "session_not_found", "session_id": session_id}
    if not _can_view(session, viewer, see_all_private):
        # 존재 자체를 숨기는 security._private_guard 와 같은 태도.
        return {"error": "session_not_found", "session_id": session_id}
    if session.get("deleted_at") or session.get("status") not in ("done", "reused"):
        return {"error": "session_not_active", "session_id": session_id}

    info = {"session_id": session_id,
            "product": session.get("product") or "",
            "product_type": session.get("product_type") or "",
            "family_product": session.get("family_product") or "",
            "lot_id": session.get("lot_id") or "",
            "file_name": session.get("file_name") or "",
            "created_at": session.get("created_at") or 0,
            "uploaded_by": session.get("uploaded_by") or "",
            "analysis_key": session.get("analysis_key") or ""}

    if str(session.get("source") or "") == "web_report":
        issues, note = _web_report_issues(session)
    else:
        issues, note = _xlsx_issues(session)

    if item_keyword:
        needle = str(item_keyword).lower()
        issues = [i for i in issues if needle in str(i.get("item", "")).lower()]
    return {"session": info, "source": session.get("source") or "",
            "issues": issues, "note": note}


def _web_report_issues(session):
    """web_report 세션 — 편집 DB(진실) 또는 legacy manifest 폴백."""
    from web_report import edits

    session_id = session["session_id"]
    rev = report_db.get_webreport_edit_rev(session_id)
    if rev > 0:
        state = edits.load_edit_state(report_db, session_id)
        note = ""
    else:
        # rev==0 은 편집 DB 로 이전되지 않은 legacy 세션 — manifest 가 진실이다
        # (web_report/edits.py:effective_state). manifest 로드는 실패할 수 있어 격리한다.
        state, note = _legacy_state(session)

    comments = state.get("issue_comments") or {}
    statuses = state.get("issue_status") or {}
    hidden = set(state.get("issue_hidden") or [])

    issues = []
    for row_key, cols in comments.items():
        rk = rowkey.parse(row_key)
        if rk is None:
            continue
        skey = rowkey.status_key(rk)
        kept = {c: str(v).strip() for c, v in (cols or {}).items()
                if c in COMMENT_COLS and str(v or "").strip()}
        issues.append({
            "category": rk.category,
            "bin": rk.bin,
            "item": rk.item,
            "status": "Close" if str(statuses.get(skey) or "") == "Close" else "Open",
            "hidden": skey in hidden,
            "comments": kept,
            "row_key": row_key,
        })

    # 코멘트가 없는데 Close 만 찍힌 이슈도 이력이다(조치 텍스트 없이 종결된 경우).
    seen = set()
    for row_key in comments:
        rk = rowkey.parse(row_key)
        if rk is not None:
            seen.add(rowkey.status_key(rk))
    for skey, value in statuses.items():
        if skey in seen:
            continue
        rk = rowkey.parse_status_key(skey)
        if rk is None:
            continue
        issues.append({
            "category": rk.category,
            "bin": rk.bin,
            "item": rk.item,
            "status": "Close" if str(value or "") == "Close" else "Open",
            "hidden": skey in hidden,
            "comments": {},
            "row_key": skey,
        })
    issues.sort(key=lambda i: (i["category"], i["bin"] if i["bin"] is not None else -1,
                               i["item"]))
    return issues, note


def _legacy_state(session):
    """rev==0 세션의 manifest 폴백 상태. 실패하면 빈 상태 + 사유 문자열."""
    try:
        import config
        from web_report import cache, edits
        manifest = cache.load_manifest_cached(session.get("analysis_key"),
                                              Path(config.REPORT_UPLOAD_DIR))
        return edits.state_from_manifest(manifest), "legacy 세션(manifest 폴백)"
    except Exception as exc:  # 저장소 접근 실패 등 — 조회가 죽지 않게 격리
        return ({"issue_comments": {}, "issue_status": {}, "issue_hidden": []},
                f"legacy manifest 로드 실패: {exc}")


def _xlsx_issues(session):
    """xlsx 업로드 세션 — 업로드 시점 텍스트 스냅샷(report_sheet_data).

    web_report 와 컬럼 스키마가 다르다(row_key·Status 없음). 원본 헤더를 그대로 둔 채
    item/comment 만 최선으로 뽑아내고, 그 사실을 note 로 알린다.
    """
    akey = session.get("analysis_key")
    if not akey:
        return [], "analysis_key 없음"
    rows = report_db.get_sheet_data(akey, "issue_table")  # 이미 역직렬화된 값
    if not rows:
        return [], "issue_table 시트 없음"
    if not isinstance(rows, list):
        return [], "issue_table 형식이 리스트가 아님"

    issues = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = str(row.get("Item") or row.get("item") or "").strip()
        if not item:
            continue
        comments = {k: str(v).strip() for k, v in row.items()
                    if "comment" in str(k).lower() and str(v or "").strip()}
        issues.append({
            "category": str(row.get("Category") or "").strip(),
            "bin": row.get("Bin") if row.get("Bin") is not None else row.get("bin"),
            "item": item,
            "status": "",          # xlsx 흐름에는 Open/Close 개념이 없다
            "hidden": False,
            "comments": comments,
            "row_key": "",
        })
    return issues, "xlsx 업로드 세션 — Status(Open/Close) 개념 없음, 업로드 시점 텍스트"


# ── 4. 세션 횡단 item 검색 ──────────────────────────────────────────────────
def search_item_in_sessions(item_keyword, *, viewer, see_all_private=False,
                            product_type=None, family_product=None, limit=50):
    """item 이름 부분일치로 **여러 세션에 걸친** Issue Table 이슈를 찾는다.

    언제 쓰나: eval.db 가 없거나(개발 환경) item 축을 report.db 만으로 확인하고 싶을 때.
    `tools_eval.search_item_candidates` 가 마스터/alias 기반이라면 이쪽은 실제 세션에
    남은 코멘트/Status 기반이다 — 근거(session_id)가 바로 나온다는 것이 장점이다.

    구현 노트: `report_webreport_edit.item_key` 는 `Yield|<bin>|<item>` 같은 인코딩 문자열이라
    LIKE 스캔밖에 방법이 없다((kind, item_key) 인덱스도 없다). 편집 행 수가 적은 현 규모에선
    문제가 없지만, 크게 늘면 인덱스나 별도 item 축 테이블이 필요하다.
    권한은 SQL 이 아니라 조회 후 `_can_view` 로 거른다(세션 수가 적어 비용이 무의미).
    """
    needle = str(item_keyword or "").strip()
    if not needle:
        return {"hits": [], "sessions": 0}
    sql = """
        SELECT e.session_id, e.kind, e.item_key, e.value, e.updated_at, e.updated_by,
               s.product, s.product_type, s.family_product, s.lot_id, s.created_at,
               s.is_private, s.uploaded_by, s.file_name
        FROM report_webreport_edit e
        JOIN report_session s ON s.session_id = e.session_id
        WHERE e.kind IN ('issue_comment', 'issue_status')
          AND e.item_key LIKE ?
          AND s.deleted_at IS NULL
          AND s.status IN ('done', 'reused')
        ORDER BY s.created_at DESC, e.rowid
    """
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, (f"%{needle}%",)).fetchall()]

    # (session_id, 이슈키) 단위로 코멘트/Status 를 모은다.
    bucket: dict[tuple, dict] = {}
    for row in rows:
        if not _can_view(row, viewer, see_all_private):
            continue
        if product_type and row.get("product_type") != product_type:
            continue
        if family_product and row.get("family_product") != family_product:
            continue
        if row["kind"] == "issue_comment":
            row_key, col = rowkey.split_comment_key(row["item_key"])
            rk = rowkey.parse(row_key)
            if rk is None or col not in COMMENT_COLS:
                continue
            skey = rowkey.status_key(rk)
        else:
            rk = rowkey.parse_status_key(row["item_key"])
            if rk is None:
                continue
            skey, col = row["item_key"], None
        # item 이름이 아니라 bin 번호에 우연히 걸린 행은 버린다.
        if rk.item and needle.lower() not in rk.item.lower():
            continue
        ent = bucket.setdefault((row["session_id"], skey), {
            "session_id": row["session_id"],
            "product": row.get("product") or "",
            "product_type": row.get("product_type") or "",
            "family_product": row.get("family_product") or "",
            "lot_id": row.get("lot_id") or "",
            "created_at": row.get("created_at") or 0,
            "category": rk.category, "bin": rk.bin, "item": rk.item,
            "status": "Open", "comments": {},
            "updated_by": row.get("updated_by") or "", "updated_at": row.get("updated_at") or 0,
        })
        if not ent["item"] and rk.item:
            ent["item"] = rk.item
        if col:
            text = str(row.get("value") or "").strip()
            if text:
                ent["comments"][col] = text
        elif str(row.get("value") or "") == "Close":
            ent["status"] = "Close"
        if (row.get("updated_at") or 0) >= ent["updated_at"]:
            ent["updated_at"] = row.get("updated_at") or 0
            ent["updated_by"] = row.get("updated_by") or ent["updated_by"]

    # Yield 이슈는 Status 키가 `Yield|<bin>` 라 item 이름 LIKE 에 걸리지 않는다
    # (rowkey.py 의 비대칭). 걸린 세션들의 Status 를 따로 채워 넣는다.
    for (session_id, skey), ent in bucket.items():
        if ent["status"] == "Close":
            continue
        for row in report_db.get_webreport_edits(session_id, kinds=("issue_status",)):
            if row["item_key"] == skey and str(row["value"] or "") == "Close":
                ent["status"] = "Close"
                break

    hits = sorted(bucket.values(), key=lambda h: -int(h["created_at"] or 0))[:int(limit)]
    return {"hits": hits, "sessions": len({h["session_id"] for h in hits})}


# ── 권한 ─────────────────────────────────────────────────────────────────────
def _uploader_uid(value):
    """'DOMAIN\\user' → 'user' (소문자). sessions.py 의 _UPLOADER_MATCH 와 같은 규칙."""
    text = str(value or "")
    if "\\" in text:
        text = text.split("\\", 1)[1]
    return text.strip().lower()


def _can_view(session, viewer, see_all_private=False):
    """security._can_view 와 동일 판정 — 단 flask request 무의존(CLI 에서도 쓴다).

    master 여부는 서명 쿠키라 CLI 에서 판정할 수 없어, 호출부가 see_all_private 로 넘긴다.
    """
    if not int(session.get("is_private") or 0):
        return True
    if see_all_private:
        return True
    uid = _uploader_uid(viewer)
    if not uid:
        return False
    uploaded_by = str(session.get("uploaded_by") or "")
    if not uploaded_by:
        return True  # legacy(업로더 기록 없음) — sessions.py 비공개 필터와 동일
    if _uploader_uid(uploaded_by) == uid:
        return True
    return bool(report_db.is_session_editor(session.get("session_id"), uid))
