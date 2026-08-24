"""신규 Item 수식 엔진 — Excel 풍 수식으로 파생 측정 항목을 만든다 (2026-08-24).

Honey 의 Rawdata 허브 `신규 Item(수식) 추가` 탭이 쓰는 파서·평가기다. 사용자가
`IF(VREF_TRIM > MIN(VDD_A, VDD_B), 0, 1)` 같은 식을 조립하면 그 값을 raw data 에서 계산해
**원본 parquet 에 컬럼 하나로 박는다**. Gap Chart 와 달리 결과를 조회 시점에 다시 만들지 않고,
수식도 저장하지 않는다 — 추가되고 나면 일반 item 과 구별되지 않는다.

⚠️ **이 모듈을 `web_report/tabs/` 로 옮기지 말 것.** perf_guard 의 `S01-report-schema` 는
`web_report/tabs/**/*.py` 를 건드린 diff 에 `REPORT_SCHEMA_VERSION` bump 를 요구하는데
(신규 파일도 `git ls-files --others` 로 diff 에 들어온다), 그 bump 자체가 전 세션 report
캐시를 죽이는 콜드 폭풍이다. 이 모듈은 report payload 를 전혀 바꾸지 않는다.
같은 이유로 **`tabs.*` 를 import 하지 않는다** — Honey 클라(honey_ui/excel_edit)가 이 모듈을
import 하므로 의존을 numpy/pandas 로 묶는다(`num()` 은 아래에 자체 구현).

**수식은 평문이 아니라 토큰 배열이다.** item 이름에는 공백·`( )`·`+ - * /` 가 전부 합법이고
(honeyform 은 중복·메타충돌만 검사한다) 따옴표조차 이름에 들어갈 수 있어서, 평문을 원래
토큰으로 되돌리는 렉서는 원리적으로 존재할 수 없다. UI 는 `@` 자동완성으로 고른 항목만
item 토큰으로 만들고, `render_formula` 가 만드는 표시 문자열은 **절대 재파싱하지 않는다**.

**gap_chart.py 와의 관계**: 파서 구조(재귀하강·위반 토큰 인덱스·eval 금지)를 그대로 따르되
함수 호출과 비교 연산자를 더한 **별개 사본**이다. gap_chart 를 확장해 공유하지 않은 이유는
거기 저장된 토큰·에러 문구·spec_digest·ETag 가 운영 세션의 사용자 입력에 걸려 있기 때문이다
(CLAUDE.md §5-12). 사본 드리프트는 `tests/test_formula_item.py` 의 **동치 테스트**가 막는다 —
산술 전용 토큰에 대해 이 모듈과 gap_chart 의 AST·평가 결과가 일치해야 한다.

평가 규약 — **모든 중간값은 float64. bool 배열을 만들지 않는다.**
진리값은 1.0=TRUE / 0.0=FALSE / NaN=알 수 없음 (3-값 논리). Excel 은 빈칸을 0 으로 보지만
여기서는 결측을 전파한다 — 측정값이 없는 die 를 0 으로 보면 판정이 조용히 거짓말을 한다.
"""
from __future__ import annotations

import math
from functools import reduce

import numpy as np
import pandas as pd

# 상한 — 파싱·평가 비용을 상수로 묶는다. 클라 UI 가 같은 값을 복제한다.
MAX_TOKENS = 200
MAX_DEPTH = 16
MAX_REFS = 20
MAX_NAME = 200
MAX_ARGS = 32

_ARITH = {"+": np.add, "-": np.subtract, "*": np.multiply, "/": np.divide}
_ARITH_TEXT = {"+": "+", "-": "-", "*": "×", "/": "÷"}
_CMP = {">": np.greater, ">=": np.greater_equal, "<": np.less,
        "<=": np.less_equal, "=": np.equal, "<>": np.not_equal}
_CMP_TEXT = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "=", "<>": "≠"}

# 함수 → (최소 인자, 최대 인자 | None=무제한)
FUNCS = {
    "IF": (3, 3),
    "MIN": (1, None), "MAX": (1, None), "SUM": (1, None), "AVERAGE": (1, None),
    "AND": (1, None), "OR": (1, None), "NOT": (1, 1),
    "ABS": (1, 1), "SQRT": (1, 1), "ROUND": (1, 2),
}


