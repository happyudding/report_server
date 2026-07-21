"""Raw Data tab: 메인 payload 용 placeholder + lazy-load 조회/편집 함수."""
from __future__ import annotations

from .common import fmt_type, round_num
from .. import rawvalues
from ..honeyform import DATA_START_ROW, META_COLUMNS as _META_COLUMNS, split_honeyform


def build_raw_data_rows(tables):
    return []


def build_raw_data_columns(tables) -> dict:
    """Raw Data 탭 컬럼 선택 UI용 item 메타 + source 목록 + 전체 die 수."""
    items = {}
    for table in tables:
        for item in table.item_columns:
            items.setdefault(item, {
                "name": item,
                "unit": fmt_type(table.units.get(item)),
                "lolim": round_num(table.lolim.get(item)),
                "hilim": round_num(table.hilim.get(item)),
            })
    return {
        "items": list(items.values()),
        "sources": [t.source for t in tables],
        "total_dies": sum(len(t.data) for t in tables),
        # 프런트가 전송 전 1차 차단에 쓰는 값 규칙 — 규칙 테이블·문안의 단일 진실은 서버다
        # (raw_data.js 는 판정 프리미티브만 복제한다). 세션당 1회만 조회되는 응답이라 비용 무시.
        "value_rules": rawvalues.rules_spec(),
    }


def query_raw_data(tables, *, columns, search="", bin_filter="", source_filter="",
                   column_cap=60, row_cap=20000) -> dict:
    """columns(선택 item) + 필터(search/bin/source) 로 raw data 행을 조회한다.

    columns 개수가 column_cap 을 넘으면 ValueError — 응답 크기(die 수 × 컬럼 수) 폭발 방지.
    row_cap 초과 시 앞부분만 담고 truncated=True 로 명시 (규칙 #6 은 Distribution 전용이라
    여기는 적용 대상 아니며, 대신 사용자에게 잘렸음을 알린다).
    """
    columns = [str(c) for c in (columns or [])]
    if len(columns) > column_cap:
        raise ValueError(f"columns exceeds cap ({len(columns)} > {column_cap})")

    search_norm = str(search or "").strip().lower()
    bin_norm = str(bin_filter or "").strip()
    source_norm = str(source_filter or "").strip()

    rows = []
    total_matched = 0
    truncated = False
    for table in tables:
        if source_norm and table.source != source_norm:
            continue
        # columns 는 프런트에서 선택한 순서 그대로 온다 — 그 순서를 컬럼 출력 순서로 유지한다
        # (table.item_columns 원본 순서가 아니라 사용자가 고른 순서).
        item_set = set(table.item_columns)
        present_cols = [c for c in columns if c in item_set]
        data = table.data
        idx_list = data.index.tolist()

        # 행 단위 iterrows 대신 필터에 쓰는 컬럼만 일괄 변환해 선별한 뒤,
        # 선택된 행에 대해서만 나머지 컬럼을 변환한다.
        serial_list = [fmt_type(v) for v in data["SERIAL"].tolist()]
        dut_list = [fmt_type(v) for v in data["DUT"].tolist()]
        bin_list = [fmt_type(v) for v in data["BIN"].tolist()]

        sel = []
        for pos in range(len(idx_list)):
            if (search_norm and search_norm not in serial_list[pos].lower()
                    and search_norm not in dut_list[pos].lower()):
                continue
            if bin_norm and bin_list[pos] != bin_norm:
                continue
            total_matched += 1
            if len(rows) + len(sel) >= row_cap:
                truncated = True
                continue
            sel.append(pos)

        meta_sel = {
            "SERIAL": [serial_list[p] for p in sel],
            "DUT": [dut_list[p] for p in sel],
            "BIN": [bin_list[p] for p in sel],
        }
        for c in _META_COLUMNS:
            if c not in meta_sel:
                meta_sel[c] = [fmt_type(v) for v in data[c].iloc[sel].tolist()]
        item_sel = {c: [round_num(v) for v in data[c].iloc[sel].tolist()]
                    for c in present_cols}
        for j, pos in enumerate(sel):
            # _row_idx: table.data 내 위치(0-base). 편집 저장 시 어느 행을 고쳐야 하는지
            # 알려주는 내부용 필드 — 프런트는 화면에 표시하지 않고 편집 요청에만 실어 보낸다.
            out = {"SOURCE": table.source, "_row_idx": int(idx_list[pos])}
            for c in _META_COLUMNS:
                out[c] = meta_sel[c][j]
            for c in present_cols:
                out[c] = item_sel[c][j]
            rows.append(out)
    return {"rows": rows, "total_matched": total_matched, "truncated": truncated}


