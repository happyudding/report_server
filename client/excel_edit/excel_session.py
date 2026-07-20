"""rawdata Excel 편집 왕복 — Qt 비의존 순수 로직.

흐름 (run_excel_edit):
  1. GET .../web_report/rawdata_export → zip(manifest + source_*.parquet) 다운로드·디코드
  2. source 당 시트 1개로 임시 xlsx 작성 → xlwings 로 Excel 창 열기 (visible)
  3. 사용자가 저장·닫을 때까지 폴링. 닫힘 후 파일 해시 비교 (무변경 시 업로드 스킵)
  4. 시트명으로 원본 source 를 매칭해 재읽기 → honeyform parquet 재인코딩
     (형식 오류면 그 xlsx 를 다시 열어 사용자가 고치도록 반복).
     시트를 지웠으면 그 source 를 리포트에서 제거 — 되돌릴 수 없으므로 confirm_cb 로 확인받는다
  5. POST .../web_report/rawdata_replace 로 전체 교체 (X-Honey-Agent 헤더,
     삭제 시 source_indices 동봉)

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
from urllib.parse import quote

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


def run_excel_edit(session_id, server_base, status_cb=None, should_cancel=None,
                   confirm_cb=None) -> dict:
    """세션 rawdata 를 Excel 로 편집하고 서버에 반영. 반환 {"changed": bool, "message": str}.

    status_cb(state, message): 진행 상태 통지 콜백 (state ∈ download/excel/editing/
    reencode/upload/done/done_no_changes/cancelled). None 이면 무시.
    should_cancel(): True 를 반환하면 편집을 취소하고 열려 있는 Excel 을 강제 종료한 뒤
    {"changed": False, "cancelled": True} 로 반환. None 이면 취소 없음.
    confirm_cb(message) -> bool: 시트 삭제(= source 영구 제거) 반영 전 확인. False 면
    업로드하지 않고 취소한다(다시 실행하면 서버 원본을 새로 받으므로 원상복구). None 이면
    확인 없이 진행.
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

    # ── 2. 임시 xlsx 작성 + Excel 열기 ────────────────────────────────────
    _emit("excel", "Excel 여는 중...")
    tmp_dir = os.path.join(tempfile.gettempdir(), "honey_exceledit", _safe_name(session_id))
    os.makedirs(tmp_dir, exist_ok=True)
    xlsx_path = os.path.join(tmp_dir, "rawdata.xlsx")

    app = xw.App(visible=True, add_book=False)
    try:
        wb = app.books.add()
        used_titles = set()
        final_titles = []          # 실제 시트에 붙은 제목 — 재읽기 때 원본 idx 매칭 기준
        for idx, df in enumerate(dfs):
            title = _sheet_name(sheet_titles[idx], idx, used_titles)
            final_titles.append(title)
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
            reason = _wait_until_closed(wb, should_cancel)
            _quit_app(app)  # 파일 핸들 해제 (취소 시엔 Excel 강제 종료)

            if reason == "cancelled":
                _cleanup(tmp_dir)
                _emit("cancelled", "Rawdata 수정 취소됨 — Excel 을 닫았습니다.")
                return {"changed": False, "cancelled": True, "message": "취소됨"}

            current = _file_hash(xlsx_path)
            if current == baseline:
                _cleanup(tmp_dir)
                _emit("done_no_changes", "변경 없음 — 업로드를 건너뜁니다.")
                return {"changed": False, "message": "변경 없음"}

            _emit("reencode", "변경 내용 인코딩 중...")
            try:
                new_parquets, kept_indices, removed_names = _read_and_encode(
                    xlsx_path, final_titles, sheet_titles)
                if removed_names and not _confirm_removal(confirm_cb, removed_names):
                    _cleanup(tmp_dir)
                    _emit("cancelled", "시트 삭제 미승인 — 반영을 취소했습니다.")
                    return {"changed": False, "cancelled": True,
                            "message": "시트 삭제 미승인"}
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
    _upload_sources(base, session_id, new_parquets, kept_indices=kept_indices)
    _cleanup(tmp_dir)
    msg = ("Rawdata 수정 완료 — 서버에 반영됨"
           + (f" (source {len(removed_names)}개 제거)." if removed_names else "."))
    _emit("done", msg)
    return {"changed": True, "message": "완료"}


