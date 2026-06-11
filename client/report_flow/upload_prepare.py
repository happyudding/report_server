"""Upload preprocessing boundary for Honey.

This module decrypts the DRM/xlsx via Excel COM and extracts the cell values of
the report sheets (summary / yield / issue_table) into JSON-serializable grids,
plus the issue_table row images.  No file is rebuilt or uploaded — only the
extracted text grids and PNGs are sent to the server.
"""
import datetime
import time
import traceback

# 서버 파서가 사용하는 시트(텍스트 추출 대상). 그 외 시트(distribution 등)는 보내지 않는다.
_TARGET_SHEETS = {"summary", "yield", "issue_table"}


def _png_from_com_shape(shape):
    """Extract PNG bytes from an Excel COM Shape via clipboard."""
    import win32clipboard
    fmt = win32clipboard.RegisterClipboardFormat("PNG")
    for _ in range(2):
        try:
            shape.Copy()
            time.sleep(0.05)
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(fmt):
                    data = win32clipboard.GetClipboardData(fmt)
                    if data:
                        return bytes(data)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:  # noqa: BLE001
            pass
    return None


def _normalize_grid(value):
    """Normalize win32com Range.Value to a JSON-safe 2D list.

    날짜/시간 셀은 ISO 문자열로, 그 외 비-primitive 는 str() 로 변환한다(JSON 전송용).
    """
    def _conv(v):
        if v is None or isinstance(v, (int, float, str, bool)):
            return v
        if hasattr(v, "year"):                       # pywintypes datetime 류
            try:
                return datetime.datetime(
                    int(v.year), int(v.month), int(v.day),
                    int(v.hour), int(v.minute), int(v.second)).isoformat()
            except Exception:  # noqa: BLE001
                return str(v)
        return str(v)

    if not isinstance(value, (tuple, list)):
        return [[_conv(value)]]
    if not value or not isinstance(value[0], (tuple, list)):
        return [[_conv(x) for x in value]]
    return [[_conv(x) for x in row] for row in value]


