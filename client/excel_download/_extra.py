"""Excel Download 보강 표 빌더 — 순수 함수만 (Qt/xlwings/xlsxwriter/네트워크 비의존).

web_report 화면에는 있는데 Excel 에는 없던 것들을 시트에 넣을 수 있는 **행 배열**로 만든다.
값 계산 로직은 전부 웹 화면의 정본을 이식한 것이며(파일:함수를 각 빌더 docstring 에 적었다),
새로 계산하지 않는다 — 같은 값이 탭마다 다르게 보이면 리포트 전체가 불신된다(규칙 #13).

기입(서식·색·좌표)은 호출부(_xlsx.py)가 하고, 여기서는 **무엇을 쓸지**만 정한다.
그래서 Excel 없이 self-run 테스트로 값 검증이 가능하다.
"""
from __future__ import annotations

import colorsys
from html.parser import HTMLParser

try:
    from web_report.comment_format import strip_format
except Exception:  # 단독 실행/테스트 폴백
    def strip_format(text):
        return text or ""

try:
    from web_report.yield_agg import insert_bin_agg_rows
except Exception:  # 단독 실행/테스트 폴백 — 종전(대표행만) 구성으로 동작한다
    def insert_bin_agg_rows(rows, *_a, **_kw):
        return list(rows or [])

# Engr Comment 리치 서식 마커 — map_select.js ENGR_RICH_MARK 와 동일.
_ENGR_RICH_MARK = "<!--rich-->"
# 내용까지 통째로 버리는 태그 — map_select.js ENGR_DROP_TAGS 미러.
_ENGR_DROP_TAGS = {"script", "style", "noscript", "template", "iframe", "object", "embed"}
_ENGR_BREAK_TAGS = {"br", "div", "p"}

# Issue Status / Engr Comment 카테고리 — map_select.js issueStatusCardHtml / ENGR_COMMENT_FIELDS.
_ISSUE_CATS = ("Yield", "CPK", "ETC")
_ISSUE_CATS_TEMP = ("Yield", "CPK", "TEMP", "ETC")
_ENGR_FIELDS = (("yield", "Yield"), ("cpk", "CPK"), ("etc", "ETC"))
_ENGR_FIELDS_TEMP = (("yield", "Yield"), ("cpk", "CPK"), ("temp", "TEMP"), ("etc", "ETC"))


def _is_temp(mode) -> bool:
    return str(mode or "") == "Temperature"


# ── Summary ① Issue Status ──────────────────────────────────────────────────

def build_issue_status_rows(issue_rows, temp_rows=None, mode="Normal"):
    """카테고리별 Open/Close/진행률 — map_select.js issueStatusCounts/issueStatusCardHtml 이식.

    Issue Table + (있으면) Issue Table Temp 두 시트를 **같은 규칙으로 합산**한다
    (TEMP 섹션이 별도 시트로 빠졌으므로 합산하지 않으면 카드 값이 웹과 어긋난다).
    Status 가 빈 행(Pass/상세/서브헤더)은 비대상, "Close" 만 close 나머지는 open.
    진행률 = close/(open+close)*100 소수 1자리, 이슈 0건이면 "-".
    """
    counts = {c: {"open": 0, "close": 0} for c in _ISSUE_CATS_TEMP}
    for rows in (issue_rows or [], temp_rows or []):
        section = ""
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if row.get("Category"):
                section = str(row["Category"])
            status = str(row.get("Status") or "")
            if not status or section not in counts:
                continue
            counts[section]["close" if status == "Close" else "open"] += 1

    out = []
    for cat in (_ISSUE_CATS_TEMP if _is_temp(mode) else _ISSUE_CATS):
        c = counts[cat]
        total = c["open"] + c["close"]
        prog = f"{c['close'] / total * 100:.1f}%" if total else "-"
        out.append([cat, c["open"], c["close"], prog])
    return out


# ── Summary ② Engr Comment ──────────────────────────────────────────────────

