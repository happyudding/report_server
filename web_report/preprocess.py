"""세션 전처리 (항목 제외 · outlier 마스킹 · 셀 패치 · 조건 일괄 규칙) — 조회 경로 전용 순수 모듈.

Honey 클라의 Rawdata 허브/빠른 수정 다이얼로그가 세션 단위로 저장하는 "전처리 옵션"을
조회 시점에 tables 에 적용한다. **원본 parquet 은 건드리지 않는다** — 옵션을 비우면 즉시
원래 값으로 돌아온다 (되돌릴 수 있는 편집).

spec 형태 (세션 편집 DB kind='preprocess', item_key='spec' 의 JSON):
    {"exclude_items": ["ITEM_A", ...],
     "outlier": {"mode": "stdev", "k": 50.0},
     "edits": [{"source": "CP1", "row_idx": 12, "column": "VREF", "value": "3.3"}],
     "rules": [{"where": {"source": "CP1",
                          "conds": [{"field": "DUT", "op": "in", "values": ["3"]},
                                    {"field": "item", "item": "VREF", "op": ">",
                                     "value": 4.5}]},
                "action": {"op": "clear", "target": "VREF"}}]}

적용 순서는 **① edits → ② rules → ③ exclude_items → ④ outlier** 다 (규칙은 셀 패치가
반영된 값 기준으로 평가하고, outlier 통계는 규칙으로 걸러진 잔존 die 기준으로 낸다).

  - edits         : 표에서 고친 셀 1개씩. 원본이 불변이라 ``(source, row_idx)`` 가 안정
                    식별자다 (row_idx = table.data 의 0-base 행 위치).
                    **원본을 실제로 바꾸는 편집(Excel 왕복·웹 셀편집)이 들어오면 행 위치가
                    어긋나므로 서버가 이 키만 해제한다** — rules 는 조건 기반이라 유지된다.
  - rules         : 조건 일괄 수정. 한 규칙 안 ``conds`` 는 AND, 규칙 리스트는 **적힌 순서대로**
                    적용된다(뒤 규칙은 앞 규칙 결과 위에서 평가). action.op =
                    set/clear/offset/scale(값 변경) 또는 exclude_rows(die 제외).
  - exclude_items : 그 item 컬럼을 리포트 전 탭에서 제거 (die/행은 그대로)
  - outlier       : 항목별 mean ± k·stdev 밖의 **측정값만 결측(NaN)** 처리.

값 자체의 유효성(숫자 표기·빈값 금지 등)은 여기서 보지 않는다 — 저장 시점에 서버가
rawvalues 로 검사·정규화한 값만 spec 에 들어온다. 이 모듈의 normalize 는 **구조**만 본다.

캐시·저장소·flask·xlwings 무의존 — dist_blob.py / rawvalues.py 와 같은 클라 공유
모듈이다 (Honey 가 dist blob 프리컴퓨트·미리보기에서 같은 코드를 돌려 값 일치를
구조적으로 보장한다).
"""
from __future__ import annotations

import hashlib

from .validation import canon

OUTLIER_MODE_STDEV = "stdev"
_DIGEST_LEN = 12

# 조건에 쓸 수 있는 필드 — honeyform 메타 7열 + "item"(측정 항목, cond["item"] 로 항목명 지정).
# source 범위는 조건이 아니라 where["source"] 로 지정한다 (규칙 하나가 한 소스에만 걸리는
# 경우가 대부분이라 조건 목록에 매번 넣는 것보다 짧다).
META_FIELDS = ("SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO")
_SET_OPS = ("in", "not_in")
_CMP_OPS = (">", ">=", "<", "<=")
_VALUE_ACTIONS = ("set", "clear", "offset", "scale")

# 상한 — 초과분은 정규화 단계에서 잘라낸다(서버 save_preprocess 는 400 으로 먼저 거부).
MAX_EDITS = 10_000
MAX_RULES = 50


