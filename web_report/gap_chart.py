"""Gap Chart — 사용자 수식으로 만든 파생 분포 (2026-08-24).

Distribution 탭에서 사용자가 `(VREF_TRIM - 2) / LDO_VOUT` 같은 식을 조립하면, 그 결과를
raw data(honeyform tables)에서 조회 시점에 계산해 **Item_detail 과 같은 모양의 응답**으로
돌려준다. 원본 parquet 도 DB 도 바꾸지 않는다 — 저장되는 것은 수식(토큰 배열)뿐이고
(`edits.KIND_GAP_CHART`), 값은 요청마다 numpy 벡터 연산으로 다시 만든다.

⚠️ **이 모듈을 `web_report/tabs/` 로 옮기지 말 것.** perf_guard 의 `S01-report-schema` 는
`web_report/tabs/**/*.py` 를 건드린 diff 에 `REPORT_SCHEMA_VERSION` bump 를 요구하는데
(신규 파일도 `git ls-files --others` 로 diff 에 들어온다), 그 bump 자체가 전 세션 report
캐시를 죽이는 콜드 폭풍이다. Gap Chart 는 report payload 를 전혀 바꾸지 않으므로 bump 가
필요 없고, 그래서 tabs 밖에 있다. `tabs.cpk._stats` 를 import 하는 것은 tabs 파일을
수정하는 게 아니라 S01 과 무관하다.

**수식은 평문이 아니라 토큰 배열로 저장한다.** item 이름에는 공백·`( )`·`+ - * /` 가 전부
합법이고(honeyform 은 중복·메타충돌만 검사한다) source 명·item 명 둘 다 `_` 를 포함할 수
있어서, `IDD (1.8V) - PRE + 2` 같은 평문을 원래 토큰으로 되돌리는 렉서는 원리적으로 존재할
수 없다. 토큰 배열은 `source` 가 별도 필드라 구분자 자체가 필요 없다(dist_composite 가
pairKey 에 U+001F 를 쓰는 것과 같은 문제를 아예 회피). `render_formula` 가 만드는 표시
문자열은 **절대 재파싱하지 않는다**.

수식 모드는 저장하지 않고 매번 유도한다(파생값을 저장하면 드리프트한다 — CLAUDE.md 규칙 13).
  per_source : item 참조가 전부 "항목만" → 선택된 각 source 안에서 계산, 시리즈 N개
  explicit   : item 참조가 전부 "source 명시" → 좌표 교집합으로 계산, 시리즈 1개
  mixed      : 둘이 섞임 → 거부(400). 같은 수식이 source 마다 다른 의미가 되어 읽을 수 없다.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from .tabs.common import PASS_BIN, bin_types, fmt_type, num, round_num, to_coord
from .tabs.cpk import CPK_THRESHOLD, _stats

# 상한 — 파싱·평가 비용을 상수로 묶는다. service 의 sanitize 가 같은 값을 쓴다.
MAX_TOKENS = 200
MAX_DEPTH = 16
MAX_REFS = 20
MAX_NAME = 200

_OPS = {"+": np.add, "-": np.subtract, "*": np.multiply, "/": np.divide}
_OP_TEXT = {"+": "+", "-": "-", "*": "×", "/": "÷"}


class GapFormulaError(ValueError):
    """수식 문법 오류. ``index`` 는 문제가 된 토큰 위치(0-based, 모르면 None).

    프런트가 그 인덱스의 칩만 빨갛게 칠할 수 있게 위치를 함께 담는다."""

    def __init__(self, message: str, index=None):
        super().__init__(message)
        self.index = index


# ── 토큰 정규화 ───────────────────────────────────────────────────────────────

def normalize_tokens(raw) -> list:
    """저장/평가에 쓸 토큰 배열로 화이트리스트 재조립. 위반은 GapFormulaError.

    받아들이는 토큰 5종:
      {"t":"num","v":<유한수>} / {"t":"item","item":str} / {"t":"item","source":str,"item":str}
      {"t":"op","v":"+"|"-"|"*"|"/"} / {"t":"lp"} / {"t":"rp"}
    모르는 키는 버린다(dist_composite sanitize 와 같은 방식 — 통과시키면 나중에 의미가 생긴다).
    """
    if not isinstance(raw, list) or not raw:
        raise GapFormulaError("수식이 비어 있습니다")
    if len(raw) > MAX_TOKENS:
        raise GapFormulaError(f"수식이 너무 깁니다 (토큰 {MAX_TOKENS}개 이하)")
    out = []
    for i, tok in enumerate(raw):
        if not isinstance(tok, dict):
            raise GapFormulaError(f"{i + 1}번째 토큰의 형식이 올바르지 않습니다", i)
        kind = tok.get("t")
        if kind == "num":
            value = num(tok.get("v"))
            if value is None or not np.isfinite(value):
                raise GapFormulaError(f"{i + 1}번째 토큰의 숫자가 올바르지 않습니다", i)
            out.append({"t": "num", "v": float(value)})
        elif kind == "item":
            item = str(tok.get("item") or "").strip()
            if not item or len(item) > MAX_NAME:
                raise GapFormulaError(f"{i + 1}번째 항목 이름이 올바르지 않습니다", i)
            clean = {"t": "item", "item": item}
            source = str(tok.get("source") or "").strip()
            if source:
                if len(source) > MAX_NAME:
                    raise GapFormulaError(f"{i + 1}번째 source 이름이 너무 깁니다", i)
                clean["source"] = source
            out.append(clean)
        elif kind == "op":
            op = str(tok.get("v") or "")
            if op not in _OPS:
                raise GapFormulaError(f"{i + 1}번째 연산자를 쓸 수 없습니다 (+ - * / 만 가능)", i)
            out.append({"t": "op", "v": op})
        elif kind in ("lp", "rp"):
            out.append({"t": kind})
        else:
            raise GapFormulaError(f"{i + 1}번째 토큰의 종류를 알 수 없습니다", i)
    refs = item_refs(out)
    if not refs:
        raise GapFormulaError("항목을 하나 이상 넣어야 합니다")
    if len(refs) > MAX_REFS:
        raise GapFormulaError(f"참조 항목이 너무 많습니다 ({MAX_REFS}개 이하)")
    parse_tokens(out)          # 문법까지 통과해야 저장한다
    return out


def item_refs(tokens) -> list:
    """수식이 참조하는 (source|"", item) 고유 목록 — 등장 순서 보존."""
    seen, out = set(), []
    for tok in tokens:
        if tok.get("t") != "item":
            continue
        key = (tok.get("source") or "", tok["item"])
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def formula_mode(tokens) -> str:
    """"per_source" | "explicit" | "mixed" — 저장하지 않고 매번 유도한다."""
    qualified = any((t.get("source") or "") for t in tokens if t.get("t") == "item")
    plain = any(not (t.get("source") or "") for t in tokens if t.get("t") == "item")
    if qualified and plain:
        return "mixed"
    return "explicit" if qualified else "per_source"


def render_formula(tokens) -> str:
    """표시용 문자열. **재파싱 대상이 아니다**(위 모듈 docstring)."""
    parts = []
    for tok in tokens:
        kind = tok.get("t")
        if kind == "num":
            value = tok["v"]
            parts.append(str(int(value)) if float(value).is_integer() else str(value))
        elif kind == "item":
            source = tok.get("source") or ""
            parts.append(f"{source}_{tok['item']}" if source else tok["item"])
        elif kind == "op":
            parts.append(_OP_TEXT[tok["v"]])
        elif kind == "lp":
            parts.append("(")
        elif kind == "rp":
            parts.append(")")
    text = " ".join(parts)
    return text.replace("( ", "(").replace(" )", ")")


def spec_digest(spec) -> str:
    """정의 필드(name/sources/tokens/limit)의 정준 JSON sha256 앞 16자.

    updated_by/updated_at 은 **제외한다** — 같은 정의를 다시 저장했을 뿐인데 캐시가
    죽지 않게. 이름은 응답 subject 로 나가므로 포함한다.

    **캐시 키와 ETag 가 이 한 함수만 쓴다** — 라우트가 따로 조립하면 두 값이 갈려
    "수식을 고쳤는데 옛 숫자가 그대로"가 난다(캐시 키만 갱신되면 브라우저가 304 로 옛
    응답을 재사용하고, ETag 만 갱신되면 서버 캐시가 옛 bytes 를 돌려준다)."""
    core = {k: spec.get(k) for k in ("name", "sources", "tokens", "limit")}
    canon = json.dumps(core, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


# ── 파서 (재귀하강) ───────────────────────────────────────────────────────────
#
# expr   := term  (('+'|'-') term)*
# term   := unary (('*'|'/') unary)*
# unary  := ('-'|'+')? primary
# primary:= num | item | '(' expr ')'
#
# shunting-yard 가 아니라 재귀하강인 이유: 위반 토큰의 **인덱스**를 알 수 있어
# "3번째 토큰 뒤에 …" 같은 한국어 메시지를 그대로 400 으로 돌려줄 수 있다.
# AST 노드: ("num", v) / ("ref", source|"", item) / ("bin", op, l, r) / ("neg", node)

class _Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def parse_expr(self, depth):
        node = self.parse_term(depth)
        while True:
            tok = self.peek()
            if not tok or tok.get("t") != "op" or tok["v"] not in "+-":
                return node
            self.i += 1
            node = ("bin", tok["v"], node, self.parse_term(depth))

    def parse_term(self, depth):
        node = self.parse_unary(depth)
        while True:
            tok = self.peek()
            if not tok or tok.get("t") != "op" or tok["v"] not in "*/":
                return node
            self.i += 1
            node = ("bin", tok["v"], node, self.parse_unary(depth))

    def parse_unary(self, depth):
        tok = self.peek()
        if tok and tok.get("t") == "op" and tok["v"] in "+-":
            self.i += 1
            node = self.parse_unary(depth)
            return ("neg", node) if tok["v"] == "-" else node
        return self.parse_primary(depth)

    def parse_primary(self, depth):
        tok = self.peek()
        if tok is None:
            raise GapFormulaError("수식이 끝나지 않았습니다 — 항목이나 숫자가 필요합니다",
                                  len(self.toks) - 1 if self.toks else None)
        kind = tok.get("t")
        if kind == "num":
            self.i += 1
            return ("num", tok["v"])
        if kind == "item":
            self.i += 1
            return ("ref", tok.get("source") or "", tok["item"])
        if kind == "lp":
            if depth + 1 > MAX_DEPTH:
                raise GapFormulaError(f"괄호가 너무 깊습니다 ({MAX_DEPTH}단 이하)", self.i)
            self.i += 1
            node = self.parse_expr(depth + 1)
            close = self.peek()
            if not close or close.get("t") != "rp":
                raise GapFormulaError("닫는 괄호가 없습니다", self.i)
            self.i += 1
            return node
        at = self.i
        if kind == "rp":
            raise GapFormulaError(f"{at + 1}번째 위치에 여는 괄호가 없습니다", at)
        raise GapFormulaError(f"{at + 1}번째 위치에 항목이나 숫자가 필요합니다", at)


def parse_tokens(tokens):
    """토큰 배열 → AST. 문법 위반은 GapFormulaError(index 포함)."""
    parser = _Parser(tokens)
    node = parser.parse_expr(0)
    if parser.i < len(parser.toks):
        at = parser.i
        raise GapFormulaError(f"{at + 1}번째 토큰 앞에 연산자가 필요합니다", at)
    return node


def _evaluate(node, column_of):
    """AST 평가 — 잎(ref)은 ``column_of(source, item)`` 이 주는 float64 배열.

    0 나눗셈은 예외로 막지 않고 inf/NaN 을 만든 뒤 호출부의 유한값 마스크가 걸러낸다
    (`scatter_item` 의 finite_mask 와 같은 규약)."""
    kind = node[0]
    if kind == "num":
        return np.float64(node[1])
    if kind == "ref":
        return column_of(node[1], node[2])
    if kind == "neg":
        return np.negative(_evaluate(node[1], column_of))
    left = _evaluate(node[2], column_of)
    right = _evaluate(node[3], column_of)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return _OPS[node[1]](left, right)


# ── 계산 ──────────────────────────────────────────────────────────────────────

def _float_col(table, item):
    """item 컬럼 → float64 배열. int64 오버플로 방지 + 비수치(P/F 텍스트) 자동 NaN."""
    return pd.to_numeric(table.data[item], errors="coerce").to_numpy().astype(np.float64)


def _limit_pair(spec):
    limit = spec.get("limit") or {}
    if str(limit.get("mode") or "") != "manual":
        return None, None
    return num(limit.get("lo")), num(limit.get("hi"))


def _series_entry(name, values, serial, xpos, ypos, lo, hi):
    """values(유한값만) + hover meta → sources/stats 항목 한 쌍.

    통계 모집단은 **표시 분포와 같다** — gap 값은 합성이라 "양품 기준 CPK" 라는 기존 규약이
    대응할 원본 항목이 없고, 다른 화면에 같은 값이 나오지도 않는다(규칙 13 무관). 양품만
    보려면 사용자가 Bin1 토글을 쓴다(그때는 disp mask 자체가 좁아진다)."""
    stat = _stats(pd.Series(values), lo, hi)
    degenerate = (stat["n"] is None or stat["n"] < 2
                  or stat["stdev"] is None or stat["stdev"] <= 0)
    source = {
        "name": name,
        "values": np.round(values, 6).tolist(),
        "serial": [fmt_type(v) for v in serial],
        "xpos": [fmt_type(v) for v in xpos],
        "ypos": [fmt_type(v) for v in ypos],
    }
    return source, {"source": name, "degenerate": degenerate, **stat}


def _bin1_keep(table, bin1, bin1_sources):
    """Bin1 토글이 이 소스에 걸리는가."""
    return bool(bin1) and (bin1_sources is None or table.source in bin1_sources)


def _coord_index(table):
    """{(x, y): 행 인덱스} — 좌표 중복(재검)은 **첫 행 우선**.

    tabs/compare.py `_coord_bin_map` 과 문자 그대로 같은 규칙이다. 뒤집으면 에러 없이
    숫자만 조용히 달라진다."""
    xs = table.data["XPOS"].to_numpy()
    ys = table.data["YPOS"].to_numpy()
    out = {}
    for i in range(len(xs)):
        coord = to_coord(xs[i], ys[i])
        if coord is not None and coord not in out:
            out[coord] = i
    return out


def _build_per_source(tables, spec, node, refs, bin1, bin1_sources):
    by_name = {t.source: t for t in tables}
    lo, hi = _limit_pair(spec)
    sources, stats, missing, matched = [], [], [], {}
    dropped = 0
    for name in spec.get("sources") or []:
        table = by_name.get(name)
        if table is None:
            missing.append(f"{name}: 이 세션에 없는 source")
            continue
        absent = [item for _, item in refs if item not in table.item_columns]
        if absent:
            missing.append(f"{name}: {', '.join(absent)} 없음")
            continue
        cols = {item: _float_col(table, item) for _, item in refs}
        result = np.asarray(_evaluate(node, lambda _s, item: cols[item]), dtype=np.float64)
        keep = np.isfinite(result)
        if _bin1_keep(table, bin1, bin1_sources):
            keep = keep & np.asarray([b == PASS_BIN for b in bin_types(table)], dtype=bool)
        dropped += int(np.count_nonzero(~np.isfinite(result)))
        data = table.data
        entry, stat = _series_entry(
            name, result[keep], data["SERIAL"].to_numpy()[keep],
            data["XPOS"].to_numpy()[keep], data["YPOS"].to_numpy()[keep], lo, hi)
        sources.append(entry)
        stats.append(stat)
        matched[name] = int(keep.sum())
    return sources, stats, missing, matched, dropped


def _build_explicit(tables, spec, node, refs, bin1, bin1_sources):
    """source 명시 수식 — 참조 소스들의 **좌표 교집합**에서 1개 시리즈."""
    by_name = {t.source: t for t in tables}
    lo, hi = _limit_pair(spec)
    ref_sources = []
    for source, _item in refs:
        if source not in ref_sources:
            ref_sources.append(source)
    missing = []
    for source in ref_sources:
        table = by_name.get(source)
        if table is None:
            missing.append(f"{source}: 이 세션에 없는 source")
            continue
        absent = [i for s, i in refs if s == source and i not in table.item_columns]
        if absent:
            missing.append(f"{source}: {', '.join(absent)} 없음")
    if missing:
        return [], [], missing, {}, 0

    index_of = {s: _coord_index(by_name[s]) for s in ref_sources}
    base = ref_sources[0]
    # 첫 참조 소스의 **행 순서**로 정렬해 결정성을 확보한다(dict 는 삽입 순서 = 행 순서).
    coords = [c for c in index_of[base] if all(c in index_of[s] for s in ref_sources[1:])]
    if not coords:
        return [], [], ["두 source 에 공통인 die 좌표가 없습니다"], {}, 0

    rows_of = {s: np.asarray([index_of[s][c] for c in coords], dtype=np.int64)
               for s in ref_sources}
    cols = {(s, i): _float_col(by_name[s], i)[rows_of[s]] for s, i in refs}
    result = np.asarray(_evaluate(node, lambda s, i: cols[(s, i)]), dtype=np.float64)
    keep = np.isfinite(result)
    base_table = by_name[base]
    if _bin1_keep(base_table, bin1, bin1_sources):
        flags = np.asarray([b == PASS_BIN for b in bin_types(base_table)], dtype=bool)
        keep = keep & flags[rows_of[base]]
    dropped = int(np.count_nonzero(~np.isfinite(result)))
    data = base_table.data
    name = str(spec.get("name") or "Gap")
    entry, stat = _series_entry(
        name, result[keep], data["SERIAL"].to_numpy()[rows_of[base]][keep],
        data["XPOS"].to_numpy()[rows_of[base]][keep],
        data["YPOS"].to_numpy()[rows_of[base]][keep], lo, hi)
    return [entry], [stat], [], {name: int(keep.sum())}, dropped


def build_gap_item(tables, spec, *, chart_id="", bin1=False, bin1_sources=None) -> dict:
    """수식 정의 → Item_detail 과 **같은 구조**의 응답.

    `tabs.distribution.scatter_item` 의 반환 키를 그대로 포함해야 프런트가 그 화면을
    수정 없이 재사용한다(테스트가 키 집합 포함 관계를 기계로 고정한다).
    `fail_rows`/`is_fail` 은 항상 비어 있다 — gap 값에는 FAILTNO 로 귀속할 원본 항목이 없다.
    """
    tokens = spec.get("tokens") or []
    mode = formula_mode(tokens)
    if mode == "mixed":
        raise GapFormulaError(
            "항목만 쓴 참조와 source 를 붙인 참조를 한 수식에 섞을 수 없습니다")
    node = parse_tokens(tokens)
    refs = item_refs(tokens)
    if mode == "per_source":
        sources, stats, missing, matched, dropped = _build_per_source(
            tables, spec, node, refs, bin1, bin1_sources)
    else:
        sources, stats, missing, matched, dropped = _build_explicit(
            tables, spec, node, refs, bin1, bin1_sources)

    lo, hi = _limit_pair(spec)
    cpks = [s["cpk"] for s in stats if s.get("cpk") is not None]
    cpk = min(cpks) if cpks else None
    return {
        "subject": str(spec.get("name") or "Gap"),
        # 차트 주석 키 네임스페이스 — `cdf:<subject>` 가 동명의 실제 항목과 섞이지 않게
        # chart_notes.js 가 이 값을 우선한다. 서버 _CHART_KEY_RE 는 이미 gap: 을 허용한다.
        "note_subject": f"gap:{chart_id}" if chart_id else "",
        "is_gap": True,
        "gap_id": str(chart_id or ""),
        "gap_mode": mode,
        "formula": render_formula(tokens),
        "matched_dies": int(sum(matched.values())),
        "matched_by_source": matched,
        "dropped_nonfinite": dropped,
        "missing": missing,
        "test_num": "",
        "units": "",
        "lower_limit": round_num(lo),
        "upper_limit": round_num(hi),
        "cpk": round_num(cpk, 3),
        "is_fail": False,
        "status": "cpk_low" if (cpk is not None and cpk < CPK_THRESHOLD) else "ok",
        "sources": sources,
        "stats": stats,
        "fail_rows": [],
        "fail_total": 0,
        "fail_truncated": False,
    }