class _EngrTextExtractor(HTMLParser):
    """제한 HTML → 평문. BR/DIV/P 는 개행, script 류는 내용까지 버린다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._drop_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _ENGR_DROP_TAGS:
            self._drop_depth += 1
        elif tag == "br":
            self.parts.append("\n")
        elif tag in _ENGR_BREAK_TAGS and self.parts:
            # div/p 는 블록이라 **여는 쪽에서도** 줄이 바뀐다("A<div>B</div>" → A / B).
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _ENGR_DROP_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
        elif tag in _ENGR_BREAK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._drop_depth:
            self.parts.append(data)


def engr_plain(value) -> str:
    """Engr Comment 저장값 → Excel 셀에 넣을 평문.

    저장값은 문자열 1개다. 서식이 붙은 값만 선두에 ``<!--rich-->`` 마커 + 제한 HTML 이고,
    마커가 없으면 예전 그대로 평문이다(map_select.js §Engr Comment 서식).
    ``@[..]/#[..]/$[..]`` 링크 토큰은 **원문 유지** — 화면 밖에서도 어디를 가리키는지가
    정보이기 때문. ``*[..]`` 서식 토큰만 strip_format 으로 벗긴다.
    """
    text = str(value or "")
    if not text:
        return ""
    if text.startswith(_ENGR_RICH_MARK):
        parser = _EngrTextExtractor()
        try:
            parser.feed(text[len(_ENGR_RICH_MARK):])
            parser.close()
            text = "".join(parser.parts)
        except Exception:
            pass                    # 파싱 실패 시 원문 그대로 — 값을 잃지 않는 쪽
    text = strip_format(text) or ""
    # 연속 개행 정리 (div+br 중첩으로 빈 줄이 과하게 생기는 것만 접는다)
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln.strip():
            out.append(ln)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def build_engr_rows(summary_engr, mode="Normal"):
    """[[라벨, 평문 코멘트], …] — 값이 없는 칸도 행은 유지(웹 4칸 그리드와 같은 구성)."""
    engr = summary_engr if isinstance(summary_engr, dict) else {}
    fields = _ENGR_FIELDS_TEMP if _is_temp(mode) else _ENGR_FIELDS
    return [[label, engr_plain(engr.get(key))] for key, label in fields]


# ── Yield 상단 요약 ─────────────────────────────────────────────────────────

def _pct(value):
    """웹 표기와 같은 소수 2자리 문자열 (숫자가 아니면 원값 그대로)."""
    if isinstance(value, (int, float)):
        return f"{value:.2f}"
    return value if value is not None else ""


def _basis_by_source(yield_basis):
    rows = (yield_basis or {}).get("by_source")
    return {str(r.get("source")): r for r in rows} if isinstance(rows, list) else {}


def basis_caption(yield_summary, yield_basis) -> str:
    """분모 기준 한 줄 — sheets.js yieldBasisBadgeHtml 이식. 정보가 없으면 빈 문자열."""
    b = yield_basis or {}
    if not b:
        return ""
    tested = (yield_summary or {}).get("tested")
    rows = b.get("by_source") if isinstance(b.get("by_source"), list) else []
    n_gross = sum(1 for r in rows if r.get("basis") == "gross")
    basis = b.get("basis")
    if basis == "mixed":
        txt = (f"분모: 소스별 — Gross Die {b.get('gross_die')} {n_gross}개 / "
               f"Test data {len(rows) - n_gross}개")
    elif basis == "gross":
        txt = f"분모: Gross Die {b.get('gross_die')}"
    else:
        txt = "분모: Test data 개수" + (f" {tested}" if tested is not None else "")
        return txt
    if tested is not None:
        txt += f" · 측정 die {tested}"
    return txt


def build_yield_overview(yield_summary, yield_basis=None):
    """Yield 탭 상단 요약 3블록 — sheets.js yieldOverviewHtml 이식.

    반환 ``{"overall": {header, rows, caption}, "by_step": {...}|None, "by_source": {...}|None}``
    - by_source 는 **소스 2개 이상**일 때만 (단일 소스는 Total 과 같은 값 반복)
    - by_step 은 **STEP 2개 이상**일 때만 (하나뿐이면 전체 수율 카드와 같은 값)
    둘 다 웹 화면의 표시 조건과 동일 — 웹에 없는 표가 Excel 에만 생기지 않게 한다.
    """
    ov = yield_summary if isinstance(yield_summary, dict) else {}
    if not ov:
        return {"overall": None, "by_step": None, "by_source": None}

    basis = yield_basis or {}
    total_label = "Gross Die" if basis.get("basis") == "gross" else "Total"
    overall = {
        "header": ["Yield (%)", "Pass", total_label, "Fail"],
        "rows": [[_pct(ov.get("yield_pct")), ov.get("pass"), ov.get("total"), ov.get("fail")]],
        "caption": basis_caption(ov, basis),
    }

    by_source = None
    src_rows = ov.get("by_source") if isinstance(ov.get("by_source"), list) else []
    if len(src_rows) >= 2:
        bmap = _basis_by_source(basis)
        rows = []
        for s in src_rows:
            bi = bmap.get(str(s.get("source")))
            btxt = (("Gross " if bi.get("basis") == "gross" else "Test ") + str(bi.get("total"))
                    if bi else "")
            rows.append([s.get("source"), f"{_pct(s.get('yield_pct'))}%",
                         f"{s.get('pass')} / {s.get('total')}", btxt])
        by_source = {"header": ["Source", "Yield (%)", "Pass / Total", "분모"], "rows": rows}

    by_step = None
    step_rows = ov.get("by_step") if isinstance(ov.get("by_step"), list) else []
    if len(step_rows) > 1:
        rows, merges = [], []
        for s in step_rows:
            srcs = s.get("sources") if isinstance(s.get("sources"), list) else []
            if not srcs:        # 옛 payload 폴백 — pooled 값 1행
                srcs = [{"source": "", "yield_pct": s.get("step_yield_pct"),
                         "survivor": s.get("survivor"), "entered": s.get("entered"),
                         "fail": s.get("fail"), "cum_fail": s.get("cum_fail")}]
            avg = s.get("avg_yield_pct")
            avg = _pct(avg) if avg is not None else _pct(s.get("step_yield_pct"))
            start = len(rows)
            for i, sr in enumerate(srcs):
                cum = sr.get("cum_fail")
                fail_txt = f"{sr.get('fail')}" if cum is None else f"{sr.get('fail')} / {cum}"
                rows.append([f"{s.get('step')}\navg {avg}%" if i == 0 else "",
                             sr.get("source"), f"{_pct(sr.get('yield_pct'))}%",
                             f"{sr.get('survivor')} / {sr.get('entered')}", fail_txt])
            if len(srcs) > 1:
                merges.append((start, len(rows) - 1))
        by_step = {"header": ["Step", "Source", "Cum Yield (%)", "Pass / In",
                              "Fail (step / cum)"],
                   "rows": rows, "merges": merges}

    return {"overall": overall, "by_step": by_step, "by_source": by_source}


# ── fail 빨강 그라데이션 (웹 CSS 파리티) ─────────────────────────────────────

# report_view.html: hsl(0, 78%, calc(94% - var(--yw) * 36%))
_GRAD_HUE = 0.0
_GRAD_SAT = 0.78
_GRAD_L_TOP = 94.0
_GRAD_L_SPAN = 36.0
GRAD_LEVELS = 16        # 양자화 단계 — 서식 객체/COM 왕복 수를 상한 짓는다


def grad_fill_rgb(ratio) -> str:
    """비율(0~1) → 웹과 같은 빨강 그라데이션 색 ``"RRGGBB"``."""
    try:
        yw = float(ratio)
    except Exception:
        yw = 0.0
    yw = min(1.0, max(0.0, yw))
    light = (_GRAD_L_TOP - _GRAD_L_SPAN * yw) / 100.0
    r, g, b = colorsys.hls_to_rgb(_GRAD_HUE, light, _GRAD_SAT)
    return "{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def quantize_ratio(ratio, levels=GRAD_LEVELS) -> float:
    """비율을 ``levels`` 단계로 반올림 — 색 수를 제한해 서식 객체 폭증을 막는다.

    같은 비율이면 같은 색이므로 호출부가 색→셀목록으로 묶어 한 번에 칠할 수 있다.
    0 은 0 으로 남긴다(칠하지 않는 셀과 구분).
    """
    try:
        yw = float(ratio)
    except Exception:
        return 0.0
    yw = min(1.0, max(0.0, yw))
    if yw <= 0:
        return 0.0
    step = 1.0 / max(1, int(levels))
    return min(1.0, round(yw / step) * step) or step


def column_max(values):
    """그라데이션 정규화 기준 — sheets.js 와 같은 '그 컬럼의 최대값'."""
    nums = [abs(float(v)) for v in values
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return max(nums) if nums else 0.0


def grad_ratio(value, col_max):
    """sheets.js: yw = min(1, 값 / 컬럼최대). 값이 없거나 0 이하면 None(칠하지 않음)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0 or not col_max:
        return None
    return min(1.0, float(value) / float(col_max))


