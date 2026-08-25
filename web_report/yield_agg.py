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


def insert_bin_agg_rows(issue_rows, blank_cols=AGG_BLANK_COLS):
    """Issue Table 행 목록 → **펼침 표시 구성** (sheets.js ``insertBinAggRows`` 의 파이썬 짝).

    대표행 뒤에 집계 헤더행을 끼우고, 종전에 "대표행과 중복"이라며 빼던 첫 상세행
    (= most-fail 항목)을 되살린다. 항목이 1개뿐인 Bin 은 집계행을 만들지 않고 그 중복
    상세행만 뺀다(= 종전 표시와 동일).

    ⚠ 대표행이 **집계행인 표에만** 적용해야 한다. Issue Table Temp
    (``tabs/temp_fail._group_by_bin``)는 같은 ``_grp``/``_detail`` 규약을 쓰지만 대표행이
    항목 행 자체라 합계 개념이 없다 — ``rep["Item"] == 첫 상세행["Item"]`` 가드가 그 표를
    자동으로 제외한다(프런트가 쓰던 가드를 그대로 승계).

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
        if first is None or str(r.get("Item") or "") != str(first.get("Item") or ""):
            continue
        n_items = int(r.get("_ndetail") or 0)
        if n_items <= 1:
            cp = dict(r)
            cp["_ndetail"] = 0
            out[-1] = cp
            drop.append(id(first))
            continue
        agg = dict(r)
        agg["TNO"] = BIN_AGG_TNO
        agg["Item"] = bin_agg_label(r.get("Bin"), n_items)
        agg["_agg"] = True
        for col in blank_cols:
            if col in agg:
                agg[col] = ""
        agg.pop("_sig", None)
        agg.pop("_sigrev", None)
        rep_cp = dict(r)
        rep_cp["_hasAgg"] = True
        out[-1] = rep_cp
        out.append(agg)
    if not drop:
        return out
    dropped = set(drop)
    return [r for r in out if id(r) not in dropped]
