"""Issue Table tab payload builder.

레이아웃은 client/report_generator/_xlsx_sheets.py::_fill_issue_table 와 동일한
Category 그룹 구조를 따른다: Yield(Pass bin 포함 전체 bin) → CPK(항목별 worst-case Bin1 cpk < 임계값) →
ETC(placeholder). cpk_rows 에는 source="total"(합산) 행이 없으므로, 항목(subject)별로
모든 source 행 중 가장 낮은 cpk 값을 기준으로 이슈 여부를 판단한다.
프런트(server/report/report_view.html) 의 renderSheetTable(kind="issue") 가 이 컬럼 순서
(Category/Step/Bin/TNO/Item/avg → {source}_yield... → Distribution → comment...)와
CPK 서브헤더 행(Category="CPK", avg="cpk") 감지를 이미 지원한다.
ETC 섹션은 ENGR 가 임의로 추가한 item(manifest.etc_items, service.update_issue_etc_items 가
갱신)을 받아 Bin/TNO 는 tables 메타에서, avg/{source}_yield 는 yield_rows 매칭 항목에서
매 조회마다 다시 채운다(저장하는 값은 item 이름뿐). 그 뒤에 etc_auto_items(수율·cpk 는
정상인데 eval 룰만 위반한 item — web_report/ai_comment.py 산출)를 자동 행으로 잇는다.
PTE/개발 comment 는 manifest.issue_comments 에 row_key 단위로 저장된다
(service.update_issue_comments 가 갱신, 여기서는 조회 시 채우기만 한다).
row_key: Yield 행 "Yield|<bin>|<item>", CPK 데이터 행 "CPK|<item>", TEMP 행 "TEMP|<item>",
ETC 행 "ETC|<item>".
행 숨김/Status(edits.KIND_ISSUE_HIDDEN/KIND_ISSUE_STATUS) 키는 이슈 단위:
Yield 는 bin 단위 "Yield|<bin>"(대표행+상세행 일괄), CPK/TEMP/ETC 는
"CPK|<item>"/"TEMP|<item>"/"ETC|<item>".
프런트 sheets.js issueHideStatusKey 와 반드시 동일해야 한다.

Temperature 모드에서는 호출부(TAB_REGISTRY)가 **RT source 테이블과 RT 기준 yield_rows** 만
넘긴다 — 이 모듈에는 모드 분기가 없다. CT/HT 이슈는 별도 시트("Issue Table Temp",
tabs/temp_fail.py)로 나가고, 여기서는 그 item 목록(temp_items)을 받아 자동 ETC 행에서
겹치지 않게 빼는 것만 한다 (2026-08-05).
"""
from __future__ import annotations

from .common import PASS_BIN, fmt_type, item_meta as _item_meta, _is_passfail_unit
from .cpk import CPK_THRESHOLD, worst_cpk_by_subject
from .yield_tab import build_yield_bin_groups

_COMMENT_COLS = ["PTE comment", "개발 comment"]

# CPK 섹션에서 뺄 item 이름 토큰 (2026-08-10 사용자 요청) — OTP 기록값·CHIP ID 는
# 측정 산포가 의미 없는 식별/기록 항목이라 cpk 가 낮아도 이슈가 아니다. 대소문자 무시.
_CPK_SKIP_TOKENS = ("OTP_", "CHIP_ID", "CHIPID")
# CPK 섹션 정렬에서 **뒤로 미룰** item 이름 토큰 (2026-08-11 사용자 요청). 대소문자 무시.
_CPK_CODE_TOKEN = "CODE_"

# service.update_issue_comments 의 컬럼 검증용 공개 이름.
# AI Comment(아래) 는 여기 절대 추가하지 말 것 — 미포함이 곧 읽기전용 보장
# (서버 편집 검증 + 프런트 ISSUE_COMMENT_COLS 양쪽에서 편집 불가).
COMMENT_COLS = list(_COMMENT_COLS)

# ai_comment 옵션 세션에만 존재하는 읽기전용 컬럼 (web_report/ai_comment.py 가 값 생성).
# 이름에 "comment" 가 들어가 프런트 orderColumns 가 comment 블록으로 자동 배치하며,
# dict 삽입 순서(PTE comment 앞)가 블록 내 표시 순서다 (docs/13).
AI_COMMENT_COL = "AI Comment"