# ── Issue Table 행 조립 ─────────────────────────────────────────────────────

ISSUE_ID_COLS = ["Category", "Step", "Bin", "TNO", "Item"]
COMMENT_COLS = ["PTE comment", "개발 comment"]   # 저장 키 — 화면 라벨과 다르다(규칙 #12)
CPK_THRESHOLD = 1.33
PASS_BIN = "1"
# Status 셀 배경 — report_view.html .iss-open / .iss-close 파스텔 색.
STATUS_FILL = {"open": "FFC7CE", "close": "DCFCE7"}


def issue_header(source_names):
    """웹 Issue Table 컬럼 순서 — 식별 → Map → Distribution → avg → source → Status → comment."""
    return (list(ISSUE_ID_COLS) + ["Map", "Distribution", "avg"]
            + list(source_names) + ["Status"] + list(COMMENT_COLS))


def build_issue_matrix(issue_rows, source_names, *, header_row=3, start_col=2):
    """Issue Table 표를 값·강조·병합까지 한 번에 조립 (기입 엔진 무의존).

    _sheets.write_issue_sheet(COM, 동결)의 표 조립과 같은 규약을 따르되, 웹 화면의 색까지
    재현한다: fail 셀은 **컬럼별 최대값 대비 빨강 그라데이션**(단색이 아니라), Status 셀은
    Open/Close 파스텔 배경. 접힌 detail 행의 comment 는 같은 bin 대표행에 모아 쓰고,
    TEMP 섹션은 접지 않는다(행 하나하나가 독립 항목이라 접으면 사라진다).

    반환 dict:
      header / rows                     기입할 값
      grad_cells  {(row, col): ratio}   빨강 그라데이션 (0<ratio<=1)
      cpk_cells   [(row, col)]          cpk<1.33 연노랑
      status_cells {(row,col): "open"|"close"}
      subhead_rows [row]                CPK 서브헤더 (헤더 서식)
      merges      [(r1, r2)]            Category 세로 병합
      rows_meta / map_rows / temp_rows  썸네일 부착 대상 (excel 행 번호)
      dist_col / map_col                썸네일 열 번호
    """
    header = issue_header(source_names)
    c1 = start_col
    # 열 번호는 헤더 위치에서 직접 얻는다 — 산술식(c1+7+len(sources) 등)으로 세면 컬럼이
    # 하나 늘거나 줄 때 조용히 어긋난다(실제로 Status 색이 옆 칸에 칠해진 적이 있다).
    map_col = c1 + header.index("Map")
    dist_col = c1 + header.index("Distribution")
    avg_col = c1 + header.index("avg")
    status_col = c1 + header.index("Status")

    # 웹 화면과 같은 행 구성 — Bin 집계 헤더행 + 그 Bin 의 모든 TNO 행(2026-08-25).
    # 규약 정본은 web_report/yield_agg.py 이고 프런트 sheets.js insertBinAggRows 와 짝이다.
    issue_rows = insert_bin_agg_rows(issue_rows)
    agg_grps = {r.get("_grp") for r in issue_rows if r.get("_agg")}

    # 집계행이 생긴 그룹은 상세행이 각자 한 줄로 나가므로 comment 를 대표행에 합치지 않는다
    # (합치기는 상세행을 아예 안 내보내던 시절의 보완책이다).
    detail_comments = {}
    scan_section = ""
    for r in issue_rows or []:
        if r.get("Category") in ("Yield", "CPK", "ETC", "TEMP"):
            scan_section = r["Category"]
        if scan_section == "TEMP" or not r.get("_detail"):
            continue
        grp = r.get("_grp")
        if grp in agg_grps:
            continue
        for col in COMMENT_COLS:
            text = (strip_format(r.get(col)) or "").strip()
            if text:
                detail_comments.setdefault((grp, col), []).append(f"{r.get('Item')}: {text}")

    rows, rows_meta, map_rows, temp_rows = [], [], [], []
    subhead_rows, merges = [], []
    cpk_cells, status_cells = [], {}
    # 그라데이션은 컬럼별 최대값이 필요해 2-pass — 먼저 (행,열,값)을 모으고 마지막에 정규화.
    fail_values = []
    spans = []
    section = ""
    for r in issue_rows or []:
        if r.get("Category") in ("Yield", "CPK", "ETC", "TEMP"):
            section = r["Category"]
        if r.get("_detail") and section != "TEMP" and r.get("_grp") not in agg_grps:
            continue
        # 접힘 전용 대표행은 집계 헤더행과 같은 줄이라 Excel 에는 한 번만 나간다.
        if r.get("_hasAgg"):
            continue
        excel_row = header_row + 1 + len(rows)
        subhead = section == "CPK" and str(r.get("avg") or "").strip().lower() == "cpk"
        bin_text = str(r.get("Bin") or "").strip()
        is_pass = bin_text == PASS_BIN

        src_vals = (list(source_names) if subhead
                    else [r.get(f"{s}_yield") for s in source_names])
        status = r.get("Status") or ""
        vals = ([r.get("Category"), r.get("Step"), r.get("Bin"), r.get("TNO"), r.get("Item"),
                 "", "",                    # Map / Distribution 이미지 자리
                 r.get("avg")] + src_vals + [status])
        for col in COMMENT_COLS:
            parts = []
            own = (strip_format(r.get(col)) or "").strip()
            if own:
                parts.append(own)
            parts.extend(detail_comments.get((r.get("_grp"), col), []))
            vals.append("\n".join(parts))

        if subhead:
            subhead_rows.append(excel_row)
        else:
            if not is_pass:
                for off, v in enumerate([r.get("avg")]
                                        + [r.get(f"{s}_yield") for s in source_names]):
                    num = _num(v)
                    if num is None:
                        continue
                    if section in ("Yield", "ETC", "TEMP") and num > 0:
                        fail_values.append((excel_row, avg_col + off, num))
                    elif section == "CPK" and num < CPK_THRESHOLD:
                        cpk_cells.append((excel_row, avg_col + off))
            if status:
                status_cells[(excel_row, status_col)] = (
                    "close" if str(status) == "Close" else "open")
            # 집계 헤더행은 Map/Distribution 썸네일을 넣지 않는다(웹 화면과 동일 — 바로
            # 아래 항목 행들과 그림이 같다).
            if not is_pass and bin_text and section in ("Yield", "ETC") and not r.get("_agg"):
                map_rows.append((bin_text, excel_row))
            elif section == "TEMP" and str(r.get("Item") or "").strip():
                temp_rows.append((str(r["Item"]).strip(), excel_row))

        # 집계 헤더행의 Item 은 측정 항목이 아니라 라벨이라 Distribution 썸네일 대상이 아니다.
        rows_meta.append((None if r.get("_agg") else r.get("Item"), excel_row, section))
        if spans and spans[-1][0] == section:
            spans[-1][2] = excel_row
        else:
            spans.append([section, excel_row, excel_row])
        rows.append(vals)

    for _sec, r1, r2 in spans:
        if r2 > r1:
            merges.append((r1, r2))

    # 컬럼별 최대값으로 정규화 — sheets.js 의 --yw 계산과 같은 기준.
    col_max = {}
    for _row, col, value in fail_values:
        col_max[col] = max(col_max.get(col, 0.0), abs(value))
    grad_cells = {}
    for row, col, value in fail_values:
        ratio = grad_ratio(value, col_max.get(col, 0.0))
        if ratio:
            grad_cells[(row, col)] = quantize_ratio(ratio)

    return {"header": header, "rows": rows, "grad_cells": grad_cells,
            "cpk_cells": cpk_cells, "status_cells": status_cells,
            "subhead_rows": subhead_rows, "merges": merges,
            "rows_meta": rows_meta, "map_rows": map_rows, "temp_rows": temp_rows,
            "dist_col": dist_col, "map_col": map_col}