class FormulaError(ValueError):
    """수식 문법 오류. ``index`` 는 문제가 된 토큰 위치(0-based, 모르면 None).

    UI 가 그 인덱스의 칩만 빨갛게 칠할 수 있게 위치를 함께 담는다
    (gap_chart.GapFormulaError 와 같은 계약)."""

    def __init__(self, message, index=None):
        super().__init__(message)
        self.index = index


def num(value):
    """tabs.common.num 과 같은 규칙 — tabs 를 import 하지 않으려고 여기 둔다."""
    try:
        if value is None:
            return None
        f = float(value)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


# ── 토큰 정규화 ───────────────────────────────────────────────────────────────

def normalize_tokens(raw) -> list:
    """저장/평가에 쓸 토큰 배열로 화이트리스트 재조립. 위반은 FormulaError.

    받아들이는 토큰 7종:
      {"t":"num","v":<유한수>} / {"t":"item","item":str}
      {"t":"op","v":"+|-|*|/|>|>=|<|<=|=|<>"} / {"t":"fn","v":<FUNCS 키>}
      {"t":"lp"} / {"t":"rp"} / {"t":"comma"}
    모르는 키는 버린다 (통과시키면 나중에 의미가 생긴다 — gap_chart 와 같은 방식).
    """
    if not isinstance(raw, list) or not raw:
        raise FormulaError("수식이 비어 있습니다")
    if len(raw) > MAX_TOKENS:
        raise FormulaError(f"수식이 너무 깁니다 (토큰 {MAX_TOKENS}개 이하)")
    out = []
    for i, tok in enumerate(raw):
        if not isinstance(tok, dict):
            raise FormulaError(f"{i + 1}번째 토큰의 형식이 올바르지 않습니다", i)
        kind = tok.get("t")
        if kind == "num":
            value = num(tok.get("v"))
            if value is None:
                raise FormulaError(f"{i + 1}번째 토큰의 숫자가 올바르지 않습니다", i)
            out.append({"t": "num", "v": float(value)})
        elif kind == "item":
            item = str(tok.get("item") or "").strip()
            if not item or len(item) > MAX_NAME:
                raise FormulaError(f"{i + 1}번째 항목 이름이 올바르지 않습니다", i)
            # source 지정은 이 수식에서 의미가 없다 — 신규 item 은 각 source 안에서
            # 그 source 자기 값으로 계산된다(Gap Chart 의 explicit 모드가 없다).
            if str(tok.get("source") or "").strip():
                raise FormulaError(
                    f"{i + 1}번째: 이 수식에서는 source 를 지정할 수 없습니다 "
                    "(모든 source 에서 각각 계산합니다)", i)
            out.append({"t": "item", "item": item})
        elif kind == "op":
            op = str(tok.get("v") or "")
            if op not in _ARITH and op not in _CMP:
                raise FormulaError(
                    f"{i + 1}번째 연산자를 쓸 수 없습니다 (+ - * / > >= < <= = <> 만 가능)", i)
            out.append({"t": "op", "v": op})
        elif kind == "fn":
            name = str(tok.get("v") or "").strip().upper()
            if name not in FUNCS:
                raise FormulaError(
                    f"{i + 1}번째: 쓸 수 없는 함수입니다 "
                    f"({', '.join(sorted(FUNCS))} 만 가능)", i)
            out.append({"t": "fn", "v": name})
        elif kind in ("lp", "rp", "comma"):
            out.append({"t": kind})
        else:
            raise FormulaError(f"{i + 1}번째 토큰의 종류를 알 수 없습니다", i)
    refs = item_refs(out)
    if not refs:
        raise FormulaError("항목을 하나 이상 넣어야 합니다")
    if len(refs) > MAX_REFS:
        raise FormulaError(f"참조 항목이 너무 많습니다 ({MAX_REFS}개 이하)")
    parse_tokens(out)          # 문법까지 통과해야 반환한다
    return out


def item_refs(tokens) -> list:
    """수식이 참조하는 item 이름 고유 목록 — 등장 순서 보존."""
    seen, out = set(), []
    for tok in tokens:
        if tok.get("t") != "item":
            continue
        name = tok["item"]
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def token_text(tok) -> str:
    """토큰 1개의 표시 문자열 (UI 칩 라벨과 render_formula 가 공유한다)."""
    kind = tok.get("t")
    if kind == "num":
        value = tok["v"]
        return str(int(value)) if float(value).is_integer() else str(value)
    if kind == "item":
        return tok["item"]
    if kind == "op":
        op = tok["v"]
        return _ARITH_TEXT.get(op) or _CMP_TEXT[op]
    if kind == "fn":
        return tok["v"]
    if kind == "lp":
        return "("
    if kind == "rp":
        return ")"
    return ","


