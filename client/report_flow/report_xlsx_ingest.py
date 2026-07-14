"""report_generator 보고서 xlsx → web_report 세션화 전처리 (Excel COM).

report_generator 가 만든 보고서 xlsx 를 Excel COM(DRM 해제)으로 열어

  1. Raw Data 시트(원본 측정값)를 7-meta honeyform DataFrame 으로 복원하고,
  2. Summary / Issue_table 시트의 사용자 코멘트를 특정 셀에서 추출한다.

산출물은 기존 web_report 업로드 경로(uploader.post_webreport → /upload_webreport
→ ingest_webreport)로 그대로 전송돼 일반 web_report 세션과 동일한 세션을 만든다.
서버·web_report·report_generator 는 무변경이다.

Raw Data 시트 레이아웃(report_generator/_xlsx_writer.py `_copy_df_via_csv`,
`df.to_csv(index=False)` + Serial 열 제거):
  row0 = 헤더(DUT, XCoord, YCoord, Bin, item…)   ← Serial 없음
  row1 = Units / row2 = Lower Limit / row3 = Upper Limit
  row4,5 = Limit 중복 / row6+ = die 별 측정 데이터

df_honey(5-meta) → honeyform(7-meta) 매핑:
  DUT→DUT, XCoord→XPOS, YCoord→YPOS, Bin→BIN,  SERIAL/SHOT/FAILTNO=공란
  Units→UNIT, Upper Limit→HILIM, Lower Limit→LOLIM,  TSEQ/TNO/STEP=공란
FAILTNO 부재로 Yield fail-분해/Issue Table Yield 섹션은 비지만 CPK/Distribution/
Map/Pass 수율은 정확하다(측정값·BIN·좌표 복원). 향후 raw data 가 7-meta parquet
규약으로 오면 헤더 감지로 그대로 통과(완전 충실)한다.
"""
import traceback

import pandas as pd

from web_report.honeyform import (
    META_COLUMNS,
    encode_honeyform_parquet,
    validate_honeyform_df,
)

from .upload_prepare import _normalize_grid

# report_generator 표준 시트(= Raw Data 후보에서 제외). 나머지는 헤더로 raw 여부 판정.
_KNOWN_EXACT = {"summary", "yield", "cpk", "fail_item", "issue_table",
                "distribution", "histogram", "map"}

# 7-meta honeyform 헤더(pass-through 감지용)와 df_honey 5-meta 필수 메타 컬럼.
_HONEYFORM_META = ["serial", "shot", "dut", "xpos", "ypos", "bin", "failtno"]
_DF_HONEY_META = {"dut", "xcoord", "ycoord", "bin", "serial"}

# Issue_table 코멘트 4열 → web_report 2열(역할별 병합). dict 삽입 순서 = 병합 순서.
_ISSUE_COMMENT_SRC = {
    "comment": "PTE comment",
    "pte 2차 comment": "PTE comment",
    "개발 1차 comment": "개발 comment",
    "개발 2차 comment": "개발 comment",
}

# Summary '3. Evaluation Summary' Category → summary_engr 슬롯. Temp 는 슬롯 없어 스킵.
_SUMMARY_ENGR_MAP = {"yield": "yield", "cpk": "cpk", "etc": "etc"}


def _s(value) -> str:
    return "" if value is None else str(value).strip()


def _fmt_bin(value) -> str:
    """BIN 값을 row_key 용으로 정규화 ('5.0'→'5') — 서버 fmt_type 과 동일 규칙."""
    text = _s(value)
    try:
        f = float(text)
        if f.is_integer():
            return str(int(f))
    except (TypeError, ValueError):
        pass
    return text


def _is_known_non_raw(name: str) -> bool:
    n = _s(name).lower()
    if n in _KNOWN_EXACT:
        return True
    if n.startswith("정리") or n.startswith("goodlog"):
        return True
    if n.startswith("cpk_") or n.startswith("distribution_"):
        return True
    return False


def _is_raw_header(grid) -> bool:
    """grid 첫 행이 Raw Data(7-meta honeyform 또는 5-meta df_honey) 헤더인가."""
    if not grid or not grid[0]:
        return False
    hdr = [_s(c).lower() for c in grid[0]]
    if hdr[:7] == _HONEYFORM_META:
        return True
    lead = hdr[:6]
    return all(m in lead for m in ("dut", "xcoord", "ycoord", "bin"))


