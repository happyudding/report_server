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