def render_formula(tokens) -> str:
    """표시용 문자열. **재파싱 대상이 아니다**(모듈 docstring).

    공백은 문자 치환이 아니라 앞뒤 토큰 종류로 결정한다 — item 이름에 괄호·쉼표·공백이
    전부 합법이라 치환으로 다듬으면 이름 안쪽까지 건드린다.
    """
    parts = []
    prev = None
    for tok in tokens:
        kind = tok.get("t")
        text = token_text(tok)
        if prev is not None:
            # 붙여 쓰는 자리: 함수 뒤의 '(' · '(' 다음 · ')' 앞 · ',' 앞
            tight = (kind in ("rp", "comma")
                     or prev == "lp"
                     or (kind == "lp" and prev == "fn"))
            if not tight:
                parts.append(" ")
        parts.append(text)
        prev = kind
    return "".join(parts)


# ── 파서 (재귀하강) ───────────────────────────────────────────────────────────
#
# expr    := cmp
# cmp     := add ( CMPOP add )?          # 비연관 — 2번째 비교 연산자는 거부
# add     := mul ( ('+'|'-') mul )*
# mul     := unary ( ('*'|'/') unary )*
# unary   := ('-'|'+')? primary
# primary := num | item | fn lp args rp | lp expr rp
# args    := ε | expr ( comma expr )*
#
# shunting-yard 가 아니라 재귀하강인 이유: 위반 토큰의 **인덱스**를 알 수 있어
# "3번째 토큰 앞에 …" 같은 한국어 메시지를 그대로 UI 로 돌려줄 수 있다.
#
# 비교를 비연관으로 고정한 이유: Excel 은 `1<2<3` 을 좌결합 + TRUE 승격으로 처리하는데,
# 그 규칙을 따라가려면 "TRUE 는 모든 숫자보다 크다" 같은 Excel 고유 서수까지 흉내내야 한다.
# 대신 명시적으로 거부하고 AND(...) 로 묶으라고 안내한다.
#
# AST 노드: ("num", v) / ("ref", item) / ("neg", node) / ("bin", op, l, r)
#           ("cmp", op, l, r) / ("call", name, [args...])
# 앞 4종은 gap_chart 와 같은 모양이다(("ref",...) 만 arity 가 다르다) — 동치 테스트의 근거.