_ERROR_LIST_CAP = 5      # 배너가 좁아 이 건수까지만 나열하고 나머지는 건수로만 알린다


def _row_label(table, row_idx) -> str:
    """편집 위치를 사람이 읽는 문자열로 — 프런트 rawRowLabel 과 같은 조합.

    diff 모달의 '위치' 컬럼과 서버 오류 메시지가 같은 표기를 써야 사용자가 어느 줄인지
    바로 찾는다 (SOURCE · SHOT · DUT · (X,Y) · BIN).
    """
    row = table.data.iloc[row_idx]
    parts = [table.source]
    for label in ("SHOT", "DUT"):
        value = fmt_type(row.get(label))
        if value:
            parts.append(f"{label} {value}")
    x, y = fmt_type(row.get("XPOS")), fmt_type(row.get("YPOS"))
    if x or y:
        parts.append(f"(X,Y)=({x},{y})")
    bin_value = fmt_type(row.get("BIN"))
    if bin_value:
        parts.append(f"BIN {bin_value}")
    return " · ".join(parts)


def apply_raw_data_edits(tables, edits):
    """편집 목록을 tables 에 반영한 HoneyformTable 리스트를 반환한다.

    주의: 원본 table.df 를 in-place 로 수정한다 (사본이 아님). 호출자는 이 함수가
    tables 를 변형시킨다는 점을 전제로 써야 한다 — service.edit_raw_data 는 매 요청마다
    parquet 원본을 새로 디코드해 tables 를 만들므로 in-place 변형이 다음 요청에 새지 않는다.

    edits: [{"source", "row_idx", "column", "value"}, ...]. source 는 반드시 tables 중
    하나와 일치해야 하고, column 은 그 테이블의 item_columns 또는 메타 컬럼이어야 한다.
    편집이 있었던 source 는 df(원본 7-meta 프레임) 를 고쳐 split_honeyform 으로 재구성해
    .data 등 파생 필드까지 일관되게 갱신한다.

    **값 검증(rawvalues.check_cell_value)은 편집한 셀만 본다** — 업로드 당시 통과한 기존
    데이터를 나중 편집이 소급 거부하면 안 되기 때문. 전건을 먼저 검증하고 위반이 하나라도
    있으면 **한 셀도 쓰지 않고** ValueError 를 올린다(라우트에서 400, 원본 무손상).
    """
    by_source = {t.source: t for t in tables}
    planned = []          # (table, row_idx, column, 정규화된 값)
    errors = []
    for e in edits or []:
        source = str(e.get("source") or "")
        table = by_source.get(source)
        if table is None:
            raise ValueError(f"unknown source: {source}")
        column = str(e.get("column") or "")
        is_item = column in table.item_columns
        if not is_item and column not in _META_COLUMNS:
            raise ValueError(f"unknown column: {column}")
        try:
            row_idx = int(e.get("row_idx"))
        except (TypeError, ValueError):
            raise ValueError(f"invalid row_idx: {e.get('row_idx')!r}")
        if not (0 <= row_idx < len(table.data)):
            raise ValueError(f"row_idx out of range: {row_idx}")

        value = e.get("value")
        reason = rawvalues.check_cell_value(column, value, is_item=is_item)
        if reason:
            # 라벨 조립은 나열할 건수까지만 — 위반이 수백 건이면 라벨 비용이 아깝다.
            if len(errors) < _ERROR_LIST_CAP:
                errors.append(f"{_row_label(table, row_idx)} → [{column}] {reason}")
            else:
                errors.append("")
            continue
        planned.append((table, row_idx, column,
                        rawvalues.normalize_cell_value(column, value, is_item=is_item)))

    if errors:
        listed = [m for m in errors if m][:_ERROR_LIST_CAP]
        head = "\n".join(f"· {m}" for m in listed)
        more = f"\n· 외 {len(errors) - len(listed)}건" if len(errors) > len(listed) else ""
        raise ValueError(
            f"값이 올바르지 않아 저장하지 않았습니다 "
            f"({len(errors)}건 / 전체 {len(edits or [])}건).\n{head}{more}")

    touched = set()
    for table, row_idx, column, value in planned:
        table.df.at[DATA_START_ROW + row_idx, column] = value
        touched.add(table.source)

    for source in touched:
        t = by_source[source]
        by_source[source] = split_honeyform(t.df, source=t.source, file_name=t.file_name)

    return [by_source[t.source] for t in tables]

