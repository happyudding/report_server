# -*- coding: utf-8 -*-
"""Issue Table 열 접기(Signature/AI Comment) 회귀 — 2026-09-02 "표가 통째로 사라짐" 방지.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_issue_col_fold_js.py

**무엇이 깨졌었나** (2026-09-01 커밋 d763282, 두 버그가 맞물렸다):

① CSS 셀렉터 그룹화 오류 — report_view.html 의 접기 규칙이 이렇게 쓰여 있었다:
     `#panel-issues.fold-sig .sheet-table,` ← 콤마로 끊겨 **독립 셀렉터**가 됐다
     `... #panel-issue-cmp-table.fold-sig .sheet-table.kind-issue td.fold-col-sig { display:none }`
   의도는 "각 패널에서 td.fold-col-sig 를 감춘다" 였는데, 앞 항목들이 대상 요소와
   이어지지 않아 **`.sheet-table` 전체가 display:none** 이 됐다. 즉 열 하나를 접으면
   Issue Table 표가 통째로 사라졌다. (콤마 뒤는 언제나 새 셀렉터다 — 공통 접두를
   나눠 쓸 수 없다.)

② `issuePanels()` 오타 — 실제 함수명은 core.js 의 `issuePanelEls()` 다. yield_issue.js
   두 줄만 없는 이름을 불렀다.

두 버그의 합: 화살표를 누르면 `colFoldSet` 이 localStorage 에 접힘을 **먼저** 쓰고,
다음 줄 `applyColFoldAll()` 이 ReferenceError 로 죽어 화면은 무반응. 세션을 다시 열면
저장된 값이 복원돼 ①이 표를 감추고, 되돌릴 화살표에는 닿을 수 없어 **영구 고착**됐다.
("처음엔 보였는데 나갔다 오니 Issue Table 이 아예 안 뜬다" 신고의 실제 경로.)

이 기능에는 테스트가 하나도 없어 test_webreport_sheets_js.py 가 전부 통과하는데도
잡히지 않았다.

검증 항목:
  (a) [정적] yield_issue.js 가 부르는 issuePanel* 이름이 실제로 정의돼 있다 (②)
  (b) [정적] 접기 CSS 의 모든 셀렉터가 대상 요소(td/col/th.fold-col-*)로 끝난다 (①)
  (c) [Edge] fold-sig 를 켜도 **표는 보이고** 해당 td 만 사라진다 (①의 실동작)
  (d) [Edge] 한 열의 접기가 다른 열·다른 패널로 새지 않는다

Edge 가 없으면 (a)(b) 만 하고 (c)(d) 는 SKIP 한다(이 저장소는 node 가 없다).
pytest 미사용(tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = Path(tempfile.mkdtemp(prefix="colfold_js_"))

VIEW = _ROOT / "server" / "report" / "report_view.html"
YIELD_ISSUE = _ROOT / "server" / "report" / "static" / "webreport" / "yield_issue.js"
CORE = _ROOT / "server" / "report" / "static" / "webreport" / "core.js"

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 접기 CSS 블록을 뽑는 표식 — 이 두 클래스가 걸린 규칙만 검사한다.
_FOLD_CLS = ("fold-col-sig", "fold-col-aic")


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


# ── (a) 호출하는 함수가 실제로 있는가 ────────────────────────────────────────

def test_a_panel_helper_defined():
    js = YIELD_ISSUE.read_text(encoding="utf-8")
    core = CORE.read_text(encoding="utf-8")
    called = set(re.findall(r"\b(issuePanel[A-Za-z]*)\s*\(", js))
    defined = set(re.findall(r"function\s+(issuePanel[A-Za-z]*)\s*\(", js + core))
    missing = sorted(called - defined)
    assert not missing, (
        f"yield_issue.js 가 정의되지 않은 함수를 부른다: {missing} "
        f"(정의된 것: {sorted(defined)}) - 2026-09-02 issuePanels/issuePanelEls 오타 회귀")
    print("  (a) issuePanel* 호출 이름이 모두 정의돼 있다 OK")


# ── (b) 접기 CSS 셀렉터가 대상 요소로 끝나는가 ───────────────────────────────

def test_b_fold_selectors_target_cells():
    css = VIEW.read_text(encoding="utf-8")
    # <style> 안의 규칙만 본다. 주석은 셀렉터 파싱을 어지럽히므로 먼저 지운다.
    style = "\n".join(re.findall(r"<style>(.*?)</style>", css, re.S))
    style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
    bad = []
    for sel_blob, _body in re.findall(r"([^{}]+)\{([^{}]*)\}", style):
        if not any(c in sel_blob for c in _FOLD_CLS):
            continue
        for sel in sel_blob.split(","):
            sel = sel.strip()
            if not sel or ".fold-sig" not in sel and ".fold-aic" not in sel:
                continue
            # 패널 접힘 클래스를 쓰는 셀렉터는 반드시 대상 열 요소까지 지목해야 한다.
            if not any(c in sel for c in _FOLD_CLS):
                bad.append(sel)
    assert not bad, (
        "접기 CSS 셀렉터가 대상 요소(td/col/th.fold-col-*)로 끝나지 않는다 - "
        "표 전체가 display:none 된다(2026-09-02 회귀):\n  " + "\n  ".join(bad))
    print("  (b) 접기 CSS 셀렉터가 모두 대상 열 요소를 지목한다 OK")


# ── (c)(d) 실제 렌더 — Edge ──────────────────────────────────────────────────

_PROBE = """
<div id="panel-issues" class="fold-sig">
  <table class="sheet-table kind-issue">
    <tr><th class="fold-col-sig">Signature<button class="col-fold-btn">B</button></th>
        <th class="fold-col-aic">AI Comment</th><th>Bin</th></tr>
    <tr><td class="fold-col-sig">SIG</td><td class="fold-col-aic">AIC</td><td>1</td></tr>
  </table>
