"""Input File Information 모달 + 세션 이름 인라인 편집 JS 회귀 — headless Edge.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_input_info_js.py

**왜 이 파일이 필요한가** (2026-08-20): 두 기능 다 파이썬 테스트가 못 잡는 방식으로 깨진다.
  · 모달은 "값이 하나도 없는 열은 숨긴다"가 핵심 규칙이다. 이게 뒤집히면 STDF 를 아직 못
    받는 지금 화면이 '-' 열 15개로 뒤덮인다(에러가 아니라 아무도 신고하지 않는다).
  · 세션 이름은 **사용자가 입력한 값**이다(CLAUDE.md 규칙 #12). 편집 권한이 없는 사람에게
    편집 자리가 열리거나, 반대로 권한자에게 안 열리면 화면에서만 드러난다.

검증하는 것:
  (a) STDF 가 실린 데이터 → LOT ID/Wafer No/Test Time 열이 표에 나온다
  (b) STDF 가 없으면 그 열들이 **통째로** 사라지고 필수 열(#/Source/Input File)만 남는다
  (c) has_stdf/has_file_info=false 면 왜 비었는지 안내문이 뜬다
  (d) Compare → group_index 순(Before→After) 정렬 + 그룹 소제목 행
  (e) Test Time 은 test_time_sec 이 없으면 start/finish 차이로 계산된다
  (f) 값 이스케이프 (source 이름에 태그를 넣어도 태그가 되지 않는다)
  (g) renderMeta 가 편집 권한자에게만 sname-editable 을 붙인다
  (h) 정적: classic script 유지 + report_view.html 로드 등록

Edge 가 없으면 정적 검사만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "server" / "report" / "static" / "webreport"
_TMP = Path(tempfile.mkdtemp(prefix="wr_iinfo_js_"))

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def js_literal(obj) -> str:
    """JSON 을 <script> 안에 안전하게 심는다 — `</` 만 끊어 조기 종료를 막는다."""
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


def run_probe(scripts, body_html, harness_js, name) -> str:
    """지정 JS 를 인라인한 페이지를 돌리고 `<pre id=res>` 내용을 반환.

    stdout 은 **파일로** 리다이렉트한다 — 파이프로 받으면 Windows 에서 빈 출력이 온다.
    """
    tags = "".join(f"<script>{(_JS / n).read_text(encoding='utf-8')}</script>"
                   for n in scripts)
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            + body_html + tags + harness_js + "</body></html>")
    page = _TMP / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / f"{name}.dom.txt"
    with open(dump, "wb") as fh:
        subprocess.run(
            [edge_path(), "--headless=new", "--disable-gpu", "--no-sandbox",
             "--virtual-time-budget=5000", "--dump-dom", page.as_uri()],
            stdout=fh, stderr=subprocess.DEVNULL, timeout=120, check=False)
    raw = dump.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'<pre id="res">([\s\S]*?)</pre>', raw)
    assert m, f"{name}: 하네스가 실행되지 않았습니다 (스크립트 파싱 오류 의심)"
    return m.group(1).strip()


# input_info.js 가 요구하는 최소 DOM (모달 껍데기 + 버튼) — 없으면 addEventListener 에서
# 죽어 스크립트 전체가 멎는다. report_view.html 의 id 와 같은 이름을 쓴다.
_MODAL_DOM = ("<button id='btnInputInfo'></button>"
              "<div id='inputInfoModal'><p id='iinfoDesc'></p><div id='iinfoNote'></div>"
              "<div id='iinfoBody'></div><button id='iinfoClose'></button></div>"
              "<div id='toast'></div>")

FULL = {
    "mode": "Normal", "has_file_info": True, "has_stdf": True,
    "sources": [{
        "index": 0, "name": "602XX2_3", "group": "", "group_index": -1, "role": "",
        "file_name": "602XX2_3_final.std", "file_path": r"D:\lot\602XX2_3_final.std",
        "input_files": [], "file_size": 5242880,
        "file_created": "2026-08-01 09:30:00", "file_modified": "2026-08-01 10:05:00",
        "stdf": {"lot_id": "602XX2", "wafer_id": "3", "start_time": "2026-08-01 09:31:00",
                 "finish_time": "2026-08-01 10:04:00", "test_time_sec": 1980},
    }],
}
BARE = {
    "mode": "Normal", "has_file_info": False, "has_stdf": False,
    "sources": [{"index": 0, "name": "<b>Lot0</b>", "group": "", "group_index": -1,
                 "role": "", "file_name": "Lot0.csv", "file_path": "", "input_files": [],
                 "file_size": None, "file_created": "", "file_modified": "", "stdf": {}}],
}
CMP = {
    "mode": "Compare", "has_file_info": True, "has_stdf": False,
    "sources": [
        {"index": 0, "name": "AFT1", "group": "After", "group_index": 1, "role": "",
         "file_name": "a1.std", "file_path": r"D:\a1.std", "input_files": [],
         "file_size": 1024, "file_created": "2026-08-02 08:00:00",
         "file_modified": "2026-08-02 08:00:00", "stdf": {}},
        {"index": 1, "name": "BEF1", "group": "Before", "group_index": 0, "role": "",
         "file_name": "b1.std", "file_path": r"D:\b1.std", "input_files": [],
         "file_size": 2048, "file_created": "2026-08-01 08:00:00",
         "file_modified": "2026-08-01 08:00:00", "stdf": {}},
    ],
}
# test_time_sec 없이 시작/종료만 — (e) 계산 폴백 확인용 (37분 = 2220초).
CALC = {
    "mode": "Normal", "has_file_info": True, "has_stdf": True,
    "sources": [{"index": 0, "name": "S", "group": "", "group_index": -1, "role": "",
                 "file_name": "s.std", "file_path": "", "input_files": [],
                 "file_size": None, "file_created": "", "file_modified": "",
                 "stdf": {"start_time": "2026-08-01 09:00:00",
                          "finish_time": "2026-08-01 09:37:00"}}],
}


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_no_es_module():
    """분할 JS 는 classic script 순서 로드다 — import/export 를 쓰면 전부 죽는다."""
    src = (_JS / "input_info.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\s", src, re.M), "input_info.js: ES module 금지"
    print("[정적] classic script 유지 OK")


def test_registered():
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert "static/webreport/input_info.js" in view, "input_info.js 가 로드되지 않았습니다"
    # esc() 를 쓰므로 core.js 뒤여야 한다.
    assert view.index("core.js") < view.index("input_info.js"), \
        "input_info.js 는 core.js 뒤에 로드돼야 합니다"
    for el in ("inputInfoModal", "iinfoBody", "iinfoNote", "iinfoDesc", "iinfoClose",
               "btnInputInfo"):
        assert f'id="{el}"' in view, f"report_view.html 에 #{el} 이 없습니다"
    print("[정적] input_info.js 로드 순서 + 모달 DOM OK")


def test_name_route_static():
    """세션 이름 인라인 편집이 **이름 전용 라우트**를 쓰는가 (meta 라우트 재사용 금지).

    meta 라우트로 보내면 X-Honey-Agent 가 없어 403 이고, 통과시켜도 기준정보 14컬럼이
    함께 덮인다(rename_session docstring).
    """
    src = (_JS / "edit_mode.js").read_text(encoding="utf-8")
    assert "/name`" in src and "openSessionNameEdit" in src, \
        "세션 이름 인라인 편집이 edit_mode.js 에 없습니다"
    assert re.search(r'method:\s*"PATCH"[\s\S]{0,200}X-CSRF-Token', src), \
        "이름 저장 요청에 CSRF 토큰이 없습니다"
    routes = (_ROOT / "server" / "report" / "routes_session.py").read_text(encoding="utf-8")
    assert '@report_bp.patch("/session/<session_id>/name")' in routes, \
        "이름 전용 라우트가 없습니다"
    assert "rename_session" in routes, "이름 저장이 rename_session 을 쓰지 않습니다"
    print("[정적] 이름 전용 라우트 + CSRF OK")


# ── (a)~(f) 모달 렌더 ────────────────────────────────────────────────────────

def test_render():
    harness = (
        "<script>(function(){var out={};"
        "function cols(){return [].map.call(document.querySelectorAll('#iinfoBody th'),"
        "  function(th){return th.textContent;});}"
        # (a) STDF 있는 세션
        "iinfoRender(" + js_literal(FULL) + ");"
        "out.fullCols = cols();"
        "out.fullRow = [].map.call(document.querySelectorAll('#iinfoBody tbody td'),"
        "  function(td){return td.textContent;});"
        "out.fullNote = document.getElementById('iinfoNote').textContent;"
        # (b)(c) 아무 정보 없는 옛 세션
        "iinfoRender(" + js_literal(BARE) + ");"
        "out.bareCols = cols();"
        "out.bareNote = document.getElementById('iinfoNote').textContent;"
        "out.bareImg = document.querySelectorAll('#iinfoBody b').length;"
        "out.bareText = document.querySelector('#iinfoBody tbody tr').textContent;"
        # (d) Compare 정렬 + 그룹 행
        "iinfoRender(" + js_literal(CMP) + ");"
        "out.cmpGroups = [].map.call(document.querySelectorAll('#iinfoBody .iinfo-grouprow'),"
        "  function(tr){return tr.textContent;});"
        "out.cmpOrder = [].map.call("
        "  document.querySelectorAll('#iinfoBody tbody tr:not(.iinfo-grouprow) td:nth-child(2)'),"
        "  function(td){return td.textContent;});"
        # (e) Test Time 계산 폴백
        "out.calc = iinfoTestTime(" + js_literal(CALC["sources"][0]) + ");"
        "out.size = iinfoSize(5242880);"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);"
        "})();</script>")
    out = json.loads(run_probe(["core.js", "input_info.js"], _MODAL_DOM, harness, "render"))

    # (a)
    for want in ("LOT ID", "Wafer No", "Test Time", "파일 생성", "크기", "경로"):
        assert want in out["fullCols"], f"STDF 세션인데 '{want}' 열이 없다: {out['fullCols']}"
    assert "602XX2" in out["fullRow"] and "3" in out["fullRow"], out["fullRow"]
    assert "33m 0s" in out["fullRow"], f"Test Time 포맷: {out['fullRow']}"
    assert out["fullNote"].strip() == "", f"정보가 다 있는데 안내문이 떴다: {out['fullNote']}"
    print(f"  [ok] (a) STDF 열 렌더 ({len(out['fullCols'])}열)")

    # (b) 값 없는 열은 통째로 사라진다
    assert out["bareCols"] == ["#", "Source", "Input File"], \
        f"빈 열이 남았다: {out['bareCols']}"
    print("  [ok] (b) 값 없는 열 전부 숨김 → 필수 3열만")

    # (c) 왜 비었는지 안내
    assert "업로드" in out["bareNote"] and "STDF" in out["bareNote"], out["bareNote"]
    print("  [ok] (c) 빈 이유 안내문 표시")

    # (f) 이스케이프
    # 태그가 되지 않았는지는 b 요소 개수로 본다 — textContent 를 그대로 비교하면
    # --dump-dom 이 <pre> 안을 한 번 더 이스케이프해 기대값이 표현에 끌려다닌다.
    assert out["bareImg"] == 0, "source 이름의 <b> 가 태그가 되었다"
    assert "Lot0" in out["bareText"], out["bareText"]
    print("  [ok] (f) 값 이스케이프")

    # (d) Compare
    assert out["cmpGroups"] == ["Before", "After"], f"그룹 행 순서: {out['cmpGroups']}"
    assert out["cmpOrder"] == ["BEF1", "AFT1"], f"Before 가 먼저여야 한다: {out['cmpOrder']}"
    print("  [ok] (d) Compare Before→After 정렬 + 그룹 소제목")

    # (e)
    assert out["calc"] == "37m 0s", f"Test Time 계산 폴백: {out['calc']}"
    assert out["size"] == "5.0 MB", f"크기 포맷: {out['size']}"
    print("  [ok] (e) Test Time 계산 폴백 + 크기 포맷")


# ── (g) 세션 이름 편집 자리 노출 ─────────────────────────────────────────────

_TOPBAR_DOM = ("<div id='topbarMeta'></div><button id='btnImportant'></button>"
               "<button id='btnPrivate'></button><div id='stickyHead'></div>"
               "<div id='tabs'></div><div id='toast'></div>")

# renderMeta 가 기대는 이웃 전역들 — tabs_topbar.js 밖(core.js/user_name.js/boot.js)에서
# 오는 것만 스텁한다. 스크립트 **뒤에** 대입한다(core.js 가 스스로 선언하는 이름과 충돌 금지).
_TOPBAR_STUBS = (
    "<script>"
    "DATA={session:{file_name:'세션A'},web_report:{sources:[]}};"
    "MY_IMPORTANT=false; UPLOADER_NAME='';"
    "window.UserName={uid:function(v){return v||'';},fmt:function(a,b){return b||a||'';}};"
    "window.isWebReportSession=function(){return true;};"
    "window.syncStickyHeadHeight=function(){};"
    "window.updatePrivateBtn=function(){};"
    "window.syncTabVisibility=function(){};"
    "</script>")


def test_name_editable_gate():
    """편집 권한자에게만 이름 자리가 열린다 — 권한 없는 사람에게 열리면 400/403 만 본다."""
    harness = (
        _TOPBAR_STUBS +
        "<script>(function(){var out={};"
        "function draw(can){ window.canEditSession=function(){return can;};"
        "  renderMeta(DATA.session);"
        "  var el=document.getElementById('sessionNameVal');"
        "  return {exists: !!el, editable: !!(el && el.classList.contains('sname-editable')),"
        "          text: el?el.textContent:''}; }"
        "try{ out.can = draw(true); out.cannot = draw(false); out.err=''; }"
        "catch(e){ out.err = e.message; }"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);"
        "})();</script>")
    out = json.loads(run_probe(["core.js", "tabs_topbar.js"], _TOPBAR_DOM, harness, "topbar"))
    assert not out.get("err"), f"renderMeta 실행 실패: {out['err']}"
    assert out["can"]["exists"] and out["can"]["editable"], f"권한자에게 안 열림: {out['can']}"
    assert out["can"]["text"] == "세션A", out["can"]
    assert out["cannot"]["exists"], "이름 표시 자체가 사라지면 안 된다"
    assert not out["cannot"]["editable"], f"권한 없는데 편집 자리가 열렸다: {out['cannot']}"
    print("  [ok] (g) 이름 편집 자리는 편집 권한자에게만")


def main():
    print("[Input File Information · 세션 이름 편집 JS]")
    try:
        test_no_es_module()
        test_registered()
        test_name_route_static()
        if not edge_path():
            print("[SKIP] Edge 없음 — 렌더 검사 생략 (정적 검사만 수행)")
            return
        test_render()
        test_name_editable_gate()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("[통과] 모달 렌더 + 이름 편집 게이트 정상")


if __name__ == "__main__":
    main()
