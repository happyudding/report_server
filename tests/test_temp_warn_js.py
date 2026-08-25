"""Temperature 그룹 해석 실패 경고 배지 — headless Edge (2026-08-25).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_temp_warn_js.py

왜 필요한가: 서버는 옵션의 RT 이름이 현재 source 이름과 안 맞으면 그룹을 통째로 버리고
(validation.webreport_temperature_groups), metrics._temperature_context 가 **전체 source**
를 Yield 입력으로 돌려준다 — Yield/Issue Table 에 CT/HT 가 섞여 계산되는데 **에러도
경고도 없다**("전체를 RT로 인식" 신고). 그래서 화면 배지가 유일한 발견 수단이고,
이 판정이 조용히 죽으면 사용자는 다시 틀린 숫자를 말없이 보게 된다.

판정 근거는 `temp_corner` 부재다 — payload 에 경고 키를 새로 넣으면
REPORT_SCHEMA_VERSION bump = 전 세션 콜드 폭풍이라 넣지 않았다. 서버 쪽 계약
(실패 시 temp_corner 가 안 붙는다)은 tests/test_temperature_payload.py 가 고정한다.

검증하는 것:
  (a) Temperature + temp_corner 있음 → 배지 없음 (거짓 경고 방지 — 가장 중요)
  (b) Temperature + temp_corner 전무 → 배지 표시 + 문구에 "전체 source" 가 들어간다
  (c) Normal 모드 → 배지 없음 (다른 모드 무영향)
  (d) sources 가 비었을 때 → 배지 없음 (로딩 중 오탐 방지)
  (e) distTempFilterHtml() 은 broken 일 때 배지를 내면서 `dist-temp-filter` 클래스를
      유지한다 — distRenderTempFilters 가 그 선택자로 제자리 교체하므로 잃으면 이후
      재렌더가 자리를 못 찾는다

Edge 가 없으면 정적 검사만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from html import unescape
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "server" / "report" / "static" / "webreport"
_TMP = Path(tempfile.mkdtemp(prefix="wr_tempwarn_js_"))

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


_EMIT = ("<script>function _emit(o){var p=document.createElement('pre');"
         "p.id='res';p.textContent=JSON.stringify(o);document.body.appendChild(p);}</script>")

DEPS = ["core.js", "distribution.js"]


def run_probe(harness_js, name) -> str:
    """지정 JS 를 인라인한 페이지를 돌리고 `_emit()` 이 남긴 JSON 을 반환.

    stdout 은 **파일로** 리다이렉트한다 — 파이프로 받으면 Windows 에서 빈 출력이 온다
    (tests/test_dist_seq_js.py 와 같은 규약)."""
    tags = "".join(f"<script>{(_JS / n).read_text(encoding='utf-8')}</script>"
                   for n in DEPS)
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            + tags + _EMIT + harness_js + "</body></html>")
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
    found = re.findall(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert found, f"{name}: 하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    return unescape(found[-1]).strip()


def test_static():
    """정적 — 두 함수가 존재하고, distTempFilterHtml 이 배지 경로를 갖는다."""
    src = (_JS / "distribution.js").read_text(encoding="utf-8")
    assert "function tempGroupsBroken(" in src
    assert "function tempWarnHtml(" in src
    # 필터 HTML 이 broken 분기를 타면서 클래스를 유지하는지(정적으로도 한 번 걸어둔다)
    assert "tempGroupsBroken()" in src and 'class="dist-temp-filter"' in src

    # Yield/Issue Table 도 같은 함수를 쓴다(문구 사본 금지)
    yi = (_JS / "yield_issue.js").read_text(encoding="utf-8")
    assert yi.count("tempWarnHtml()") >= 2, "Yield·Issue Table 두 곳에 배지가 필요하다"
    assert "temp-warn" not in yi, "배지 마크업 사본이 생겼다 — tempWarnHtml() 하나만 쓸 것"


def _case(mode, sources):
    return ("DATA={session:{source:'web_report',mode:%s},web_report:{sources:%s}};"
            % (json.dumps(mode), json.dumps(sources)))


def test_browser():
    if not edge_path():
        print("  [skip] Edge 없음 — 브라우저 검사 생략")
        return
    harness = "<script>" + """
      var out = {};
      function probe(k, setup) {
        eval(setup);
        out[k] = { broken: tempGroupsBroken(), warn: tempWarnHtml(),
                   filter: distTempFilterHtml() };
      }
    """ + (
        "probe('ok', %s);" % json.dumps(
            _case("Temperature", [{"name": "WF1_RT", "temp_corner": "RT"},
                                  {"name": "WF1_CT", "temp_corner": "CT"}]))
        + "probe('broken', %s);" % json.dumps(
            _case("Temperature", [{"name": "WF1_RT"}, {"name": "WF1_CT"}]))
        + "probe('normal', %s);" % json.dumps(
            _case("Normal", [{"name": "WF1"}, {"name": "WF2"}]))
        + "probe('empty', %s);" % json.dumps(_case("Temperature", []))
    ) + "_emit(out);</script>"

    res = json.loads(run_probe(harness, "temp_warn"))

    # (a) 정상 Temperature — 배지가 뜨면 안 된다 (거짓 경고는 신뢰를 깎는다)
    assert res["ok"]["broken"] is False, res["ok"]
    assert res["ok"]["warn"] == "", res["ok"]
    assert "distseg" in res["ok"]["filter"], "정상이면 종전 필터 버튼이 나와야 한다"
    print("  [browser] (a) 정상 Temperature — 배지 없음 · 필터 버튼 유지 OK")

    # (b) 깨진 Temperature — 배지 + 핵심 문구
    assert res["broken"]["broken"] is True, res["broken"]
    assert "temp-warn" in res["broken"]["warn"], res["broken"]
    assert "전체 source" in res["broken"]["warn"], res["broken"]
    # (e) 필터 자리가 배지로 바뀌되 클래스는 유지 (distRenderTempFilters 가 찾는 선택자)
    assert 'class="dist-temp-filter"' in res["broken"]["filter"], res["broken"]
    assert "distseg" not in res["broken"]["filter"], "죽은 필터 버튼을 남기면 안 된다"
    print("  [browser] (b)(e) 깨진 Temperature — 배지 표시 · 필터 자리 교체 · 클래스 유지 OK")

    # (c) Normal 모드 무영향
    assert res["normal"]["broken"] is False and res["normal"]["warn"] == "", res["normal"]
    assert res["normal"]["filter"] == "", "Temperature 아닌 모드는 DOM 자체가 없다"
    print("  [browser] (c) Normal 모드 — 무영향 OK")

    # (d) sources 가 아직 없을 때(로딩 중) 오탐 금지
    assert res["empty"]["broken"] is False, res["empty"]
    print("  [browser] (d) sources 빈 상태 — 오탐 없음 OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    test_static()
    print("  [static] 함수 존재 · 배지 문구 단일 출처 OK")
    test_browser()
    print("[통과] Temperature 그룹 실패 경고 배지 정상")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)