class _Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def _is_op(self, tok, table):
        return bool(tok) and tok.get("t") == "op" and tok.get("v") in table

    def parse_expr(self, depth):
        node = self.parse_add(depth)
        tok = self.peek()
        if not self._is_op(tok, _CMP):
            return node
        self.i += 1
        node = ("cmp", tok["v"], node, self.parse_add(depth))
        if self._is_op(self.peek(), _CMP):
            raise FormulaError(
                "비교 연산자는 한 수식에 한 번만 쓸 수 있습니다 — "
                "AND(A > B, C > D) 처럼 묶어 주세요", self.i)
        return node

    def parse_add(self, depth):
        node = self.parse_mul(depth)
        while True:
            tok = self.peek()
            if not tok or tok.get("t") != "op" or tok["v"] not in ("+", "-"):
                return node
            self.i += 1
            node = ("bin", tok["v"], node, self.parse_mul(depth))

    def parse_mul(self, depth):
        node = self.parse_unary(depth)
        while True:
            tok = self.peek()
            if not tok or tok.get("t") != "op" or tok["v"] not in ("*", "/"):
                return node
            self.i += 1
            node = ("bin", tok["v"], node, self.parse_unary(depth))

    def parse_unary(self, depth):
        tok = self.peek()
        if tok and tok.get("t") == "op" and tok["v"] in ("+", "-"):
            self.i += 1
            node = self.parse_unary(depth)
            return ("neg", node) if tok["v"] == "-" else node
        return self.parse_primary(depth)

    def parse_primary(self, depth):
        tok = self.peek()
        if tok is None:
            raise FormulaError("수식이 끝나지 않았습니다 — 항목이나 숫자가 필요합니다",
                               len(self.toks) - 1 if self.toks else None)
        kind = tok.get("t")
        if kind == "num":
            self.i += 1
            return ("num", tok["v"])
        if kind == "item":
            self.i += 1
            return ("ref", tok["item"])
        if kind == "fn":
            return self.parse_call(depth)
        if kind == "lp":
            if depth + 1 > MAX_DEPTH:
                raise FormulaError(f"괄호가 너무 깊습니다 ({MAX_DEPTH}단 이하)", self.i)
            self.i += 1
            node = self.parse_expr(depth + 1)
            close = self.peek()
            if not close or close.get("t") != "rp":
                raise FormulaError("닫는 괄호가 없습니다", self.i)
            self.i += 1
            return node
        at = self.i
        if kind == "rp":
            raise FormulaError(f"{at + 1}번째 위치에 여는 괄호가 없습니다", at)
        if kind == "comma":
            raise FormulaError(f"{at + 1}번째 쉼표는 함수 안에서만 쓸 수 있습니다", at)
        raise FormulaError(f"{at + 1}번째 위치에 항목이나 숫자가 필요합니다", at)

    def parse_call(self, depth):
        at = self.i
        name = self.toks[at]["v"]
        self.i += 1
        if depth + 1 > MAX_DEPTH:
            raise FormulaError(f"괄호가 너무 깊습니다 ({MAX_DEPTH}단 이하)", at)
        open_tok = self.peek()
        if not open_tok or open_tok.get("t") != "lp":
            raise FormulaError(f"{name} 뒤에 여는 괄호가 필요합니다", at)
        self.i += 1
        args = []
        if not (self.peek() or {}).get("t") == "rp":
            while True:
                args.append(self.parse_expr(depth + 1))
                nxt = self.peek()
                if nxt and nxt.get("t") == "comma":
                    self.i += 1
                    if len(args) >= MAX_ARGS:
                        raise FormulaError(
                            f"{name} 의 인자가 너무 많습니다 ({MAX_ARGS}개 이하)", at)
                    continue
                break
        close = self.peek()
        if not close or close.get("t") != "rp":
            raise FormulaError(f"{name} 의 닫는 괄호가 없습니다", self.i)
        self.i += 1
        lo, hi = FUNCS[name]
        if len(args) < lo or (hi is not None and len(args) > hi):
            want = f"{lo}개" if hi == lo else (f"{lo}개 이상" if hi is None
                                              else f"{lo}~{hi}개")
            raise FormulaError(f"{name} 는 인자가 {want} 필요합니다 "
                               f"(지금 {len(args)}개)", at)
        if name == "ROUND" and len(args) == 2 and not _const_num(args[1]):
            # 배열 자릿수를 허용하면 행마다 np.round 를 돌아야 한다.
            raise FormulaError("ROUND 의 두 번째 인자는 숫자여야 합니다", at)
        return ("call", name, args)


def _const_num(node):
    """파스 타임 상수인가 — ("num",v) 또는 ("neg", ("num",v))."""
    if node[0] == "num":
        return True
    return node[0] == "neg" and _const_num(node[1])


def _const_value(node):
    return -_const_value(node[1]) if node[0] == "neg" else float(node[1])


def parse_tokens(tokens):
    """토큰 배열 → AST. 문법 위반은 FormulaError(index 포함)."""
    parser = _Parser(tokens)
    node = parser.parse_expr(0)
    if parser.i < len(parser.toks):
        at = parser.i
        raise FormulaError(f"{at + 1}번째 토큰 앞에 연산자가 필요합니다", at)
    return node


# ── 평가 ─────────────────────────────────────────────────────────────────────

def _truth(x):
    """float64 → 진리값 float64 (1.0 / 0.0 / NaN)."""
    with np.errstate(invalid="ignore"):
        return np.where(np.isnan(x), np.nan, (x != 0).astype(np.float64))


def _cmp(op, left, right):
    """비교 → 진리값. **NaN 은 어느 쪽에도 속하지 않는다.**

    numpy 는 NaN 비교를 False 로 준다. 그대로 두면 측정값이 없는 die 가 조용히
    FALSE 가 되어 `IF(A > B, 0, 1)` 이 미측정 die 에 1 을 찍는다 — 에러 없이 틀린 값이다.
    """
    with np.errstate(invalid="ignore"):
        raw = _CMP[op](left, right)
        bad = np.isnan(left) | np.isnan(right)
        return np.where(bad, np.nan, np.asarray(raw, dtype=np.float64))


def _call(name, args):
    if name == "IF":
        cond, a, b = args
        with np.errstate(invalid="ignore"):
            out = np.where(cond != 0, a, b)
        # cond != 0 은 NaN 에서 True 를 주므로 이 줄이 덮는다. 순서를 뒤집지 말 것.
        return np.where(np.isnan(cond), np.nan, out)
    if name == "NOT":
        x = _truth(args[0])
        return np.where(np.isnan(x), np.nan, 1.0 - x)
    if name in ("AND", "OR"):
        # np.minimum/maximum 은 NaN 을 전파해 3-값 논리가 그대로 나온다.
        # logical_and 를 쓰면 NaN 이 True 로 뭉개진다 — 쓰지 말 것.
        fn = np.minimum if name == "AND" else np.maximum
        return reduce(fn, [_truth(a) for a in args])
    if name == "MIN":
        return reduce(np.minimum, args)
    if name == "MAX":
        return reduce(np.maximum, args)
    if name == "SUM":
        return reduce(np.add, args)
    if name == "AVERAGE":
        return reduce(np.add, args) / float(len(args))
    if name == "ABS":
        return np.abs(args[0])
    if name == "SQRT":
        with np.errstate(invalid="ignore"):
            return np.sqrt(args[0])
    if name == "ROUND":
        digits = int(args[1]) if len(args) > 1 else 0
        with np.errstate(invalid="ignore"):
            return np.round(args[0], digits)
    raise FormulaError(f"쓸 수 없는 함수입니다: {name}")


def _eval(node, column_of):
    kind = node[0]
    if kind == "num":
        return np.float64(node[1])
    if kind == "ref":
        return column_of(node[1])
    if kind == "neg":
        return np.negative(_eval(node[1], column_of))
    if kind == "bin":
        left = _eval(node[2], column_of)
        right = _eval(node[3], column_of)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            return _ARITH[node[1]](left, right)
    if kind == "cmp":
        return _cmp(node[1], _eval(node[2], column_of), _eval(node[3], column_of))
    if kind == "call":
        name = node[1]
        if name == "ROUND" and len(node[2]) == 2:
            # 두 번째 인자는 파스 타임에 상수로 강제돼 있다.
            return _call(name, [_eval(node[2][0], column_of),
                                _const_value(node[2][1])])
        return _call(name, [_eval(a, column_of) for a in node[2]])
    raise FormulaError(f"알 수 없는 수식 노드: {kind}")


def evaluate(node, column_of, n_rows) -> np.ndarray:
    """AST 평가 → 길이 n_rows 의 float64 배열.

    ``column_of(item)`` 이 item 컬럼의 float64 배열을 준다(Gap Chart 의 2인자 콜백과 달리
    source 인자가 없다 — 신규 item 은 항상 각 source 안에서 계산된다).

    ±inf 는 마지막에 NaN 으로 정규화한다. **이 정규화가 빠지면 조용히 망가진다** —
    encode_honeyform_parquet 이 값을 문자열로 저장하므로 inf 가 "inf" 로 parquet 에 박히고,
    조회 때 to_numeric 이 그걸 되살려 평균·σ·CPK 를 통째로 오염시킨다.
    """
    value = _eval(node, column_of)
    out = np.asarray(value, dtype=np.float64)
    if out.ndim == 0:
        out = np.full(int(n_rows), float(out), dtype=np.float64)
    elif out.shape[0] != int(n_rows):
        raise FormulaError("수식 결과의 길이가 데이터 행 수와 다릅니다")
    else:
        out = out.copy()
    out[~np.isfinite(out)] = np.nan
    return out


# ── HoneyformTable 편의 래퍼 ──────────────────────────────────────────────────

def _float_col(table, item) -> np.ndarray:
    """item 컬럼 → float64 배열. 비수치(P/F 텍스트)는 자동 NaN."""
    return pd.to_numeric(table.data[item], errors="coerce").to_numpy().astype(np.float64)


def missing_items(table, tokens) -> list:
    """이 source 에 없는 참조 item (등장 순서). 비어 있으면 계산할 수 있다."""
    have = set(table.item_columns)
    return [name for name in item_refs(tokens) if name not in have]


def eval_for_table(table, tokens) -> np.ndarray:
    """HoneyformTable 하나에 대해 수식을 계산한다. 길이 = len(table.data)."""
    node = parse_tokens(tokens)
    cache = {}

    def column_of(item):
        if item not in cache:
            if item not in table.item_columns:
                raise FormulaError(f"이 source 에 '{item}' 항목이 없습니다")
            cache[item] = _float_col(table, item)
        return cache[item]

    return evaluate(node, column_of, len(table.data))