def build_yield_grad_cells(rows, header, *, header_row=3, start_col=2):
    """Yield 표의 fail % 셀 그라데이션 — avg/{src} (%) 컬럼, Pass(Bin1) 행 제외.

    rows 는 _sheets.yield_header 순서( Step, Bin, TNO, Item, avg (%), …)의 값 배열.
    """
    bin_idx = header.index("Bin") if "Bin" in header else 1
    pct_cols = [i for i, name in enumerate(header)
                if name == "avg (%)" or str(name).endswith(" (%)")]
    values = []
    for r_off, row in enumerate(rows or []):
        if str(row[bin_idx] if bin_idx < len(row) else "").strip() == PASS_BIN:
            continue
        for i in pct_cols:
            num = _num(row[i]) if i < len(row) else None
            if num is not None and num > 0:
                values.append((header_row + 1 + r_off, start_col + i, num))
    col_max = {}
    for _row, col, value in values:
        col_max[col] = max(col_max.get(col, 0.0), abs(value))
    out = {}
    for row, col, value in values:
        ratio = grad_ratio(value, col_max.get(col, 0.0))
        if ratio:
            out[(row, col)] = quantize_ratio(ratio)
    return out


# ── Compare ─────────────────────────────────────────────────────────────────

# goodlog 컬럼 순서 — web_report/tabs/compare.py GOODLOG_HEADER 와 **동일 순서** 고정.
GOODLOG_HEADER = [
    "after_item_name", "after_lolimit", "after_hilimit", "after_unit", "after_value",
    "compare_item_name", "compare_lolimit", "compare_hilimit", "comment", "gap",
    "Before_item_name", "Before_lolimit", "Before_hilimit", "Before_unit", "Before_value",
]
GOODLOG_GAP_WARN_PCT = 10.0     # compare.js 와 같은 강조 기준


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bool_cell(value):
    """compare_* 컬럼은 True/False/None — Excel 에는 O/X/빈칸으로."""
    if value is True:
        return "O"
    if value is False:
        return "X"
    return ""


