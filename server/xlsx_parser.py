"""Honey 가 업로드한 .xlsx 에서 summary / yield / issue_table 텍스트만 추출.

xlsx_export.py (legacy) 의 시트 구조 (summary/yield/cpk/fail_data/fail_values/
issue_table/distribution/histogram) 중 텍스트가 의미있는 3시트만 파싱.

견고성: 셀 좌표 하드코딩 대신 anchor 텍스트(A열)와 헤더 행 매칭을 우선한다.
"""
from io import BytesIO


def parse_report_xlsx(xlsx_bytes: bytes) -> dict:
    """xlsx 바이트열을 받아 {'summary', 'yield_rows', 'issue_rows'} dict 반환."""
    try:
        import openpyxl
    except ImportError as exc:
        raise RuntimeError("openpyxl not installed; pip install openpyxl") from exc

    wb = openpyxl.load_workbook(BytesIO(xlsx_bytes), data_only=True, read_only=False)

    summary = _extract_summary_sheet(_get_sheet(wb, "summary"))
    yield_rows = _rows_with_header(_get_sheet(wb, "yield"))
    issue_rows = _rows_with_header(_get_sheet(wb, "issue_table"),
                                   drop_cols={"Distribution"})

    return {
        "summary": summary,
        "yield_rows": yield_rows,
        "issue_rows": issue_rows,
    }


def _get_sheet(wb, name):
    """시트 이름이 정확히 일치하지 않을 때 대소문자/공백 무시 fallback."""
    if name in wb.sheetnames:
        return wb[name]
    key = name.strip().lower()
    for s in wb.sheetnames:
        if s.strip().lower() == key:
            return wb[s]
    return None


def _extract_summary_sheet(ws) -> dict:
    """summary 시트에서 Feature / Yield Summary / Evaluation Summary 섹션을 dict 로."""
    if ws is None:
        return {}
    out = {}
    rows = list(ws.iter_rows(values_only=True))

    out["title"] = _stringify(rows[0][0]) if rows and rows[0] else ""

    anchors = _find_anchors(rows, {"Feature", "Yield Summary",
                                   "Major Fail Bins", "Evaluation Summary"})

    if "Feature" in anchors:
        out["feature"] = _read_section(rows, anchors["Feature"])
    if "Yield Summary" in anchors:
        out["yield_summary_text"] = _read_text_lines(rows, anchors["Yield Summary"], max_lines=8)
    if "Major Fail Bins" in anchors:
        out["major_fail_bins"] = _read_table_section(rows, anchors["Major Fail Bins"])
    if "Evaluation Summary" in anchors:
        out["evaluation"] = _read_section(rows, anchors["Evaluation Summary"])

    if not out:
        out["raw_rows"] = [
            [_stringify(c) for c in r if c is not None]
            for r in rows if any(c is not None for c in r)
        ]
    return out


def _find_anchors(rows, names):
    """A열 텍스트로 섹션 시작 행 인덱스 찾기."""
    result = {}
    for i, row in enumerate(rows):
        if not row:
            continue
        val = _stringify(row[0])
        for name in names:
            if name in result:
                continue
            if val == name or val.startswith(name):
                result[name] = i
    return result


def _read_section(rows, anchor_idx):
    """anchor 다음 헤더 행(+1) 과 값 행(+2) 으로 dict 변환.
    헤더/값이 비어있으면 다음 비어있지 않은 행까지 스캔."""
    header_idx = anchor_idx + 1
    value_idx = anchor_idx + 2
    if header_idx >= len(rows) or value_idx >= len(rows):
        return {}
    header = [_stringify(c) for c in rows[header_idx]]
    values = list(rows[value_idx])
    out = {}
    for k, v in zip(header, values):
        if k:
            out[k] = _normalize(v)
    return out


def _read_text_lines(rows, anchor_idx, max_lines=5):
    """anchor 행과 그 이후 max_lines 행의 A열 텍스트 라인을 리스트로."""
    out = []
    for i in range(anchor_idx, min(anchor_idx + max_lines + 1, len(rows))):
        row = rows[i]
        if not row:
            continue
        line = " ".join(_stringify(c) for c in row if c is not None).strip()
        if line:
            out.append(line)
    return out


def _read_table_section(rows, anchor_idx, max_data_rows=20):
    """anchor + 1 헤더, 그 이후 빈 행 만날 때까지 list[dict] 로."""
    header_idx = anchor_idx + 1
    if header_idx >= len(rows):
        return []
    header = [_stringify(c) for c in rows[header_idx]]
    out = []
    for i in range(header_idx + 1, min(header_idx + 1 + max_data_rows, len(rows))):
        row = rows[i]
        if not row or all(c is None or _stringify(c) == "" for c in row):
            break
        if _is_section_header(row):
            break
        d = {k: _normalize(v) for k, v in zip(header, row) if k}
        if d:
            out.append(d)
    return out


def _is_section_header(row):
    """A열에 텍스트만 있고 나머지가 비어있으면 새 섹션 헤더로 간주."""
    if not row or row[0] is None:
        return False
    first = _stringify(row[0])
    if not first:
        return False
    rest_empty = all(c is None or _stringify(c) == "" for c in row[1:])
    return rest_empty and not first.replace(".", "").replace("-", "").isdigit()


def _rows_with_header(ws, drop_cols=None) -> list:
    """행 1 을 헤더로 잡고 나머지 행을 list[dict] 로 변환.

    drop_cols: 무시할 헤더명 집합 (예: 이미지가 들어있는 'Distribution' 컬럼).
    """
    if ws is None:
        return []
    drop = set(drop_cols or ())
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = [_stringify(c) for c in next(rows_iter)]
    except StopIteration:
        return []

    keep_idx = [i for i, h in enumerate(header) if h and h not in drop]
    out = []
    for row in rows_iter:
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