# 발화 signature 컬럼 — AI Comment 와 같은 조건(ai_comment 옵션 세션)에만 생긴다.
# 이름에 "comment" 가 없어 프런트 orderColumns 가 comment 블록으로 자동 배치하지 못하므로,
# sheets.js issue 분기에서 **Status 뒤 · comment 앞**(= AI Comment 왼쪽)에 명시 배치한다.
SIGNATURE_COL = "Signature"
# 표시 보조 필드(화면 컬럼 아님 — orderColumns 가 제외): 선택 목록 원본 / ENGR 확정 여부.
SIGNATURE_IDS_FIELD = "_sig"
SIGNATURE_REVIEWED_FIELD = "_sigrev"
# fail 이 있는데 발화 signature 가 없는 케이스. 엔진은 (결측이 없으면) 이걸 OK 로 낼 수도
# 있어 화면에서 구분해 준다 — 판정은 바꾸지 않고 표시만 한다.
UNCLASSIFIED = "미분류"


def _sig_values(signatures, row_key, issue_row=True):
    """Signature 셀 + 보조 필드. signatures=None 이면 빈 dict(컬럼 자체가 안 생긴다).

    signatures = {"engine": {row_key: [id..]}, "engr": {row_key: [id..]}}.
    ENGR 확정값(engr)이 있으면 그걸 쓰고, 없으면 엔진 발화값(engine)을 제안으로 보여준다.
    """
    if signatures is None:
        return {}
    if not issue_row:                       # Pass 요약행·섹션 divider 등 이슈가 아닌 행
        return {SIGNATURE_COL: "", SIGNATURE_IDS_FIELD: [], SIGNATURE_REVIEWED_FIELD: 0}
    engr = (signatures.get("engr") or {}).get(row_key)
    ids = [str(v) for v in (engr or (signatures.get("engine") or {}).get(row_key) or [])]
    return {SIGNATURE_COL: "+".join(ids) if ids else UNCLASSIFIED,
            SIGNATURE_IDS_FIELD: ids,
            SIGNATURE_REVIEWED_FIELD: 1 if engr else 0}


def _comment_values(issue_comments, row_key, ai_comments=None, signatures=None,
                    issue_row=True):
    saved = (issue_comments or {}).get(row_key) or {}
    out = _sig_values(signatures, row_key, issue_row)
    if ai_comments is not None:
        out[AI_COMMENT_COL] = str(ai_comments.get(row_key) or "")
    for col in _COMMENT_COLS:
        out[col] = str(saved.get(col) or "")
    return out


def _blank_row(sources, ai=False, signatures=None):
    row = {f"{src}_yield": "" for src in sources}
    row["Map"] = ""
    row["Distribution"] = ""
    row["Status"] = ""
    row.update(_sig_values(signatures, "", issue_row=False))
    if ai:
        row[AI_COMMENT_COL] = ""
    for col in _COMMENT_COLS:
        row[col] = ""
    return row


def _etc_rows(tables, yield_rows, etc_items, sources, issue_comments=None,
              ai_comments=None, status_of=None, signatures=None):
    if not etc_items:
        return []
    meta = _item_meta(tables)
    by_item = {}
    for r in yield_rows or []:
        item = r.get("Item")
        if item and item not in by_item:
            by_item[item] = r

    rows = []
    for item in etc_items:
        m = meta.get(item, {})
        match = by_item.get(item) or {}
        data = {
            "Category": "", "Step": fmt_type(m.get("step")), "Bin": match.get("bin", ""),
            "TNO": fmt_type(m.get("tno")), "Item": item, "avg": match.get("avg", ""),
        }
        for src in sources:
            data[f"{src}_yield"] = match.get(f"{src}_yield", "")
        data["Map"] = ""
        data["Distribution"] = ""
        data["Status"] = status_of(f"ETC|{item}") if status_of else "Open"
        data.update(_comment_values(issue_comments, f"ETC|{item}", ai_comments,
                                    signatures))
        rows.append(data)
    return rows


def _cpk_skip_subject(subject, unit) -> bool:
    """CPK 섹션(이슈)에서 제외할 항목인가 — Pass/Fail 단위이거나 OTP/CHIP ID 계열 이름.

    metrics 단계의 ``passfail_or_empty_items`` 가 이미 Pass/Fail 항목을 cpk 계산에서
    빼지만, 여기서도 unit 을 다시 본다 — 이 표에 무엇이 오르는지의 판정을 한곳에 모아
    상류 제외 규칙이 바뀌어도 Issue Table 기준이 흔들리지 않게 한다.
    """
    if _is_passfail_unit(unit):
        return True
    name = str(subject or "").upper()
    return any(token in name for token in _CPK_SKIP_TOKENS)


