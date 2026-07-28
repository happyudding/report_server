"""rawdata Excel 편집 왕복 — Qt 비의존 순수 로직.

흐름 (run_excel_edit):
  1. GET .../web_report/rawdata_export → zip(manifest + source_*.parquet) 다운로드·디코드
  2. source 당 시트 1개로 임시 xlsx 작성 → xlwings 로 Excel 창 열기 (visible)
  3. 사용자가 저장·닫을 때까지 폴링. 닫힘 후 파일 해시 비교 (무변경 시 업로드 스킵)
  4. 시트명으로 원본 source 를 매칭해 재읽기 → 자동 교정(유령 행/열·메타 컬럼명 케이스) →
     정수 dtype 복원 → 원본 대비 diff·경고 수집 → honeyform parquet 재인코딩
     (형식 오류면 그 xlsx 를 다시 열어 사용자가 고치도록 반복).
     시트를 지웠으면 그 source 를 리포트에서 제거.
  4-1. 되돌릴 수 없으므로 **반영 전에** 변경 요약(셀 diff·자동 교정·경고·시트 삭제)을
     confirm_cb 로 한 번 확인받는다. 값 검증 규칙·문안은 web_report.rawvalues 가 단일 진실.
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

from web_report import rawvalues
from web_report.honeyform import (
    DATA_START_ROW,
    META_COLUMNS,
    decode_split_honeyform_parquet,
    encode_honeyform_parquet,
)

try:
    from transport.config import REQUEST_TIMEOUT_SEC
except Exception:  # 단독 실행/테스트 폴백
    REQUEST_TIMEOUT_SEC = (10, 300)

_POLL_SEC = 1.5
# 확인창 표에 담을 source 당 셀 변경 상세 최대 건수. 표는 수만 행도 다루므로 사실상
# 전량을 담고, 이 값은 메모리 폭주 방지선이다 (초과분은 확인창이 "… 외 N건"으로 명시).
_CELL_DETAIL_LIMIT = 50_000
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
    dfs, sheet_titles, int_cols, manifest = _download_sources(base, session_id)

    # ── 2. 임시 xlsx 작성 + Excel 열기 ────────────────────────────────────
    # 작업 폴더는 캐시 폴더의 하위 work/ — 끝나고 이걸 지워도 export zip 캐시(상위)는 남는다.
    _emit("excel", "Excel 여는 중...")
    tmp_dir = os.path.join(_cache_dir(session_id), "work")
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
                new_parquets, kept_indices, removed_names, reports, fixes = _read_and_encode(
                    xlsx_path, final_titles, sheet_titles, dfs, int_cols)
                # 무엇이 바뀌는지(셀 diff·자동 교정·경고·시트 삭제)를 업로드 전에 한 번 보여준다
                # — Excel 편집은 서버에서 되돌릴 수 없다. UI 는 스크롤되는 확인창이라 줄 수를
                # 자르지 않는 구조화 payload 를 넘긴다(문안 조립은 UI 소관).
                payload = rawvalues.build_confirm_sections(
                    reports, removed_names, fixes_by_source=fixes)
                has_changes = bool(payload["sections"] or payload["removed"])
                if has_changes and not _confirm_changes(confirm_cb, payload):
                    _cleanup(tmp_dir)
                    _emit("cancelled", "반영 미승인 — 저장하지 않았습니다.")
                    return {"changed": False, "cancelled": True,
                            "message": "반영 미승인"}
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
    # 편집 결과로 Distribution pack 을 다시 만들어 함께 보낸다 — 안 보내면 content_hash 가
    # 바뀌어 구 pack 이 무효화되므로 서버가 조회 때 정렬을 다시 하게 된다.
    kept_titles = ([sheet_titles[i] for i in kept_indices if i < len(sheet_titles)]
                   if kept_indices else sheet_titles)
    dist_pack = _build_dist_pack(
        new_parquets, kept_titles, manifest,
        emit=lambda msg: _emit("upload", msg))
    _emit("upload", "서버 반영 중...")
    _upload_sources(base, session_id, new_parquets, kept_indices=kept_indices,
                    dist_pack=dist_pack)
    _cleanup(tmp_dir)
    msg = ("Rawdata 수정 완료 — 서버에 반영됨"
           + (f" (source {len(removed_names)}개 제거)." if removed_names else "."))
    _emit("done", msg)
    return {"changed": True, "message": "완료"}


def _confirm_changes(confirm_cb, payload):
    """반영 전 변경 내용 확인(셀 diff·자동 교정·경고·시트 삭제 통합).

    payload 는 rawvalues.build_confirm_sections 의 구조화 dict — UI 가 스크롤 가능한
    확인창으로 렌더한다. 콜백이 없으면 승인, 예외는 거부로 본다(파괴적 반영의 기본값은 '안 함')."""
    if confirm_cb is None:
        return True
    try:
        return bool(confirm_cb(payload))
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


def _cache_dir(session_id):
    return os.path.join(tempfile.gettempdir(), "honey_exceledit", _safe_name(session_id))


def _fetch_export_zip(base, session_id):
    """rawdata zip 을 받는다 — temp 캐시가 유효하면 재사용(서버는 304 만 응답).

    서버 ETag = content_hash 라 raw parquet 이 안 바뀌었으면 내려줄 내용이 100% 같다.
    캐시 히트 시 서버는 전 source 를 storage 에서 메모리로 올려 zip 으로 싸는 작업 자체를
    하지 않는다(이 경로의 서버 부하 대부분이 거기 있다).

    캐시 파일: `<temp>/honey_exceledit/<sid>/export_<etag>.zip` + `.etag` (ETag 원문).
    쓰기·읽기 실패는 전부 무시하고 그냥 새로 받는다 — 캐시는 최적화일 뿐이다.
    """
    cache_dir = _cache_dir(session_id)
    etag_path = os.path.join(cache_dir, "export.etag")
    headers = dict(_honey_headers())
    cached_etag = ""
    try:
        with open(etag_path, encoding="utf-8") as fh:
            cached_etag = fh.read().strip()
    except OSError:
        cached_etag = ""
    cached_zip = os.path.join(cache_dir, f"export_{_safe_name(cached_etag)}.zip")
    if cached_etag and os.path.exists(cached_zip):
        headers["If-None-Match"] = cached_etag

    url = f"{base}/pe/report/session/{session_id}/web_report/rawdata_export"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC, headers=headers)
    if resp.status_code == 304 and cached_etag:
        with open(cached_zip, "rb") as fh:
            return fh.read()
    resp.raise_for_status()
    blob = resp.content
    etag = (resp.headers.get("ETag") or "").strip()
    if etag:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            new_zip = os.path.join(cache_dir, f"export_{_safe_name(etag)}.zip")
            with open(new_zip, "wb") as fh:
                fh.write(blob)
            with open(etag_path, "w", encoding="utf-8") as fh:
                fh.write(etag)
            if cached_zip != new_zip and os.path.exists(cached_zip):
                os.remove(cached_zip)      # 세대는 항상 1개만 유지
        except OSError:
            pass
    return blob


def _download_sources(base, session_id):
    """반환 (dfs, titles, int_cols, manifest).

    int_cols[i] 는 source i 에서 **원본 parquet 이 int64 였던 item 컬럼** 집합. xlwings 는
    range.value 로 숫자를 전부 float 로 돌려주므로(1 → 1.0) 왕복 후 편집하지 않은 정수
    컬럼까지 dtype 이 드리프트한다. 원본 dtype 을 여기서 기억해 두었다가 재인코딩 직전에만
    되돌린다 — '값이 전부 정수면 int' 로 판정하면 원래 float64 였던 컬럼이 int64 로 뒤집혀
    회귀 기준(정수 컬럼 int64 보존)을 반대 방향으로 깬다.
    """
    zf = zipfile.ZipFile(io.BytesIO(_fetch_export_zip(base, session_id)))
    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    names = sorted(
        (n for n in zf.namelist() if n.startswith("source_") and n.endswith(".parquet")),
        key=lambda n: int(n[len("source_"):-len(".parquet")]),
    )
    if not names:
        raise ValueError("세션에 rawdata source 가 없습니다.")
    # decode_split 은 decode 와 같은 _decode_parts 를 쓰므로 typed(.data) 프레임을 함께 얻는
    # 추가 비용이 없다. dtype 집합만 뽑고 바로 참조를 끊어 피크 메모리를 회수한다.
    dfs, int_cols = [], []
    for name in names:
        table = decode_split_honeyform_parquet(zf.read(name), source=name, keep_df=True)
        dfs.append(table.df)
        int_cols.append({c for c in table.item_columns
                         if getattr(table.data[c].dtype, "kind", "") == "i"})
        del table
    sources_meta = manifest.get("sources") or []
    titles = []
    for idx in range(len(dfs)):
        info = sources_meta[idx] if idx < len(sources_meta) else {}
        titles.append(str(info.get("name") or f"source_{idx}"))
    return dfs, titles, int_cols, manifest


def fetch_rawdata_tables(server_base, session_id, indices=None, status_cb=None):
    """rawdata export zip → HoneyformTable 리스트 (빠른 수정 다이얼로그용 공개 헬퍼).

    Excel 왕복과 **같은 zip·같은 ETag 캐시**(_fetch_export_zip)를 쓴다 — 빠른 수정은
    원본을 바꾸지 않으므로 content_hash 가 그대로고, 두 번째부터는 서버가 304 만 응답한다
    (전 source 를 메모리에 올려 zip 으로 싸는 서버 작업이 통째로 사라진다).

    indices: 디코드할 source 원본 idx 목록(None 이면 전부). 다운로드는 어차피 zip 통짜지만
    디코드가 비용의 대부분이라, 고를 source 만 푸는 것이 여는 속도를 좌우한다.
    반환 (tables, manifest, names) — names 는 원본 idx 순서의 전체 source 이름.
    """
    from web_report.honeyform import decode_split_honeyform_parquet

    def _emit(message):
        if status_cb:
            try:
                status_cb(message)
            except Exception:
                pass

    _emit("rawdata 내려받는 중...")
    zf = zipfile.ZipFile(io.BytesIO(_fetch_export_zip(server_base, session_id)))
    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    names_in_zip = sorted(
        (n for n in zf.namelist() if n.startswith("source_") and n.endswith(".parquet")),
        key=lambda n: int(n[len("source_"):-len(".parquet")]),
    )
    if not names_in_zip:
        raise ValueError("세션에 rawdata source 가 없습니다.")
    sources_meta = manifest.get("sources") or []
    names = [str((sources_meta[i] if i < len(sources_meta) else {}).get("name")
                 or f"source_{i}") for i in range(len(names_in_zip))]

    wanted = list(range(len(names_in_zip))) if indices is None else list(indices)
    tables = []
    for pos, idx in enumerate(wanted):
        if not (0 <= idx < len(names_in_zip)):
            continue
        _emit(f"{names[idx]} 읽는 중... ({pos + 1}/{len(wanted)})")
        # keep_df=False — 표시·미리보기 전용이라 재인코딩용 전체 프레임이 필요 없다
        # (메모리 절반 이하. 저장은 patch spec 만 보내므로 df 를 쓸 일이 없다).
        tables.append(decode_split_honeyform_parquet(
            zf.read(names_in_zip[idx]), source=names[idx], keep_df=False))
    return tables, manifest, names


def _build_dist_pack(parquet_list, titles, manifest, emit=None):
    """편집 결과 parquet 으로 Distribution pack 을 다시 만든다 (업로드 경로와 같은 코드).

    첨부하지 않으면 서버가 조회 때 수십 초짜리 정렬을 다시 한다 — 클라는 이미 데이터를
    손에 들고 있으므로 여기서 만들어 보내는 편이 서버·사용자 모두에게 싸다.
    실패는 무해(None 반환) — 서버가 폴백 계산한다."""
    try:
        from web_report.dist_pack import build_pack_from_parquet

        sources = [{"data": data, "name": titles[idx] if idx < len(titles) else "",
                    "file_name": titles[idx] if idx < len(titles) else ""}
                   for idx, data in enumerate(parquet_list)]
        return build_pack_from_parquet(
            sources, (manifest or {}).get("selected_items") or [],
            (manifest or {}).get("mode") or "Normal", stage_cb=emit)
    except Exception:
        return None


def _upload_sources(base, session_id, parquet_list, kept_indices=None, dist_pack=None):
    """parquet 전체 업로드. kept_indices 가 있으면(= 시트 삭제) 남긴 원본 idx 를 동봉한다.

    dist_pack 이 있으면 업로드 라우트와 같은 필드명(dist_pack_index + dist_pack_chunk_<n>)
    으로 함께 보낸다 — 서버가 영구 저장해 반영 후 첫 조회의 dist 정렬이 사라진다."""
    url = f"{base}/pe/report/session/{session_id}/web_report/rawdata_replace"
    files = {
        f"webreport_{idx}": (f"source_{idx}.parquet", data, "application/vnd.apache.parquet")
        for idx, data in enumerate(parquet_list)
    }
    data = {"source_indices": json.dumps(kept_indices)} if kept_indices else {}
    if dist_pack and dist_pack.get("index") and dist_pack.get("chunks"):
        data["dist_pack_index"] = dist_pack["index"]
        for chunk_id, blob in sorted(dist_pack["chunks"].items()):
            files[f"dist_pack_chunk_{int(chunk_id)}"] = (
                f"chunk_{int(chunk_id)}.json.gz", blob, "application/gzip")
    resp = requests.post(url, files=files, data=data or None,
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


def _read_and_encode(xlsx_path, expected_titles, source_names, old_dfs=None, int_cols=None):
    """저장된 xlsx 를 시트명 매칭 순서로 읽어 교정·검사한 뒤 honeyform parquet 로 재인코딩.

    반환 (parquet list, kept_indices|None, removed_names, reports, fixes_by_source).
    kept_indices 는 삭제가 있을 때만 채워지고(원본 idx 오름차순, parquet 순서와 1:1),
    전체 유지면 None. reports/fixes 는 확인창 문안 재료다(rawvalues 참조).

    교정·검사 순서가 중요하다: sanitize(유령 행/열·컬럼명) → int 복원 → inspect(원본 대비
    diff·경고) → encode. inspect 를 sanitize 뒤에 두어야 '유령 행이 늘었다' 같은 우리가 이미
    고친 잡음이 사용자에게 보고되지 않는다.
    """
    import xlwings as xw

    old_dfs = old_dfs or []
    int_cols = int_cols or []
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
        # (시트 위치, 원본 idx) — 이름 매칭이 실패한 폴백에서는 위치 = 원본 idx 로 본다.
        order = pairs if pairs is not None else [(p, p) for p in range(len(sheets))]
        out, reports, fixes = [], [], {}
        for pos, src_idx in order:
            name = (str(source_names[src_idx]) if src_idx < len(source_names)
                    else f"source_{src_idx}")
            df = _values_to_df(sheets[pos].used_range.value)
            df, fixed = rawvalues.sanitize_excel_frame(df)
            # 아주 큰 시트에서는 정수 복원을 건너뛰되 조용히 넘어가지 않고 확인창에 알린다.
            n_cells = max(len(df) - DATA_START_ROW, 0) * max(len(df.columns), 1)
            if n_cells <= rawvalues.EXCEL_SCAN_CELL_BUDGET:
                df, _restored = rawvalues.restore_int_columns(
                    df, int_cols[src_idx] if src_idx < len(int_cols) else ())
            else:
                fixed.append("정수 서식 복원을 생략했습니다 (데이터가 큽니다) — 편집하지 않은 "
                             "정수 항목이 1 → 1.0 으로 표기될 수 있습니다.")
            if fixed:
                fixes[name] = fixed
            old_df = old_dfs[src_idx] if src_idx < len(old_dfs) else None
            # 확인창이 표라 셀 목록을 전량에 가깝게 담는다(구 QMessageBox 시절엔 20개를
            # 넘으면 창이 화면을 벗어나 버튼이 사라졌다). 넘치는 건수는 "… 외 N건"으로 표기.
            reports.append(rawvalues.inspect_edited_frame(
                old_df, df, source_name=name, cell_limit=_CELL_DETAIL_LIMIT))
            out.append(encode_honeyform_parquet(df))
        wb.close()
        kept_indices = [idx for _, idx in pairs] if removed else None
        removed_names = [
            str(source_names[i]) if i < len(source_names) else f"source_{i}"
            for i in removed]
        return out, kept_indices, removed_names, reports, fixes
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