def _read_workbook_grids(src_path):
    """COM 으로 열어 (raw_sheets, summary_grid, issue_grid) 반환.

    raw_sheets = [(sheet_name, grid_2d), ...] (등장 순서), grid = UsedRange.Value 정규화.
    Issue_table 시트(대소문자 무시)가 없으면 Honey excel report 형식이 아니므로 ValueError.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    excel = None
    wb = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        wb = excel.Workbooks.Open(src_path, UpdateLinks=0, ReadOnly=True)

        names_low = {_s(sht.Name).lower() for sht in wb.Worksheets}
        if "issue_table" not in names_low:
            raise ValueError(
                "Honey excel report 형식이 아닙니다.\n"
                "(Issue_table 시트를 찾지 못했습니다)")

        raw_sheets = []
        summary_grid = None
        issue_grid = None
        for sht in wb.Worksheets:
            name = sht.Name
            nl = _s(name).lower()
            if nl == "summary":
                summary_grid = _normalize_grid(sht.UsedRange.Value)
            elif nl == "issue_table":
                issue_grid = _normalize_grid(sht.UsedRange.Value)
            elif _is_known_non_raw(name):
                continue
            else:
                grid = _normalize_grid(sht.UsedRange.Value)
                if _is_raw_header(grid):
                    raw_sheets.append((name, grid))
        return raw_sheets, summary_grid, issue_grid
    finally:
        try:
            if wb is not None:
                wb.Close(SaveChanges=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            if excel is not None:
                excel.Quit()
        except Exception:  # noqa: BLE001
            pass
        pythoncom.CoUninitialize()


# ── honeyform 검증 실패 메시지 (사용자용 한국어 + 형식 설명 + 불일치 위치) ──────
_FORMAT_HELP_HONEYFORM = (
    "[honeyform(Raw Data) 필수 형식]\n"
    "· 1행 헤더: SERIAL, SHOT, DUT, XPOS, YPOS, BIN, FAILTNO + 측정 항목 1개 이상\n"
    "· 2~7행 A열(순서 고정): TSEQ, TNO, STEP, UNIT, HILIM, LOLIM\n"
    "· 8행부터: die 별 측정 데이터 (최소 1행)\n"
    "· 측정 항목 컬럼 이름은 중복될 수 없습니다")

_FORMAT_HELP_RAWDATA = (
    "[Raw Data 시트 필수 형식]\n"
    "· 1행 헤더: DUT, XCoord, YCoord, Bin + 측정 항목 1개 이상\n"
    "· 2행 Units / 3행 Lower Limit / 4행 Upper Limit\n"
    "· 7행부터: die 별 측정 데이터 (최소 1행)\n"
    "· 측정 항목 컬럼 이름은 중복될 수 없습니다")


def _kor_issue(issue: str) -> str:
    """validate_honeyform_df 영문 이슈 1건 → 한국어 설명 (어디가 안 맞는지)."""
    def _after(sep):
        return issue.split(sep, 1)[1].strip() if sep in issue else ""

    if issue.startswith("first 7 columns must be"):
        return f"앞 7개 컬럼이 규격과 다릅니다 → 실제: {_after('got')}"
    if issue.startswith("metadata row labels must be"):
        return f"메타 행 레이블(A열)이 규격과 다릅니다 → 실제: {_after('got')}"
    if issue.startswith("duplicate item columns"):
        return f"측정 항목 컬럼 이름이 중복됩니다: {_after(':')}"
    if "item column are required" in issue:
        return "컬럼 부족: 앞 7개 메타 컬럼과 측정 항목 1개 이상이 필요합니다"
    if "metadata rows are required" in issue:
        return "메타데이터 행(TSEQ/TNO/STEP/UNIT/HILIM/LOLIM 6행)이 부족합니다"
    if "data row is required" in issue:
        return "측정 데이터가 최소 1행 필요합니다"
    return issue    # 알 수 없는 이슈는 원문 노출


def _honeyform_error(issues, *, converted: bool) -> ValueError:
    """honeyform 검증 이슈 목록 → 형식 설명 + 불일치 위치를 담은 ValueError."""
    lead = ("Raw Data → honeyform 변환 검증에 실패했습니다."
            if converted else "honeyform(Raw Data) 형식에 맞지 않습니다.")
    help_text = _FORMAT_HELP_RAWDATA if converted else _FORMAT_HELP_HONEYFORM
    where = "\n".join(f"· {_kor_issue(i)}" for i in issues)
    return ValueError(f"{lead}\n\n{help_text}\n\n[맞지 않는 부분]\n{where}")


def _sheet_to_honeyform(grid) -> pd.DataFrame:
    """Raw Data 시트 grid → 7-meta honeyform DataFrame (encode 가 재검증)."""
    hdr = [_s(c) for c in grid[0]]
    hdr_low = [h.lower() for h in hdr]

    # 7-meta honeyform 직접 덤프(미래 규약) — 그대로 통과.
    if hdr_low[:7] == _HONEYFORM_META:
        df = pd.DataFrame(grid[1:], columns=hdr)
        issues = validate_honeyform_df(df)
        if issues:
            raise _honeyform_error(issues, converted=False)
        return df

    # 5-meta df_honey 매핑. 메타 컬럼은 이름으로 위치를 찾는다(순서 견고성).
    def _col(name):
        try:
            return hdr_low.index(name)
        except ValueError:
            return None

    dut_i, x_i, y_i, bin_i = _col("dut"), _col("xcoord"), _col("ycoord"), _col("bin")
    meta_idx = {i for i in (dut_i, x_i, y_i, bin_i, _col("serial")) if i is not None}
    item_j = [j for j in range(len(hdr)) if j not in meta_idx]
    items = [hdr[j] for j in item_j]
    if not items:
        raise ValueError("Raw Data 시트에 측정 항목 컬럼이 없습니다")

    def _meta_row_vals(gi):
        row = grid[gi] if gi < len(grid) else []
        return [row[j] if j < len(row) else None for j in item_j]

    units = _meta_row_vals(1)   # Excel row2 = df_honey Units
    lolim = _meta_row_vals(2)   # Excel row3 = Lower Limit
    hilim = _meta_row_vals(3)   # Excel row4 = Upper Limit

    n = len(items)
    _pad = [None] * 6           # SHOT..FAILTNO 메타셀(검증·계산 미사용)
    meta_rows = [
        ["TSEQ"] + _pad + [None] * n,
        ["TNO"] + _pad + [None] * n,
        ["STEP"] + _pad + [None] * n,
        ["UNIT"] + _pad + units,
        ["HILIM"] + _pad + hilim,
        ["LOLIM"] + _pad + lolim,
    ]

    data_rows = []
    for row in grid[6:]:
        if row is None or all(c is None for c in row):
            continue

        def _g(j, r=row):
            return r[j] if (j is not None and j < len(r)) else None

        data_rows.append(
            [None, None, _g(dut_i), _g(x_i), _g(y_i), _g(bin_i), None]
            + [_g(j) for j in item_j])
    if not data_rows:
        raise ValueError("Raw Data 시트에 측정 데이터 행이 없습니다")

    df = pd.DataFrame(meta_rows + data_rows, columns=list(META_COLUMNS) + items)
    issues = validate_honeyform_df(df)
    if issues:
        raise _honeyform_error(issues, converted=True)
    return df


def _extract_summary_engr(grid) -> dict:
    """Summary '3. Evaluation Summary' 의 Result 열(Yield/CPK/ETC) → summary_engr."""
    if not grid:
        return {}
    cat_j = res_j = None
    start = None
    for i, row in enumerate(grid):
        low = [_s(c).lower() for c in row]
        if "category" in low and "result" in low:
            cat_j, res_j, start = low.index("category"), low.index("result"), i + 1
            break
    if start is None:
        return {}
    out = {}
    for row in grid[start:]:
        cat = _s(row[cat_j]).lower() if cat_j < len(row) else ""
        key = _SUMMARY_ENGR_MAP.get(cat)
        if not key:
            continue
        val = _s(row[res_j]) if res_j < len(row) else ""
        if val and val != "-":      # '-' 은 report_generator placeholder
            out[key] = val
    return out


def _extract_issue_comments(grid):
    """Issue_table 코멘트 → (issue_comments{row_key:{col:val}}, etc_items[])."""
    if not grid:
        return {}, []
    header_i = None
    col = {}
    for i, row in enumerate(grid[:15]):
        low = [_s(c).lower() for c in row]
        if "category" in low and "item" in low:
            header_i = i
            col = {name: j for j, name in enumerate(low)}
            break
    if header_i is None:
        return {}, []

    cat_j, item_j, bin_j = col.get("category"), col.get("item"), col.get("bin")
    src_cols = {name: col[name] for name in _ISSUE_COMMENT_SRC if name in col}
    if item_j is None or not src_cols:
        return {}, []

    issue_comments = {}
    etc_items = []
    section = None
    for row in grid[header_i + 1:]:
        def _c(j, r=row):
            return r[j] if (j is not None and j < len(r)) else None

        cat = _s(_c(cat_j)).lower()
        if cat in ("yield", "cpk", "etc"):
            section = cat
        item = _s(_c(item_j))
        if not item or item.lower() == "item name":   # 빈 행·CPK subhead 스킵
            continue

        if section == "yield":
            row_key = f"Yield|{_fmt_bin(_c(bin_j))}|{item}"
        elif section == "cpk":
            row_key = f"CPK|{item}"
        elif section == "etc":
            row_key = f"ETC|{item}"
            if item not in etc_items:
                etc_items.append(item)
        else:
            continue

        merged = {}
        for src_name, target in _ISSUE_COMMENT_SRC.items():
            text = _s(_c(src_cols.get(src_name)))
            if text:
                merged.setdefault(target, []).append(text)
        cols_out = {target: "\n".join(parts) for target, parts in merged.items()}
        if cols_out:
            issue_comments[row_key] = cols_out
    return issue_comments, etc_items


def prepare_report_webreport(src_path: str):
    """report_generator 보고서 xlsx → (sources, parquet_items, seed, all_items).

    sources       = [{index, name, file_name}]        (manifest.sources)
    parquet_items = [{index, name, file_name, data}]   (uploader.post_webreport)
    seed          = {issue_comments?, etc_items?, summary_engr?}  (manifest 편집 시드)
    all_items     = 측정 항목 합집합(등장 순서)          (manifest.selected_items)

    Raw Data 시트 없음/형식 오류/COM 실패 시 안내 ValueError.
    """
    try:
        raw_sheets, summary_grid, issue_grid = _read_workbook_grids(src_path)
    except ValueError:
        raise
    except Exception:  # noqa: BLE001
        raise ValueError(
            "선택한 파일을 처리할 수 없습니다.\n"
            "Excel COM 으로 시트 데이터를 추출하지 못했습니다.\n"
            "DRM(NASCA) 파일은 Excel 이 설치된 PC 에서만 업로드할 수 있습니다.\n\n"
            f"[Excel 처리 실패 원인]\n{traceback.format_exc()}")

    if not raw_sheets:
        raise ValueError(
            "Raw Data 시트를 찾지 못했습니다.\n"
            "보고서 생성 시 'Raw Data' 옵션을 켜고 다시 만든 파일을 올려 주세요.")

    sources = []
    parquet_items = []
    all_items = []
    seen = set()
    for idx, (name, grid) in enumerate(raw_sheets):
        df = _sheet_to_honeyform(grid)
        parquet_items.append({
            "index": idx, "name": name,
            "file_name": f"{name}.parquet", "data": encode_honeyform_parquet(df),
        })
        sources.append({"index": idx, "name": name, "file_name": name})
        for item in list(df.columns[len(META_COLUMNS):]):
            if item not in seen:
                seen.add(item)
                all_items.append(item)

    seed = {}
    engr = _extract_summary_engr(summary_grid)
    if engr:
        seed["summary_engr"] = engr
    issue_comments, etc_items = _extract_issue_comments(issue_grid)
    if issue_comments:
        seed["issue_comments"] = issue_comments
    if etc_items:
        seed["etc_items"] = etc_items

    return sources, parquet_items, seed, all_items
