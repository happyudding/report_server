"""Honey 가 업로드한 .xlsx 에서 summary / yield / issue_table 추출.

클라이언트 report generator 산출물 레이아웃 전제(실측 기준):
  - 표는 A열을 비우고 B열부터 시작, 제목 배너 A1, 표 헤더 3행, 데이터 4행~.
  - summary 는 B4="DEVICE"(Device Feature 헤더) + "2. Yield" + "Major Fail Bins" 블록.
  - yield 는 B3="bin" 헤더행, 데이터 4행~.
  - issue_table 은 B3="Category" / C3="Bin" 헤더행, I열=Distribution(행별 PNG).

두 가지 산출물을 함께 만든다:
  1) 의미 단위 dict (summary / yield_rows / issue_rows) — 검색/요약/DB 매핑용(하위호환).
  2) grid model (grids) — 표를 "원형에 가깝게"(열너비·행높이·폰트크기·정렬·구조)
     재현하기 위한 셀 격자. 앵커 셀(B4/B3/C3)을 우선 인덱스로, 실패 시 키워드로 탐색.

견고성: 셀 좌표 하드코딩 대신 앵커 텍스트(지정 셀 우선 → 전체 스캔)와 헤더 행 매칭을 우선한다.
"""
from io import BytesIO

# Issue_table 행별 임베드 PNG 추출 활성화 플래그. 골격 단계에선 False(빈 리스트).
# 외부 프로젝트 브랜치 시 True 로 바꾸면 upload_xlsx 의 조건부 훅이 동작한다.
ISSUE_IMAGES_ENABLED = False


def parse_report_xlsx(xlsx_bytes: bytes) -> dict:
    """xlsx 바이트열을 받아 의미 dict + grid model + 이미지 훅 결과를 반환."""
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("openpyxl not installed; pip install openpyxl") from exc

    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), data_only=True, read_only=False)

    summary = _extract_summary_sheet(_get_sheet(wb, "summary"))
    yield_rows = _rows_with_header(_get_sheet(wb, "yield"))
    issue_rows = [
        r for r in _rows_with_header(_get_sheet(wb, "issue_table"),
                                     drop_cols={"Distribution"})
        # Category 그룹의 CPK/ETC 플레이스홀더(빈 bin) 행 제외 — Yield 블록만 의미
        if _stringify(r.get("bin")) != ""
    ]

    grids, issue_hdr = _extract_grids(wb)
    issue_ws = _get_sheet(wb, "issue_table")
    issue_images = _extract_issue_images(issue_ws, issue_hdr,
                                         enabled=ISSUE_IMAGES_ENABLED)

    return {
        "summary": summary,
        "yield_rows": yield_rows,
        "issue_rows": issue_rows,
        "grids": grids,
        "issue_images": issue_images,
    }


# ── grid model 추출 (원형 재현용) ─────────────────────────────────────────────

def _extract_grids(wb):
    """시트별 grid model 생성. 반환 (grids dict, issue_table 헤더행 or None).

    - summary : 시트 사용 영역 전체(Device Feature + 2.Yield + Major Fail Bins).
    - yield   : B3="bin" 헤더행부터 빈 행 직전까지의 표.
    - issue_table : C3="Bin" 헤더행부터 빈 행 직전까지의 표.
    앵커 탐색 실패 시 키워드 전체 스캔 → 그래도 없으면 헤더행 추정으로 폴백.
    """
    grids = {}
    issue_hdr = None

    sm = _get_sheet(wb, "summary")
    if sm is not None:
        a = _find_anchor(sm, "DEVICE", "B4")
        reg = _used_region(sm)
        if reg:
            grids["summary"] = _build_grid(sm, reg, _anchor_info(a))

    yl = _get_sheet(wb, "yield")
    if yl is not None:
        a = _find_anchor(yl, "bin", "B3")
        hdr = a[0] if a else _guess_header_row(yl)
        reg = _table_region(yl, hdr)
        if reg:
            grids["yield"] = _build_grid(yl, reg, _anchor_info(a))

    it = _get_sheet(wb, "issue_table")
    if it is not None:
        a = _find_anchor(it, "bin", "C3")
        issue_hdr = a[0] if a else _guess_header_row(it)
        reg = _table_region(it, issue_hdr)
        if reg:
            grids["issue_table"] = _build_grid(it, reg, _anchor_info(a))

    return grids, issue_hdr