def build_compare_tables(compare):
    """Compare 탭 표 3종 → 시트 블록. 데이터가 없는 블록은 None.

    강조(marks)는 ``{(행index, 열index): "bad"|"good"|"warn"}`` — 색은 호출부가 정한다.
    산포 비교의 Distribution 미니셀(이미지)은 이번 범위 밖이라 열 자체를 만들지 않는다.
    """
    cmp_ = compare if isinstance(compare, dict) else {}
    if not cmp_:
        return {}
    return {
        "equivalence": _build_equivalence(cmp_.get("equivalence")),
        "dist_shift": _build_dist_shift(cmp_.get("dist_shift")),
        "goodlog": _build_goodlog(cmp_.get("goodlog")),
    }


def _build_equivalence(eq):
    if not isinstance(eq, dict) or not eq.get("rows"):
        return None
    th = eq.get("thresholds") or {}
    avg_limit = _num(th.get("avg_pct"))
    cpk_limit = _num(th.get("cpk"))
    summary = eq.get("summary") or {}
    header = ["STEP", "Item", "UNIT", "HiLIM", "LoLIM",
              "Before AVG", "Before STD", "Before CPK",
              "After AVG", "After STD", "After CPK", "AVG차", "AVG차(%)", "동일성"]
    rows, marks = [], {}
    for i, r in enumerate(eq.get("rows") or []):
        before = r.get("before") or {}
        after = r.get("after") or {}
        grade = r.get("grade")
        rows.append([r.get("step"), r.get("subject"), r.get("units"),
                     r.get("hilim"), r.get("lolim"),
                     before.get("average"), before.get("stdev"), before.get("cpk"),
                     after.get("average"), after.get("stdev"), after.get("cpk"),
                     r.get("delta_avg"), r.get("delta_pct"), f"Grade{grade}" if grade else ""])
        dpct, acpk = _num(r.get("delta_pct")), _num(after.get("cpk"))
        if avg_limit is not None and dpct is not None and abs(dpct) > avg_limit:
            marks[(i, 12)] = "bad"
        if cpk_limit is not None and acpk is not None and acpk < cpk_limit:
            marks[(i, 10)] = "warn"
        if grade == 3:
            marks[(i, 13)] = "bad"
    caption = (f"{eq.get('before')} → {eq.get('after')} · 전체 {summary.get('total', 0)}항목 "
               f"(Grade1 {summary.get('grade1', 0)} / Grade2 {summary.get('grade2', 0)} / "
               f"Grade3 {summary.get('grade3', 0)}) · 기준 AVG차 {th.get('avg_pct')}% · "
               f"CPK {th.get('cpk')}")
    return {"title": "동일성 검증", "header": header, "rows": rows, "marks": marks,
            "caption": caption}