</div>
<div id="panel-issue-temp" class="fold-aic">
  <table class="sheet-table kind-issue">
    <tr><td class="fold-col-sig">SIG2</td><td class="fold-col-aic">AIC2</td></tr>
  </table>
</div>
<pre id="res"></pre>
<script>
(function () {
  var d = function (s) { return getComputedStyle(document.querySelector(s)).display; };
  var out = {
    sigTable: d('#panel-issues .sheet-table'),
    sigTd:    d('#panel-issues td.fold-col-sig'),
    sigTh:    d('#panel-issues th.fold-col-sig'),
    aicTd:    d('#panel-issues td.fold-col-aic'),
    binTd:    d('#panel-issues tr:nth-child(2) td:nth-child(3)'),
    tempTable: d('#panel-issue-temp .sheet-table'),
    tempAicTd: d('#panel-issue-temp td.fold-col-aic'),
    tempSigTd: d('#panel-issue-temp td.fold-col-sig')
  };
  document.getElementById('res').textContent = JSON.stringify(out);
})();
</script>
"""


def _run_probe() -> dict:
    import json
    style = "\n".join(re.findall(r"<style>(.*?)</style>",
                                 VIEW.read_text(encoding="utf-8"), re.S))
    html = ("<!doctype html><meta charset=\"utf-8\">\n<style>" + style + "</style>\n"
            + _PROBE)
    page = _TMP / "colfold.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / "colfold.dom.txt"
    with open(dump, "wb") as fh:
        subprocess.run(
            [edge_path(), "--headless=new", "--disable-gpu", "--no-sandbox",
             "--virtual-time-budget=5000", "--dump-dom", page.as_uri()],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=120, check=False)
    raw = dump.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert m, "하네스가 실행되지 않았습니다 (CSS/스크립트 파싱 오류 의심)"
    return json.loads(m.group(1).strip())


def test_cd_fold_hides_only_the_column():
    got = _run_probe()
    # (c) 이것이 회귀의 본체 — 표가 살아 있어야 한다.
    assert got["sigTable"] != "none", (
        "fold-sig 를 켜자 Issue Table 표가 통째로 사라졌다 - "
        "CSS 셀렉터 그룹화 오류(2026-09-02 회귀)")
    assert got["sigTd"] == "none", "접은 열의 td 가 그대로 보인다"
    assert got["sigTh"] != "none", "헤더까지 감추면 되돌릴 화살표에 닿을 수 없다"
    assert got["binTd"] != "none", "접기와 무관한 열이 사라졌다"
    print("  (c) fold-sig - 표는 보이고 해당 td 만 접힌다 OK")

    # (d) 다른 열·다른 패널로 새지 않는다.
    assert got["aicTd"] != "none", "fold-sig 인데 AI Comment 열까지 접혔다"
    assert got["tempTable"] != "none", "fold-aic 패널의 표가 사라졌다"
    assert got["tempAicTd"] == "none", "fold-aic 패널에서 aic 열이 안 접혔다"
    assert got["tempSigTd"] != "none", "fold-aic 인데 sig 열까지 접혔다"
    print("  (d) 접기가 다른 열·패널로 새지 않는다 OK")


def main() -> int:
    print("Issue Table 열 접기 회귀 검증")
    try:
        test_a_panel_helper_defined()
        test_b_fold_selectors_target_cells()
        if edge_path():
            test_cd_fold_hides_only_the_column()
        else:
            print("  (c,d) SKIP - headless Edge 를 찾지 못했습니다")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
