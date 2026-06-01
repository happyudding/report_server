"""Honey 가 업로드한 .xlsx 에서 summary / yield / issue_table 텍스트만 추출.

클라이언트 xlsx_writer 가 templete.xlsx 레이아웃으로 출력한다는 전제:
  - 표는 A열을 비우고 B열부터 시작, 제목 배너 A1, 표 헤더 3행, 데이터 4행~.
  - summary 는 번호 섹션("1. Device Feature" / "2. Yield" / "3. Evaluation Summary")
    + "Major Fail Bins"(E열) 으로 구성 → anchor 텍스트를 행 전체(2D)에서 찾는다.
  - issue_table 은 Category 그룹(Yield/CPK/ETC); bin 이 있는 Yield 행만 의미.

견고성: 셀 좌표 하드코딩 대신 anchor 텍스트(위치 2D)와 헤더 행 매칭을 우선한다.
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
    issue_rows = [
        r for r in _rows_with_header(_get_sheet(wb, "issue_table"),
                                     drop_cols={"Distribution"})
        # Category 그룹의 CPK/ETC 플레이스홀더(빈 bin) 행 제외 — Yield 블록만 의미
        if _stringify(r.get("bin")) != ""
    ]

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
