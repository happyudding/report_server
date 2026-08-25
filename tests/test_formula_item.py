"""신규 Item 수식 엔진(web_report/formula.py) 파서·평가 계약 (2026-08-24).

실행:
    server\\.venv\\Scripts\\python.exe tests/test_formula_item.py

왜 이 파일이 필요한가:
    이 엔진이 깨지는 방식은 전부 **조용하다** — 예외도 안 나고 화면도 멀쩡한데 숫자만 틀린다.
      · 비교의 NaN 마스크를 빼면 미측정 die 가 FALSE 로 뭉개져 IF(A>B,0,1) 이 거기에 1 을 찍는다.
      · ±inf 를 NaN 으로 정규화하지 않으면 "inf" 문자열이 parquet 에 박혀 평균·σ·CPK 가 오염된다.
      · AND/OR 를 logical_and 로 바꾸면 NaN 이 True 로 뭉개진다.
      · MIN/MAX 를 fmin/fmax 로 바꾸면 결측을 무시해 "부분 모집단으로 계산한 값"이 나온다.
    그리고 이 모듈은 gap_chart.py 파서의 **사본**이라, 한쪽만 고치면 두 수식 기능의 계산이
    갈린다 — 마지막 (i) 동치 테스트가 그 드리프트를 막는다.

검증 항목:
  (a) 토큰 정규화 — 화이트리스트·unknown 키 제거·source 키 거부·상한
  (b) 문법 거부 — 함수 인자수/괄호/쉼표/비교 2연속/말미 연산자, FormulaError.index 정확도
  (c) 평가 정확도 — 함수 11종 + 비교 6종을 손계산 기대값과 원소 단위 비교
  (d) 사용자 예시 IF(A > MIN(B,C), 0, 1)
  (e) NaN 전파 — 비교·IF·MIN·AND·SUM 각각
  (f) **비교의 NaN 은 FALSE 가 아니다** (핵심 1)
  (g) **inf → NaN 정규화** (핵심 2)
  (h) 스칼라 수식·길이 정합
  (i) **gap_chart 동치** — 산술 전용 토큰의 AST·평가 결과 일치 (핵심 3)
  (j) 평문 렉싱 `lex` — `@"이름"` 인용·대소문자·escape·거부 문구·span 오프셋

pytest 미사용 (tests/ 관례 — 자체 실행 + assert). 서버·DB 불필요.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_report import formula as F                                    # noqa: E402
from web_report import gap_chart as G                                  # noqa: E402


# ── 토큰 조립 헬퍼 ───────────────────────────────────────────────────────────

def T(*specs):
    """짧은 스펙 → 토큰 배열. 항목은 문자열, 숫자는 int/float, 나머지는 기호."""
    out = []
    for s in specs:
        if s == "(":
            out.append({"t": "lp"})
        elif s == ")":
            out.append({"t": "rp"})
        elif s == ",":
            out.append({"t": "comma"})
        elif isinstance(s, str) and s in F.FUNCS:
            out.append({"t": "fn", "v": s})
        elif isinstance(s, str) and (s in ("+", "-", "*", "/") or s in (">", ">=", "<", "<=", "=", "<>")):
            out.append({"t": "op", "v": s})
        elif isinstance(s, (int, float)) and not isinstance(s, bool):
            out.append({"t": "num", "v": float(s)})
        else:
            out.append({"t": "item", "item": s})
    return out


def ev(tokens, cols, n=None):
    """토큰 → 값 배열. cols 는 {item: list|array}."""
    arrays = {k: np.asarray(v, dtype=np.float64) for k, v in cols.items()}
    rows = n if n is not None else (len(next(iter(arrays.values()))) if arrays else 1)
    node = F.parse_tokens(F.normalize_tokens(tokens))
    return F.evaluate(node, lambda item: arrays[item], rows)


def same(got, want, label):
    got = np.asarray(got, dtype=np.float64)
    want = np.asarray(want, dtype=np.float64)
    assert got.shape == want.shape, f"{label}: 길이 {got.shape} != {want.shape}"
    ok = np.array_equal(np.isnan(got), np.isnan(want)) and np.allclose(
        got[~np.isnan(got)], want[~np.isnan(want)], rtol=1e-12, atol=1e-12)
    assert ok, f"{label}\n  got  {got}\n  want {want}"


def rejects(tokens, label, want_index=None):
    try:
        F.normalize_tokens(tokens)
    except F.FormulaError as exc:
        if want_index is not None:
            assert exc.index == want_index, \
                f"{label}: index {exc.index} != {want_index} ({exc})"
        return str(exc)
    raise AssertionError(f"{label}: 거부되어야 하는데 통과했다")


# ── (a) 토큰 정규화 ──────────────────────────────────────────────────────────

def test_normalize():
    tokens = F.normalize_tokens([
        {"t": "item", "item": "  A  ", "junk": 1},      # strip + unknown 키 제거
        {"t": "op", "v": "+"},
        {"t": "num", "v": "2.5"},                        # 문자열 숫자 허용
    ])
    assert tokens == [{"t": "item", "item": "A"}, {"t": "op", "v": "+"},
                      {"t": "num", "v": 2.5}], tokens

    # 함수명은 대문자로 정규화된다
    t = F.normalize_tokens(T("A") [:1] + [{"t": "op", "v": "+"},
                                          {"t": "fn", "v": "abs"}, {"t": "lp"},
                                          {"t": "item", "item": "B"}, {"t": "rp"}])
    assert t[2] == {"t": "fn", "v": "ABS"}, t

    rejects([], "빈 수식")
    rejects(T(1, "+", 2), "항목 없음")
    rejects([{"t": "item", "item": "A", "source": "WF1"}], "source 키", want_index=0)
    rejects([{"t": "fn", "v": "VLOOKUP"}, {"t": "lp"},
             {"t": "item", "item": "A"}, {"t": "rp"}], "미지원 함수", want_index=0)
    rejects([{"t": "op", "v": "^"}, {"t": "item", "item": "A"}], "미지원 연산자", want_index=0)
    rejects([{"t": "item", "item": "A"}, {"t": "zzz"}], "미지 토큰 종류", want_index=1)
    rejects([{"t": "num", "v": float("inf")}, {"t": "op", "v": "+"},
             {"t": "item", "item": "A"}], "무한 상수", want_index=0)
    rejects(T(*(["A", "+"] * (F.MAX_TOKENS // 2 + 2))), "MAX_TOKENS")
    rejects(T(*(sum([[f"I{i}", "+"] for i in range(F.MAX_REFS + 2)], [])[:-1])), "MAX_REFS")
    deep = ["("] * (F.MAX_DEPTH + 2) + ["A"] + [")"] * (F.MAX_DEPTH + 2)
    rejects(T(*deep), "MAX_DEPTH")
    print("  [ok] (a) 토큰 정규화 - 화이트리스트·unknown 키·source 거부·상한 6종")


# ── (b) 문법 거부 ────────────────────────────────────────────────────────────

def test_syntax():
    rejects(T("IF", "(", "A", ",", 1, ")"), "IF 인자 2개", want_index=0)
    rejects(T("IF", "(", "A", ",", 1, ",", 2, ",", 3, ")"), "IF 인자 4개", want_index=0)
    rejects(T("NOT", "(", "A", ",", "B", ")"), "NOT 인자 2개", want_index=0)
    rejects(T("MIN", "(", ")") + T("+", "A"), "MIN 인자 0개", want_index=0)
    rejects(T("IF", "A", ">", 1), "fn 뒤 괄호 없음", want_index=0)
    rejects(T("(", "A", "+", 1), "닫는 괄호 없음")
    rejects(T("A", "+", 1, ")"), "여는 괄호 없음")
    rejects(T("A", ",", "B"), "괄호 밖 쉼표", want_index=1)
    rejects(T("A", ">", "B", ">", "C"), "비교 2연속")
    rejects(T("A", "+"), "말미 연산자")
    rejects(T("A", "B"), "인접 피연산자", want_index=1)
    rejects(T("ROUND", "(", "A", ",", "B", ")"), "ROUND 자릿수가 항목", want_index=0)
    # ROUND 두 번째 인자가 상수면 통과 (음수 자릿수 포함)
    F.normalize_tokens(T("ROUND", "(", "A", ",", "-", 1, ")"))
    print("  [ok] (b) 문법 거부 12종 + FormulaError.index")


# ── (c) 평가 정확도 ──────────────────────────────────────────────────────────

def test_eval_basic():
    A = [1.0, 2.0, 3.0, 4.0]
    B = [4.0, 3.0, 2.0, 1.0]
    cols = {"A": A, "B": B}
    a, b = np.array(A), np.array(B)

    same(ev(T("A", "+", "B"), cols), a + b, "A+B")
    same(ev(T("A", "-", "B"), cols), a - b, "A-B")
    same(ev(T("A", "*", "B"), cols), a * b, "A*B")
    same(ev(T("A", "/", "B"), cols), a / b, "A/B")
    same(ev(T("-", "A"), cols), -a, "-A")
    same(ev(T("(", "A", "+", "B", ")", "*", 2), cols), (a + b) * 2, "(A+B)*2")

    same(ev(T("MIN", "(", "A", ",", "B", ")"), cols), np.minimum(a, b), "MIN")
    same(ev(T("MAX", "(", "A", ",", "B", ")"), cols), np.maximum(a, b), "MAX")
    same(ev(T("SUM", "(", "A", ",", "B", ",", 1, ")"), cols), a + b + 1, "SUM 3항")
    same(ev(T("AVERAGE", "(", "A", ",", "B", ")"), cols), (a + b) / 2, "AVERAGE")
    same(ev(T("ABS", "(", "A", "-", "B", ")"), cols), np.abs(a - b), "ABS")
    same(ev(T("SQRT", "(", "A", ")"), cols), np.sqrt(a), "SQRT")
    same(ev(T("ROUND", "(", "A", "/", "B", ",", 2, ")"), cols), np.round(a / b, 2), "ROUND 2")
    same(ev(T("ROUND", "(", "A", "/", "B", ")"), cols), np.round(a / b, 0), "ROUND 기본 0")

    # 비교 6종 → 1.0 / 0.0
    for op, fn in ((">", np.greater), (">=", np.greater_equal), ("<", np.less),
                   ("<=", np.less_equal), ("=", np.equal), ("<>", np.not_equal)):
        same(ev(T("A", op, "B"), cols), fn(a, b).astype(float), f"A {op} B")

    # 논리 3종
    same(ev(T("AND", "(", "A", ">", 1, ",", "B", ">", 1, ")"), cols),
         ((a > 1) & (b > 1)).astype(float), "AND")
    same(ev(T("OR", "(", "A", ">", 3, ",", "B", ">", 3, ")"), cols),
         ((a > 3) | (b > 3)).astype(float), "OR")
    same(ev(T("NOT", "(", "A", ">", 2, ")"), cols), (~(a > 2)).astype(float), "NOT")

    # SQRT(음수) → NaN
    same(ev(T("SQRT", "(", "-", "A", ")"), {"A": [1.0, 4.0]}), [np.nan, np.nan], "SQRT 음수")
    print("  [ok] (c) 함수 11종 + 비교 6종 + 사칙연산 원소 단위 일치")


# ── (d) 사용자 예시 ──────────────────────────────────────────────────────────

def test_user_example():
    cols = {"TESTITEM1": [5.0, 1.0, 3.0, 9.9],
            "TESTITEM2": [2.0, 2.0, 2.0, 2.0],
            "TESTITEM3": [4.0, 4.0, 4.0, 4.0]}
    tokens = T("IF", "(", "TESTITEM1", ">", "MIN", "(", "TESTITEM2", ",", "TESTITEM3", ")",
               ",", 0, ",", 1, ")")
    # MIN = 2 이므로 5>2→0, 1>2→1, 3>2→0, 9.9>2→0
    same(ev(tokens, cols), [0.0, 1.0, 0.0, 0.0], "사용자 예시")
    assert F.render_formula(F.normalize_tokens(tokens)) == \
        "IF(TESTITEM1 > MIN(TESTITEM2, TESTITEM3), 0, 1)", \
        F.render_formula(F.normalize_tokens(tokens))
    print("  [ok] (d) IF(A > MIN(B,C), 0, 1) - 값·표시 문자열")


# ── (e)(f) NaN 전파 ──────────────────────────────────────────────────────────

def test_nan():
    nan = np.nan
    cols = {"A": [1.0, nan, 3.0], "B": [2.0, 2.0, nan]}

    # (f) 핵심 — 비교의 NaN 은 FALSE(0.0) 가 아니라 NaN 이다.
    same(ev(T("A", ">", "B"), cols), [0.0, nan, nan], "비교 NaN")
    same(ev(T("A", "<>", "B"), cols), [1.0, nan, nan], "<> NaN")
    same(ev(T("A", "=", "B"), cols), [0.0, nan, nan], "= NaN")

    # IF 는 조건이 NaN 이면 결과도 NaN (분기 값이 멀쩡해도)
    same(ev(T("IF", "(", "A", ">", "B", ",", 10, ",", 20, ")"), cols),
         [20.0, nan, nan], "IF NaN 조건")

    # MIN/MAX/SUM/AVERAGE 는 결측을 전파한다 (fmin 이 아니라 minimum)
    same(ev(T("MIN", "(", "A", ",", "B", ")"), cols), [1.0, nan, nan], "MIN NaN")
    same(ev(T("MAX", "(", "A", ",", "B", ")"), cols), [2.0, nan, nan], "MAX NaN")
    same(ev(T("SUM", "(", "A", ",", "B", ")"), cols), [3.0, nan, nan], "SUM NaN")
    same(ev(T("AVERAGE", "(", "A", ",", "B", ")"), cols), [1.5, nan, nan], "AVERAGE NaN")

    # AND/OR 는 3-값 논리 (logical_and 이면 NaN 이 True 로 뭉개진다)
    same(ev(T("AND", "(", "A", ",", "B", ")"), cols), [1.0, nan, nan], "AND NaN")
    same(ev(T("OR", "(", "A", ",", "B", ")"), cols), [1.0, nan, nan], "OR NaN")
    same(ev(T("NOT", "(", "A", ")"), cols), [0.0, nan, 0.0], "NOT NaN")

    # 산술은 numpy 기본 전파
    same(ev(T("A", "+", "B"), cols), [3.0, nan, nan], "산술 NaN")
    print("  [ok] (e)(f) NaN 전파 11종 - 비교의 NaN 이 FALSE 로 뭉개지지 않는다")


# ── (g) inf → NaN ────────────────────────────────────────────────────────────

def test_infinite():
    cols = {"A": [1.0, -1.0, 0.0], "Z": [0.0, 0.0, 0.0]}
    out = ev(T("A", "/", "Z"), cols)
    assert np.isnan(out).all(), f"0 나눗셈이 NaN 이 아니다: {out}"
    assert np.isfinite(out[~np.isnan(out)]).all()

    # overflow 도 마찬가지 — 1e308 * 10 = inf
    out2 = ev(T("A", "*", 1e308, "*", 10), {"A": [1e10, 1.0, 0.0]})
    assert not np.isinf(out2).any(), f"overflow inf 가 남았다: {out2}"
    print("  [ok] (g) 0 나눗셈·overflow 가 inf 로 남지 않는다 (parquet 오염 방지)")


# ── (h) 스칼라·길이 ──────────────────────────────────────────────────────────

def test_shape():
    cols = {"A": [1.0, 2.0, 3.0]}
    same(ev(T("A", "*", 0, "+", 7), cols), [7.0, 7.0, 7.0], "상수화")
    # 항목이 아예 없으면 normalize 가 막는다 → 스칼라만 있는 수식은 만들 수 없다
    rejects(T(1, "+", 2), "항목 없는 상수 수식")

    class _Tbl:
        item_columns = ["A", "B"]

    assert F.missing_items(_Tbl(), T("A", "+", "C")) == ["C"]
    assert F.missing_items(_Tbl(), T("A", "+", "B")) == []
    print("  [ok] (h) 길이 정합 · missing_items")


# ── (i) gap_chart 동치 ───────────────────────────────────────────────────────

def _adapt(node):
    """formula AST → gap_chart AST 모양 (("ref", item) → ("ref", "", item))."""
    kind = node[0]
    if kind == "ref":
        return ("ref", "", node[1])
    if kind == "num":
        return node
    if kind == "neg":
        return ("neg", _adapt(node[1]))
    if kind == "bin":
        return ("bin", node[1], _adapt(node[2]), _adapt(node[3]))
    raise AssertionError(f"산술 전용 케이스에 없어야 할 노드: {kind}")


def test_gap_chart_equivalence():
    """산술 전용 수식은 gap_chart 파서·평가와 결과가 같아야 한다.

    formula.py 는 gap_chart 파서의 사본이다. 한쪽만 고치면 두 수식 기능의 계산이 조용히
    갈리므로, 겹치는 영역(사칙연산·괄호·단항)을 기계로 묶어 둔다.
    """
    cases = [
        ("A",), ("-", "A"), ("+", "A"),
        ("A", "+", "B"), ("A", "-", "B"), ("A", "*", "B"), ("A", "/", "B"),
        ("A", "+", "B", "*", "C"), ("(", "A", "+", "B", ")", "*", "C"),
        ("A", "-", "B", "-", "C"), ("A", "/", "B", "/", "C"),
        ("-", "(", "A", "+", "B", ")"), ("A", "*", "-", "B"),
        ("A", "+", 2), (2, "*", "A"), ("A", "/", 0.5),
        ("(", "(", "A", ")", ")"), ("A", "+", "B", "+", "C", "+", 1),
        ("A", "*", "B", "/", "C"), ("-", "-", "A"),
    ]
    cols = {"A": np.array([1.0, 2.0, -3.0, np.nan]),
            "B": np.array([4.0, 0.0, 2.0, 5.0]),
            "C": np.array([2.0, 8.0, np.nan, 1.0])}

    for spec in cases:
        tokens = T(*spec)
        mine = F.normalize_tokens(tokens)
        # gap_chart 는 comma/fn 을 모르지만 산술 전용 케이스라 같은 토큰이 그대로 통과한다
        theirs = G.normalize_tokens([dict(t) for t in tokens])
        assert _adapt(F.parse_tokens(mine)) == G.parse_tokens(theirs), \
            f"AST 불일치: {spec}"

        got = F.evaluate(F.parse_tokens(mine), lambda i: cols[i], 4)
        want = G._evaluate(G.parse_tokens(theirs), lambda _s, i: cols[i])
        want = np.asarray(want, dtype=np.float64).copy()
        want[~np.isfinite(want)] = np.nan          # formula 만 하는 최종 정규화를 맞춰준다
        same(got, want, f"평가 불일치: {spec}")

    print(f"  [ok] (i) gap_chart 동치 {len(cases)}종 - AST·평가 결과 일치")


def test_lex():
    """(j) 평문 렉싱 — 항목은 `@"이름"` 인용으로만, 나머지는 자유 타이핑."""
    items = ["VDD_A", "VDD_B", "VDD-VSS", "IDD (1.8V)", 'A"B', "SUM", "Vref", "vref"]

    def toks(text, known=items):
        return F.lex(text, known)[0]

    def kinds(text, known=items):
        return [t.get("item") if t["t"] == "item" else t.get("v", t["t"])
                for t in toks(text, known)]

    def bad(text, needle, known=items):
        try:
            tokens, spans = F.lex(text, known)
            F.normalize_tokens(tokens)
        except F.FormulaError as exc:
            assert needle in str(exc), (text, str(exc))
            span = exc.span or F.error_span(spans, exc.index, len(text))
            assert span and 0 <= span[0] < span[1] <= len(text), (text, span)
            return str(exc)
        raise AssertionError(f"거부되지 않았다: {text!r}")

    # 대소문자 — 함수명·항목 조회 둘 다 무관, 토큰은 목록의 **원본 이름**
    assert kinds('if(@"vdd_a" > min(@"VDD_B", 2), 0, 1)') == [
        "IF", "lp", "VDD_A", ">", "MIN", "lp", "VDD_B", "comma", 2.0, "rp",
        "comma", 0.0, "comma", 1.0, "rp"]
    assert toks('@"VDD_A"')[0]["item"] == "VDD_A"

    # 이름에 연산자·공백·괄호·따옴표가 있어도 안전 (인용이 구분자)
    assert kinds('@"VDD-VSS" * 2') == ["VDD-VSS", "*", 2.0]
    assert kinds('@"IDD (1.8V)" + @"A""B"') == ["IDD (1.8V)", "+", 'A"B']
    assert F.quote_item('A"B') == '@"A""B"'
    assert toks(F.quote_item('IDD (1.8V)'))[0]["item"] == "IDD (1.8V)"

    # 함수명과 같은 이름의 항목 — 인용이 충돌을 없앤다
    got = toks('SUM(@"SUM", 1)')
    assert got[0]["t"] == "fn" and got[2]["t"] == "item"

    # 숫자 형태 3종 + 2글자 비교 연산자
    assert kinds('@"VDD_A" > 1.5') == ["VDD_A", ">", 1.5]
    assert kinds('@"VDD_A" >= .5') == ["VDD_A", ">=", 0.5]
    assert kinds('@"VDD_A" <> 2e-3') == ["VDD_A", "<>", 0.002]
    assert kinds('@"VDD_A" <= 1') == ["VDD_A", "<=", 1.0]

    # 자유 텍스트는 항목이 될 수 없다 / 별칭은 받지 않는다 / 특수문자 거부
    bad("VDD_A + 1", "쓸 수 없는 함수")
    bad('vlookup(@"VDD_A")', "쓸 수 없는 함수")
    bad('@"VDD_A" != 1', "<> 로 씁니다")
    bad('@"VDD_A" == 1', "= 하나로 씁니다")
    bad('@"VDD_A" ※ 1', "쓸 수 없는 문자")
    bad('@VDD_A', '@"항목명"')
    bad('@"VDD_A', '닫는 " 가 없습니다')

    # 없는 항목 — 근접 후보를 알려 준다 (이름 한 글자를 지운 상황)
    message = bad('@"VDD_" + 1', "항목이 없습니다")
    assert "VDD_A" in message, message

    # 대소문자만 다른 항목이 둘이면 추측하지 않는다
    bad('@"VREF" + 1', "대소문자만 다른 항목")
    assert toks('@"Vref" + 1')[0]["item"] == "Vref"      # 정확 일치는 그대로 통과

    # span 오프셋 정확도
    tokens, spans = F.lex('@"VDD_A" + 12', items)
    assert spans == [(0, 8), (9, 10), (11, 13)], spans
    text = 'if(@"VDD_A" > 1, 0, 1)'
    tokens, spans = F.lex(text, items)
    assert text[spans[0][0]:spans[0][1]] == "if"
    assert text[spans[2][0]:spans[2][1]] == '@"VDD_A"'

    # 오류 지점까지 읽은 부분 결과가 예외에 실린다 (에디터 색칠 유지용)
    try:
        F.lex('@"VDD_A" + %', items)
    except F.FormulaError as exc:
        assert [t["t"] for t in exc.tokens] == ["item", "op"], exc.tokens
        assert exc.spans == [(0, 8), (9, 10)], exc.spans
    else:
        raise AssertionError("특수문자가 통과했다")

    # 파서 오류의 토큰 index → 문자 위치 (위치 없는 오류를 만들지 않는다)
    text = 'IF(@"VDD_A" > MIN(@"VDD_B", 1), 0, 1'
    tokens, spans = F.lex(text, items)
    try:
        F.normalize_tokens(tokens)
    except F.FormulaError as exc:
        start, end = F.error_span(spans, exc.index, len(text))
        assert 0 <= start < end <= len(text), (start, end)
    else:
        raise AssertionError("닫는 괄호 누락이 통과했다")

    # 길이 상한
    bad("1 " * (F.MAX_TEXT // 2 + 10), "너무 깁니다")
    print("  [ok] (j) 평문 렉싱 — 인용 항목·대소문자·escape·거부 7종·span·부분결과")


def main():
    print("[신규 Item 수식 엔진]")
    test_normalize()
    test_syntax()
    test_eval_basic()
    test_user_example()
    test_nan()
    test_infinite()
    test_shape()
    test_gap_chart_equivalence()
    test_lex()
    print("[통과] 수식 파서·평가 계약 정상")


if __name__ == "__main__":
    main()
