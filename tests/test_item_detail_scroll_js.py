r"""Item_detail Back 스크롤 복원 회귀 — headless Edge (2026-08-25).

실행:
    server\.venv\Scripts\python.exe tests\test_item_detail_scroll_js.py

**왜 파이썬 테스트로 안 되나**: 이 동작이 깨지는 방식은 에러가 아니라 "화면이 맨 위로
튄다"이다. Distribution 갤러리를 한참 내려가 카드를 열고 ← Back 하면 매번 처음으로
돌아가 어디를 보고 있었는지 잃는다(2026-08-25 신고).

검증하는 것:
  (a) 상세를 **새로 열 때만** 직전 문서 스크롤을 기억한다
  (b) 상세 안 항목 이동(prev/next = 이미 열린 상태에서의 openItemDetail)은 기억값을
      덮지 않는다 — 덮으면 상세 화면 스크롤(대개 0)이 복귀 위치가 되어 버그가 되살아난다
  (c) closeItemDetail(← Back / Esc)이 그 자리로 되돌리고 기억값을 비운다
  (d) hideItemDetail(탭 전환)은 복원하지 않고 기억값만 버린다

Edge 가 없으면 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_dist_composite_js import edge_path, run_probe   # noqa: E402

# item_detail.js 는 core.js(esc/showToast/fetchJson202) · distribution.js(distVariantQuery
# 등) · chart_notes.js(_cnDirty) 위에서 돈다.
DEPS = ["core.js", "distribution.js", "chart_notes.js", "item_detail.js"]

# 실제 세션 상세와 같은 최소 DOM — 복귀 대상 탭 패널 + 상세 패널.
# 복귀 패널에 충분한 높이를 줘야 스크롤이 실제로 내려간다(문서가 짧으면 브라우저가
# scrollTo 를 0 으로 클램프해 테스트가 통과해도 의미가 없다).
BODY = """
<style>.panel{display:none}.panel.active{display:block}</style>
<div class="content">
  <div id="panel-distribution" class="panel active"><div style="height:4000px"></div></div>
  <div id="panel-item-detail" class="panel"></div>
</div>
"""

# fetch 는 file:// 에서 못 돈다 — 영원히 pending 인 Promise 로 갈아끼워 렌더 경로를 막는다
# (이 테스트는 스크롤만 본다). Plotly 는 __plotlyReady 대기 분기를 피하려고 스텁을 둔다.
SETUP = """
window.Plotly = { purge: function(){}, react: function(){}, newPlot: function(){} };
fetchJson202 = function(){ return new Promise(function(){}); };
"""


def probe(harness: str, name: str):
    return json.loads(run_probe(DEPS, BODY, f"<script>{SETUP}{harness}</script>", name))


def test_save_restore():
    """(a)(b)(c) 열 때 기억 → 항목 이동은 유지 → Back 이 복원."""
    res = probe("""
      try {
        var out = {};
        window.scrollTo(0, 1500);
        out.before = Math.round(window.scrollY);
        openItemDetail("ITEM_A", ["ITEM_A", "ITEM_B"]);
        out.saved = Math.round(_itemDetailReturnScroll);
        out.returnId = _itemDetailReturnId;
        out.afterOpen = Math.round(window.scrollY);
        // 상세 안에서 다음 항목으로 이동 — 이미 active 라 기억값은 그대로여야 한다
        openItemDetail("ITEM_B", ["ITEM_A", "ITEM_B"]);
        out.savedAfterNav = Math.round(_itemDetailReturnScroll);
        closeItemDetail();
        out.afterClose = Math.round(window.scrollY);
        out.reset = _itemDetailReturnScroll;
        out.backActive = document.getElementById("panel-distribution").classList.contains("active");
        _emit(out);
      } catch (e) { _emit({ error: String((e && e.message) || e) }); }
    """, "idet_scroll_restore")
    assert "error" not in res, res
    assert res["before"] == 1500, f"하네스 스크롤이 안 됨: {res}"
    assert res["saved"] == 1500, f"(a) 열 때 위치를 기억하지 않음: {res}"
    assert res["returnId"] == "panel-distribution", res
    assert res["afterOpen"] == 0, f"상세는 맨 위에서 시작해야 한다: {res}"
    assert res["savedAfterNav"] == 1500, f"(b) 항목 이동이 기억값을 덮었다: {res}"
    assert res["backActive"] is True, res
    assert res["afterClose"] == 1500, f"(c) Back 이 원래 자리로 복원하지 않음: {res}"
    assert res["reset"] == 0, f"(c) 복원 후 기억값을 비우지 않음: {res}"
    print("  [OK] 열 때 기억 → 항목 이동 유지 → Back 복원 (1500px)")


def test_tab_switch_discards():
    """(d) 탭 전환(hideItemDetail)은 복원하지 않고 기억값을 버린다."""
    res = probe("""
      try {
        var out = {};
        window.scrollTo(0, 900);
        openItemDetail("ITEM_A", ["ITEM_A"]);
        out.saved = Math.round(_itemDetailReturnScroll);
        hideItemDetail();
        out.reset = _itemDetailReturnScroll;
        out.scrollY = Math.round(window.scrollY);
        // 이후 상세를 새로 열고 Back — 옛 위치(900)가 되살아나면 안 된다
        document.getElementById("panel-distribution").classList.add("active");
        window.scrollTo(0, 300);
        openItemDetail("ITEM_A", ["ITEM_A"]);
        closeItemDetail();
        out.afterClose = Math.round(window.scrollY);
        _emit(out);
      } catch (e) { _emit({ error: String((e && e.message) || e) }); }
    """, "idet_scroll_hide")
    assert "error" not in res, res
    assert res["saved"] == 900, res
    assert res["reset"] == 0, f"(d) 탭 전환이 기억값을 버리지 않음: {res}"
    assert res["afterClose"] == 300, f"(d) 옛 위치가 되살아남: {res}"
    print("  [OK] 탭 전환은 기억값을 버린다 / 다음 진입은 새 위치를 쓴다")


def main():
    if not edge_path():
        print("SKIP: Edge 를 찾지 못했습니다 (headless 검증 불가)")
        return 0
    fails = 0
    for fn in (test_save_restore, test_tab_switch_discards):
        try:
            fn()
        except AssertionError as e:
            fails += 1
            print(f"  [FAIL] {fn.__name__}: {e}")
    print("ALL PASS" if not fails else f"{fails} FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
