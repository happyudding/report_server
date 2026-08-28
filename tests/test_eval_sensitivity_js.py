"""AI Comment 민감도 게이지 프런트 회귀 — headless Edge (2026-08-28).

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_eval_sensitivity_js.py

왜 파이썬 테스트로 안 되나: 이 기능이 깨지는 방식은 화면에서만 드러난다.
  · `esensBuildSpec` 이 게이지 3단계 키까지 실으면 **기본 설정 세션의 옵션 원문이 바뀐다**
    → `report_key` 가 갈려 전 세션 콜드 재빌드(콜드 폭풍)이고, 제품군 오버레이
    (/pe/eval 의 MDDI bimodality_warn 0.33 등)도 세션값에 덮여 조용히 무력해진다.
  · 읽기 전용 사용자에게 입력란·저장 버튼이 새면 400 만 받는 UI 가 된다.
  · 툴팁(설명)이 안 붙으면 처음 보는 사람은 숫자만 보고 뜻을 모른다.
  · 렌더가 죽어도 모달이 빈 채로 뜰 뿐 에러가 없다.

검증하는 것:
  (a) 정적: classic script(ES module 금지) · 단계표 하드코딩 없음 · 버튼/모달 id 짝
  (b) 정적: 다크모드 CSS 짝 (신규 .esens-* 클래스)
  (c) esensBuildSpec — 기본이면 null / 게이지 그룹만 실림 / 직접입력 우선
  (d) 렌더 — 행 수·입력란·고정 그룹 disabled·선택 단계 표시·툴팁
  (e) 읽기 전용 — 입력란 없음·저장 버튼 숨김·기본 설정 안내

Edge 가 없으면 정적 검사만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from html import unescape
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "server" / "report" / "static" / "webreport"
_HTML = _ROOT / "server" / "report" / "report_view.html"
_TMP = Path(tempfile.mkdtemp(prefix="wr_esens_js_"))

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

# 모듈이 최상위에서 붙잡는 DOM. 없으면 스크립트가 통째로 죽는다.
_BODY = "".join(f"<div id='{i}'></div>" for i in (
    "btnEvalSens", "esensClose", "esensSave", "esensBody", "evalSensModal",
    "esensNote", "esensDesc"))

# core.js 전체를 얹지 않는 이유: 이 모듈이 쓰는 전역은 esc/csrfToken/SESSION_ID 셋뿐이라
# 스텁이 더 정확하다(core.js 는 다른 DOM 을 잔뜩 요구한다).
_PRELUDE = ("<script>window.SESSION_ID='SID';"
            "window.esc=s=>String(s===undefined||s===null?'':s);"
            "window.csrfToken=()=>'tok';</script>")

# 검증 코드는 **모듈과 같은 script 블록**에 이어붙여야 한다 — let 바인딩
# (_esensCatalog/_esensCache/_esensDraft)은 window 로도 eval 로도 못 건드린다.
_CATALOG = {
    "groups": [
        {"id": "OUTLIER", "label_ko": "이상치", "signatures": ["OUTLIER"],
         "gauge_fixed": False,
         "keys": [{"key": "outlier_fail_mad_min",
                   "levels": [4.24, 4.12, 4.0, 3.88, 3.76], "default": 4.0}]},
        {"id": "LOW_CPK", "label_ko": "공정능력", "signatures": ["LOW_CPK"],
         "gauge_fixed": True,
         "keys": [{"key": "cpk_warn", "levels": [1.33] * 5, "default": 1.33}]},
    ],
    "help": {"outlier_fail_mad_min": {"what": "무리 거리", "effect": "낮추면 민감"}},
}

_HARNESS = """
(function () {
  const out = {};
  _esensCatalog = %s;

  out.spec_default = esensBuildSpec(
    {global: 3, groups: {OUTLIER: 3, LOW_CPK: 3}, manual: {}});
  out.spec_gauge5 = esensBuildSpec(
    {global: 0, groups: {OUTLIER: 5, LOW_CPK: 3}, manual: {}});
  out.spec_manual = esensBuildSpec(
    {global: 3, groups: {OUTLIER: 3, LOW_CPK: 3}, manual: {cpk_warn: 1.5}});
  out.spec_manual_wins = esensBuildSpec(
    {global: 0, groups: {OUTLIER: 5, LOW_CPK: 3}, manual: {outlier_fail_mad_min: 2.0}});

  _esensCache = {applied: true, can_edit: true, ai_comment: true, global: 5,
                 groups: {OUTLIER: 5, LOW_CPK: 3},
                 items: [{key: 'outlier_fail_mad_min', value: 3.76, default: 4.0,
                          source: 'gauge', signatures: ['OUTLIER']}]};
  _esensDraft = esensDraftFrom(_esensCache);
  esensRender();
  esensBindHelp();
  const body = document.getElementById('esensBody');
  out.rows = body.querySelectorAll('tbody tr').length;
  out.inputs = body.querySelectorAll('input.esens-val').length;
  out.fixed_disabled =
    body.querySelectorAll('.esens-step[data-group="LOW_CPK"][disabled]').length;
  out.gauge_on = body.querySelector(
    '.esens-step[data-group="OUTLIER"][data-level="5"]').classList.contains('on');
  const keyEl = body.querySelector('[data-help="outlier_fail_mad_min"]');
  out.tooltip = keyEl ? String(keyEl.title || '') : '';
  out.save_shown = document.getElementById('esensSave').style.display !== 'none';

  _esensCache = {applied: false, can_edit: false, ai_comment: true, groups: {}, items: []};
  _esensDraft = esensDraftFrom(_esensCache);
  esensRender();
  out.ro_inputs =
    document.getElementById('esensBody').querySelectorAll('input.esens-val').length;
  out.ro_save_hidden = document.getElementById('esensSave').style.display === 'none';
  out.ro_note = document.getElementById('esensNote').textContent;

  _emit(out);
})();
""" % json.dumps(_CATALOG, ensure_ascii=False)


def run_probe() -> dict:
    """모듈 + 하네스를 한 script 블록에 넣어 돌리고 `_emit()` JSON 을 반환.

    stdout 은 **파일로** 리다이렉트한다 — 파이프로 받으면 Windows 에서 빈 출력이 온다."""
    js = (_JS / "eval_sensitivity_info.js").read_text(encoding="utf-8")
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            + _BODY + _PRELUDE + _EMIT
            + "<script>" + js + "\n" + _HARNESS + "</script></body></html>")
    page = _TMP / "esens.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / "esens.dom.txt"
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
    assert found, "하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    return json.loads(unescape(found[-1]).strip())


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_static_wiring():
    """(a) classic script · 단계표 미복제 · 버튼/모달 id 짝."""
    src = (_JS / "eval_sensitivity_info.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\s", src, re.M), "ES module 금지"

    # 단계표(레벨 값)를 클라에 복제하면 서버의 "3단계" 와 갈린다. 정본은 sensitivity.yaml.
    for literal in ("4.24", "3.76", "0.555", "region_fail_share_min"):
        assert literal not in src, f"단계표 값을 클라에 하드코딩하지 말 것: {literal}"

    html = _HTML.read_text(encoding="utf-8")
    assert 'id="btnEvalSens"' in html, "우상단 민감도 버튼이 없습니다"
    assert 'id="evalSensModal"' in html, "민감도 모달이 없습니다"
    assert "eval_sensitivity_info.js" in html, "script 목록에 모듈이 없습니다"
    for dom_id in ("esensBody", "esensNote", "esensDesc", "esensSave", "esensClose"):
        assert f'id="{dom_id}"' in html, f"{dom_id} 마크업 누락"
    # 버튼은 AI Comment 세션에서만 — boot.js 가 켠다.
    boot = (_JS / "boot.js").read_text(encoding="utf-8")
    assert "evalSensButtonSync" in boot, "boot.js 가 버튼 노출을 켜지 않습니다"
    print("  (a) 정적 배선 OK")


def test_dark_theme_pairs():
    """(b) 신규 .esens-* 클래스마다 다크모드 짝이 있어야 한다(라이트에서만 보이는 UI 방지)."""
    html = _HTML.read_text(encoding="utf-8")
    for cls in ("esens-step", "esens-val", "esens-help", "esens-custom"):
        assert f".{cls}" in html, f"{cls} 스타일 없음"
        assert re.search(r'html\[data-theme="dark"\][^\n]*\.' + cls, html), \
            f"{cls} 다크모드 짝 없음"
    print("  (b) 다크모드 CSS 짝 OK")


# ── 동적 검사 (Edge 필요) ────────────────────────────────────────────────────

def test_build_spec(res):
    """(c) 저장 payload 규칙 — 콜드 폭풍·오버레이 무력화를 막는 핵심."""
    assert res["spec_default"] is None, \
        f"기본 설정은 옵션에 키를 싣지 않아야 한다: {res['spec_default']}"

    g5 = res["spec_gauge5"]
    assert g5 and list(g5["overrides"]) == ["outlier_fail_mad_min"], \
        f"게이지 3인 그룹의 키가 실렸다(제품군 오버레이가 덮인다): {g5['overrides']}"
    assert g5["overrides"]["outlier_fail_mad_min"] == 3.76

    man = res["spec_manual"]
    assert man["overrides"]["cpk_warn"] == 1.5
    assert man["manual"]["cpk_warn"] == 1.5
    assert "outlier_fail_mad_min" not in man["overrides"], "게이지 3 키가 새어 들어갔다"

    assert res["spec_manual_wins"]["overrides"]["outlier_fail_mad_min"] == 2.0, \
        "직접 입력이 게이지 값을 이겨야 한다"
    print("  (c) esensBuildSpec 규칙 OK")


def test_render(res):
    """(d) 렌더 — 전체 행 + 그룹 2행, 입력란, 고정 그룹 disabled, 선택 단계, 툴팁."""
    assert res["rows"] == 3, f"전체행+그룹2 = 3행이어야 한다 (받은 값 {res['rows']})"
    assert res["inputs"] == 2, f"키 2개의 입력란 (받은 값 {res['inputs']})"
    assert res["fixed_disabled"] == 5, \
        f"gauge_fixed 그룹은 단계 5개가 전부 disabled (받은 값 {res['fixed_disabled']})"
    assert res["gauge_on"] is True, "선택 단계가 표시되지 않는다"
    assert "무리 거리" in res["tooltip"], f"툴팁 설명 없음: {res['tooltip']!r}"
    assert res["save_shown"] is True, "편집 권한이 있으면 저장 버튼이 보여야 한다"
    print("  (d) 렌더 OK")


def test_readonly(res):
    """(e) 읽기 전용 — 입력란 없음·저장 숨김·기본 설정 안내."""
    assert res["ro_inputs"] == 0, "읽기 전용에 입력란이 새면 400 만 받는다"
    assert res["ro_save_hidden"] is True, "읽기 전용에 저장 버튼이 보인다"
    assert "기본 설정" in res["ro_note"], f"기본 설정 안내 없음: {res['ro_note']!r}"
    print("  (e) 읽기 전용 OK")


if __name__ == "__main__":
    print("eval_sensitivity 프런트 검증")
    test_static_wiring()
    test_dark_theme_pairs()
    if not edge_path():
        print("  [SKIP] Edge 없음 — 동적 검사 생략")
    else:
        result = run_probe()
        test_build_spec(result)
        test_render(result)
        test_readonly(result)
    print("전부 통과")