def normalize(spec) -> dict:
    """저장/전달된 spec 을 정규형으로. 의미 없는 값은 전부 떨어져 **빈 dict** 가 된다.

    빈 dict = 전처리 없음 = 기존 동작(캐시 키·코드 경로 완전 동일)이라, "옵션이 실질적으로
    없는 상태"를 여기 한 곳에서 판정하는 것이 중요하다.
    """
    if not isinstance(spec, dict):
        return {}
    out = {}

    excluded = spec.get("exclude_items")
    if isinstance(excluded, (list, tuple, set)):
        names = sorted({str(v).strip() for v in excluded if str(v).strip()})
        if names:
            out["exclude_items"] = names

    outlier = spec.get("outlier")
    if isinstance(outlier, dict):
        mode = str(outlier.get("mode") or OUTLIER_MODE_STDEV).strip()
        try:
            k = float(outlier.get("k"))
        except (TypeError, ValueError):
            k = 0.0
        # k<=0 / NaN / inf 는 "해제"로 본다 (빈칸 입력 = 옵션 끄기).
        if mode == OUTLIER_MODE_STDEV and k > 0 and k == k and k != float("inf"):
            out["outlier"] = {"mode": OUTLIER_MODE_STDEV, "k": k}

    edits = _normalize_edits(spec.get("edits"))
    if edits:
        out["edits"] = edits

    rules = _normalize_rules(spec.get("rules"))
    if rules:
        out["rules"] = rules

    return out


def _finite(value):
    """숫자로 읽고 NaN/inf 를 배제. 실패하면 None."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f


def _normalize_edits(raw) -> list:
    """셀 패치 목록 정규화. 같은 셀을 여러 번 고쳤으면 마지막 값만 남는다.

    (source, row_idx, column) 오름차순으로 정렬해 돌려준다 — 클라가 보낸 순서와 무관하게
    같은 편집 집합이면 같은 digest 가 나와야 캐시가 헛돌지 않는다.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    by_key = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        column = str(item.get("column") or "").strip()
        if not source or not column:
            continue
        try:
            row_idx = int(item.get("row_idx"))
        except (TypeError, ValueError):
            continue
        if row_idx < 0:
            continue
        value = item.get("value")
        by_key[(source, row_idx, column)] = "" if value is None else str(value).strip()
    return [{"source": s, "row_idx": r, "column": c, "value": v}
            for (s, r, c), v in sorted(by_key.items())][:MAX_EDITS]