def _find_anchor(ws, text, expect_cell):
    """앵커 텍스트 위치 (row, col, by) 탐색. 1-based. by∈{position,keyword}.

    1) 지정 셀(expect_cell, 예: 'B4')이 text 와 대소문자 무시 일치하면 그 위치.
    2) 아니면 시트 전체를 스캔해 첫 일치 셀.
    3) 못 찾으면 None.
    """
    target = text.strip().lower()
    try:
        cell = ws[expect_cell]
        if _stringify(cell.value).lower() == target:
            return (cell.row, cell.column, "position")
    except (KeyError, ValueError, AttributeError):
        pass
    for row in ws.iter_rows():
        for cell in row:
            if _stringify(cell.value).lower() == target:
                return (cell.row, cell.column, "keyword")
    return None


def _anchor_info(a):
    if not a:
        return {"cell": None, "by": "none"}
    from openpyxl.utils import get_column_letter
    return {"cell": f"{get_column_letter(a[1])}{a[0]}", "by": a[2]}


def _guess_header_row(ws):
    """비어있지 않은 셀이 2개 이상인 첫 행(1-based) — 배너(셀1개) 건너뜀."""
    for row in ws.iter_rows(min_row=1, max_row=min(10, ws.max_row or 1)):
        if sum(1 for c in row if _stringify(c.value) != "") >= 2:
            return row[0].row
    return 1


def _table_region(ws, header_row):
    """헤더행 기준 (r0,c0,r1,c1). 열=헤더행 비어있지 않은 최소~최대 col,
    행=헤더행부터 [c0..c1] 전부 빈 행 직전까지."""
    if not header_row:
        return None
    cols = [c.column for c in ws[header_row] if _stringify(c.value) != ""]
    if not cols:
        return None
    c0, c1 = min(cols), max(cols)
    r1 = header_row
    r = header_row + 1
    last = ws.max_row or header_row
    while r <= last:
        if any(_stringify(ws.cell(r, c).value) != "" for c in range(c0, c1 + 1)):
            r1 = r
            r += 1
        else:
            break
    return (header_row, c0, r1, c1)


def _used_region(ws):
    """시트의 비어있지 않은 셀 경계 (r0,c0,r1,c1). 없으면 None."""
    min_r = min_c = None
    max_r = max_c = 0
    for row in ws.iter_rows():
        for cell in row:
            if _stringify(cell.value) == "":
                continue
            r, c = cell.row, cell.column
            if min_r is None or r < min_r:
                min_r = r
            if min_c is None or c < min_c:
                min_c = c
            max_r = max(max_r, r)
            max_c = max(max_c, c)
    if min_r is None:
        return None
    return (min_r, min_c, max_r, max_c)


def _build_grid(ws, region, anchor_info):
    """경계 사각형을 순회해 grid model dict 생성 (구조 + 상대크기).
    fill/border/merge 는 저장하지 않음(결정: '구조+상대크기만')."""
    from openpyxl.utils import get_column_letter
    r0, c0, r1, c1 = region
    cd = ws.column_dimensions
    rd = ws.row_dimensions

    cols = []
    for c in range(c0, c1 + 1):
        letter = get_column_letter(c)
        w = cd[letter].width if letter in cd else None
        cols.append({"w": (round(float(w), 2) if w else None)})

    rows = []
    cells = []
    for r in range(r0, r1 + 1):
        h = rd[r].height if r in rd else None
        rows.append({"h": (round(float(h), 2) if h else None)})
        cells.append([_cell_style(ws.cell(r, c)) for c in range(c0, c1 + 1)])

    return {"anchor": anchor_info, "cols": cols, "rows": rows, "cells": cells}