def _build_dist_shift(ds):
    if not isinstance(ds, dict) or not ds.get("rows"):
        return None
    th = ds.get("thresholds") or {}
    cpk_low = _num(th.get("cpk_low"))
    sd_limit = _num(th.get("stdev_delta_pct"))
    header = ["Item", "Unit", "After Avg", "After Stdev", "After Cpk",
              "Before Avg", "Before Stdev", "Before Cpk",
              "MeanShift(σ)", "Cpk(%)", "Stdev증가율(%)", "Median Shift", "주목"]
    rows, marks = [], {}
    for i, r in enumerate(ds.get("rows") or []):
        after = r.get("after") or {}
        before = r.get("before") or {}
        rows.append([r.get("subject"), r.get("units"),
                     after.get("average"), after.get("stdev"), after.get("cpk"),
                     before.get("average"), before.get("stdev"), before.get("cpk"),
                     r.get("meanshift_sigma"), r.get("cpk_ratio_pct"),
                     r.get("stdev_delta_pct"), r.get("median_shift"),
                     "주목" if r.get("focus") else ""])
        acpk, sd = _num(after.get("cpk")), _num(r.get("stdev_delta_pct"))
        if cpk_low is not None and acpk is not None and acpk < cpk_low:
            marks[(i, 4)] = "warn"
        if sd_limit is not None and sd is not None and abs(sd) >= sd_limit:
            marks[(i, 10)] = "bad"
        if r.get("focus"):
            marks[(i, 12)] = "warn"
    summary = ds.get("summary") or {}
    caption = (f"{ds.get('before')} → {ds.get('after')} · 전체 {summary.get('total', 0)}항목 "
               f"중 주목 {summary.get('focus', 0)}항목 · 기준 Cpk<{th.get('cpk_low')} · "
               f"Stdev 증가 {th.get('stdev_delta_pct')}% 이상")
    return {"title": "산포 비교", "header": header, "rows": rows, "marks": marks,
            "caption": caption}