def _normalize_rules(raw) -> list:
    """조건 일괄 규칙 목록 정규화. **순서를 보존한다**(뒤 규칙이 앞 결과 위에서 평가됨).

    잘못된 규칙은 조용히 버린다 — 저장 시점 검증은 서버(save_preprocess)가 하고,
    여기서는 옛 spec/손상된 값이 조회 경로를 죽이지 않게 하는 것이 목적이다.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for rule in raw:
        norm = _normalize_rule(rule)
        if norm:
            out.append(norm)
        if len(out) >= MAX_RULES:
            break
    return out


def normalize_where(where) -> dict | None:
    """조건 묶음(where)만 정규화. 조건이 없거나 해석 불가면 None.

    빠른 수정 다이얼로그의 **조회 필터**와 **규칙 조건**이 같은 구조·같은 판정을 쓰도록
    공개한다 ("지금 조회한 행에 이 동작을 건다" 가 그대로 규칙이 되는 것이 설계 요점).
    """
    if not isinstance(where, dict):
        return None
    conds = []
    for cond in where.get("conds") or ():
        norm = _normalize_cond(cond)
        if norm is None:
            return None          # 조건 하나라도 해석 불가면 묶음 전체를 버린다
        conds.append(norm)
    if not conds:
        # 조건 없는 규칙은 전 행이 대상이 되어 exclude_rows 가 소스를 통째로 비운다.
        # "조건 일괄 수정"의 의미가 아니므로 여기서 막는다.
        return None
    out = {"conds": conds}
    source = str(where.get("source") or "").strip()
    if source:
        out["source"] = source
    return out


def match_rows(table, where):
    """table 에서 조건에 맞는 행의 bool 배열 (numpy). 대상이 아니거나 조건 불가면 None.

    빠른 수정 다이얼로그가 조회 필터·규칙 미리보기에 쓴다 — 서버의 규칙 적중 판정과
    같은 함수를 타므로 "화면에서 본 N행"과 "저장 후 바뀌는 N행"이 어긋나지 않는다.
    """
    norm = normalize_where(where)
    return None if norm is None else _rule_mask(table.data, norm, table)


def _normalize_rule(rule):
    if not isinstance(rule, dict):
        return None
    where = normalize_where(rule.get("where"))
    if where is None:
        return None
    action = _normalize_action(rule.get("action"))
    if action is None:
        return None
    return {"where": where, "action": action}


def _normalize_cond(cond):
    if not isinstance(cond, dict):
        return None
    field = str(cond.get("field") or "").strip()
    op = str(cond.get("op") or "").strip()
    if field == "item":
        name = str(cond.get("item") or "").strip()
        if not name:
            return None
        out = {"field": "item", "item": name, "op": op}
    elif field in META_FIELDS:
        out = {"field": field, "op": op}
    else:
        return None

    if op in _SET_OPS:
        values = cond.get("values")
        if not isinstance(values, (list, tuple, set)):
            return None
        # 값 비교는 fmt_type 정규화 문자열 집합이라 순서·중복이 의미 없다 → 정렬 집합.
        out["values"] = sorted({str(v).strip() for v in values})
        return out if out["values"] else None
    if op in _CMP_OPS:
        value = _finite(cond.get("value"))
        if value is None:
            return None
        out["value"] = value
        return out
    if op == "spec_out":
        return out if field == "item" else None      # 규격은 측정 항목에만 있다
    return None


def _normalize_action(action):
    if not isinstance(action, dict):
        return None
    op = str(action.get("op") or "").strip()
    if op == "exclude_rows":
        return {"op": op}
    if op not in _VALUE_ACTIONS:
        return None
    target = str(action.get("target") or "").strip()
    if not target:
        return None
    if op == "clear":
        return {"op": op, "target": target}
    if op == "set":
        value = action.get("value")
        return {"op": op, "target": target,
                "value": "" if value is None else str(value).strip()}
    value = _finite(action.get("value"))      # offset / scale
    return None if value is None else {"op": op, "target": target, "value": value}


def digest(spec) -> str:
    """정규화된 spec 의 캐시 키 조각. **전처리가 없으면 빈 문자열**.

    빈 문자열이면 cache_policy 빌더가 키에 아무것도 덧붙이지 않아 기존 캐시가 그대로
    유효하다 (무회귀의 핵심).
    """
    norm = normalize(spec)
    if not norm:
        return ""
    return hashlib.sha256(canon(norm)).hexdigest()[:_DIGEST_LEN]


def session_digest(report_db, session_id: str) -> str:
    """세션의 저장된 전처리 spec → 캐시 키 조각 (없으면 빈 문자열).

    캐시 키를 만들기 전에 tables 를 로드할 수 없는 호출부(dist/map/scatter gzip 캐시)가
    쓰는 편의 함수 — 작은 인덱스 SELECT 1회다."""
    from .edits import load_preprocess

    return digest(load_preprocess(report_db, session_id))


def describe(spec) -> str:
    """사용자에게 보여줄 한 줄 요약 (리포트 배지·다이얼로그 공용). 없으면 빈 문자열."""
    norm = normalize(spec)
    parts = []
    if norm.get("exclude_items"):
        parts.append(f"항목 {len(norm['exclude_items'])}개 제외")
    outlier = norm.get("outlier")
    if outlier:
        parts.append(f"outlier ±{_fmt_k(outlier['k'])}σ 제거")
    if norm.get("edits"):
        parts.append(f"셀 수정 {len(norm['edits'])}건")
    if norm.get("rules"):
        parts.append(f"일괄 규칙 {len(norm['rules'])}건")
    return " · ".join(parts)


def _fmt_k(k) -> str:
    f = float(k)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _fmt_num(value) -> str:
    f = float(value)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def describe_rule(rule) -> str:
    """규칙 1건을 사람이 읽는 한 줄로 — 허브 상태 목록·빠른 수정 규칙 목록 공용.

    문안을 클라에서 따로 조립하면 서버 저장값과 표기가 갈리므로 여기 한 곳에 둔다.
    """
    norm = _normalize_rule(rule)
    if not norm:
        return ""
    where, action = norm["where"], norm["action"]
    parts = []
    if where.get("source"):
        parts.append(where["source"])
    parts.extend(_describe_cond(c) for c in where["conds"])
    op, target = action["op"], action.get("target", "")
    if op == "exclude_rows":
        what = "die 제외"
    elif op == "clear":
        what = f"{target} 빈값"
    elif op == "set":
        what = f"{target} = {action['value'] or '(빈값)'}"
    elif op == "offset":
        value = float(action["value"])
        what = f"{target} {'+' if value >= 0 else '-'} {_fmt_num(abs(value))}"
    else:                                   # scale
        what = f"{target} × {_fmt_num(action['value'])}"
    return " · ".join(parts) + " → " + what


def _describe_cond(cond) -> str:
    name = cond["item"] if cond["field"] == "item" else cond["field"]
    op = cond["op"]
    if op == "spec_out":
        return f"{name} 규격 밖"
    if op in _SET_OPS:
        values = ", ".join(v or "(빈값)" for v in cond["values"])
        return f"{name} {'∈' if op == 'in' else '∉'} [{values}]"
    return f"{name} {op} {_fmt_num(cond['value'])}"


# ── 적용 ─────────────────────────────────────────────────────────────────────
def apply_tables(tables, spec):
    """tables 에 전처리를 적용한 새 리스트를 반환. 반환 (tables, stats).

    stats = {"removed": {item: 제거 건수}, "removed_total": int, "excluded": [item, ...],
             "edited_cells": int, "rule_hits": int, "excluded_dies": int}
    — 클라 미리보기와 로그용이다. spec 이 비면 **입력 tables 를 그대로** 돌려준다
    (객체 동일성까지 유지 — 무전처리 경로에 비용 0).

    호출 전제: 표시(조회) 경로 전용. 재인코딩/편집 경로에서 부르지 말 것 —
    전처리된 테이블의 `df`(재인코딩용 전체 프레임)는 None 으로 지워 원본과 다른 값이
    parquet 으로 되돌아가는 사고를 구조적으로 막는다.
    """
    norm = normalize(spec)
    stats = {"removed": {}, "removed_total": 0, "excluded": [],
             "edited_cells": 0, "rule_hits": 0, "excluded_dies": 0}
    if not norm or not tables:
        return tables, stats

    excluded = set(norm.get("exclude_items") or ())
    outlier = norm.get("outlier")
    edits_by_source = {}
    for edit in norm.get("edits") or ():
        edits_by_source.setdefault(edit["source"], []).append(edit)
    rules = norm.get("rules") or []

    out = []
    for table in tables:
        new_table = _apply_one(table, excluded, outlier,
                               edits_by_source.get(table.source) or (), rules, stats)
        out.append(new_table)
    stats["excluded"] = sorted(excluded)
    return out, stats


def _apply_one(table, excluded, outlier, edits, rules, stats):
    """항목 제외는 **item_columns 만** 줄인다 — manifest.selected_items 필터와 동일 의미론.

    메타 dict(tno/step/units/hilim/lolim)와 data 프레임 컬럼은 그대로 둔다. 여기서 메타까지
    지우면 Yield 표의 fail 집계(fail_counts 는 전체 table.tno 기준)가 제외 항목의 fail die 를
    잃어버려 **표 행 합과 수율이 어긋난다** — 제외한 항목의 fail die 도 BIN 상으로는 여전히
    fail 이기 때문이다. 제외는 "그 항목을 분석 대상에서 뺀다"이지 "그 die 를 없앤다"가 아니다.
    (같은 이유로 test_yield_step_selected_items 가 selected_items 경로의 이 동작을 고정한다.)

    셀 패치(edits)·규칙(rules)은 그와 달리 **data 프레임의 값/행 자체**를 바꾼다.
    """
    from .honeyform import HoneyformTable

    item_columns = [c for c in table.item_columns if c not in excluded]
    data = table.data
    if edits:
        data = _apply_edits(data, edits, set(table.item_columns), stats)
    if rules:
        data = _apply_rules(data, rules, table, stats)
    if outlier and item_columns:
        data = _mask_outliers(data, item_columns, float(outlier["k"]), stats)

    return HoneyformTable(
        source=table.source,
        file_name=table.file_name,
        # 전처리 결과는 표시 전용 — 재인코딩 경로가 실수로 쓰지 못하게 df 를 지운다.
        df=None,
        item_columns=item_columns,
        tseq=dict(table.tseq),
        tno=dict(table.tno),
        step=dict(table.step),
        units=dict(table.units),
        hilim=dict(table.hilim),
        lolim=dict(table.lolim),
        data=data,
    )


def _mask_outliers(data, item_columns, k, stats):
    """항목별 mean ± k·stdev 밖 값을 NaN 으로. 바뀐 컬럼이 없으면 원본 프레임 그대로.

    항목 블록 전체를 한 번에 float64 배열로 만들면 (행 × 항목수) 크기의 복사본이 생겨
    mass data 에서 메모리가 터진다 — 컬럼 단위로 돌고, 실제로 바뀐 컬럼만 모아 concat
    1회로 합친다 (컬럼별 대입 반복은 프레임 재배치로 느리다).
    """
    import numpy as np
    import pandas as pd

    changed = {}
    for name in item_columns:
        if name not in data.columns:
            continue
        col = data[name]
        vals = pd.to_numeric(col, errors="coerce").to_numpy(dtype="float64", copy=True)
        finite = np.isfinite(vals)
        n = int(finite.sum())
        if n < 2:
            continue                      # 표본 1개 이하 — 표준편차 정의 불가
        mean = float(vals[finite].mean())
        std = float(vals[finite].std(ddof=1))
        if not np.isfinite(std) or std <= 0:
            continue                      # 전부 같은 값 — 제거할 outlier 가 없다
        mask = finite & (np.abs(vals - mean) > k * std)
        removed = int(mask.sum())
        if not removed:
            continue
        vals[mask] = np.nan
        changed[name] = pd.Series(vals, index=data.index, name=name)
        stats["removed"][name] = removed
        stats["removed_total"] += removed

    return _merge_columns(data, changed)


def _merge_columns(data, changed):
    """바뀐 컬럼(Series)만 원본 프레임에 갈아끼운 새 프레임. 바뀐 게 없으면 원본 그대로.

    컬럼별 대입을 반복하면 프레임이 매번 재배치되므로 concat 1회로 합치고 원래 컬럼
    순서를 복원한다 (mass data 에서 이 차이가 크다).
    """
    import pandas as pd

    if not changed:
        return data
    order = list(data.columns)
    keep = [c for c in order if c not in changed]
    merged = pd.concat([data[keep], pd.DataFrame(changed, index=data.index)], axis=1)
    return merged[order]


# ── 셀 패치 (edits) ──────────────────────────────────────────────────────────
def _apply_edits(data, edits, item_set, stats):
    """이 source 의 셀 패치를 적용한 프레임. row_idx 는 data 의 0-base 행 위치.

    범위를 벗어난 row_idx·없는 컬럼은 조용히 건너뛴다 — 원본이 Excel 왕복으로 줄어든
    뒤 남아 있던 패치가 조회를 죽이면 안 되기 때문이다(서버가 그 경우 패치를 해제하지만
    조회 경로는 그것과 무관하게 견뎌야 한다).
    """
    by_column = {}
    for edit in edits:
        by_column.setdefault(edit["column"], []).append((edit["row_idx"], edit["value"]))

    n = len(data)
    changed = {}
    for column, pairs in by_column.items():
        if column not in data.columns:
            continue
        pairs = [(idx, value) for idx, value in pairs if 0 <= idx < n]
        if not pairs:
            continue
        series = _set_cells(data[column], pairs, is_item=column in item_set)
        if series is None:
            continue
        changed[column] = series
        stats["edited_cells"] += len(pairs)
    return _merge_columns(data, changed)


def _set_cells(col, pairs, *, is_item):
    """컬럼 하나의 지정 위치에 값을 써 넣은 새 Series. 쓸 값이 없으면 None.

    dtype 을 함부로 바꾸지 않는 것이 요점이다 — 정수 컬럼에 정수만 들어오면 int64 를
    유지하고(리포트 표기 회귀 방지), 빈값·소수가 섞이면 그때만 float/object 로 넓힌다.
    """
    import numpy as np
    import pandas as pd

    kind = getattr(col.dtype, "kind", "")
    if is_item:
        values = [_finite(v) if str(v).strip() != "" else None for _, v in pairs]
        if kind == "i" and all(v is not None and float(v).is_integer() for v in values):
            arr = col.to_numpy(dtype="int64", copy=True)
            for (idx, _), value in zip(pairs, values):
                arr[idx] = int(value)
        else:
            arr = pd.to_numeric(col, errors="coerce").to_numpy(dtype="float64", copy=True)
            for (idx, _), value in zip(pairs, values):
                arr[idx] = np.nan if value is None else value
        return pd.Series(arr, index=col.index, name=col.name)

    texts = [str(v).strip() for _, v in pairs]
    ints = [_int_text(t) for t in texts]
    if kind in "iu" and all(v is not None for v in ints):
        arr = col.to_numpy(dtype="int64", copy=True)
        for (idx, _), value in zip(pairs, ints):
            arr[idx] = value
        return pd.Series(arr, index=col.index, name=col.name)
    # 빈값·비정수가 섞이면 object 로 넓힌다 — 표시 경로는 전부 fmt_type 을 거치므로
    # 숫자/문자 혼재가 값 판정에 영향을 주지 않는다.
    arr = col.to_numpy(dtype=object, copy=True)
    for (idx, _), text in zip(pairs, texts):
        arr[idx] = text
    return pd.Series(arr, index=col.index, name=col.name)


def _int_text(text):
    """정수로 읽히는 문자열이면 int, 아니면 None ('5.0' 은 5 로 본다)."""
    value = _finite(text)
    if value is None or not float(value).is_integer() or abs(value) >= 2 ** 53:
        return None
    return int(value)


# ── 조건 일괄 규칙 (rules) ───────────────────────────────────────────────────
def _apply_rules(data, rules, table, stats):
    """규칙을 **적힌 순서대로** 적용한 프레임. 뒤 규칙은 앞 규칙 결과 위에서 평가된다."""
    item_set = set(table.item_columns)
    for rule in rules:
        if not len(data):
            break
        mask = _rule_mask(data, rule["where"], table)
        if mask is None:
            continue                      # 이 source 대상 아님 / 컬럼 없음 → 무시
        hits = int(mask.sum())
        if not hits:
            continue
        action = rule["action"]
        if action["op"] == "exclude_rows":
            data = data[~mask].reset_index(drop=True)
            stats["excluded_dies"] += hits
            continue
        new_data = _apply_action(data, mask, action, item_set)
        if new_data is data:
            continue                      # 대상 컬럼 없음 등 — 적중으로 세지 않는다
        data = new_data
        stats["rule_hits"] += hits
    return data


def _rule_mask(data, where, table):
    """규칙 조건(AND)에 걸리는 행의 bool 배열. 적용 대상이 아니면 None."""
    import numpy as np

    source = where.get("source")
    if source and source != table.source:
        return None
    mask = np.ones(len(data), dtype=bool)
    for cond in where["conds"]:
        part = _cond_mask(data, cond, table)
        if part is None:
            return None                   # 조건이 가리키는 컬럼이 없다 → 규칙 미적용
        mask &= part
        if not mask.any():
            break
    return mask


def _cond_mask(data, cond, table):
    """조건 1개의 bool 배열. 대상 컬럼이 없으면 None."""
    import numpy as np
    import pandas as pd

    from .tabs.common import fmt_type

    column = cond["item"] if cond["field"] == "item" else cond["field"]
    if column not in data.columns:
        return None
    col = data[column]

    op = cond["op"]
    if op in _SET_OPS:
        # 비교는 리포트 표기(fmt_type)와 같은 정규형으로 — '1'/'1.0'/1 이 같은 값이어야
        # 사용자가 화면에서 본 값을 그대로 조건에 적을 수 있다.
        wanted = set(cond["values"])
        hit = np.array([fmt_type(v) in wanted for v in col.tolist()], dtype=bool)
        return hit if op == "in" else ~hit

    values = pd.to_numeric(col, errors="coerce").to_numpy(dtype="float64")
    finite = np.isfinite(values)
    if op == "spec_out":
        hilim, lolim = _finite(table.hilim.get(column)), _finite(table.lolim.get(column))
        hit = np.zeros(len(values), dtype=bool)
        if hilim is not None:
            hit |= finite & (values > hilim)
        if lolim is not None:
            hit |= finite & (values < lolim)
        return hit
    # 숫자 비교 — 결측(NaN)은 어느 쪽에도 걸리지 않는다.
    bound = cond["value"]
    if op == ">":
        return finite & (values > bound)
    if op == ">=":
        return finite & (values >= bound)
    if op == "<":
        return finite & (values < bound)
    return finite & (values <= bound)


def _apply_action(data, mask, action, item_set):
    """값 변경 동작(set/clear/offset/scale)을 적중 행에 적용한 프레임."""
    import numpy as np
    import pandas as pd

    target = action["target"]
    if target not in data.columns:
        return data
    is_item = target in item_set
    op = action["op"]
    col = data[target]

    if op in ("offset", "scale"):
        if not is_item:
            return data                   # 측정값 산술은 item 에만 의미가 있다
        values = pd.to_numeric(col, errors="coerce").to_numpy(dtype="float64", copy=True)
        delta = float(action["value"])
        values[mask] = values[mask] + delta if op == "offset" else values[mask] * delta
        series = pd.Series(values, index=col.index, name=col.name)
    elif op == "clear":
        if is_item:
            values = pd.to_numeric(col, errors="coerce").to_numpy(dtype="float64", copy=True)
            values[mask] = np.nan
            series = pd.Series(values, index=col.index, name=col.name)
        else:
            values = col.to_numpy(dtype=object, copy=True)
            values[mask] = ""
            series = pd.Series(values, index=col.index, name=col.name)
    else:                                  # set — 셀 패치와 같은 dtype 규칙을 재사용
        series = _set_cells(col, [(int(i), action["value"]) for i in np.flatnonzero(mask)],
                            is_item=is_item)
        if series is None:
            return data
    return _merge_columns(data, {target: series})