def _cell_style(cell):
    """셀 1개 → {t[, sz, b, a]}. 빈 셀은 {'t': ''}."""
    d = {"t": _grid_text(cell.value)}
    f = cell.font
    if f is not None and f.size:
        d["sz"] = round(float(f.size), 1)
    if f is not None and f.bold:
        d["b"] = True
    al = cell.alignment
    if al is not None and al.horizontal:
        d["a"] = al.horizontal
    return d


def _grid_text(v):
    """grid 셀 표시용 문자열. 정수형 float 는 .0 제거."""
    v = _normalize(v)
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else str(v)
    return str(v)


# ── Issue_table 행별 임베드 PNG 추출 (골격) ──────────────────────────────────

def _extract_issue_images(ws, header_row, enabled=False):
    """Issue_table 의 행별 임베드 PNG 추출. 골격 단계(enabled=False)에선 빈 리스트.

    이미지 개수는 가변 — `ws._images` 에 존재하는 만큼 0..N (하드코딩 금지).
    매핑: 이미지 앵커의 0-based row(`anchor._from.row`)를 1-based 셀행으로 바꾼 뒤
    header_row 기준 오프셋(헤더=0, 첫 데이터행=1, ...)으로 grid 행 index 화.
    """
    out = []
    if not enabled or ws is None or not header_row:
        return out
    for img in (getattr(ws, "_images", None) or []):
        try:
            frm = img.anchor._from           # 0-based
            grid_row = (int(frm.row) + 1) - header_row
            ref = img.ref
            data = ref.getvalue() if hasattr(ref, "getvalue") else ref
            if data:
                out.append({"row": grid_row, "png": bytes(data)})
        except (AttributeError, TypeError, ValueError):
            continue
    return out


def _get_sheet(wb, name):
    """시트 이름이 정확히 일치하지 않을 때 대소문자/공백 무시 fallback."""
    if name in wb.sheetnames:
        return wb[name]
    key = name.strip().lower()
    for s in wb.sheetnames:
        if s.strip().lower() == key:
            return wb[s]
    return None


# ── summary ──────────────────────────────────────────────────────────────────

def _extract_summary_sheet(ws) -> dict:
    """summary 시트에서 Device Feature / Yield / Major Fail / Evaluation 추출."""
    if ws is None:
        return {}
    out = {}
    rows = list(ws.iter_rows(values_only=True))
    out["title"] = _stringify(rows[0][0]) if rows and rows[0] else ""

    anchors = _find_anchors_2d(rows, {"1. Device Feature", "2. Yield",
                                      "Major Fail Bins", "3. Evaluation Summary"})

    if "1. Device Feature" in anchors:
        r, _c = anchors["1. Device Feature"]
        out["feature"] = _read_kv_2rows(rows, r + 1, r + 2)

    if "2. Yield" in anchors:
        r, _c = anchors["2. Yield"]
        out["yield_summary"] = _read_kv_2rows(rows, r + 1, r + 2,
                                              only_keys={"Lot NO", "Yield"})

    if "Major Fail Bins" in anchors:
        r, c = anchors["Major Fail Bins"]
        out["major_fail_bins"] = _read_major_fail(rows, r + 1, c)

    if "3. Evaluation Summary" in anchors:
        r, c = anchors["3. Evaluation Summary"]
        out["evaluation"] = _read_table_2d(rows, r + 1, c)

    if not any(k in out for k in ("feature", "yield_summary", "major_fail_bins", "evaluation")):
        out["raw_rows"] = [
            [_stringify(x) for x in row if x is not None]
            for row in rows if any(x is not None for x in row)
        ]
    return out


