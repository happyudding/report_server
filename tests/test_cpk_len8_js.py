"""CPK 통계값 표시 길이 제한(fmtLen8) 회귀 — headless Edge 로 core.js/cpk.js 를 돌려 본다.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_cpk_len8_js.py

**왜 이 파일이 생겼나** (2026-08-26): CPK 탭의 통계 컬럼(min/median/max/average/stdev)은
서버가 컬럼마다 다른 자리수로 내려보내(min~max 6자리 / average 4자리 / stdev 무반올림)
`2347582934789.234783` 같은 값 하나가 컬럼 폭을 통째로 밀어냈다. 이를 표시 단에서
**8자(부호 제외)** 로 줄이고 원값은 title 툴팁에 남기는 `fmtLen8`(core.js)을 도입했다.

숫자 포맷은 눈으로 훑으면 맞아 보이지만 실제로는 **값이 조용히 틀어지는** 종류의 코드다.
설계 단계에서 실측한 버그 3종이 그 증거이며, 이 테스트는 그것들을 다시 밟지 않게 한다:
  (1) 지수부(e-10·e308) 길이를 빼고 세면 `12345678901` 이 `1.2346e1`(=12.346) 이 된다
  (2) 끝자리 0 제거를 지수표기 전체에 걸면 `1.0000e+1` 이 `1.00000e` 로 깨진다
  (3) 버림을 `Math.floor(n*10**d)/10**d` 로 하면 `8.7` 이 `8.6` 이 된다(부동소수점 곱셈)

검증하는 것:
  (a) 확정된 표시 규칙대로 나온다 (기대값 표 — 사용자 확정 예시 포함)
  (b) 출력을 다시 Number() 로 읽어 **깨지지 않고 값이 보존**된다 (상대오차 0.1% 이내)
  (c) **자리올림이 절대 일어나지 않는다** (버림이므로 |출력| <= |원값|) — 핵심 요구
  (d) cpkTableHtml 이 통계 5컬럼만 축약하고, 축약된 셀에만 title/cpk-abbr 을 붙인다
  (e) 접이식 컬럼(▸/▾)이 제거돼 전 컬럼이 기본 펼침이다

Edge 가 없으면 정적 검사만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "server" / "report" / "static" / "webreport"
_TMP = Path(tempfile.mkdtemp(prefix="cpk_len8_js_"))

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# (입력 JS 리터럴, 기대 출력) — 설계 단계에서 headless Edge 로 실측·확정한 값.
# 입력을 JS 리터럴 문자열로 두는 이유: 파이썬 float 로 왕복시키면 표기가 달라진다
# (예: 2347582934789.2347832 는 파이썬에서 2347582934789.2349 로 재출력된다).
CASES = [
    # ── 사용자 확정 예시 ──────────────────────────────────────────────
    ("99999.5", "99999.5"),                 # 8자 이하 → 원문 유지
    ("0.00033423", "3.3423e-4"),
    ("33.235235", "33.23523"),              # 버림(반올림이면 33.23524)
    ("235.235", "235.235"),
    ("0.23489543", "0.234895"),
    ("0.00000334234", "3.3423e-6"),
    ("2347582934789.2347832", "2.347e12"),  # 버림(반올림이면 2.348e12)

    # ── 자리올림이 나면 안 된다 (핵심 요구) ──────────────────────────
    ("9.9999999", "9.999999"),
    ("99.999999", "99.99999"),
    ("0.99999999", "0.999999"),
    ("999999.99", "999999.9"),

    # ── 부동소수점 곱셈 버그 (곱셈 방식이면 8.6 이 된다) ─────────────
    ("8.7000000001", "8.7"),
    ("1.0050000001", "1.005"),
    ("1.0000000001", "1"),

    # ── stdev (서버 무반올림 — 가장 긴 값) ───────────────────────────
    ("0.332346234632", "0.332346"),
    ("1.4142135623731", "1.414213"),
    ("123.45678901234", "123.4567"),
    ("0.000123456789012", "1.2345e-4"),

    # ── min/median/max (서버 6자리) — 정수부가 커질수록 소수부가 준다 ─
    ("12.123456", "12.12345"),
    ("123.123456", "123.1234"),
    ("1234.123456", "1234.123"),
    ("12345.123456", "12345.12"),
    ("123456.123456", "123456.1"),
    ("1234567.123456", "1234567"),
    ("12345678.123456", "1.2345e7"),

    # ── 작은 값: 유효숫자가 뭉개지면 지수표기 ────────────────────────
    ("0.0001234", "1.234e-4"),
    ("0.0000012345", "1.2345e-6"),
    ("0.000000000123", "1.23e-10"),         # 원문이 8자 이하 → 유지

    # ── 큰 값: 지수부 길이를 포함해 세야 값이 안 틀어진다 ────────────
    ("123456789", "1.2345e8"),
    ("12345678901", "1.234e10"),            # 지수부 미포함이면 1.2346e1 ❌
    ("123456789012345", "1.234e14"),

    # ── 음수: 부호를 길이에서 제외해 양수와 같은 형태 ────────────────
    ("-235.235", "-235.235"),
    ("-33.235235", "-33.23523"),
    ("-0.332346234632", "-0.332346"),
    ("-12345678901", "-1.234e10"),
    ("-1.2999999", "-1.299999"),

    # ── 8자 이하는 손대지 않는다 ─────────────────────────────────────
    ("0", "0"),
    ("3.3", "3.3"),
    ("100", "100"),
    ("1234.567", "1234.567"),
    ("0.000001", "0.000001"),
    ("1000000", "1000000"),
    ("12345678", "12345678"),

    # ── 극단·예외 ────────────────────────────────────────────────────
    ("1e-96", "1e-96"),
    ("1e-97", "1e-97"),                     # toFixed RangeError 경계
    ("5e-324", "5e-324"),
    ("null", ""),                           # Number(null)=0 오인 방지
    ("undefined", ""),
    ('""', ""),
    ('"abc"', "abc"),                       # 숫자 아니면 원문 통과
    ("NaN", "NaN"),
    ("Infinity", "Infinity"),
]


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def run_probe(harness_js: str, name: str, extra_js=()) -> str:
    """core.js(+extra)를 인라인한 페이지를 돌리고 `<pre id=res>` 내용을 반환.

    stdout 은 **파일로** 리다이렉트한다 — 파이프로 받으면 Windows 에서 빈 출력이 온다.
    """
    scripts = "".join(
        f"<script>{(_JS / n).read_text(encoding='utf-8')}</script>"
        for n in ("core.js",) + tuple(extra_js))
    # ⚠ DATA/SESSION_ID 를 스크립트보다 **먼저 선언하면 안 된다** — core.js 가 둘 다
    # let/const 로 스스로 선언해, 하네스의 var 선선언과 충돌하면 core.js 스크립트
    # 전체가 SyntaxError 로 죽는다(전 항목이 "파싱 오류 의심"으로 위장 실패).
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            + scripts + harness_js + "</body></html>")
    page = _TMP / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / f"{name}.dom.txt"
    args = ",".join("'%s'" % a for a in (
        "--headless=new", "--disable-gpu", "--no-sandbox",
        "--virtual-time-budget=5000", "--dump-dom", page.as_uri()))
    ps = (f"Start-Process -FilePath '{edge_path()}' -ArgumentList @({args}) "
          f"-RedirectStandardOutput '{dump}' -NoNewWindow -Wait")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=180, check=False)
    raw = dump.read_text(encoding="utf-8", errors="replace") if dump.is_file() else ""
    m = re.search(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert m, f"{name}: 하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    return m.group(1).strip()


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_no_es_module():
    """분할 JS 는 classic script 순서 로드다 — import/export 를 쓰면 전부 죽는다."""
    for name in ("core.js", "cpk.js"):
        src = (_JS / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|export)\s", src, re.M), f"{name}: ES module 금지"


def test_no_float_mul_truncation():
    """버림을 곱셈으로 구현하면 8.7 이 8.6 이 된다 — 그 패턴이 없는지 소스로 확인."""
    src = (_JS / "core.js").read_text(encoding="utf-8")
    body = src[src.index("function _len8TruncFixed"):src.index("function fmtLen8")]
    assert "Math.floor" not in body and "Math.ceil" not in body, (
        "_len8Trunc* 가 곱셈+floor/ceil 로 버림하고 있습니다 — "
        "부동소수점 오차로 8.7 이 8.6 이 됩니다. toFixed 후 문자열 절단을 쓰세요.")


def test_collapsible_removed():
    """접이식 컬럼은 제거됐다 — 전 컬럼 기본 펼침(사용자 요청 2026-08-26)."""
    src = (_JS / "cpk.js").read_text(encoding="utf-8")
    for token in ("CPK_COLLAPSIBLE_COLS", "cpkColsExpanded", "cpk-col-toggle", "cpk-col-closed"):
        assert token not in src, f"cpk.js 에 접기 잔재가 남아 있습니다: {token}"
    html = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert "cpk-col-toggle" not in html, "report_view.html 에 접기 CSS 가 남아 있습니다"


def test_fmtstdev_kept():
    """fmtStdev 는 호출자가 없어졌지만 남겨 둔다 (기존 dead code 임의 삭제 금지)."""
    src = (_JS / "core.js").read_text(encoding="utf-8")
    assert "function fmtStdev(" in src, "fmtStdev 정의를 지우지 마세요"


# ── 브라우저 검사 ────────────────────────────────────────────────────────────

def test_fmt_len8():
    """확정 표시 규칙 + 값 보존 + 자리올림 없음(3중 검산)."""
    inputs = "[" + ",".join(src for src, _ in CASES) + "]"
    harness = f"""<pre id="res"></pre><script>
    const INPUTS = {inputs};
    const SRC = {json.dumps([s for s, _ in CASES])};
    const out = INPUTS.map((v, i) => {{
      let r, err = null;
      try {{ r = fmtLen8(v); }} catch (e) {{ r = null; err = String(e); }}
      // 검산 ②③ — 숫자 입력에 한해 값 보존과 자리올림 여부를 본다.
      let keep = true, grew = false;
      if (err === null && typeof v === "number" && Number.isFinite(v) && v !== 0) {{
        const back = Number(r);
        // 1.8e308 류는 Number() 가 Infinity 를 내므로 오탐 예외.
        if (!Number.isFinite(back)) keep = Math.abs(v) > 1e307;
        else {{
          keep = Math.abs((back - v) / v) <= 0.001;
          // 버림이므로 절대값이 커질 수 없다(부동소수점 여유 1e-15).
          grew = Math.abs(back) > Math.abs(v) * (1 + 1e-15);
        }}
      }}
      return {{src: SRC[i], got: r, err: err, keep: keep, grew: grew}};
    }});
    document.getElementById("res").textContent = JSON.stringify(out);
    </script>"""
    rows = json.loads(run_probe(harness, "fmt_len8"))
    assert len(rows) == len(CASES)
    bad = []
    for row, (src, want) in zip(rows, CASES):
        if row["err"]:
            bad.append(f"  {src}: 예외 {row['err']}")
            continue
        if row["got"] != want:
            bad.append(f"  {src}: got={row['got']!r} want={want!r}")
        if not row["keep"]:
            bad.append(f"  {src}: 값이 틀어졌습니다 (got={row['got']!r})")
        if row["grew"]:
            bad.append(f"  {src}: 자리올림 발생 (got={row['got']!r}) — 버림이어야 합니다")
    assert not bad, "fmtLen8 불일치:\n" + "\n".join(bad)
    print(f"  [OK] fmtLen8 {len(CASES)}건 (표시값·값보존·자리올림없음)")


def test_cpk_table_cells():
    """cpkTableHtml: 통계 5컬럼만 축약 + 축약된 셀에만 title/cpk-abbr."""
    row = {
        "subject": "ITEM_A", "lower_limit": 1.5, "upper_limit": 9.5,
        "units": "V", "source": "S1", "n": 100,
        "min": 0.00000334234,          # → 3.3423e-6 (축약)
        "median": 33.235235,           # → 33.23523  (축약)
        "max": 1234.567,               # → 원문 유지 (8자)
        "average": 0.23489543,         # → 0.234895  (축약)
        "stdev": 0.332346234632,       # → 0.332346  (축약)
        "cpl": 1.111, "cpu": 1.222, "cp": 1.152, "cpk": 1.1,
    }
    harness = f"""<pre id="res"></pre><script>
    // cpk.js 는 webReportSheets()/MODE 등 전역에 기대므로 최소 스텁만 둔다.
    window.webReportSheets = () => ({{CPK: [{json.dumps(row)}]}});
    cpkShowLowOnly = false;      // 임계 필터 끄고 전체 행 렌더
    cpkAbnormalMode = "all";     // 동일Limit 필터 끄기
    const html = cpkTableHtml([{json.dumps(row)}]);
    const doc = new DOMParser().parseFromString(html, "text/html");
    const heads = [...doc.querySelectorAll("thead th")].map(t => t.textContent);
    const tds = [...doc.querySelectorAll("tbody td")].map(t => ({{
      text: t.textContent, title: t.getAttribute("title"),
      abbr: t.classList.contains("cpk-abbr"),
    }}));
    document.getElementById("res").textContent = JSON.stringify({{
      heads: heads, tds: tds,
      toggles: doc.querySelectorAll("[data-cpk-col]").length,
      closed: doc.querySelectorAll(".cpk-col-closed").length,
    }});
    </script>"""
    res = json.loads(run_probe(harness, "cpk_cells", extra_js=("cpk.js",)))
    heads, tds = res["heads"], res["tds"]
    assert len(tds) == len(heads), f"td 개수({len(tds)}) != th 개수({len(heads)}) — sticky 열 어긋남"
    cell = {h: t for h, t in zip(heads, tds)}

    # 접기 기능 제거 확인
    assert res["toggles"] == 0, "▸/▾ 토글 버튼이 남아 있습니다"
    assert res["closed"] == 0, "접힌 빈 셀이 남아 있습니다"

    # 축약된 컬럼: 표시값이 줄고 title 에 원값, cpk-abbr 클래스
    for col, want, raw in (("min", "3.3423e-6", "0.00000334234"),
                           ("median", "33.23523", "33.235235"),
                           ("average", "0.234895", "0.23489543"),
                           ("stdev", "0.332346", "0.332346234632")):
        c = cell[col]
        assert c["text"] == want, f"{col}: {c['text']!r} != {want!r}"
        assert c["title"] == raw, f"{col}: title 이 원값이 아닙니다 ({c['title']!r})"
        assert c["abbr"], f"{col}: cpk-abbr 클래스가 없습니다"

    # 축약이 **안 된** 값에는 title/cpk-abbr 이 붙지 않는다 (점선 밑줄 남발 방지)
    assert cell["max"]["text"] == "1234.567"
    assert cell["max"]["title"] is None, "축약 안 된 셀에 title 이 붙었습니다"
    assert not cell["max"]["abbr"], "축약 안 된 셀에 cpk-abbr 이 붙었습니다"

    # 비대상 컬럼은 종전 그대로 (서버가 이미 3자리 / limit 은 규격값)
    for col, want in (("cpl", "1.111"), ("cpu", "1.222"), ("cp", "1.152"),
                      ("cpk", "1.1"), ("lower_limit", "1.5"), ("upper_limit", "9.5"),
                      ("n", "100"), ("units", "V"), ("source", "S1")):
        assert cell[col]["text"] == want, f"{col}: {cell[col]['text']!r} != {want!r}"
        assert cell[col]["title"] is None, f"{col}: 비대상인데 title 이 붙었습니다"
    print("  [OK] cpkTableHtml 셀 렌더 (축약 대상/툴팁/기본 펼침)")


def main() -> int:
    static = [test_no_es_module, test_no_float_mul_truncation,
              test_collapsible_removed, test_fmtstdev_kept]
    browser = [test_fmt_len8, test_cpk_table_cells]
    failed = 0
    print("[정적 검사]")
    for fn in static:
        try:
            fn()
            print(f"  [OK] {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {e}")
    print("[브라우저 검사]")
    if not edge_path():
        print("  [SKIP] msedge.exe 없음 — 브라우저 검사 생략")
    else:
        for fn in browser:
            try:
                fn()
            except AssertionError as e:
                failed += 1
                print(f"  [FAIL] {fn.__name__}:\n{e}")
    print("\n" + ("전체 통과" if failed == 0 else f"실패 {failed}건"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
