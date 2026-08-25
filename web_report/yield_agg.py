"""Bin 집계 헤더행 (Yield / Issue Table 펼침 표시 전용) — 순수 모듈.

**왜 있나** (2026-08-25): 서버 대표행(rep, `yield_tab._bin_total_row`)은 숫자가 Bin 합계인데
식별정보(Step/TNO/Item)는 그 Bin 에서 가장 많이 죽은 항목 것을 그대로 쓴다. 접힌 상태에선
"Bin 요약"으로 읽혀 자연스럽지만, 펼치면 그 항목 혼자 Bin 전체만큼 죽은 것처럼 보인다
(TEST1 이 2개 fail 인데 5 로 표시 — 그 항목의 실제 값은 화면 어디에도 없었다).

그래서 **펼칠 때만** 대표행을 이 집계 헤더행으로 갈아끼우고, 그 항목은 자기 실제 값을 가진
상세행으로 되돌린다. 접힘은 종전 그대로다.

**이 규약의 정본이다.** 소비자가 4곳(웹 sheets.js / yield_issue.js, Honey Excel `_sheets.py` /
`_xlsx.py`)이라 라벨 문자열을 각자 만들면 곧 갈라진다 — 반드시 여기를 통할 것.
JS 쪽 사본은 `yield_issue.js` 의 `binAggLabel`/`yieldBinAggRow` 하나뿐이다.

**report payload 에는 실리지 않는다** — 표시 직전에 rep 에서 파생할 뿐이므로 캐시 스키마
버전(REPORT_SCHEMA_VERSION)과 무관하다.

의존 없음(순수 stdlib) — Honey 클라이언트가 `web_report.comment_format` 처럼 가볍게
import 한다. `tabs/` 패키지 안에 두면 클라가 pandas·전 탭 레지스트리까지 끌어오게 된다.
"""
from __future__ import annotations

BIN_AGG_TNO = "-"

# 라벨 가운데 공백은 **일반 space 4칸**이다(사용자 요청 2026-08-25 — 넉넉히 띄어서 표기).
# HTML 은 연속 공백을 접으므로 집계행 Item 셀에 CSS `white-space: pre` 를 함께 건다
# (report_view.html `td.bin-agg-item`). nbsp 를 쓰지 않는 이유는 이 문자열이 Excel·TSV
# 복사로도 그대로 나가기 때문 — 데이터는 평범한 공백으로 두고 표시만 CSS 로 지킨다.
BIN_AGG_GAP = "    "


def bin_agg_label(bin_value, n_items):
    """펼침 집계 헤더행의 Item 표기 — ``BIN 15    (3 items)``."""
    return f"BIN {bin_value}{BIN_AGG_GAP}({n_items} items)"


def build_bin_agg_row(group):
    """``build_yield_bin_groups`` 의 그룹 → 펼침용 집계 헤더행 (없으면 None).

    숫자(avg/{src}_yield/{src}_count)는 rep 를 그대로 승계하고 식별정보만 Bin 집계 표기로
    바꾼다 — 값을 다시 계산하지 않는다(CLAUDE.md 규칙 13).

    항목이 1개뿐인 Bin 은 ``None`` 이다: 펼쳐도 상세가 1줄이라 집계행이 같은 값을 두 번
    보여줄 뿐이다(사용자 확정 2026-08-25). 이 경우 화면·Excel 모두 종전처럼 항목 행 하나만
    그린다.
    """
    rows = group.get("rows") or []
    n_items = len(rows) - 1          # rows[0] 은 rep 자기 자신
    if n_items <= 1:
        return None
    row = dict(group.get("rep") or {})
    row["TNO"] = BIN_AGG_TNO
    row["Item"] = bin_agg_label(group.get("bin"), n_items)
    return row


def expand_bin_group(group):
    """그룹 → **펼침 표시 순서**의 행 목록.

    항목이 여럿이면 ``[집계 헤더행] + 그 Bin 의 모든 TNO 행``(most-fail 포함),
    항목이 하나면 ``[대표행]``(= 그 항목 행과 값이 같다). 화면·Excel 이 같은 구성을 쓰도록
    이 함수 하나로 모은다.
    """
    rows = group.get("rows") or []
    agg = build_bin_agg_row(group)
    if agg is None:
        rep = group.get("rep")
        return [rep] if rep is not None else []
    return [agg] + list(rows[1:])


# Issue Table 집계 헤더행에서 **비우는** 열.
#   Map/Distribution — 바로 아래 항목 행들과 그림이 같아 중복이고, 미니셀이 빠져야 헤더행
#                      높이가 숫자에 맞게 좁아진다(사용자 요청).
#   comment/Signature — 저장 키가 항목 단위(``Yield|<bin>|<item>``)라 첫 TNO 행이 주인이다.
# Status 는 비우지 않는다 — 저장 키가 bin 단위(``Yield|<bin>``)라 집계행이 자연스러운 주인.
AGG_BLANK_COLS = ("Map", "Distribution", "AI Comment", "Signature",
                  "PTE comment", "개발 comment")