def _extract_via_excel_com(src_path, header_row=3):
    """Excel COM 으로 열어 대상 시트 grid + issue_table 행 이미지 추출.

    Returns ``(sheet_grids, issue_imgs)`` —
      sheet_grids = {"summary": {"origin":[r0,c0], "values":[[...]]}, ...}
      issue_imgs  = [{"row": int, "png": bytes}]  (0-based 데이터행)
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

        sheet_grids = {}
        issue_imgs = []
        for sht in wb.Worksheets:
            low = sht.Name.lower()
            if low not in _TARGET_SHEETS:
                continue
            ur = sht.UsedRange
            r0, c0 = int(ur.Row), int(ur.Column)
            values = _normalize_grid(ur.Value)
            sheet_grids[low] = {"origin": [r0, c0], "values": values}
            if low == "issue_table":
                for shape in sht.Shapes:
                    ri = int(shape.TopLeftCell.Row) - (header_row + 1)
                    if ri < 0:
                        continue
                    png = _png_from_com_shape(shape)
                    if png:
                        issue_imgs.append({"row": ri, "png": png})
        return sheet_grids, sorted(issue_imgs, key=lambda x: x["row"])
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


def fill_device_if_empty(sheet_grids: dict, product: str) -> None:
    """summary 시트의 Device 값이 비어있으면 선택한 product(part_id)로 채운다 (in-place).

    서버 파서와 동일하게 'DEVICE' 헤더 셀 바로 아래 셀을 Device 값으로 본다.
    summary 없음 / DEVICE 미발견 / 아래 행 없음 / product 빈값 / 아래셀에 이미 값 있음
    → 아무것도 하지 않는다(원본 유지).
    """
    if not product:
        return
    summary = (sheet_grids or {}).get("summary")
    if not summary:
        return
    values = summary.get("values")
    if not isinstance(values, list):
        return
    for i, row in enumerate(values):
        if not isinstance(row, (list, tuple)):
            continue
        for j, cell in enumerate(row):
            if str(cell).strip().upper() != "DEVICE":
                continue
            below = i + 1
            if below >= len(values):
                return
            target_row = values[below]
            if not isinstance(target_row, list) or j >= len(target_row):
                return
            cur = target_row[j]
            if cur is None or str(cur).strip() == "":
                target_row[j] = product
            return


def _stringify(v) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bin_int(v):
    f = _to_float(v)
    if f is None or not f.is_integer():
        return None
    return int(f)


def _ci_get(d, key):
    key = key.lower()
    for k, v in d.items():
        if k.lower() == key:
            return v
    return None


def _ordinal_fail_label(index):
    suffix = "th"
    if index == 1:
        suffix = "st"
    elif index == 2:
        suffix = "nd"
    elif index == 3:
        suffix = "rd"
    return f"{index}{suffix} Fail"


def _grid_rows_with_header(values):
    """2D grid → (header, rows). 첫 비어있지 않은 셀 2개 이상인 행을 헤더로 사용."""
    if not values:
        return [], []
    hdr_i = 0
    for i, row in enumerate(values[:10]):
        if sum(1 for c in row if _stringify(c) != "") >= 2:
            hdr_i = i
            break
    header = [_stringify(c) for c in values[hdr_i]]
    rows = []
    for row in values[hdr_i + 1:]:
        if not row or all(c is None for c in row):
            continue
        d = {}
        for i, h in enumerate(header):
            if h and i < len(row):
                d[h] = row[i]
        if d:
            rows.append(d)
    return header, rows


def _yield_pass_avg_and_fails(yield_values):
    """yield grid → (Bin=1 행의 avg, [(fail Item, avg), ...] avg 내림차순 상위 5)."""
    _, rows = _grid_rows_with_header(yield_values)
    pass_avg = None
    fails = []
    for r in rows:
        bin_val = _bin_int(_ci_get(r, "bin"))
        if bin_val is None:
            continue
        avg = _to_float(_ci_get(r, "avg"))
        if bin_val == 1:
            if pass_avg is None and avg is not None:
                pass_avg = avg
        elif avg is not None:
            item = _stringify(_ci_get(r, "item"))
            if item:
                fails.append((item, avg))
    fails.sort(key=lambda x: x[1], reverse=True)
    return pass_avg, fails[:5]


def _find_yield_section(summary_values):
    """summary grid 에서 'Lot NO'+'Yield' 헤더 행 탐색.

    반환: (hdr_i, lot_col, yield_col, mf_col) 0-based, 없으면 None.
    """
    for i, row in enumerate(summary_values):
        texts = [_stringify(c).lower() for c in row]
        if "lot no" in texts and "yield" in texts:
            lot_col = texts.index("lot no")
            yield_col = texts.index("yield")
            mf_col = texts.index("major fail bins") if "major fail bins" in texts else yield_col + 1
            return i, lot_col, yield_col, mf_col
    return None


def _build_yield_only_summary(lot_id, pass_avg, fails):
    """summary 가 없을 때 — '2. Yield' 섹션(B7:H13)만 담은 13x8 grid (origin [1,1])."""
    rows = [[None] * 8 for _ in range(13)]
    rows[6][1] = "2. Yield"          # B7
    rows[7][1] = "Lot NO"            # B8
    rows[7][3] = "Yield"             # D8
    rows[7][4] = "Major Fail Bins"   # E8
    rows[7][7] = "Comment"           # H8
    rows[8][1] = lot_id or "-"       # B9
    rows[8][3] = pass_avg            # D9
    for i in range(5):
        r = 8 + i  # 데이터 행 9~13 (idx 8~12)
        rows[r][4] = _ordinal_fail_label(i + 1)  # E
        if i < len(fails):
            rows[r][5] = fails[i][0]  # F: subject
            rows[r][6] = fails[i][1]  # G: percent
    return rows


def _patch_yield_block(summary_values, hdr_i, lot_col, yield_col, mf_col, lot_id, pass_avg, fails):
    """기존 summary grid 의 '2. Yield' 데이터 행(B9~G13 상당)만 덮어쓴다 (in-place)."""
    needed_rows = hdr_i + 6  # hdr_i+1 ~ hdr_i+5 (데이터 5행)
    while len(summary_values) < needed_rows:
        summary_values.append([])
    needed_cols = max(yield_col, mf_col + 2) + 1
    for row in summary_values:
        while len(row) < needed_cols:
            row.append(None)

    data_i = hdr_i + 1
    summary_values[data_i][lot_col] = lot_id or "-"
    summary_values[data_i][yield_col] = pass_avg
    for i in range(5):
        r = data_i + i
        summary_values[r][mf_col] = _ordinal_fail_label(i + 1)
        if i < len(fails):
            summary_values[r][mf_col + 1] = fails[i][0]
            summary_values[r][mf_col + 2] = fails[i][1]


def ensure_summary_yield(sheet_grids: dict, lot_id: str = "") -> None:
    """summary 의 '2. Yield' 섹션이 없거나 Yield 값이 비었으면 yield grid 로 보강 (in-place).

    report_generator/_xlsx_sheets.py:_fill_summary 의 '2. Yield' 채움 로직(Bin=1 행의
    avg → Yield 값, 나머지 fail 행 avg 상위 5 → Major Fail Bins)을 yield 시트 grid
    기준으로 재현한다.

    summary 가 이미 Yield 값을 가지고 있거나, yield grid 에 Bin=1 행이 없으면
    아무것도 하지 않는다(원본 유지).
    """
    yield_values = ((sheet_grids or {}).get("yield") or {}).get("values")
    if not yield_values:
        return

    pass_avg, fails = _yield_pass_avg_and_fails(yield_values)
    if pass_avg is None:
        return

    summary = sheet_grids.get("summary")
    summary_values = (summary or {}).get("values") or []
    section = _find_yield_section(summary_values)

    if section is not None:
        hdr_i, lot_col, yield_col, mf_col = section
        cur = None
        if hdr_i + 1 < len(summary_values) and yield_col < len(summary_values[hdr_i + 1]):
            cur = summary_values[hdr_i + 1][yield_col]
        if _stringify(cur) not in ("", "-"):
            return
        _patch_yield_block(summary_values, hdr_i, lot_col, yield_col, mf_col, lot_id, pass_avg, fails)
    else:
        sheet_grids["summary"] = {"origin": [1, 1], "values": _build_yield_only_summary(lot_id, pass_avg, fails)}


def prepare_upload_xlsx(src_path: str) -> tuple:
    """업로드용 추출 데이터 준비.

    Returns ``(sheet_grids, issue_imgs)``.  Excel COM 추출 실패 시 안내 ValueError.
    """
    try:
        return _extract_via_excel_com(src_path)
    except Exception:  # noqa: BLE001
        com_error = traceback.format_exc()
    raise ValueError(
        "선택한 파일을 처리할 수 없습니다.\n"
        "Excel COM 으로 시트 데이터를 추출하지 못했습니다.\n"
        "DRM(NASCA) 파일은 Excel 이 설치된 PC 에서만 업로드할 수 있습니다.\n\n"
        f"[Excel 처리 실패 원인]\n{com_error}")
