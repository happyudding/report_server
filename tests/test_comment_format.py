"""comment 서식 토큰 strip(web_report/comment_format.py) 검증.

실행:
    python tests/test_comment_format.py

검증 항목:
  (a) 토큰을 본문만 남기고 벗긴다 (굵게/색/색+굵게).
  (b) 모르는 스타일 글자(*x[..])·단독 '*' 는 토큰이 아니라 원문 그대로 둔다.
  (c) @[항목]/#[태그]/$[시트] 링크 토큰은 건드리지 않는다.
  (d) 멱등: strip(strip(x)) == strip(x).
  (e) eval_export._merge_comment 가 실제로 벗긴 값을 내보낸다
      (= eval.db label.human_comment 로 가는 관문이 막혀 있다).
  (f) **JS 짝 드리프트 가드** — sheets.js 의 정규식·색 테이블이 이 모듈과 같은지.
      문법 정본이 JS 라, 한쪽만 고치면 웹 화면과 Excel 이 다른 글자를 보여준다.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))

from web_report.comment_format import _COLORS, _TOKEN_RE, strip_format  # noqa: E402

SHEETS_JS = ROOT / "server" / "report" / "static" / "webreport" / "sheets.js"

# (입력, 기대 출력)
CASES = [
    ("*[중요] 확인",            "중요 확인"),
    ("*r[NG]",                  "NG"),
    ("*R[NG] / *b[OK]",         "NG / OK"),
    ("*O[주황] *g[초록]",        "주황 초록"),
    ("*x[NG]",                  "*x[NG]"),          # 모르는 글자 = 토큰 아님
    ("*Z[NG]",                  "*Z[NG]"),
    ("3*4 = 12",                "3*4 = 12"),        # 단독 * 무해
    ("*주의",                    "*주의"),            # 대괄호 없으면 토큰 아님
    ("*[]",                     "*[]"),             # 빈 본문은 [^\]]+ 에 안 걸린다
    ("@[item] *b[a] #[T] $[S]", "@[item] a #[T] $[S]"),   # 링크 토큰 불변
    ("*[a][b]",                 "a[b]"),
    ("앞 *R[가운데] 뒤",         "앞 가운데 뒤"),
    ("",                        ""),
    (None,                      ""),
    (123,                       "123"),             # 비문자열 정규화
]


def test_strip():
    for src, want in CASES:
        got = strip_format(src)
        assert got == want, f"strip_format({src!r}) = {got!r}, want {want!r}"


def test_idempotent():
    for src, _ in CASES:
        once = strip_format(src)
        assert strip_format(once) == once, f"not idempotent: {src!r} -> {once!r}"


def test_merge_comment_strips():
    """eval.db 로 나가는 관문(_merge_comment)이 실제로 벗기는지."""
    from web_report import eval_export

    got = eval_export._merge_comment({"PTE comment": "*R[NG] 발생", "개발 comment": "*b[확인]"})
    assert got == "[PTE] NG 발생\n[개발] 확인", got
    # 빈 값은 종전대로 빠진다
    assert eval_export._merge_comment({"PTE comment": "  "}) == ""
    # 모르는 글자는 원문 유지
    assert eval_export._merge_comment({"PTE comment": "*x[a]"}) == "[PTE] *x[a]"


def test_js_python_parity():
    """sheets.js 와 문법이 갈라지지 않았는지 (정규식 리터럴 + 색 글자 집합)."""
    js = SHEETS_JS.read_text(encoding="utf-8")
    # 1) strip 정규식이 Python 쪽과 문자 그대로 같아야 한다.
    assert _TOKEN_RE.pattern == r"\*([A-Za-z]?)\[([^\]]+)\]", _TOKEN_RE.pattern
    assert r"/\*([A-Za-z]?)\[([^\]]+)\]/g" in js, "sheets.js stripCommentFormat 정규식 불일치"
    # 2) linkifyComment 의 통합 정규식에도 같은 서식 분기가 있어야 한다.
    assert r"/([@#$])\[([^\]]+)\]|\*([A-Za-z]?)\[([^\]]+)\]/g" in js, \
        "sheets.js linkifyComment 정규식 불일치"
    # 3) 색 글자 집합이 같아야 한다.
    m = re.search(r"const CMT_FMT_COLORS = \{([^}]*)\}", js)
    assert m, "sheets.js CMT_FMT_COLORS 를 찾지 못함"
    js_letters = set(re.findall(r"(\w+)\s*:", m.group(1)))
    assert js_letters == set(_COLORS), f"색 글자 불일치: js={js_letters} py={set(_COLORS)}"


def test_chatbot_strips():
    """챗봇이 report.db 편집행을 직접 읽는 경로도 벗기는지 (import 가능 여부 포함)."""
    from chatbot import tools_report

    assert tools_report.strip_format("*R[NG]") == "NG"


def main():
    tests = [test_strip, test_idempotent, test_merge_comment_strips,
             test_js_python_parity, test_chatbot_strips]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
        except Exception as exc:   # import 실패 등도 실패로 본다
            failed += 1
            print(f"  ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    print("FAILED" if failed else "ALL PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