def _cpk_fail_subjects(cpk_rows):
    """subject 별 모든 source 행 중 최저(worst-case) cpk 기준으로 임계값 미만 항목만 반환.

    cpk 는 Bin1(양품) 기준 단일 값이다 (2026-07-23 통일 — CPK 탭과 같은 통계)."""
    worst = worst_cpk_by_subject(cpk_rows)
    fails = [(subject, cpk) for subject, cpk in worst.items() if cpk < CPK_THRESHOLD]
    # 이름에 CODE_ 가 든 항목을 **뒤로** 몰고, 각 덩어리 안에서 cpk 오름차순(낮은 순 위 →
    # 아래). CODE_ 계열은 코드값 산포라 cpk 가 구조적으로 낮게 나와 위를 다 차지하는데,
    # 먼저 봐야 하는 건 그 아래 깔리던 일반 측정 항목이다 (2026-08-11 요청).
    fails.sort(key=lambda sc: (_CPK_CODE_TOKEN in str(sc[0] or "").upper(), sc[1]))
    return fails


def build_issue_bin_summary(yield_rows):
    """Bin 별 FailTNO(Item) 구성 요약: {bin(str): [yield_row, ...]} (avg 내림차순).

    같은 Bin 이라도 서로 다른 TNO(Item)에서 fail 한 유닛이 섞일 수 있어, Issue Table 에서
    Item 클릭 시 그 Bin 전체의 구성을 한눈에 보여주기 위한 조회용 인덱스.
    """
    groups = {}
    for row in yield_rows or []:
        bin_value = row.get("bin")
        if str(bin_value).strip() == "1":
            continue
        item = row.get("Item")
        if not item:
            continue
        groups.setdefault(str(bin_value), []).append(row)
    for rows in groups.values():
        rows.sort(key=lambda r: r.get("avg") or 0, reverse=True)
    return groups


def _auto_etc_items(etc_auto_items, etc_items, cpk_fails, yield_rows, hidden,
                    temp_items=None):
    """룰만 위반한 item(ai_comment.etc_auto_items) 중 ETC 자동 행으로 올릴 목록.

    이미 다른 섹션/행으로 보이는 것은 뺀다: 수동 ETC 항목, CPK 섹션 항목(cpk<1.33),
    Yield 행이 있는 항목(fail bin), TEMP 섹션 항목(Temperature 모드), 사용자가 숨긴 이슈.
    사용자 편집값이 아니라 매 조회마다 다시 계산되는 값이라, 룰이 조용해지면 행도
    자동으로 사라진다.
    """
    if not etc_auto_items:
        return []
    seen = set(etc_items or ())
    seen.update(subject for subject, _ in cpk_fails)
    seen.update(temp_items or ())
    for r in yield_rows or []:
        if str(r.get("bin")).strip() != PASS_BIN and r.get("Item"):
            seen.add(r["Item"])
    return [it for it in etc_auto_items
            if it not in seen and f"ETC|{it}" not in hidden]


