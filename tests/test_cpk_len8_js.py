"""CPK 통계값 표시 길이 제한(fmtLen8) 회귀 — headless Edge 로 core.js/cpk.js 를 돌려 본다.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_cpk_len8_js.py

**왜 이 파일이 생겼나** (2026-08-26): CPK 탭의 통계 컬럼(min/median/max/average/stdev)은
서버가 컬럼마다 다른 자리수로 내려보내(min~max 6자리 / average 4자리 / stdev 무반올림)
`2347582934789.234783` 같은 값 하나가 컬럼 폭을 통째로 밀어냈다. 이를 표시 단에서
줄이고 원값은 title 툴팁에 남기는 `fmtLen8`(core.js)을 도입했다.

**표시 규칙 (3차 확정)** — 소수 최대 4자리 반올림 + 전체 8자(부호 제외) 이내.
원문이 8자 이하면 손대지 않고, 반올림이 **자리올림**을 일으키면(999999.99 → 1000000
처럼 앞자리가 통째로 바뀌면) 그 자리에서 **버림**으로 대체한다. 소수 4자리로 잘라
유효숫자가 2자리 미만이 되면 지수표기(`0.00034345` → `3.4345e-4`).

숫자 포맷은 눈으로 훑으면 맞아 보이지만 실제로는 **값이 조용히 틀어지는** 종류의 코드다.
설계 단계에서 실측한 버그 3종이 그 증거이며, 이 테스트는 그것들을 다시 밟지 않게 한다:
  (1) 지수부(e-10·e308) 길이를 빼고 세면 `12345678901` 이 `1.2346e1`(=12.346) 이 된다
  (2) 끝자리 0 제거를 지수표기 전체에 걸면 `1.0000e+1` 이 `1.00000e` 로 깨진다
  (3) 버림을 `Math.floor(n*10**d)/10**d` 로 하면 `8.7` 이 `8.6` 이 된다(부동소수점 곱셈)

검증하는 것:
  (a) 확정된 표시 규칙대로 나온다 (기대값 표 — 사용자 확정 예시 포함)
  (b) 출력을 다시 Number() 로 읽어 **깨지지 않는다**
  (c) **자리올림이 일어나지 않는다** (|출력| <= |원값|) — 핵심 요구
  (d) cpkTableHtml 이 통계 5컬럼만 축약하고, 축약된 셀에만 title/cpk-abbr 을 붙인다
  (e) 접이식 컬럼(▸/▾)이 제거돼 전 컬럼이 기본 펼침이다
  (f) 점선 밑줄 CSS 가 없다 (툴팁만 남기고 밑줄은 제거 — 사용자 요청)
  (g) **Issue Table Compare** 의 before_/after_ 통계 컬럼에도 같은 규칙이 적용되고,
      같은 렌더러를 쓰는 일반 Issue Table 컬럼은 영향받지 않는다

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
    # ── 사용자 확정 예시 (2026-08-26 3차 규칙) ───────────────────────
    ("0.00034345", "3.4345e-4"),            # 소수4 로는 0.0003 = 유효숫자 1 → 지수
    ("0.00234234", "0.0023"),
    ("0.0234325", "0.0234"),
    ("1.234565473457", "1.2346"),           # 반올림(버림이면 1.2345)
    ("2346.23", "2346.23"),
    ("123461.234", "123461.2"),             # 전체 8자 상한
    ("234626.2346234", "234626.2"),

    # ── 자리올림이 나면 그 자리에서 버림 (핵심 요구) ─────────────────
    # 반올림하면 앞자리가 통째로 바뀌어(999999.99 → 1000000) 다른 값처럼 보인다.
    ("999999.99", "999999.9"),
    ("99999.999", "99999.99"),
    ("0.99999999", "0.9999"),
    ("99.999999", "99.9999"),
    ("9.9999999", "9.9999"),
    ("1.99999999", "1.9999"),

    # ── 부동소수점 곱셈 버그 (곱셈 방식이면 8.6 이 된다) ─────────────
    ("8.7000000001", "8.7"),
    ("1.0050000001", "1.005"),
    ("1.0000000001", "1"),

    # ── stdev (서버 무반올림 — 가장 긴 값) ───────────────────────────
    ("0.332346234632", "0.3323"),
    ("1.4142135623731", "1.4142"),
    ("123.45678901234", "123.4568"),
    ("0.000123456789012", "1.2346e-4"),
    ("0.00033423", "3.3423e-4"),

    # ── min/median/max (서버 6자리) — 정수부가 커질수록 소수부가 준다 ─
    ("12.123456", "12.1235"),
    ("123.123456", "123.1235"),
    ("1234.123456", "1234.123"),
    ("12345.123456", "12345.12"),
    ("123456.123456", "123456.1"),
    ("12345678.123456", "12345678"),

    # ── 작은 값: 잘려서 유효숫자를 잃으면 지수, 원래 짧으면 그대로 ───
    ("0.0003", "0.0003"),                   # 원값이 짧다 → 유지
    ("0.0001", "0.0001"),
    ("0.00012345", "1.2345e-4"),            # 잘리면 0.0001 = 유효숫자 1 → 지수
    ("0.000001234", "1.234e-6"),
    ("0.00000334234", "3.3423e-6"),

    # ── 큰 값: 지수부 길이를 포함해 세야 값이 안 틀어진다 ────────────
    ("123456789", "1.2346e8"),
    ("12345678901", "1.235e10"),            # 지수부 미포함이면 1.2346e1 ❌
    ("2347582934789.2347832", "2.348e12"),

    # ── 음수: 부호를 길이에서 제외해 양수와 같은 형태 ────────────────
    ("-235.235", "-235.235"),
    ("-33.235235", "-33.2352"),
    ("-0.332346234632", "-0.3323"),
    ("-0.00034345", "-3.435e-4"),
    ("-999999.99", "-999999.9"),

    # ── 원문 8자 이하는 손대지 않는다 (자리올림 자체가 없다) ─────────
    ("9.99999", "9.99999"),
    ("19.99999", "19.99999"),
    ("0.9999", "0.9999"),
    ("99999.5", "99999.5"),
    ("0", "0"),
    ("3.3", "3.3"),
    ("100", "100"),
    ("1234.567", "1234.567"),
    ("0.000001", "0.000001"),
    ("1000000", "1000000"),
    ("12345678", "12345678"),

    # ── 극단·예외 ────────────────────────────────────────────────────
    ("1e-96", "1e-96"),
    ("1e-97", "1e-97"),
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
    body = src[src.index("function _len8Trunc("):src.index("function fmtLen8")]
    assert "Math.floor" not in body and "Math.ceil" not in body, (
        "_len8Trunc 가 곱셈+floor/ceil 로 버림하고 있습니다 — "
        "부동소수점 오차로 8.7 이 8.6 이 됩니다. toFixed 후 문자열 절단을 쓰세요.")


def test_no_abbr_underline():
    """축약 셀 점선 밑줄은 제거됐다 — 툴팁만 남긴다(사용자 요청 2026-08-26)."""
    html = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    for m in re.finditer(r"\.sheet-table\.cpk-sheet td\.cpk-abbr\s*\{([^}]*)\}", html):
        assert "underline" not in m.group(1), (
            "CPK 표 축약 셀에 점선 밑줄이 다시 붙었습니다 — 통계 컬럼 대부분이 축약 "
            "대상이라 표 전체에 밑줄이 깔립니다. 툴팁만 남기세요.")


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
          // 소수 4자리 반올림이라 작은 값은 상대오차가 클 수 있다(0.00034345 → 0.0003 은
          // 12.6%). "값이 통째로 틀어졌는가"만 보므로 넉넉히 50% 로 둔다 —
          // 12345678901 → 1.2346e1 같은 자리수 오류(99.9%)를 잡는 것이 목적이다.
          keep = Math.abs((back - v) / v) <= 0.5;
          // ★ 자리올림 검사 — 막으려는 것은 **일반표기에서 정수부가 바뀌는** 올림뿐이다
          //   (999999.99 → 1000000 처럼 앞자리가 통째로 달라지는 것).
          //   · 마지막 소수 자리가 올라 값이 미세하게 커지는 건 반올림의 정상 동작이고,
          //   · 지수표기(1.2346e8)는 가수부만 남아 정수부 비교 자체가 무의미하다.
          grew = r.indexOf("e") < 0
                 && Math.floor(Math.abs(back)) > Math.floor(Math.abs(v));
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
        "median": 33.235235,           # → 33.2352   (축약)
        "max": 1234.567,               # → 원문 유지 (8자)
        "average": 0.23489543,         # → 0.2349    (축약)
        "stdev": 0.332346234632,       # → 0.3323    (축약)
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
                           ("median", "33.2352", "33.235235"),
                           ("average", "0.2349", "0.23489543"),
                           ("stdev", "0.3323", "0.332346234632")):
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


def test_compare_issue_cells():
    """Issue Table Compare: before_/after_ 통계 컬럼만 축약, 일반 컬럼은 무변경.

    이 표는 전용 렌더러가 없고 **일반 Issue Table 과 같은 renderSheetTable(kind:"issue")**
    를 쓴다(compare_issue.js → yield_issue.js → sheets.js). 그래서 컬럼명으로만 대상을
    가르며, 그 경계가 무너지면 일반 Issue Table 의 값 표시까지 바뀐다.
    """
    # compare_issue.py `_stat_cells` 가 만드는 실제 컬럼 구성 + 일반 Issue Table 컬럼 혼합.
    # ⚠ `Category` 는 orderColumns 가 화면에서 빼고(섹션 판정 전용), `avg` 는 _yield 컬럼이
    #   1개 이하면 제외된다 — 그래서 "일반 컬럼 무변경"은 avg 대신 실제로 렌더되는
    #   `WF1_yield`(+ avg 동반 표시)로 확인한다.
    row = {
        "Category": "CMPDIST", "구분": "산포", "Step": "P1", "TNO": "10", "Item": "ITEM_A",
        "before_avg": 0.23489543,        # → 0.2349     (축약)
        "before_stdev": 0.332346234632,  # → 0.3323     (축약)
        "before_cpk": 1.233,             # → 원문 유지  (8자 이하)
        "after_avg": 1234.567,           # → 원문 유지  (8자)
        "after_stdev": 0.00034345,       # → 3.4345e-4  (축약·지수)
        "after_cpk": 0.987,              # → 원문 유지
        # 일반 Issue Table 값 컬럼 — **축약 대상이 아니므로 무변경이어야 한다**
        "avg": 12.345678901,
        "WF1_yield": 98.7654321,
        "WF2_yield": 97.1234567,
        "PTE comment": "코멘트",
    }
    # ⚠ kind:"issue" 는 헤더를 <thead> 가 아니라 tbody 안 섹션 헤더 행으로 넣는다
    #   (issueSectionHeadRowsHtml). 컬럼 매핑은 데이터 행의 data-c 인덱스로 한다.
    harness = f"""<pre id="res"></pre><script>
    const ROW = {json.dumps(row, ensure_ascii=False)};
    const cols = orderColumns(Object.keys(ROW), "issue");
    const html = renderSheetTable([ROW], {{ kind: "issue", columns: cols }});
    const doc = new DOMParser().parseFromString(html, "text/html");
    // 데이터 행 = data-r 이 붙은 td 를 가진 마지막 tr (섹션 헤더 행에는 data-r 이 없다)
    const dataTds = [...doc.querySelectorAll("td[data-r]")].map(t => ({{
      c: Number(t.dataset.c), text: t.textContent,
      title: t.getAttribute("title"), abbr: t.classList.contains("cpk-abbr"),
    }}));
    document.getElementById("res").textContent = JSON.stringify({{cols: cols, tds: dataTds}});
    </script>"""
    res = json.loads(run_probe(harness, "cmp_issue_cells",
                               extra_js=("sig_reason.js", "sheets.js")))
    cols, tds = res["cols"], res["tds"]
    assert tds, "데이터 행 td 가 렌더되지 않았습니다"
    cell = {cols[t["c"]]: t for t in tds if t["c"] < len(cols)}
    # orderColumns 가 화면에서 빼는 컬럼은 검사 대상에서 제외한다 —
    # Category(섹션 판정 전용) + 구분(2026-08-26 사용자 요청으로 화면에서 제거).
    hidden_cols = ("Category", "구분")
    missing = [c for c in row if c not in hidden_cols and c not in cell]
    assert not missing, f"렌더되지 않은 컬럼: {missing} (렌더된 컬럼: {sorted(cell)})"
    for c in hidden_cols:
        assert c not in cell, f"{c} 는 화면 컬럼에서 빠져야 합니다"

    # 축약 대상: 표시값이 줄고 title 에 원값
    for col, want, raw in (("before_avg", "0.2349", "0.23489543"),
                           ("before_stdev", "0.3323", "0.332346234632"),
                           ("after_stdev", "3.4345e-4", "0.00034345")):
        c = cell[col]
        assert c["text"] == want, f"{col}: {c['text']!r} != {want!r}"
        assert c["title"] == raw, f"{col}: title 이 원값이 아닙니다 ({c['title']!r})"
        assert c["abbr"], f"{col}: cpk-abbr 클래스가 없습니다"

    # 8자 이하라 축약이 안 된 통계 컬럼 — title 도 안 붙는다
    for col, want in (("before_cpk", "1.233"), ("after_avg", "1234.567"),
                      ("after_cpk", "0.987")):
        assert cell[col]["text"] == want, f"{col}: {cell[col]['text']!r} != {want!r}"
        assert cell[col]["title"] is None, f"{col}: 축약 안 됐는데 title 이 붙었습니다"

    # ★ 공용 렌더러 오염 방지 — 일반 Issue Table 값 컬럼은 종전 그대로여야 한다
    for col, want in (("avg", "12.345678901"),
                      ("WF1_yield", "98.7654321"),
                      ("WF2_yield", "97.1234567")):
        assert cell[col]["text"] == want, (
            f"일반 Issue Table 의 {col} 컬럼까지 축약됐습니다"
            f" (got={cell[col]['text']!r}) — CMP_STAT_COL_RE 경계가 무너졌습니다")
        assert cell[col]["title"] is None, f"일반 {col} 컬럼에 title 이 붙었습니다"
    assert cell["Item"]["text"] == "ITEM_A"
    print("  [OK] Issue Table Compare 셀 렌더 (통계 컬럼만 축약·일반 컬럼 무변경)")


def main() -> int:
    static = [test_no_es_module, test_no_float_mul_truncation, test_no_abbr_underline,
              test_collapsible_removed, test_fmtstdev_kept]
    browser = [test_fmt_len8, test_cpk_table_cells, test_compare_issue_cells]
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
