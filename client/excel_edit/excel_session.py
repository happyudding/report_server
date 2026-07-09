"""rawdata Excel 편집 왕복 — Qt 비의존 순수 로직.

흐름 (run_excel_edit):
  1. GET .../web_report/rawdata_export → zip(manifest + source_*.parquet) 다운로드·디코드
  2. source 당 시트 1개로 임시 xlsx 작성 → xlwings 로 Excel 창 열기 (visible)
  3. 사용자가 저장·닫을 때까지 폴링. 닫힘 후 파일 해시 비교 (무변경 시 업로드 스킵)
  4. 시트 순서(index)대로 재읽기 → honeyform parquet 재인코딩
     (형식 오류면 그 xlsx 를 다시 열어 사용자가 고치도록 반복)
  5. POST .../web_report/rawdata_replace 로 전체 교체 (X-Honey-Agent 헤더)

honeyform 스키마(메타 7열 SERIAL..FAILTNO + 메타 6행 TSEQ..LOLIM + 데이터)는
web_report.honeyform 공유 모듈을 그대로 재사용한다 (클라·서버 동일 인코딩).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import time
import zipfile

import requests

from web_report.honeyform import (
    DATA_START_ROW,
    META_COLUMNS,
    decode_honeyform_parquet,
    encode_honeyform_parquet,
)

try:
    from transport.config import REQUEST_TIMEOUT_SEC
except Exception:  # 단독 실행/테스트 폴백
    REQUEST_TIMEOUT_SEC = (10, 300)

_POLL_SEC = 1.5
_WRITE_CHUNK_ROWS = 50000
_INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def run_excel_edit(session_id, server_base, status_cb=None) -> dict:
    """세션 rawdata 를 Excel 로 편집하고 서버에 반영. 반환 {"changed": bool, "message": str}.

    status_cb(state, message): 진행 상태 통지 콜백 (state ∈ download/excel/editing/
    reencode/upload/done/done_no_changes). None 이면 무시.
    """
    import xlwings as xw

    def _emit(state, message):
        if status_cb:
            try:
                status_cb(state, message)
            except Exception:
                pass

    base = str(server_base).rstrip("/")

    # ── 1. 다운로드 + 디코드 ──────────────────────────────────────────────
    _emit("download", "rawdata 다운로드 중...")
    dfs, sheet_titles = _download_sources(base, session_id)
    n_sources = len(dfs)

    # ── 2. 임시 xlsx 작성 + Excel 열기 ────────────────────────────────────
    _emit("excel", "Excel 여는 중...")
    tmp_dir = os.path.join(tempfile.gettempdir(), "honey_exceledit", _safe_name(session_id))
    os.makedirs(tmp_dir, exist_ok=True)
    xlsx_path = os.path.join(tmp_dir, "rawdata.xlsx")

    app = xw.App(visible=True, add_book=False)
    try:
        wb = app.books.add()
        used_titles = set()
        for idx, df in enumerate(dfs):
            title = _sheet_name(sheet_titles[idx], idx, used_titles)
            sht = wb.sheets[0] if idx == 0 else wb.sheets.add(name=title, after=wb.sheets[-1])
            if idx == 0:
                sht.name = title
            _write_sheet(sht, df)
        wb.sheets[0].activate()
        wb.save(xlsx_path)
        baseline = _file_hash(xlsx_path)

        # ── 3~4. 편집·닫힘 감시 → 재읽기(형식 오류 시 반복) ────────────────
        while True:
            _emit("editing", "Excel 에서 편집 후 [저장]하고 창을 닫으세요...")
            _wait_until_closed(wb)
            _quit_app(app)  # 파일 핸들 해제

            current = _file_hash(xlsx_path)
            if current == baseline:
                _cleanup(tmp_dir)
                _emit("done_no_changes", "변경 없음 — 업로드를 건너뜁니다.")
                return {"changed": False, "message": "변경 없음"}

            _emit("reencode", "변경 내용 인코딩 중...")
            try:
                new_parquets = _read_and_encode(xlsx_path, n_sources)
                break
            except ValueError as exc:
                # 헤더/메타행 파손 — 다시 열어 사용자가 고치도록 한다
                baseline = current
                app = xw.App(visible=True, add_book=False)
                wb = app.books.open(xlsx_path)
                _emit("editing", f"형식 오류: {exc} — 고쳐서 다시 저장·닫으세요")
                continue
    except Exception:
        _quit_app(app)
        raise

    # ── 5. 업로드 (전체 교체) ────────────────────────────────────────────
    _emit("upload", "서버 반영 중...")
    _upload_sources(base, session_id, new_parquets)
    _cleanup(tmp_dir)
    _emit("done", "Rawdata 수정 완료 — 서버에 반영됨.")
    return {"changed": True, "message": "완료"}


# ── 다운로드/업로드 ──────────────────────────────────────────────────────────
def _download_sources(base, session_id):
    url = f"{base}/pe/report/session/{session_id}/web_report/rawdata_export"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    names = sorted(
        (n for n in zf.namelist() if n.startswith("source_") and n.endswith(".parquet")),
        key=lambda n: int(n[len("source_"):-len(".parquet")]),
    )
    if not names:
        raise ValueError("세션에 rawdata source 가 없습니다.")
    dfs = [decode_honeyform_parquet(zf.read(n)) for n in names]
    sources_meta = manifest.get("sources") or []
    titles = []
    for idx in range(len(dfs)):
        info = sources_meta[idx] if idx < len(sources_meta) else {}
        titles.append(str(info.get("name") or f"source_{idx}"))
    return dfs, titles


def _upload_sources(base, session_id, parquet_list):
    url = f"{base}/pe/report/session/{session_id}/web_report/rawdata_replace"
    files = {
        f"webreport_{idx}": (f"source_{idx}.parquet", data, "application/vnd.apache.parquet")
        for idx, data in enumerate(parquet_list)
    }
    resp = requests.post(url, files=files, headers={"X-Honey-Agent": "1"},
                         timeout=REQUEST_TIMEOUT_SEC)
    if resp.status_code != 200:
        detail = ""
        try:
            detail = resp.json().get("error") or ""
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(f"서버 반영 실패 ({resp.status_code}): {detail}")


# ── xlsx 쓰기/읽기 ───────────────────────────────────────────────────────────
def _write_sheet(sht, df):
    """honeyform df 를 시트에 원형 그대로 기록. 메타 영역은 텍스트 서식(선행 0 보존)."""
    import pandas as pd

    df = df.astype(object).where(pd.notna(df), None)
    values = [[_hdr(c) for c in df.columns]] + df.values.tolist()
    n_rows = len(values)
    n_cols = len(values[0]) if values else 0
    if n_rows < DATA_START_ROW + 2 or n_cols <= len(META_COLUMNS):
        raise ValueError("source 형식이 올바르지 않습니다.")

    # 전체 텍스트 서식 지정 후, 데이터 영역(메타 6행 아래·아이템 열)만 숫자 서식으로 되돌린다.
    sht.range((1, 1), (n_rows, n_cols)).number_format = "@"
    sht.range((DATA_START_ROW + 2, len(META_COLUMNS) + 1),
              (n_rows, n_cols)).number_format = "General"

    start = 0
    while start < n_rows:
        block = values[start:start + _WRITE_CHUNK_ROWS]
        sht.range((1 + start, 1)).value = block
        start += _WRITE_CHUNK_ROWS


def _read_and_encode(xlsx_path, n_sources):
    """저장된 xlsx 를 시트 순서대로 읽어 honeyform parquet list 로 재인코딩."""
    import xlwings as xw

    read_app = xw.App(visible=False, add_book=False)
    try:
        wb = read_app.books.open(xlsx_path)
        sheets = list(wb.sheets)
        if len(sheets) != n_sources:
            raise ValueError(
                f"시트 개수가 원본과 다릅니다 (원본 {n_sources}, 현재 {len(sheets)}). "
                f"시트를 추가·삭제하지 마세요.")
        out = []
        for sht in sheets:
            df = _values_to_df(sht.used_range.value)
            out.append(encode_honeyform_parquet(df))
        wb.close()
        return out
    finally:
        _quit_app(read_app)


def _values_to_df(vals):
    import pandas as pd

    if not isinstance(vals, list) or not vals or not isinstance(vals[0], list):
        raise ValueError("시트가 비어 있거나 형식이 올바르지 않습니다.")
    header = [_hdr(c) for c in vals[0]]
    rows = [list(r) for r in vals[1:]]
    df = pd.DataFrame(rows, columns=header)
    df = df.astype(object).where(pd.notna(df), None)
    return df


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────
def _hdr(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _sheet_name(raw, idx, used):
    name = _INVALID_SHEET_CHARS.sub("", str(raw or f"source_{idx}")).strip()[:31] or f"src{idx}"
    base = name[:28]
    while name in used:
        idx += 1
        name = f"{base}_{idx}"[:31]
    used.add(name)
    return name


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(text))[:80] or "session"


def _file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _wait_until_closed(wb):
    """workbook 이 닫힐 때까지 폴링. COM 접근이 예외를 내면 닫힌 것으로 본다."""
    while True:
        time.sleep(_POLL_SEC)
        try:
            _ = wb.name
        except Exception:
            return


def _quit_app(app):
    try:
        app.quit()
    except Exception:
        pass


def _cleanup(tmp_dir):
    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass
