"""신규 Item(수식) 추가 — 원본 rawdata 에 파생 컬럼 하나를 붙인다 (Qt 비의존 순수 로직).

Honey 의 Rawdata 허브 `신규 Item(수식) 추가` 탭이 [원본에 추가] 를 눌렀을 때 도는 경로다.
Excel 을 전혀 쓰지 않는다는 점만 빼면 excel_session.run_excel_edit 과 같은 왕복이다:

  1. GET .../web_report/rawdata_export → zip 다운로드·디코드
     (참조 item 이 **전부 있는** source 만 keep_df=True 로 풀고, 나머지는 parquet bytes 그대로)
  2. source 마다 수식을 계산해 컬럼 1개를 df 끝에 붙이고 honeyform parquet 재인코딩
  3. 되돌릴 수 없으므로 **반영 전에** 요약을 confirm_cb 로 확인받는다
  4. manifest 사본의 selected_items 에 신규 이름을 더한 뒤 Distribution pack 재생성
  5. POST .../web_report/rawdata_replace (add_items + rows_preserved 동봉)

**왜 서버가 아니라 여기서 계산하나**: 서버가 parquet 전량을 디코드·계산·재인코딩하면 대형
세션에서 웹 프로세스가 통째로 묶인다. 클라는 이미 데이터를 손에 들고 있다(빠른 수정
다이얼로그를 웹이 아니라 Honey 에 둔 것과 같은 판단).

honeyform 스키마(메타 7열 + 메타 6행 + 데이터)와 수식 엔진은 web_report 공유 모듈을 그대로
재사용한다 — 클라·서버가 같은 코드를 돌아 값이 갈릴 수 없다.
"""
from __future__ import annotations

import numpy as np

from web_report import formula
from web_report.honeyform import DATA_START_ROW, META_ROW_LABELS, encode_honeyform_parquet

# excel_session 은 requests 를 끌어온다 — 계산·검증만 쓰는 호출부(테스트 포함)가 네트워크
# 스택 없이 이 모듈을 import 할 수 있도록 왕복 함수 안에서만 늦게 가져온다.

# 미리보기 표에 담을 샘플 값 개수 (source 당).
_SAMPLE_N = 10


def _emit(status_cb, message):
    if status_cb:
        try:
            status_cb(message)
        except Exception:
            pass


# ── 메타 기본값 / 검증 ───────────────────────────────────────────────────────

def default_meta(tables) -> dict:
    """새 item 의 메타 기본값 — TSEQ/TNO 는 전 source 최대 + 1, STEP 은 마지막 item 과 동일.

    **전 source 공통 값 하나**를 쓴다. source 마다 다르면 tno_to_item_map(yield_tab)·
    item_meta(tabs/common, setdefault)·scatter_item(distribution) 세 곳이 서로 다른 기준을
    잡아 fail 귀속과 표시 TNO 가 조용히 갈린다.

    마지막 item 의 TSEQ/TNO 가 숫자가 아니면 `+1` 을 만들 수 없다 — 그 칸은 **빈 문자열**로
    두고 사용자가 직접 넣게 한다(0 이나 1 로 추측하면 기존 항목과 부딪힐 수 있다).
    """
    def _max_of(attr):
        best = None
        for table in tables:
            meta = getattr(table, attr, None) or {}
            for item in table.item_columns:
                value = _meta_number(meta.get(item))
                if value is not None and (best is None or value > best):
                    best = value
        return best

    steps = []
    for table in tables:
        step_map = getattr(table, "step", None) or {}
        for item in table.item_columns:
            text = str(step_map.get(item) or "").strip()
            if text and text not in steps:
                steps.append(text)

    last_step = ""
    if tables:
        first = tables[0]
        for item in reversed(first.item_columns):
            text = str((getattr(first, "step", None) or {}).get(item) or "").strip()
            if text:
                last_step = text
                break

    tseq, tno = _max_of("tseq"), _max_of("tno")
    return {
        "name": "",
        "tseq": "" if tseq is None else str(int(tseq) + 1),
        "tno": "" if tno is None else str(int(tno) + 1),
        "step": last_step,
        "unit": "", "hilim": "", "lolim": "",
        "step_choices": steps,
    }


def existing_items(tables) -> list:
    """전 source item 이름 합집합 (등장 순서 보존) — 중복 검사·자동완성 후보."""
    seen, out = set(), []
    for table in tables:
        for item in table.item_columns:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def existing_tnos(tables) -> dict:
    """{TNO 정수: 그 TNO 를 쓰는 item 이름} — 전 source 합집합. 중복 검사용."""
    out = {}
    for table in tables:
        tno_map = getattr(table, "tno", None) or {}
        for item in table.item_columns:
            value = _meta_number(tno_map.get(item))
            if value is not None and float(value).is_integer():
                out.setdefault(int(value), item)
    return out


def _meta_number(value):
    """메타 6행에 들어갈 값의 숫자 판정 — **rawvalues 규칙**을 그대로 쓴다.

    formula.num 을 쓰면 안 된다: 그건 float() 기반이라 `1_000` 같은 파이썬 표기를
    통과시키는데, 메타 값은 parquet 에 **문자열 그대로** 저장되므로 조회 때
    pd.to_numeric 이 NaN 으로 떨궈 **규격이 조용히 사라진다**(웹 raw_data.js RAW_NUM_RE 와
    문자 그대로 같은 정규식이어야 하는 이유와 동일).
    """
    from web_report import rawvalues

    return rawvalues.parse_number(str(value or "").strip())


def _int_text(value):
    parsed = _meta_number(value)
    if parsed is None or not float(parsed).is_integer():
        return None
    return int(parsed)


def validate_meta(meta, tables) -> list:
    """메타 7칸 검증 — 위반 메시지 목록(비면 통과). 미리보기·추가 직전 공통.

    값 형식 판정은 rawvalues 와 같은 규칙을 쓴다(사본을 만들지 않는다).
    """
    from web_report.honeyform import META_COLUMNS

    issues = []
    name = str(meta.get("name") or "").strip()
    if not name:
        issues.append("ITEMNAME 을 입력하세요.")
    elif len(name) > formula.MAX_NAME:
        issues.append(f"ITEMNAME 이 너무 깁니다 ({formula.MAX_NAME}자 이하).")
    elif any(ch in name for ch in "\r\n\t"):
        issues.append("ITEMNAME 에 줄바꿈·탭을 넣을 수 없습니다.")
    elif name.strip().upper() in {c.upper() for c in META_COLUMNS}:
        issues.append(f"ITEMNAME '{name}' 은 예약된 메타 컬럼 이름입니다 "
                      f"({'/'.join(META_COLUMNS)}).")
    else:
        clash = [i for i in existing_items(tables) if i.strip() == name]
        if clash:
            issues.append(f"ITEMNAME '{name}' 은 이미 있는 항목입니다.")

    tseq = _int_text(meta.get("tseq"))
    if tseq is None:
        issues.append("TSEQ 는 정수여야 합니다.")
    tno = _int_text(meta.get("tno"))
    if tno is None:
        issues.append("TNO 는 정수여야 합니다.")
    else:
        owner = existing_tnos(tables).get(tno)
        if owner:
            # TNO 는 fail 귀속의 키다 — 겹치면 tno_to_item_map 이 TSEQ 가 앞선 항목 하나만
            # 남겨서 기존 항목의 Fail 집계가 통째로 사라진다(에러 없이).
            issues.append(f"TNO {tno} 은 이미 '{owner}' 가 쓰고 있습니다. "
                          "이 값을 쓰면 그 항목의 Fail 집계가 사라집니다.")

    hi_text = str(meta.get("hilim") or "").strip()
    lo_text = str(meta.get("lolim") or "").strip()
    hi = _meta_number(hi_text) if hi_text else None
    lo = _meta_number(lo_text) if lo_text else None
    if hi_text and hi is None:
        issues.append("HILIM 은 숫자여야 합니다 (비워 두면 규격 없음).")
    if lo_text and lo is None:
        issues.append("LOLIM 은 숫자여야 합니다 (비워 두면 규격 없음).")
    if hi is not None and lo is not None and lo > hi:
        issues.append(f"LOLIM({lo_text}) 이 HILIM({hi_text}) 보다 큽니다.")
    return issues


# ── 미리보기 ─────────────────────────────────────────────────────────────────

def preview(tables, tokens) -> dict:
    """계산만 하고 프레임을 바꾸지 않는다 — 확정 전에 사용자가 볼 요약.

    반환 {"rows": [...], "skipped": [...], "total_finite", "total_nonfinite"}.
    rows[i] 는 {"source","n","ok","fail","mean","min","max","sample"} 이며, 참조 item 이
    없는 source 는 {"source","skipped":True,"why"} 로 담긴다.
    """
    rows, skipped = [], []
    total_ok = total_fail = 0
    for table in tables:
        missing = formula.missing_items(table, tokens)
        if missing:
            why = f"{', '.join(missing[:3])} 없음"
            rows.append({"source": table.source, "skipped": True, "why": why})
            skipped.append({"source": table.source, "missing": missing})
            continue
        values = formula.eval_for_table(table, tokens)
        finite = np.isfinite(values)
        ok, fail = int(finite.sum()), int((~finite).sum())
        total_ok += ok
        total_fail += fail
        good = values[finite]
        rows.append({
            "source": table.source, "skipped": False,
            "n": int(values.size), "ok": ok, "fail": fail,
            "mean": float(good.mean()) if ok else None,
            "min": float(good.min()) if ok else None,
            "max": float(good.max()) if ok else None,
            "sample": [float(v) for v in good[:_SAMPLE_N]],
        })
    return {"rows": rows, "skipped": skipped,
            "total_finite": total_ok, "total_nonfinite": total_fail}


# ── 컬럼 추가 ────────────────────────────────────────────────────────────────

def _cells(values) -> list:
    """계산값 배열 → parquet 에 넣을 셀 목록. NaN 은 None, 정수 전용이면 int.

    encode_honeyform_parquet 이 값을 문자열로 저장하므로 float 1.0 은 "1.0" 이 되고 되읽으면
    float64 가 된다 — IF(...,0,1) 같은 판정 컬럼의 표기가 0.0/1.0 으로 드리프트한다.
    결측을 뺀 유한값이 전부 정수일 때만 int 로 넣어 표기를 보존한다
    (excel_session.restore_int_columns 와 같은 취지).
    """
    arr = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(arr)
    as_int = bool(finite.any()) and bool(np.all(arr[finite] == np.floor(arr[finite])))
    out = []
    for value, ok in zip(arr.tolist(), finite.tolist()):
        if not ok:
            out.append(None)
        elif as_int:
            out.append(int(value))
        else:
            out.append(value)
    return out


def apply_to_frame(df, table, tokens, meta):
    """table 의 값으로 수식을 계산해 df 끝에 컬럼 1개를 붙인 **같은 df** 를 돌려준다.

    df 는 decode_split_honeyform_parquet(keep_df=True) 가 준 전체 프레임(메타 6행 + 데이터)
    이고, table.data 는 그 데이터부의 수치 프레임이다 — 둘은 위치 1:1 이다
    (honeyform._assemble_df / decode_split 이 같은 tail 을 쓴다).
    """
    name = str(meta["name"]).strip()
    # **적용 직전 재검사** — pandas 의 df[name] = ... 는 이름이 겹치면 새 컬럼을 만들지 않고
    # 기존 컬럼을 조용히 덮어쓴다. UI 의 validate_meta 를 통과했더라도(그 사이 다른 사람이
    # Excel 왕복으로 같은 이름을 만들었을 수 있다) 여기서 한 번 더 막는다 — 덮어쓰면 원본
    # 측정값이나 BIN 이 통째로 사라지고, 그 parquet 은 유효해서 아무도 불평하지 않는다.
    if name in [str(c) for c in df.columns]:
        raise ValueError(f"{table.source}: '{name}' 은 이미 있는 컬럼입니다 "
                         "(덮어쓰면 원래 값이 사라집니다).")
    values = formula.eval_for_table(table, tokens)
    rows = len(df) - DATA_START_ROW
    if len(values) != rows:
        raise ValueError(f"{table.source}: 계산 결과 {len(values)}개가 데이터 {rows}행과 다릅니다.")
    column = [str(meta.get(key.lower()) or "").strip() for key in META_ROW_LABELS]
    df[name] = column + _cells(values)
    return df


def build_sources(tables, dfs, tokens, meta):
    """참조 item 이 있는 source 에만 컬럼을 붙여 재인코딩한다.

    반환 (parquets, applied_titles, skipped_titles). parquets[i] 는 tables[i] 에 대응하며,
    건너뛴 source 는 None 이다 — 호출부가 그 자리에 원본 bytes 를 그대로 쓴다(재인코딩조차
    하지 않는 편이 싸고, 값이 한 비트도 안 바뀐다는 보장이 공짜로 따라온다).
    """
    parquets, applied, skipped = [], [], []
    for table, df in zip(tables, dfs):
        if formula.missing_items(table, tokens):
            parquets.append(None)
            skipped.append(table.source)
            continue
        parquets.append(encode_honeyform_parquet(apply_to_frame(df, table, tokens, meta)))
        applied.append(table.source)
    return parquets, applied, skipped


# ── 왕복 ─────────────────────────────────────────────────────────────────────

def fetch_tables(server_base, session_id, status_cb=None):
    """미리보기용 테이블 로드 (keep_df=False — 값·메타만 있으면 된다).

    Excel 왕복과 **같은 zip·같은 ETag 캐시**를 쓴다 — 원본이 안 바뀐 세션은 두 번째부터
    서버가 304 만 응답한다.
    """
    from .excel_session import fetch_rawdata_tables

    return fetch_rawdata_tables(server_base, session_id, None, status_cb=status_cb)


def _open_export(server_base, session_id):
    """rawdata export zip 을 열어 (zipfile, manifest, [(idx, 파일명, source 이름)]) 를 준다.

    Excel 왕복·빠른 수정과 **같은 zip·같은 ETag 캐시**(_fetch_export_zip)라 원본이 안 바뀐
    세션은 두 번째부터 서버가 304 만 응답한다.
    """
    import io as _io
    import json
    import zipfile

    from .excel_session import _fetch_export_zip

    zf = zipfile.ZipFile(_io.BytesIO(_fetch_export_zip(server_base, session_id)))
    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
    names = sorted(
        (n for n in zf.namelist() if n.startswith("source_") and n.endswith(".parquet")),
        key=lambda n: int(n[len("source_"):-len(".parquet")]),
    )
    if not names:
        raise ValueError("세션에 rawdata source 가 없습니다.")
    meta = manifest.get("sources") or []
    entries = [(i, names[i],
                str((meta[i] if i < len(meta) else {}).get("name") or f"source_{i}"))
               for i in range(len(names))]
    return zf, manifest, entries


def add_item(session_id, server_base, spec, *, status_cb=None, confirm_cb=None) -> dict:
    """수식 item 을 원본에 추가한다. 반환 {"changed", "message", "skipped"}.

    spec: {"name","tseq","tno","step","unit","hilim","lolim","tokens"}

    **참조 항목이 있는 source 만 디코드한다** - 스키마(footer)만 읽어 판정하므로 건너뛸
    source 는 풀지도 재인코딩하지도 않고 zip 의 **원본 bytes 를 그대로 되올린다**. 대형
    세션에서 불필요한 디코드가 사라지고, 건드리지 않은 source 의 값이 한 비트도 안 바뀐다는
    보장이 공짜로 따라온다.
    """
    from web_report.honeyform import decode_split_honeyform_parquet, parquet_item_columns

    from .excel_session import _build_dist_pack, _upload_sources

    tokens = formula.normalize_tokens(spec.get("tokens"))
    name = str(spec.get("name") or "").strip()
    if not name:
        raise ValueError("ITEMNAME 이 없습니다.")
    refs = set(formula.item_refs(tokens))

    _emit(status_cb, "rawdata 내려받는 중...")
    zf, manifest, entries = _open_export(server_base, session_id)

    raw = {}                      # idx -> 원본 parquet bytes
    targets = []                  # 계산 대상 (idx, source 이름)
    skipped = []
    for idx, filename, source in entries:
        data = zf.read(filename)
        raw[idx] = data
        if refs - set(parquet_item_columns(data)):
            skipped.append(source)
        else:
            targets.append((idx, source))
    if not targets:
        raise ValueError("참조한 항목이 있는 source 가 하나도 없습니다 - 추가하지 않았습니다.")

    tables, dfs, order = [], [], []
    for pos, (idx, source) in enumerate(targets):
        _emit(status_cb, f"{source} 읽는 중... ({pos + 1}/{len(targets)})")
        table = decode_split_honeyform_parquet(raw[idx], source=source, keep_df=True)
        tables.append(table)
        dfs.append(table.df)
        order.append(idx)

    _emit(status_cb, "수식 계산 중...")
    parquets, applied, _ = build_sources(tables, dfs, tokens, spec)

    if confirm_cb is not None:
        payload = _confirm_payload(name, spec, tokens, tables,
                                   applied=applied, skipped=skipped)
        if not confirm_cb(payload):
            return {"changed": False, "message": "반영 미승인", "skipped": skipped}

    _emit(status_cb, "원본 재구성 중...")
    final = [raw[idx] for idx, _f, _n in entries]     # 기본은 전부 원본 bytes
    for pos, idx in enumerate(order):
        if parquets[pos] is not None:
            final[idx] = parquets[pos]

    # manifest 사본의 selected_items 를 먼저 늘린 뒤 pack 을 만든다 - 옛 목록으로 만들면
    # 신규 항목이 pack 에서 빠지고, get_distribution_batch 는 pack 을 그대로 믿어 갤러리에서
    # 그 카드만 조용히 빈다.
    manifest = dict(manifest)
    selected = list(manifest.get("selected_items") or [])
    if selected and name not in selected:
        manifest["selected_items"] = selected + [name]

    _emit(status_cb, "Distribution 준비 중...")
    titles = [source for _idx, _f, source in entries]
    dist_pack = _build_dist_pack(final, titles, manifest,
                                 emit=lambda m: _emit(status_cb, m))

    _emit(status_cb, "서버에 반영 중...")
    _upload_sources(server_base, session_id, final, kept_indices=None,
                    dist_pack=dist_pack, add_items=[name], rows_preserved=True)
    note = f" ({len(skipped)}개 source 는 참조 항목이 없어 건너뜀)" if skipped else ""
    return {"changed": True,
            "message": f"신규 item '{name}' 을 {len(applied)}개 source 에 추가했습니다{note}.",
            "skipped": skipped}



def _confirm_payload(name, meta, tokens, tables, applied, skipped) -> dict:
    """change_review_dialog.ask_change_review 가 그리는 구조화 확인 payload.

    되돌릴 수 없는 동작이므로 "무엇이 어디에 생기는가"와 "왜 되돌릴 수 없는가"를 함께 담는다.
    ``tables`` 는 계산 대상 source 만, ``skipped`` 는 건너뛴 source 이름 목록이다.
    """
    summary = preview(tables, tokens)
    warnings = [
        f"수식: {formula.render_formula(tokens)}",
        f"메타: TSEQ={meta.get('tseq')} TNO={meta.get('tno')} STEP={meta.get('step')} "
        f"UNIT={meta.get('unit') or '-'} HILIM={meta.get('hilim') or '-'} "
        f"LOLIM={meta.get('lolim') or '-'}",
        "되돌릴 수 없습니다 - 앱 안에 undo 가 없고, 서버 백업은 1세대뿐이라 이후 원본을 "
        "한 번 더 고치면 사라집니다. 잘못 만들면 [Rawdata 원본 수정](Excel)에서 그 열을 지우세요.",
        "같은 원본을 쓰는 세션이 함께 바뀝니다 - 원본은 analysis_key 단위라 같은 데이터를 "
        "재업로드해 만든 세션에도 이 항목이 생깁니다.",
        "BIN·FAILTNO 는 바뀌지 않습니다 - 수율·Wafer Map 은 그대로이고, 새 항목은 "
        "Distribution·CPK·Raw Data 에만 나타납니다.",
    ]
    if skipped:
        warnings.insert(1, "건너뛴 source (참조 항목 없음): " + ", ".join(skipped))

    sections = [{
        "name": f"신규 item '{name}'", "structure": [], "fixes": [],
        "warnings": warnings, "cells": [], "cell_total": 0,
    }]
    for row in summary["rows"]:
        detail = f"신규 item '{name}' 추가 - 값 {row['ok']:,}개"
        if row["fail"]:
            detail += f", 빈값 {row['fail']:,}개"
        sections.append({
            "name": row["source"], "structure": [detail], "fixes": [],
            "warnings": [], "cells": [], "cell_total": 0,
        })
    for source in skipped:
        sections.append({
            "name": source, "structure": [], "fixes": [],
            "warnings": ["참조 항목이 없어 이 source 는 건너뜁니다 (원본 그대로)"],
            "cells": [], "cell_total": 0,
        })
    return {
        "totals": {"sources": len(applied), "cells": summary["total_finite"],
                   "warnings": len(warnings) + len(skipped)},
        "sections": sections,
        "removed": [],
    }