def _build_goodlog(gl):
    if not isinstance(gl, dict) or not gl.get("rows"):
        return None
    header = list(gl.get("header") or GOODLOG_HEADER)
    rows, marks = [], {}
    bool_cols = {header.index(c) for c in ("compare_item_name", "compare_lolimit",
                                           "compare_hilimit") if c in header}
    gap_col = header.index("gap") if "gap" in header else None
    for i, r in enumerate(gl.get("rows") or []):
        row = []
        for j, key in enumerate(header):
            value = r.get(key)
            if j in bool_cols:
                row.append(_bool_cell(value))
                if value is False:
                    marks[(i, j)] = "bad"
                elif value is True:
                    marks[(i, j)] = "good"
            else:
                row.append(value)
        if gap_col is not None:
            gap = _num(r.get("gap"))
            if gap is not None and abs(gap) >= GOODLOG_GAP_WARN_PCT:
                marks[(i, gap_col)] = "bad"
        rows.append(row)
    ident = gl.get("identical")
    caption = (f"{gl.get('before_source')} → {gl.get('after_source')} · "
               + ("항목/limit 전부 일치" if ident else "차이 있음 (O=일치, X=불일치)")
               + f" · gap {GOODLOG_GAP_WARN_PCT:g}% 이상 강조")
    return {"title": "Good Log 비교", "header": header, "rows": rows, "marks": marks,
            "caption": caption}