def build_issue_table_rows(tables, yield_rows=None, cpk_rows=None, etc_items=None,
                           issue_comments=None, ai_comments=None, etc_auto_items=None,
                           hidden_keys=None, statuses=None, temp_items=None,
                           signatures=None):
    # ai_comments: None = 컬럼 미표시(기존 세션 payload 불변) / dict = AI Comment 컬럼
    # 표시(값은 row_key 매칭, 빈 dict 면 빈 셀). service 가 옵션 판정 후 전달.
    # signatures: None = Signature 컬럼 미표시 / {"engine","engr"} = 표시 (ai_comments 와
    #   같은 조건에서만 온다 — 엔진 발화값이 있어야 제안이 의미가 있다).
    # hidden_keys: 숨긴 이슈 키 목록("Yield|<bin>"|"CPK|<item>") — 해당 이슈 행 미출력.
    # statuses: 이슈 키 → "Close" dict — 부재=Open. 둘 다 세션 편집 DB(edits.py) 유래.
    # temp_items: Temperature 모드만 — Temp 시트에 이미 선 item 목록(자동 ETC 행에서 제외).
    #   tables/yield_rows 는 Temperature 면 호출부가 RT source 기준으로 넘긴다.
    sources = [t.source for t in (tables or [])]
    ai = ai_comments is not None
    hidden = set(hidden_keys or ())
    statuses = statuses or {}
    base_rows = yield_rows

    def _status(key):
        return "Close" if statuses.get(key) == "Close" else "Open"

    rows = []

    # Yield 섹션 최상단: Pass(Bin1) 요약 행 (Yield 탭처럼 전체/소스별 통과율을 맨 위에 표시).
    # build_yield_rows 가 항상 yield_rows[0] 에 넣는 Pass 행을 Issue Table 컬럼 구조로 옮긴다.
    # 프런트(sheets.js)가 Bin==1 을 Pass 행으로 인식해 초록 스타일 + Map/Distribution/빨강강조를 뺀다.
    pass_src = base_rows[0] if base_rows else None
    pass_added = bool(pass_src and str(pass_src.get("bin")).strip() == PASS_BIN)
    if pass_added:
        pass_item = pass_src.get("Item") or "Pass"
        prow = {
            "Category": "Yield", "Step": pass_src.get("step", ""),
            "Bin": pass_src.get("bin"), "TNO": pass_src.get("TNO", ""),
            "Item": pass_item, "avg": pass_src.get("avg"),
        }
        for src in sources:
            prow[f"{src}_yield"] = pass_src.get(f"{src}_yield", "")
        prow["Map"] = ""
        prow["Distribution"] = ""
        prow["Status"] = ""   # Pass 행은 이슈 행이 아님 — Status/숨김 비대상.
        prow.update(_comment_values(
            issue_comments, f"Yield|{pass_src.get('bin')}|{pass_item}", ai_comments,
            signatures, issue_row=False))
        rows.append(prow)

    # Yield 섹션: Bin 당 대표(Bin 총합 집계, 식별정보는 most-fail TNO) 행 + 그 Bin 의
    # 전체 fail TNO 행(detail, 접힘 — most-fail TNO 포함).
    # 프런트가 대표행 STEP 옆 ▼ 토글로 detail 행을 펼친다(Yield 탭과 동일). 정렬은
    # build_yield_bin_groups 순서(= Bin 별 fail 비중 큰 순)를 그대로 쓴다. Category("Yield")는
    # 섹션 첫 행에만 채우고 이후 행은 ""(프런트가 시각적으로 셀 병합).
    # _grp/_detail/_ndetail 은 프런트 토글 전용 내부 필드(orderColumns 가 화면 컬럼에서 제외).
    # 숨긴 bin("Yield|<bin>")은 대표행+상세행을 통째로 제외한다(그 아래 comment 는 DB 에
    # 남아 '삭제 전체 초기화' 후 재표시). Category 라벨은 필터 후 첫 그룹 기준.
    groups = [g for g in build_yield_bin_groups(base_rows)
              if f"Yield|{g['bin']}" not in hidden]
    # Yield 섹션에 **실제로 출력된** item 이름 — 아래 CPK 섹션에서 같은 item 을 빼는 데 쓴다
    # (같은 항목이 두 섹션에 중복으로 뜨지 않게, 2026-08-14 사용자 요청). 숨긴 bin 은
    # groups 에서 이미 빠졌으므로 자연히 대상 밖이다.
    yield_items = set()
    for gi, group in enumerate(groups):
        group_rows = group["rows"]
        grp_id = f"y{gi}"
        for j, gr in enumerate(group_rows):
            bin_value = gr.get("bin")
            item = gr.get("Item")
            if item:
                yield_items.add(item)
            out = {
                "Category": "Yield" if (gi == 0 and j == 0 and not pass_added) else "",
                "Step": gr.get("step", ""),
                "Bin": bin_value,
                "TNO": gr.get("TNO", ""),
                "Item": item,
                "avg": gr.get("avg"),
                "_grp": grp_id,
                "_detail": j > 0,
            }
            if j == 0:
                out["_ndetail"] = len(group_rows) - 1
            for src in sources:
                out[f"{src}_yield"] = gr.get(f"{src}_yield", "")
            out["Map"] = ""
            out["Distribution"] = ""
            # Status 는 bin 이슈 단위 — 대표행에만 표시(상세행 빈칸).
            out["Status"] = _status(f"Yield|{bin_value}") if j == 0 else ""
            out.update(_comment_values(issue_comments, f"Yield|{bin_value}|{item}",
                                       ai_comments, signatures))
            rows.append(out)

    # cpk_rows 는 전 소스 기준으로 계산돼 오므로 이 표의 소스 컬럼에 맞춰 거른다.
    # 다른 모드는 sources 가 전 소스라 no-op 이고, Temperature 는 RT 기준만 남는다
    # (CT/HT 는 RT limit 재판정으로 이미 잘려 있어 그 분포로 낸 cpk 를 섞으면 기준이 어긋난다).
    src_set = set(sources)
    cpk_src_rows = [r for r in (cpk_rows or []) if r.get("source") in src_set]
    # unit 은 항목이 처음 등장하는 행 기준 (표시 unit 규칙과 동일).
    cpk_units = {}
    for r in cpk_src_rows:
        cpk_units.setdefault(r.get("subject"), r.get("units"))
    cpk_hit = [(subject, cpk) for subject, cpk in _cpk_fail_subjects(cpk_src_rows)
               if f"CPK|{subject}" not in hidden]
    # 제외 항목은 CPK 섹션에서만 빼고 _auto_etc_items 의 seen 에는 그대로 넘긴다
    # (cpk_hit) — 안 그러면 여기서 뺀 항목이 룰 위반 자동 ETC 행으로 다시 올라온다.
    # yield_items 제외도 같은 취급이다: Yield 섹션에 이미 같은 item 행이 있으므로 CPK
    # 섹션에서만 뺀다(그 항목의 "CPK|<item>" comment 는 편집 DB 에 그대로 남는다).
    cpk_fails = [(subject, cpk) for subject, cpk in cpk_hit
                 if subject not in yield_items
                 and not _cpk_skip_subject(subject, cpk_units.get(subject))]
    # CPK 구간은 source 컬럼({src}_yield)에 source 별 CPK 값을 담는다(Yield 값 대신).
    # subhead 행이 그 컬럼을 "CPK"로 재정의(프런트 isCpkSubheadRow 감지). STEP/TNO 는 항목
    # 메타에서, BIN 은 CPK 항목엔 없어 비운다. 값은 선정 기준과 동일한 Bin1 기준 cpk.
    cpk_by = {}
    for r in cpk_src_rows or []:
        cpk = r.get("cpk")
        if cpk is not None:
            cpk_by[(r.get("subject"), r.get("source"))] = cpk
    cpk_meta = _item_meta(tables)
    subhead = {"Category": "CPK", "Step": "", "Bin": "", "TNO": "", "Item": "item name", "avg": "cpk"}
    subhead.update(_blank_row(sources, ai, signatures))
    for src in sources:
        subhead[f"{src}_yield"] = "CPK"
    rows.append(subhead)
    if cpk_fails:
        for subject, cpk in cpk_fails:
            m = cpk_meta.get(subject, {})
            data = {"Category": "", "Step": fmt_type(m.get("step")), "Bin": "",
                    "TNO": fmt_type(m.get("tno")), "Item": subject, "avg": cpk}
            data.update(_blank_row(sources, ai, signatures))
            for src in sources:
                data[f"{src}_yield"] = cpk_by.get((subject, src), "")
            data["Status"] = _status(f"CPK|{subject}")
            data.update(_comment_values(issue_comments, f"CPK|{subject}", ai_comments,
                                        signatures))
            rows.append(data)
    else:
        rows.append({"Category": "", "Step": "", "Bin": "", "TNO": "", "Item": "", "avg": "",
                     **_blank_row(sources, ai, signatures)})

    etc = {"Category": "ETC", "Step": "", "Bin": "", "TNO": "", "Item": "", "avg": "",
           **_blank_row(sources, ai, signatures)}
    rows.append(etc)
    # 수동 추가분(ENGR) 뒤에 룰 위반 자동 행을 잇는다 — 행 채움 로직은 동일.
    # Temp 시트에 이미 선 item 은 자동 ETC 행에서 뺀다(같은 item 이 두 곳에 겹치지 않게).
    etc_all = list(etc_items or []) + _auto_etc_items(
        etc_auto_items, etc_items, cpk_hit, base_rows, hidden,
        temp_items=temp_items)
    rows.extend(_etc_rows(tables, base_rows, etc_all, sources,
                          issue_comments=issue_comments, ai_comments=ai_comments,
                          status_of=_status, signatures=signatures))
    return rows