# 대표행이 **항목 행 자체**인 표(Issue Table Temp)의 헤더행에서 값을 남길 열.
# 나머지 수치·편집 열은 전부 비운다 — 그 표는 항목끼리 die 가 겹쳐 합산이 뜻을 갖지 않고
# (tabs/temp_fail.py `_group_by_bin` docstring), bin 단위 저장 키도 없다(키는 `TEMP|<item>`).
AGG_ID_COLS = ("Category", "Step", "Bin", "TNO", "Item")


def insert_bin_agg_rows(issue_rows, blank_cols=AGG_BLANK_COLS):
    """Issue Table 행 목록 → **펼침 표시 구성** (sheets.js ``insertBinAggRows`` 의 파이썬 짝).

    Bin 그룹마다 대표행 뒤에 집계 헤더행을 끼워, 펼쳤을 때 첫 줄이 항상
    ``BIN 15    (3 items)`` 가 되고 그 아래에 그 Bin 의 **모든 항목 행**이 자기 값으로 서게
    한다. 접힘(대표행 1줄)은 종전 그대로다.

    ``_grp`` 규약을 공유하는 표가 둘인데 **대표행의 성격이 달라** 처리가 갈린다.
    판정은 ``rep["Item"] == 첫 상세행["Item"]`` 하나로 한다 — 구조상 항상 참/거짓이 갈린다.

    ① **합계 대표행** (Yield 섹션, ``tabs/yield_tab._bin_total_row``)
       대표행 = Bin 합계인데 이름만 most-fail 항목 것이고, 그 항목의 진짜 행이 첫 상세행이다
       (= Item 이 같다). → 헤더행이 **대표행을 대신**하고 숫자를 그대로 승계한다.
    ② **항목 대표행** (Issue Table Temp, ``tabs/temp_fail._group_by_bin``)
       대표행 = avg 최대 **항목 행 그 자체**라 합계가 없고 첫 상세행은 다른 항목이다.
       → 헤더행은 숫자를 **비우고**(합산이 틀린 값이 된다), 대표행을 상세행으로 **복제**해
       펼침에서 그 항목이 사라지지 않게 한다. 접힘은 종전대로 대표행이 숫자를 보여준다.

    두 경우 모두 결과 DOM 구조가 같아 프런트 토글 코드 한 벌로 처리된다
    (접힘=대표행 / 펼침=헤더행+상세행 전부).

    항목이 1개뿐인 Bin 은 헤더행을 만들지 않는다(사용자 확정) — ①은 중복 상세행을 빼
    종전과 같은 1행이 되고, ②는 애초에 상세행이 없다.

    원본 dict 은 바꾸지 않는다(사본만 만든다).
    """
    src = list(issue_rows or [])
    first_detail = {}
    for r in src:
        grp = (r or {}).get("_grp")
        if grp and r.get("_detail") and grp not in first_detail:
            first_detail[grp] = r
    out, drop = [], []
    for r in src:
        out.append(r)
        if not r or not r.get("_grp") or r.get("_detail"):
            continue
        first = first_detail.get(r["_grp"])
        if first is None:
            continue                    # 상세행이 없는 그룹 — 접을 것이 없다
        # 대표행이 합계행인가(①), 항목 행 자체인가(②)
        totals_rep = str(r.get("Item") or "") == str(first.get("Item") or "")
        n_detail = int(r.get("_ndetail") or 0)
        # 그 Bin 의 항목 수 — ① 은 상세행이 곧 전 항목, ② 는 대표행이 한 항목 더 든다.
        n_items = n_detail if totals_rep else n_detail + 1
        if n_items <= 1:
            if totals_rep:
                cp = dict(r)
                cp["_ndetail"] = 0
                out[-1] = cp
                drop.append(id(first))
            continue
        agg = dict(r)
        agg["TNO"] = BIN_AGG_TNO
        agg["Item"] = bin_agg_label(r.get("Bin"), n_items)
        agg["_agg"] = True
        agg["_ndetail"] = n_items
        if totals_rep:
            for col in blank_cols:
                if col in agg:
                    agg[col] = ""
        else:
            for col in list(agg):
                if not col.startswith("_") and col not in AGG_ID_COLS:
                    agg[col] = ""
        agg.pop("_sig", None)
        agg.pop("_sigrev", None)
        rep_cp = dict(r)
        rep_cp["_hasAgg"] = True
        rep_cp["_ndetail"] = n_items
        out[-1] = rep_cp
        out.append(agg)
        if not totals_rep:
            # 대표행을 상세행으로 복제 — 이게 없으면 펼쳤을 때 그 항목이 사라진다.
            # Category 는 비운다: 값이 남으면 프런트가 섹션 divider 로 보고 행을 건너뛴다.
            clone = dict(r)
            clone["_detail"] = True
            clone["Category"] = ""
            clone.pop("_ndetail", None)
            clone.pop("_hasAgg", None)
            out.append(clone)
    if not drop:
        return out
    dropped = set(drop)
    return [r for r in out if id(r) not in dropped]
