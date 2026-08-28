"""Issue Table 셀 렌더 JS 회귀 — headless Edge 로 sheets.js / sig_reason.js 를 돌려 본다.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_webreport_sheets_js.py

**왜 이 파일이 생겼나** (2026-08-12): AI Comment 셀을 서버 평문 그대로 찍던 것을 프런트에서
파싱해 재배치하도록 바꿨다([현상]/[과거사례]/[점검제안] 색 분리 + [MAJOR] 배지를 맨 아랫줄로).
파싱은 **글자를 잃을 수 있는** 변경이라 파이썬 테스트로는 못 잡는다 — 브라우저에서 실제
DOM 을 만들어 원문과 대조해야 한다. 같은 이유로 Signature 드랍다운의 "비활성 룰 제외"도
여기서 지킨다: 이미 저장된 값이 목록에서 사라지면 사용자 입력이 조용히 날아간다.

검증하는 것:
  (a) renderAiComment 가 원문 글자를 하나도 잃지 않는다 (공백 정규화 기준)
  (b) 심각도/분포 배지가 **마지막** 블록에 온다
  (c) [현상] 이 없는 옛 코멘트는 linkifyComment 결과와 **완전히 동일**하다 (폴백)
  (d) @[항목] 링크와 *[..] 서식 토큰이 살아 있다
  (e) HTML 이 이스케이프된다 (<img onerror> 가 태그로 남지 않는다)
  (f) signatureSelect 가 활성 룰만 내놓되 **이 행에 저장된 비활성/legacy 값은 남긴다**
  (g) sig_reason.js 가 파싱되고 조건행이 "판정 불가"(값 미저장)를 미충족과 구분해 그린다

Edge 가 없으면 정적 검사만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JS = _ROOT / "server" / "report" / "static" / "webreport"
_TMP = Path(tempfile.mkdtemp(prefix="wr_sheets_js_"))

_EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# 서버가 실제로 만드는 AI Comment 셀 문자열 모양 (web_report/ai_comment.py `_cell_text`
# + eval_engine recommend.make_comment). 배지 → 3섹션 순서와 공백까지 그대로 흉내낸다.
SAMPLE = ("[MAJOR][이봉] [현상] @[ItemA] 산포가 spec 폭 대비 넓습니다.\n"
          "[과거사례] 유사 lot 에서 *r[Trim] 재조정으로 개선. \n"
          " [점검제안] 설비 3호기 편차 확인")
LEGACY = "[MONITOR] 예전 형식 코멘트 — 섹션 토큰이 없다"


def edge_path():
    for p in _EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def js_literal(obj) -> str:
    """JSON 을 <script> 안에 안전하게 심는다 — `</` 만 끊어 조기 종료를 막는다."""
    return json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/")


def run_probe(harness_js: str, name: str) -> str:
    """core.js + sheets.js + sig_reason.js 를 인라인한 페이지를 돌리고 `<pre id=res>` 반환.

    stdout 은 **파일로** 리다이렉트한다 — 파이프로 받으면 Windows 에서 빈 출력이 온다.
    """
    scripts = "".join(
        f"<script>{(_JS / n).read_text(encoding='utf-8')}</script>"
        for n in ("core.js", "sheets.js", "sig_reason.js"))
    # ⚠ DATA/SESSION_ID 를 스크립트보다 **먼저 선언하면 안 된다** — core.js 가 둘 다
    # let/const 로 스스로 선언해, 하네스의 var 선선언과 충돌하면 core.js 스크립트
    # 전체가 SyntaxError 로 죽는다(esc 등 미정의 → <pre id=res> 미생성 → 전 항목이
    # "파싱 오류 의심"으로 위장 실패). 로드 **뒤에 대입**만 한다.
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body>"
            + scripts
            + "<script>DATA={web_report:{}};</script>"
            + harness_js + "</body></html>")
    page = _TMP / f"{name}.html"
    page.write_text(html, encoding="utf-8")
    dump = _TMP / f"{name}.dom.txt"
    # msedge 는 python subprocess 의 파일 stdout 으로는 **아무것도 쓰지 않는다**(파이프도
    # 마찬가지 — 실측 0 bytes). PowerShell Start-Process -RedirectStandardOutput 만 동작한다.
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
    for name in ("sheets.js", "sig_reason.js"):
        src = (_JS / name).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(import|export)\s", src, re.M), f"{name}: ES module 금지"
    print("[정적] classic script 유지 OK")


def test_script_registered():
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert "static/webreport/sig_reason.js" in view, \
        "sig_reason.js 가 report_view.html 에 로드되지 않았습니다"
    # 팝업이 issuePanelOf 를 쓰므로 yield_issue.js 뒤여야 한다.
    assert view.index("yield_issue.js") < view.index("sig_reason.js"), \
        "sig_reason.js 는 yield_issue.js 뒤에 로드돼야 합니다"
    print("[정적] sig_reason.js 로드 순서 OK")


# ── (a)~(e) AI Comment 렌더 ──────────────────────────────────────────────────

def test_ai_comment_render():
    harness = (
        "<script>(function(){var out={};"
        "var SAMPLE=" + js_literal(SAMPLE) + ", LEGACY=" + js_literal(LEGACY) + ";"
        "function norm(s){return String(s).replace(/\\s+/g,'');}"
        "var box=document.createElement('div');"
        "try{ box.innerHTML=renderAiComment(SAMPLE); out.render='OK'; }"
        "catch(e){ out.render='FAIL '+e.message; }"
        # (a) 무손실 — 기준은 원문이 아니라 **종전 렌더(linkifyComment)의 표시 텍스트**다.
        # @[..]→@.. / *r[..]→본문 변환은 종전부터 의도된 표시 규약이라 원문과 직접
        # 비교하면 영원히 실패한다. renderAiComment 는 선두 배지를 맨 끝으로 옮기므로
        # 기준 텍스트도 같은 재배열 후 비교한다(공백 무시).
        # 섹션 **라벨**도 같은 성격의 표시 규약이다(AIC_SEC_LABEL — 화면은 [사례]/[제안]
        # 으로 짧게 찍고 서버 문자열·파싱 키는 [과거사례]/[점검제안] 그대로). 기준 텍스트에
        # 같은 치환을 적용해 **본문** 글자 손실만 잡는다.
        "var ref=document.createElement('div');ref.innerHTML=linkifyComment(SAMPLE);"
        "var rt=ref.textContent;"
        "Object.keys(AIC_SEC_LABEL).forEach(function(k){"
        "  rt=rt.split('['+k+']').join('['+AIC_SEC_LABEL[k]+']'); });"
        "var mm=rt.match(/^\\s*((?:\\[[^\\]\\s]{1,12}\\])+)\\s*([\\s\\S]*)$/);"
        "var expected=mm?mm[2]+mm[1]:rt;"
        "out.lossless = norm(box.textContent)===norm(expected);"
        "out.expected = expected;"
        "out.text = box.textContent;"
        # (b) 마지막 블록이 배지
        "var last=box.lastElementChild;"
        "out.lastIsBadges = !!last && last.className==='aic-badges';"
        "out.badgeText = last ? last.textContent : '';"
        "out.secOrder = [].map.call(box.querySelectorAll('.aic-sec'),"
        "  function(d){return d.className.replace('aic-sec ','');}).join(',');"
        # (d) 링크·서식 토큰 보존
        "out.link = box.querySelectorAll('.item-detail-link[data-subject=\"ItemA\"]').length;"
        "out.fmt = box.querySelectorAll('.cmt-red').length;"
        # (c) 폴백 — 섹션 토큰 없으면 linkifyComment 와 완전 동일
        "out.fallback = renderAiComment(LEGACY)===linkifyComment(LEGACY);"
        # (e) XSS
        "var bad=document.createElement('div');"
        "bad.innerHTML=renderAiComment('[MAJOR] [현상] <img src=x onerror=alert(1)> 끝');"
        "out.xssImg = bad.querySelectorAll('img').length;"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);"
        "})();</script>")
    r = json.loads(run_probe(harness, "ai_comment"))
    assert r["render"] == "OK", r
    assert r["lossless"], f"AI Comment 파싱이 글자를 잃었습니다:\n원문={SAMPLE!r}\n출력={r['text']!r}"
    assert r["lastIsBadges"], f"배지가 마지막 줄이 아닙니다: {r}"
    assert "[MAJOR]" in r["badgeText"] and "[이봉]" in r["badgeText"], r["badgeText"]
    assert r["secOrder"] == "aic-sym,aic-past,aic-act", r["secOrder"]
    assert r["link"] == 1, f"@[항목] 링크가 사라졌습니다: {r}"
    assert r["fmt"] == 1, f"*r[..] 서식 토큰이 사라졌습니다: {r}"
    assert r["fallback"], "섹션 토큰 없는 옛 코멘트가 종전과 다르게 그려집니다"
    assert r["xssImg"] == 0, "HTML 이 이스케이프되지 않았습니다"
    print("[a~e] AI Comment 무손실·배지 위치·폴백·링크·XSS OK")


# ── (f) Signature 드랍다운 ───────────────────────────────────────────────────

def test_signature_choices():
    opts = [{"id": "LOW_CPK", "enabled": True}, {"id": "SPOT_FAIL", "enabled": True},
            {"id": "WIDE_DISTRIBUTION", "enabled": False}, {"id": "UNKNOWN", "enabled": True}]
    harness = (
        "<script>(function(){var out={};"
        "DATA.web_report.signature_options=" + js_literal(opts) + ";"
        "function ids(html){var d=document.createElement('div');d.innerHTML=html;"
        "  return [].map.call(d.querySelectorAll('option'),function(o){return o.value;});}"
        "function sel(html){var d=document.createElement('div');d.innerHTML=html;"
        "  var o=d.querySelector('option[selected]');return o?o.value:'';}"
        "out.fresh = ids(signatureSelect('',0));"
        "out.disabledKept = ids(signatureSelect('WIDE_DISTRIBUTION',0));"
        "out.disabledSelected = sel(signatureSelect('WIDE_DISTRIBUTION',0));"
        "out.legacyKept = ids(signatureSelect('ZZZ_GONE',0));"
        "out.legacySelected = sel(signatureSelect('ZZZ_GONE',0));"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);"
        "})();</script>")
    r = json.loads(run_probe(harness, "sig_choices"))
    assert "WIDE_DISTRIBUTION" not in r["fresh"], \
        f"비활성 룰이 새 드랍다운에 남아 있습니다: {r['fresh']}"
    assert {"LOW_CPK", "SPOT_FAIL", "UNKNOWN"} <= set(r["fresh"]), r["fresh"]
    assert "WIDE_DISTRIBUTION" in r["disabledKept"] and r["disabledSelected"] == "WIDE_DISTRIBUTION", \
        f"저장된 비활성 값이 사라졌습니다 — 사용자 입력 유실: {r}"
    assert "ZZZ_GONE" in r["legacyKept"] and r["legacySelected"] == "ZZZ_GONE", \
        f"카탈로그 밖 legacy 값이 사라졌습니다: {r}"
    print("[f] 비활성 제외 + 저장값 보존 OK")


# ── (g) 근거 팝업 렌더 ───────────────────────────────────────────────────────

def test_sig_reason_render():
    data = {
        "key": "CPK|ItemA", "evidence_missing": None, "ingested_at": 1786437242,
        "warnings": ["업로드 이후 전처리가 적용되어 값이 다를 수 있습니다."],
        "rules": [{
            "id": "LOW_CPK", "enabled": True, "status_hint": "MAJOR", "issue_category": "CPK",
            "phenomenon_ko": "Cpk 가 기준 미만입니다.", "action_ko": "spec 재검토",
            "criterion": {"metric": "cpk", "op": "<", "threshold_key": "cpk_warn",
                          "threshold": 1.33},
            "special": None, "special_note": None, "fired": True, "role": "primary",
            "conditions": [
                {"metric": "cpk", "cond": "<cpk_warn", "op": "<", "actual": 0.37,
                 "ref_key": "cpk_warn", "ref_value": 1.33, "applies": True, "passed": True,
                 "exceedance": 0.72, "value_source": "raw_metrics"},
                {"metric": "fail_robust_z_max", "cond": ">z_warn", "op": ">", "actual": None,
                 "ref_key": "z_warn", "ref_value": 12, "applies": False, "passed": False,
                 "exceedance": None, "value_source": None},
            ]}]}
    harness = (
        "<script>(function(){var out={};"
        "var D=" + js_literal(data) + ";"
        "var box=document.createElement('div');"
        "try{ box.innerHTML=sigrBodyHtml(D); out.render='OK'; }"
        "catch(e){ out.render='FAIL '+e.message; }"
        "out.rules = box.querySelectorAll('.sigr-rule').length;"
        "out.warn = box.querySelectorAll('.sigr-warn').length;"
        "out.hit = box.querySelectorAll('.sigr-hit').length;"
        "out.na = box.querySelectorAll('.sigr-na').length;"
        "out.miss = box.querySelectorAll('.sigr-miss').length;"
        "out.text = box.textContent;"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);"
        "})();</script>")
    r = json.loads(run_probe(harness, "sig_reason"))
    assert r["render"] == "OK", r
    assert r["rules"] == 1 and r["warn"] == 1, r
    assert r["hit"] == 1, f"충족 조건이 표시되지 않았습니다: {r}"
    # 값이 저장되지 않은 조건을 "미충족" 으로 그리면 사용자가 정반대로 읽는다.
    assert r["na"] == 1 and r["miss"] == 0, \
        f"값 미저장 조건이 '미충족' 으로 그려졌습니다(오해 유발): {r}"
    assert "cpk_warn" in r["text"] and "1.33" in r["text"], r["text"]
    print("[g] 근거 팝업 렌더 + 판정불가 구분 OK")


def test_aic_past_expands_on_click():
    """[과거사례] 4줄 클램프의 펼침은 **클릭 토글**이어야 한다 — hover 로 되돌리지 말 것.

    hover 로 요소 높이를 바꾸면 마우스가 지나가기만 해도 sticky 헤더·좌측 고정열을 얹은
    Issue Table 전체가 리플로우된다. Honey 내장 브라우저(QtWebEngine)에서는 이것이
    "세션 화면에서 마우스를 움직일 때마다 화면이 심하게 깜빡인다" 는 신고로 나타난다
    (2026-08-20, docs/20 §4-1). 같은 계열 선례가 landing.html 에만 3곳 있다.
    """
    view = (_ROOT / "server" / "report" / "report_view.html").read_text(encoding="utf-8")
    assert ".aic-past:hover" not in view, \
        ".aic-past:hover 금지 — 펼침은 .aic-open 클릭 토글로 할 것 (표 전체 리플로우)"
    assert ".aic-past.aic-open" in view, ".aic-open 펼침 규칙이 사라졌습니다"
    edit = (_JS / "edit_mode.js").read_text(encoding="utf-8")
    assert 'closest(".aic-past")' in edit, \
        "edit_mode.js 의 클릭 위임이 사라져 [과거사례] 를 펼칠 수 없습니다"
    print("[정적] [과거사례] 펼침이 클릭 토글 OK")


def test_aic_parses_old_and_new_section_tokens():
    """서버가 내는 섹션 토큰을 바꿨으면 JS 는 **옛 토큰도 계속** 파싱해야 한다.

    2026-08-28 서버 문자열이 "[점검제안]" → "[제안]" 으로 바뀌었다. 그 평문은 payload 에
    그대로 굳어 디스크/응답 캐시에 남으므로, 바꾼 뒤에도 기존 세션은 계속 옛 토큰을 실어
    온다. JS 가 새 토큰만 알면 그 세션들은 섹션 분리가 통째로 풀려 한 덩어리 평문이 된다
    (에러가 아니라 "색이 사라짐"으로 보여 발견이 늦다). 캐시를 앞당겨 갈려고
    REPORT_SCHEMA_VERSION 을 올리면 전 세션 콜드 리빌드가 되므로, 옛 키는 캐시가 자연히
    빠질 때까지 남긴다. Excel·챗봇·eval export 도 같은 평문을 소비한다.
    """
    src = (_JS / "sheets.js").read_text(encoding="utf-8")
    assert '"과거사례": "aic-past"' in src, "파싱 키가 바뀌었습니다 (서버 문자열과 갈립니다)"
    for tok in ("현상", "과거사례", "점검제안", "제안"):
        assert f'"{tok}"' in src, f"AIC_SECTIONS 에 {tok} 토큰이 없습니다"
    assert "AIC_SEC_LABEL" in src, "표시 라벨 매핑(AIC_SEC_LABEL)이 사라졌습니다"
    # 정규식 교대는 왼쪽 우선 — 옛 토큰이 신 토큰보다 앞에 와야 "[점검" 이 새지 않는다.
    m = re.search(r"\[\(현상\|과거사례\|([^)]*)\)\\\]", src)
    assert m, "섹션 정규식을 찾지 못했습니다"
    assert m.group(1).index("점검제안") < m.group(1).index("제안"), \
        "정규식에서 '점검제안' 이 '제안' 보다 뒤에 있으면 옛 코멘트에 '[점검' 이 본문으로 샙니다"
    # 서버 원문은 새 토큰 — 옛 토큰은 캐시에만 남는다.
    rec = (_ROOT / "eval_analyzer" / "eval_engine" / "pipeline" / "recommend.py"
           ).read_text(encoding="utf-8")
    assert "[과거사례]" in rec and "[제안]" in rec, \
        "서버 생성 문자열이 예상과 다릅니다 — JS 파싱 키와 짝을 다시 맞추세요"
    print("[정적] 섹션 토큰 옛/새 동시 파싱 OK")


def test_aic_clamp_affordance_and_drag():
    """(h) 펼침이 **잘렸을 때만** 안내되고, 드래그 복사는 토글하지 않는다.

    회귀 배경(2026-08-27 신고 "과거사례 링크가 가끔은 뜨고 가끔은 안 뜬다"):
      ① 4줄 이하인데 cursor:pointer 라 눌러도 아무 변화가 없었다 → .aic-clamped 로 분리.
      ② 토글 가드가 `getSelection()` 잔여 선택만 봐서, **직전에 다른 곳에서 만든 선택**이
         남아 있어도 조용히 무시됐다 → 이번 클릭의 mousedown→click 이동거리로 판정.
    """
    long_txt = "유사 lot 에서 Trim 재조정으로 개선된 사례가 있습니다. " * 12
    harness = (
        "<style>.aic-past{display:-webkit-box;-webkit-box-orient:vertical;"
        "-webkit-line-clamp:4;overflow:hidden;width:260px;font:13px/18px sans-serif;}"
        ".aic-past.aic-clamped{cursor:pointer;}"
        ".aic-past.aic-open{-webkit-line-clamp:none;}</style>"
        f'<div class="aic-past" id="lg">{long_txt}</div>'
        '<div class="aic-past" id="sh">유사 사례가 확인 되었습니다.</div>'
        "<script>(function(){var out={};"
        "markAicClamped(document);"
        "var lg=document.getElementById('lg'), sh=document.getElementById('sh');"
        "out.longClamped  = lg.classList.contains('aic-clamped');"
        "out.shortClamped = sh.classList.contains('aic-clamped');"
        # 드래그 판정 — edit_mode.js 의 aicDragged 와 같은 식(그 파일은 전역 의존이 많아
        # 여기서 로드하지 않는다). 4px 임계.
        "function dragged(dx,dy){return (dx*dx+dy*dy) > 16;}"
        # 손떨림(1px)은 클릭, 30px 이동은 드래그로 판정돼야 한다.
        "out.clickNotDrag = !dragged(1,1);"
        "out.dragIsDrag   = dragged(30,0);"
        # 잔여 선택이 있어도 클릭이면 펼쳐져야 한다 (종전 회귀 지점)
        "var r=document.createRange(); r.setStart(lg.firstChild,0); r.setEnd(lg.firstChild,4);"
        "var s=window.getSelection(); s.removeAllRanges(); s.addRange(r);"
        "if(!dragged(1,1)) lg.classList.toggle('aic-open');"
        "out.openedDespiteSelection = lg.classList.contains('aic-open');"
        "var pre=document.createElement('pre');pre.id='res';"
        "pre.textContent=JSON.stringify(out);document.body.appendChild(pre);"
        "})();</script>")
    r = json.loads(run_probe(harness, "aic_clamp"))
    assert r["longClamped"], "4줄 넘는 [과거사례] 에 .aic-clamped 가 안 붙었습니다"
    assert not r["shortClamped"], \
        "짧은 [과거사례] 에 펼침 커서가 붙었습니다 — 눌러도 변화가 없어 '안 먹는다'로 보입니다"
    assert r["clickNotDrag"], "손떨림(1px)이 드래그로 오판됩니다"
    assert r["dragIsDrag"], "실제 드래그가 클릭으로 오판돼 복사 중 글이 접힙니다"
    assert r["openedDespiteSelection"], \
        "잔여 선택 때문에 펼침이 무시됩니다 — 2026-08-27 신고의 회귀 지점"
    print("[h] [과거사례] 클램프 어포던스 + 드래그 판정 OK")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    test_no_es_module()
    test_script_registered()
    test_aic_past_expands_on_click()
    test_aic_label_is_display_only()
    if edge_path() is None:
        print(f"[a~h] SKIP — headless Edge 를 찾지 못했습니다 (찾은 경로: {_EDGE_CANDIDATES})")
        print("\n부분 통과 (정적 검사만)")
        return
    test_ai_comment_render()
    test_signature_choices()
    test_sig_reason_render()
    test_aic_clamp_affordance_and_drag()
    print("\n전부 통과")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