def _confirm_removal(confirm_cb, removed_names):
    """시트 삭제 = source 영구 제거 확인. 콜백이 없으면 승인, 예외는 거부로 본다."""
    if confirm_cb is None:
        return True
    message = ("시트 삭제 감지: " + ", ".join(removed_names)
               + "\n\n해당 source 데이터가 리포트에서 제거되고 전체 탭이 재계산됩니다.\n"
                 "서버에서 되돌릴 수 없습니다. 계속할까요?")
    try:
        return bool(confirm_cb(message))
    except Exception:
        return False


# ── 다운로드/업로드 ──────────────────────────────────────────────────────────
def _honey_headers():
    """서버 신원 토큰 — embedded_browser 와 동일 규칙(HoneyUser/<percent-encoded 계정>).

    비공개(is_private) 세션은 업로더/위임 편집자 신원이 있어야 조회 가능하다.
    수집 실패 시 토큰 없이 진행(공개 세션은 무신원으로도 조회됨)."""
    try:
        import client_identity
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    return {"User-Agent": f"python-requests HoneyUser/{quote(user, safe='')}"} if user else {}


def _download_sources(base, session_id):
    url = f"{base}/pe/report/session/{session_id}/web_report/rawdata_export"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC, headers=_honey_headers())
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


def _upload_sources(base, session_id, parquet_list, kept_indices=None):
    """parquet 전체 업로드. kept_indices 가 있으면(= 시트 삭제) 남긴 원본 idx 를 동봉한다."""
    url = f"{base}/pe/report/session/{session_id}/web_report/rawdata_replace"
    files = {
        f"webreport_{idx}": (f"source_{idx}.parquet", data, "application/vnd.apache.parquet")
        for idx, data in enumerate(parquet_list)
    }
    data = {"source_indices": json.dumps(kept_indices)} if kept_indices else None
    resp = requests.post(url, files=files, data=data,
                         headers={"X-Honey-Agent": "1", **_honey_headers()},
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

    # 텍스트 서식(선행 0 보존)은 메타 영역에만 적용한다. 데이터 블록(수백만 셀)에
    # number_format 을 한 번에 지정하면 Excel COM 이 0x800A03EC(Range NumberFormat
    # 설정 불가) 로 거부하므로, 작은 메타 행/열에만 "@" 를 걸고 대용량 데이터 영역은
    # 기본(General) 서식 그대로 둔다 (원래 데이터 영역을 General 로 되돌리던 것과 동일한 결과).
    meta_row_end = DATA_START_ROW + 1                 # 헤더 1행 + 메타 6행
    n_meta_cols = len(META_COLUMNS)
    sht.range((1, 1), (meta_row_end, n_cols)).number_format = "@"      # 상단 메타 행 전체
    sht.range((1, 1), (n_rows, n_meta_cols)).number_format = "@"       # 좌측 메타 열 전체

    start = 0
    while start < n_rows:
        block = values[start:start + _WRITE_CHUNK_ROWS]
        sht.range((1 + start, 1)).value = block
        start += _WRITE_CHUNK_ROWS


def match_sheets(sheet_names, expected_titles):
    """닫힌 xlsx 의 시트명을 작성 시점 제목과 대조해 원본 idx 매핑을 구한다 (순수 함수).

    반환 (pairs, removed): pairs=[(시트 위치, 원본 idx), ...] 를 원본 idx 오름차순으로 —
    시트 순서를 바꿔도 원본 순서를 복원한다. removed=삭제된 원본 idx 리스트.
    pairs 가 None 이면 "개수는 같은데 이름이 안 맞음" = 이름 변경으로 보고 위치 기반
    폴백(기존 동작 유지, removed 는 항상 빈 리스트).

    비교 키는 strip().casefold() — Excel 시트명은 대소문자를 구분하지 않고 유일하다.
    ValueError: 시트 추가 / 시트 없음 / 삭제와 이름 변경이 섞여 매칭 불가.
    """
    if not sheet_names:
        raise ValueError("시트가 없습니다.")
    if len(sheet_names) > len(expected_titles):
        raise ValueError(
            f"시트 개수가 원본보다 많습니다 (원본 {len(expected_titles)}, "
            f"현재 {len(sheet_names)}). 시트를 추가하지 마세요.")

    expected_map = {str(t).strip().casefold(): i for i, t in enumerate(expected_titles)}
    pairs, unmatched = [], []
    for pos, name in enumerate(sheet_names):
        idx = expected_map.get(str(name).strip().casefold())
        if idx is None:
            unmatched.append(str(name))
        else:
            pairs.append((pos, idx))

    if unmatched:
        if len(sheet_names) == len(expected_titles):
            return None, []          # 이름만 바뀐 경우 — 위치 기반 폴백(하위호환)
        raise ValueError(
            "시트를 지울 때는 남은 시트의 이름을 바꾸지 마세요 "
            f"(원본에 없는 시트: {', '.join(unmatched)}).")

    pairs.sort(key=lambda p: p[1])
    kept = {idx for _, idx in pairs}
    removed = [i for i in range(len(expected_titles)) if i not in kept]
    return pairs, removed


def _read_and_encode(xlsx_path, expected_titles, source_names):
    """저장된 xlsx 를 시트명 매칭 순서로 읽어 honeyform parquet 로 재인코딩.

    반환 (parquet list, kept_indices|None, removed_names). kept_indices 는 삭제가 있을
    때만 채워지고(원본 idx 오름차순, parquet 순서와 1:1), 전체 유지면 None 이다.
    """
    import xlwings as xw

    read_app = xw.App(visible=False, add_book=False)
    try:
        # 읽기 전용 비가시 인스턴스 — 프롬프트가 뜨면 응답할 사용자가 없어 quit 이 막히고
        # 좀비 Excel 이 남으므로 알림을 끈다 (upload_prepare 와 동일).
        read_app.display_alerts = False
    except Exception:
        pass
    try:
        wb = read_app.books.open(xlsx_path)
        sheets = list(wb.sheets)
        pairs, removed = match_sheets([s.name for s in sheets], expected_titles)
        order = [pos for pos, _ in pairs] if pairs is not None else range(len(sheets))
        out = []
        for pos in order:
            df = _values_to_df(sheets[pos].used_range.value)
            out.append(encode_honeyform_parquet(df))
        wb.close()
        kept_indices = [idx for _, idx in pairs] if removed else None
        removed_names = [
            str(source_names[i]) if i < len(source_names) else f"source_{i}"
            for i in removed]
        return out, kept_indices, removed_names
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


def _wait_until_closed(wb, should_cancel=None):
    """workbook 이 닫힐 때까지 폴링. COM 접근이 예외를 내면 닫힌 것으로 본다.

    should_cancel() 이 True 를 반환하면 "cancelled", 창이 닫히면 "closed" 를 돌려준다.
    """
    while True:
        time.sleep(_POLL_SEC)
        if should_cancel is not None and should_cancel():
            return "cancelled"
        try:
            _ = wb.name
        except Exception:
            return "closed"


def _pid_alive(pid):
    """Windows 프로세스 생존 확인 (권한 최소 핸들)."""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _quit_app(app):
    """Excel 인스턴스 종료. quit 이 프롬프트 등에 막혀 무시되면 프로세스 강제 종료
    폴백으로 좀비 Excel 잔존을 막는다 (호출 시점엔 대상 workbook 이 이미 닫혀 있어
    사용자 응답이 필요한 프롬프트가 없다)."""
    pid = None
    try:
        pid = int(app.pid)
    except Exception:
        pass
    try:
        app.display_alerts = False
    except Exception:
        pass
    try:
        app.quit()
    except Exception:
        pass
    if pid is None:
        return
    for _ in range(10):          # 종료 유예 최대 ~3초
        if not _pid_alive(pid):
            return
        time.sleep(0.3)
    try:
        app.kill()               # quit 무시 → 강제 종료
    except Exception:
        pass


def _cleanup(tmp_dir):
    try:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass
