"""CPK 탭 Source 드롭다운 + TOTAL 행 프런트 회귀 (2026-08-27).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_cpk_total_js.py

**왜 이 파일이 생겼나**: CPK 탭의 source 선택이 토글 칩 바 → **다중 선택 드롭다운**으로
바뀌고 `TOTAL`(전 source rawdata 통합) 옵션이 생겼다. 이 UI 는 눈으로 보면 멀쩡한데
실제로는 조용히 어긋나는 함정이 여럿이라, 설계 단계에서 짚은 것들을 그대로 고정한다:

  (1) 메뉴 항목을 누를 때 renderCpk()(패널 전체 재렌더)를 부르면 **메뉴 DOM 이 날아가**
      연속 다중 선택이 불가능해진다 → 부분 갱신이어야 한다
  (2) cpkDisplayRows 메모 sig 에 cpkShowTotal 이 빠지면 TOTAL 을 켜도 **옛 표가 남는다**
      (TOTAL 은 rows identity 밖에서 온다)
  (3) 메뉴에 data-issue-act / data-dc-act 를 쓰면 edit_mode.js / dist_composite.js 의
      클릭 위임이 오발한다 → 전용 네임스페이스 data-cpk-src
  (4) cpkSourceFilter(Set)에 "TOTAL" 을 특수값으로 넣으면 source 이름이 실제로 "TOTAL"
      인 세션과 충돌하고 stale 정리가 매 렌더마다 지운다 → 별도 플래그

검증하는 것:
  (a) 기본(TOTAL 미선택) 표에 TOTAL 행이 없다 — 종전 동작 불변
  (b) TOTAL 을 켜면 **CPK 임계 필터를 면제**받아 cpk 가 좋은 항목의 TOTAL 도 보인다
  (c) TOTAL 이 그 subject 의 **첫 행**이고 이어지는 source 행의 subject/limit 이 비워진다
  (d) 메뉴 마크업이 data-cpk-src 만 쓰고 issue/dc 네임스페이스를 침범하지 않는다
  (e) 메모 sig 가 cpkShowTotal 변경에 무효화된다
  (f) TOTAL 행이 없는 세션(구 캐시·Temperature)에선 메뉴에 TOTAL 항목이 안 뜬다

Edge 가 없으면 정적 검사만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_cpk_len8_js import _JS, _ROOT, edge_path, run_probe  # noqa: E402

# ITEM_A = cpk 낮음(임계 필터 통과) / ITEM_B = cpk 높음(임계 필터에서 걸러짐).
# TOTAL 면제를 검증하려면 "source 행은 걸러지는데 TOTAL 은 보이는" 항목이 필요하다.
_SRC_ROWS = [
    {"subject": "ITEM_A", "source": "S1", "units": "V",
     "lower_limit": 8, "upper_limit": 12, "n": 10, "min": 9.0, "median": 10.0,
     "max": 11.0, "average": 10.0, "stdev": 0.9, "cp": 0.74, "cpl": 0.74,
     "cpu": 0.74, "cpk": 0.74},
    {"subject": "ITEM_A", "source": "S2", "units": "V",
     "lower_limit": 8, "upper_limit": 12, "n": 10, "min": 9.2, "median": 10.1,
     "max": 11.2, "average": 10.1, "stdev": 0.8, "cp": 0.83, "cpl": 0.87,
     "cpu": 0.79, "cpk": 0.79},
    {"subject": "ITEM_B", "source": "S1", "units": "V",
     "lower_limit": 8, "upper_limit": 12, "n": 10, "min": 9.9, "median": 10.0,
     "max": 10.1, "average": 10.0, "stdev": 0.05, "cp": 13.3, "cpl": 13.3,
     "cpu": 13.3, "cpk": 13.3},
    {"subject": "ITEM_B", "source": "S2", "units": "V",
     "lower_limit": 8, "upper_limit": 12, "n": 10, "min": 9.9, "median": 10.0,
     "max": 10.1, "average": 10.0, "stdev": 0.05, "cp": 13.3, "cpl": 13.3,
     "cpu": 13.3, "cpk": 13.3},
]
_TOTAL_ROWS = [
    {"subject": "ITEM_A", "source": "TOTAL", "units": "V",
     "lower_limit": 8, "upper_limit": 12, "n": 20, "min": 9.0, "median": 10.05,
     "max": 11.2, "average": 10.05, "stdev": 0.85, "cp": 0.78, "cpl": 0.80,
     "cpu": 0.76, "cpk": 0.76},
    {"subject": "ITEM_B", "source": "TOTAL", "units": "V",
     "lower_limit": 8, "upper_limit": 12, "n": 20, "min": 9.9, "median": 10.0,
     "max": 10.1, "average": 10.0, "stdev": 0.05, "cp": 13.3, "cpl": 13.3,
     "cpu": 13.3, "cpk": 13.3},
]


def _stub(total=True):
    """cpk.js 가 기대하는 전역 스텁 — sheets 2장."""
    sheets = {"CPK": _SRC_ROWS, "CPK Total": _TOTAL_ROWS if total else []}
    return (f"window.webReportSheets = () => ({json.dumps(sheets)});\n"
            "cpkAbnormalMode = 'all';\n")


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_no_es_module():
    """분할 JS 는 classic script 순서 로드다 — import/export 를 쓰면 전부 죽는다."""
    src = (_JS / "cpk.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\s", src, re.M), "cpk.js: ES module 금지"


def test_menu_uses_own_namespace():
    """메뉴가 issue/dc 네임스페이스를 침범하면 다른 모듈의 클릭 위임이 오발한다."""
    src = (_JS / "cpk.js").read_text(encoding="utf-8")
    body = src[src.index("function cpkSourceMenuHtml("):src.index("function cpkBodyRows")]
    for token in ("data-issue-act", "data-dc-act", "data-gc-act"):
        assert token not in body, f"cpk.js Source 메뉴가 {token} 을 씁니다 — 위임 오발"
    assert "data-cpk-src" in body, "전용 네임스페이스 data-cpk-src 가 없습니다"


def test_chip_bar_removed():
    """칩 바는 드롭다운으로 대체됐다 — 죽은 코드·CSS 를 남기지 않는다."""
    src = (_JS / "cpk.js").read_text(encoding="utf-8")
    for token in ("cpkSourceBarHtml", "data-cpk-src-all"):
        assert token not in src, f"cpk.js 에 칩 바 잔재가 남아 있습니다: {token}"
    html = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert "cpk-src-bar" not in html, "report_view.html 에 칩 바 CSS 가 남아 있습니다"


def test_total_is_separate_flag():
    """TOTAL 을 cpkSourceFilter(Set)에 넣으면 실제 source 이름 'TOTAL' 과 충돌한다."""
    src = (_JS / "cpk.js").read_text(encoding="utf-8")
    assert "let cpkShowTotal" in src, "cpkShowTotal 별도 플래그가 없습니다"
    assert "cpkSourceFilter.add(CPK_TOTAL_SOURCE)" not in src, (
        "TOTAL 을 cpkSourceFilter 에 넣고 있습니다 — 별도 플래그를 쓰세요")


def test_memo_sig_includes_total():
    """메모 sig 에 cpkShowTotal 이 없으면 TOTAL 을 켜도 옛 표가 남는다."""
    src = (_JS / "cpk.js").read_text(encoding="utf-8")
    sig = src[src.index("const sig = JSON.stringify(["):]
    sig = sig[:sig.index("]);")]
    assert "cpkShowTotal" in sig, "cpkDisplayRows 메모 sig 에 cpkShowTotal 이 빠졌습니다"


def test_menu_click_does_not_full_rerender():
    """메뉴 항목 핸들러가 renderCpk() 를 부르면 메뉴가 사라져 다중 선택이 불가능하다."""
    src = (_JS / "cpk.js").read_text(encoding="utf-8")
    start = src.index('const sb = e.target.closest("[data-cpk-src]");')
    body = src[start:src.index('const pb = e.target.closest("[data-cpk-page]");')]
    assert "renderCpk()" not in body, (
        "Source 메뉴 핸들러가 패널을 통째로 다시 그립니다 — 메뉴 DOM 이 날아가 "
        "연속 다중 선택이 불가능해집니다. renderCpkTable()+cpkRefreshMenu() 를 쓰세요.")
    assert "renderCpkTable()" in body and "cpkRefreshMenu()" in body, body[:200]


def test_excel_export_untouched():
    """웹 Excel Down 은 sheets['CPK'] 전량 — TOTAL 을 섞으면 Honey 클라와 갈라진다."""
    src = (_JS / "excel_export.js").read_text(encoding="utf-8")
    assert "CPK Total" not in src, (
        "excel_export.js 가 CPK Total 을 참조합니다 — Honey Excel Download "
        "(client/excel_download/_sheets.py) 와 파리티가 깨집니다.")


# ── 브라우저 검사 ────────────────────────────────────────────────────────────

def _render(js_state, total=True):
    """주어진 상태로 cpkTableHtml 을 돌려 (subject, source) 행 목록을 돌려준다."""
    harness = f"""<pre id="res"></pre><script>
    {_stub(total)}
    {js_state}
    const html = cpkTableHtml({json.dumps(_SRC_ROWS)});
    const doc = new DOMParser().parseFromString(html, "text/html");
    const heads = [...doc.querySelectorAll("thead th")].map(t => t.textContent);
    const iSub = heads.indexOf("subject"), iSrc = heads.indexOf("source");
    const iLo = heads.indexOf("lower_limit");
    const rows = [...doc.querySelectorAll("tbody tr")].map(tr => {{
      const td = [...tr.querySelectorAll("td")].map(t => t.textContent);
      return [td[iSub], td[iSrc], td[iLo]];
    }});
    document.getElementById("res").textContent = JSON.stringify(rows);
    </script>"""
    return json.loads(run_probe(harness, "cpk_total_render", extra_js=("cpk.js",)))


def test_default_has_no_total():
    """(a) 기본 = TOTAL 미선택 — 표에 TOTAL 행이 없다(종전 동작 불변)."""
    rows = _render("cpkShowLowOnly = false;")
    assert rows, "표가 비었습니다"
    assert not [r for r in rows if r[1] == "TOTAL"], rows
    assert len(rows) == len(_SRC_ROWS), rows


def test_total_exempt_from_threshold():
    """(b) TOTAL 은 CPK 임계 필터 면제 — cpk 13.3 인 ITEM_B 의 TOTAL 도 보인다."""
    rows = _render("cpkShowLowOnly = true; cpkShowTotal = true;")
    totals = [r for r in rows if r[1] == "TOTAL"]
    assert len(totals) == 2, f"TOTAL 행 2개여야 합니다: {rows}"
    subs = {r[0] for r in totals}
    assert subs == {"ITEM_A", "ITEM_B"}, subs
    # ITEM_B 의 source 행은 cpk 13.3 이라 임계 필터에서 걸러진다 — TOTAL 만 남는다.
    assert not [r for r in rows if r[0] == "ITEM_B" and r[1] != "TOTAL"], rows
    # ITEM_A 는 source 행도 남는다(cpk < 1.33).
    assert [r for r in rows if r[1] in ("S1", "S2")], rows


def test_total_is_first_row_of_subject():
    """(c) TOTAL 이 subject 첫 행이고, 이어지는 source 행은 subject/limit 이 비워진다."""
    rows = _render("cpkShowLowOnly = false; cpkShowTotal = true;")
    # ITEM_A 구간 = 첫 3행 (TOTAL, S1, S2)
    assert rows[0][:2] == ["ITEM_A", "TOTAL"], rows[:3]
    assert rows[0][2] == "8", f"TOTAL 행의 limit 이 대표로 표시돼야 합니다: {rows[0]}"
    assert rows[1][0] == "" and rows[1][1] == "S1", rows[1]
    assert rows[1][2] == "", f"반복 행의 limit 이 비워져야 합니다: {rows[1]}"
    assert rows[2][0] == "" and rows[2][1] == "S2", rows[2]
    assert rows[3][:2] == ["ITEM_B", "TOTAL"], rows[3]


def test_menu_hides_total_when_absent():
    """(f) TOTAL 행이 없는 세션(구 캐시·Temperature)엔 메뉴에 TOTAL 항목이 없다."""
    harness = f"""<pre id="res"></pre><script>
    {_stub(total=False)}
    const off = cpkSourceMenuHtml(["S1", "S2"]);
    {_stub(total=True)}
    const on = cpkSourceMenuHtml(["S1", "S2"]);
    document.getElementById("res").textContent = JSON.stringify({{off: off, on: on}});
    </script>"""
    res = json.loads(run_probe(harness, "cpk_total_menu", extra_js=("cpk.js",)))
    assert 'data-cpk-src="TOTAL"' not in res["off"], (
        "TOTAL 행이 없는데 메뉴에 TOTAL 항목이 떴습니다 — 눌러도 빈 표가 됩니다")
    assert 'data-cpk-src="TOTAL"' in res["on"], "TOTAL 행이 있는데 메뉴에 안 뜹니다"
    for html in (res["off"], res["on"]):
        assert 'data-cpk-src="S1"' in html and 'data-cpk-src="__all__"' in html, html


def test_memo_invalidates_on_total_toggle():
    """(e) cpkShowTotal 을 바꾸면 메모가 무효화돼 표가 실제로 바뀐다."""
    harness = f"""<pre id="res"></pre><script>
    {_stub(True)}
    cpkShowLowOnly = false;
    const before = cpkDisplayRows({json.dumps(_SRC_ROWS)}).length;
    cpkShowTotal = true;
    const after = cpkDisplayRows({json.dumps(_SRC_ROWS)}).length;
    document.getElementById("res").textContent = JSON.stringify([before, after]);
    </script>"""
    before, after = json.loads(run_probe(harness, "cpk_total_memo", extra_js=("cpk.js",)))
    assert after == before + 2, (
        f"메모가 무효화되지 않았습니다 (before={before}, after={after}) — "
        "cpkDisplayRows sig 에 cpkShowTotal 이 빠졌습니다")


def main() -> int:
    static = [test_no_es_module, test_menu_uses_own_namespace, test_chip_bar_removed,
              test_total_is_separate_flag, test_memo_sig_includes_total,
              test_menu_click_does_not_full_rerender, test_excel_export_untouched]
    browser = [test_default_has_no_total, test_total_exempt_from_threshold,
               test_total_is_first_row_of_subject, test_menu_hides_total_when_absent,
               test_memo_invalidates_on_total_toggle]
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
                print(f"  [OK] {fn.__name__}")
            except AssertionError as e:
                failed += 1
                print(f"  [FAIL] {fn.__name__}:\n{e}")
    print("\n" + ("전체 통과" if failed == 0 else f"실패 {failed}건"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