def _find_anchors_2d(rows, names):
    """행 전체(모든 열)에서 anchor 텍스트의 (row_idx, col_idx) 위치 탐색."""
    result = {}
    for i, row in enumerate(rows):
        if not row:
            continue
        for j, cell in enumerate(row):
            val = _stringify(cell)
            if not val:
                continue
            for name in names:
                if name in result:
                    continue
                if val == name or val.startswith(name):
                    result[name] = (i, j)
    return result


def _read_kv_2rows(rows, hdr_i, val_i, only_keys=None):
    """헤더행/값행을 같은 열끼리 묶어 dict (빈 헤더 칸은 무시)."""
    if hdr_i >= len(rows) or val_i >= len(rows):
        return {}
    header = [_stringify(c) for c in (rows[hdr_i] or ())]
    values = list(rows[val_i] or ())
    out = {}
    for k, v in zip(header, values):
        if not k:
            continue
        if only_keys is not None and k not in only_keys:
            continue
        out[k] = _normalize(v)
    return out


def _read_major_fail(rows, start_i, lc, max_n=10):
    """Major Fail Bins: 라벨열(lc)=1st~5th Fail, lc+1=subject, lc+2=ratio."""
    out = []
    for i in range(start_i, min(start_i + max_n, len(rows))):
        row = rows[i]
        if not row or lc >= len(row):
            break
        label = _stringify(row[lc])
        if not label:
            break
        out.append({
            "rank": label,
            "subject": _normalize(row[lc + 1]) if lc + 1 < len(row) else None,
            "ratio": _normalize(row[lc + 2]) if lc + 2 < len(row) else None,
        })
    return out


def _read_table_2d(rows, hdr_i, c, max_n=20):
    """hdr_i 행의 c열~ 을 헤더로, 그 아래 행들을 list[dict] 로 (빈 행에서 종료)."""
    if hdr_i >= len(rows):
        return []
    header = [_stringify(x) for x in (rows[hdr_i] or ())[c:]]
    out = []
    for i in range(hdr_i + 1, min(hdr_i + 1 + max_n, len(rows))):
        row = rows[i]
        cells = list((row or ())[c:])
        if not cells or all(x is None or _stringify(x) == "" for x in cells):
            break
        d = {h: _normalize(v) for h, v in zip(header, cells) if h}
        if d:
            out.append(d)
    return out


# ── yield / issue_table (헤더 행 기반) ───────────────────────────────────────

def _rows_with_header(ws, drop_cols=None) -> list:
    """헤더 행을 찾아 그 아래 행들을 list[dict] 로 변환.

    제목 배너(가로 병합된 단일 셀 행) 가 맨 위에 있을 수 있으므로, 1행 고정 대신
    '비어있지 않은 셀이 2개 이상인 첫 행' 을 헤더로 잡는다 (배너는 셀 1개라 건너뜀).
    drop_cols: 무시할 헤더명 집합 (예: 이미지가 들어있는 'Distribution' 컬럼).
    """
    if ws is None:
        return []
    drop = set(drop_cols or ())
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    hdr_i = next(
        (i for i, row in enumerate(rows[:10])
         if row and sum(1 for c in row if _stringify(c) != "") >= 2),
        0,
    )
    header = [_stringify(c) for c in rows[hdr_i]]
    keep_idx = [i for i, h in enumerate(header) if h and h not in drop]
    out = []
    for row in rows[hdr_i + 1:]:
        if not row or all(c is None for c in row):
            continue
        d = {}
        for i in keep_idx:
            if i < len(row):
                d[header[i]] = _normalize(row[i])
        if d:
            out.append(d)
    return out


def _stringify(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _normalize(v):
    """primitive 만 유지 (datetime → ISO 문자열)."""
    if v is None:
        return None
    if isinstance(v, (int, float, str, bool)):
        return v
    try:
        return v.isoformat()
    except AttributeError:
        return str(v)
