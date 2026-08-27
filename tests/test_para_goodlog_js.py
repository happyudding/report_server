"""Para Conversion goodlog 렌더 회귀 (2026-08-27) — 실제 브라우저 실행 검증.

실행:
    python tests/test_para_goodlog_js.py

Para 는 goodlog 의 After Value 한 칸을 DUT 별 N칸으로 편다(서버 gl.para_duts 순서,
값은 row.after_values). 고정하는 계약:

  1. para_duts 가 있으면 After 블록 헤더 colspan 이 4+N, Value 헤더가 DUT 이름들이고
     각 행의 After Value 셀이 N개다(값은 after_values 순서 그대로).
  2. **para_duts 가 없으면 종전 15컬럼 그대로** — 헤더·셀 수·Value 값이 옛 렌더와 같다.
     (Normal Compare 세션이 이번 변경으로 달라지면 안 된다.)
  3. gl: 코멘트 키는 para 여부와 무관하게 불변 — `gl:<after>\\x1f<before>` (규칙 12).
     DUT 컬럼이 늘어도 기존 코멘트가 그대로 붙어야 한다.

Edge 가 없으면 스킵한다(정적 검사만 남는다). tests/ 관례대로 pytest 미사용.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_compare_issue_js import SEP, edge_path, js_literal, run_probe  # noqa: E402

_JS = (Path(__file__).resolve().parent.parent
       / "server" / "report" / "static" / "webreport" / "compare.js")


def _rows():
    """행 3종 — 공통 항목 / after 에만(추가) / before 에만(삭제)."""
    return [
        {"after_item_name": "ITEM_A", "after_lolimit": 0, "after_hilimit": 20,
         "after_unit": "V", "after_value": "5", "after_values": ["5", "7", "9"],
         "compare_item_name": True, "compare_lolimit": True, "compare_hilimit": True,
         "comment": "", "gap": 0.0,
         "before_item_name": "ITEM_A", "before_lolimit": 0, "before_hilimit": 20,
         "before_unit": "V", "before_value": "5"},
        {"after_item_name": "NEW_ONE", "after_lolimit": 0, "after_hilimit": 20,
         "after_unit": "V", "after_value": "1", "after_values": ["1", "2", ""],
         "compare_item_name": None, "compare_lolimit": None, "compare_hilimit": None,
         "comment": "", "gap": None,
         "before_item_name": "", "before_lolimit": None, "before_hilimit": None,
         "before_unit": "", "before_value": ""},
        {"after_item_name": "", "after_lolimit": None, "after_hilimit": None,
         "after_unit": "", "after_value": "", "after_values": ["", "", ""],
         "compare_item_name": None, "compare_lolimit": None, "compare_hilimit": None,
         "comment": "", "gap": None,
         "before_item_name": "GONE", "before_lolimit": 0, "before_hilimit": 20,
         "before_unit": "V", "before_value": "3"},
    ]


PARA_GL = {"after_source": "DUT1", "before_source": "Single", "identical": False,
           "rows": _rows(), "limit_change_map": {},
           "para_duts": ["DUT1", "DUT2", "DUT3"]}
# 같은 행이지만 para_duts 가 없다 = Normal Compare (after_values 는 서버가 안 보낸다).
NORMAL_GL = {"after_source": "WF_A", "before_source": "WF_B", "identical": False,
             "rows": [{k: v for k, v in r.items() if k != "after_values"}
                      for r in _rows()],
             "limit_change_map": {}}


def _probe(gl, name):
    harness = (
        "<script>(function(){var out={};"
        "DATA = {compare_notes:{}, web_report:{compare:{}}};"
        "MODE = 'view';"
        "document.body.insertAdjacentHTML('beforeend',"
        "  '<div id=\"gl\"><table class=\"compare-table\">'"
        "  + goodlogSectionHtml(" + js_literal(gl) + ").replace(/^[\\s\\S]*?<table[^>]*>/, '')"
        "  .replace(/<\\/table>[\\s\\S]*$/, '') + '</table></div>');"
        "var root = document.getElementById('gl');"
        "var heads = root.querySelectorAll('thead tr');"
        "out.topSpans = [].map.call(heads[0].querySelectorAll('th'),"
        "  function(th){return th.getAttribute('colspan') || '1';});"
        "out.topText = [].map.call(heads[0].querySelectorAll('th'),"
        "  function(th){return th.textContent.trim();});"
        "out.subHead = [].map.call(heads[1].querySelectorAll('th'),"
        "  function(th){return th.textContent.trim();});"
        "out.cols = root.querySelectorAll('colgroup col').length;"
        "var rows = root.querySelectorAll('tr.gl-row');"
        "out.cells = [].map.call(rows, function(tr){return tr.children.length;});"
        "out.rowText = [].map.call(rows, function(tr){"
        "  return [].map.call(tr.children, function(td){return td.textContent.trim();});});"
        "out.noteKeys = [].map.call(root.querySelectorAll('td.cmp-note-cell'),"
        "  function(td){return td.dataset.noteKey;});"
        "_emit(out);})();</script>")
    return json.loads(run_probe(
        ["core.js", "sheets.js", "compare.js"], "", harness, name))


def test_para_columns():
    """(1) DUT N개면 After 블록이 4+N, Value 헤더가 DUT 이름, 셀도 N개."""
    out = _probe(PARA_GL, "para_goodlog")
    n = len(PARA_GL["para_duts"])

    assert out["cols"] == 14 + n, f"colgroup {out['cols']} != {14 + n}"
    # 상단 헤더 = [Before(5), Compare(3), Comment, Gap%, After(4+N)]
    assert out["topSpans"] == ["5", "3", "1", "1", str(4 + n)], out["topSpans"]
    # 하위 헤더 마지막 N칸이 DUT 이름.
    assert out["subHead"][-n:] == PARA_GL["para_duts"], out["subHead"]
    assert all(c == 14 + n for c in out["cells"]), out["cells"]

    # 값이 after_values 순서 그대로 마지막 N칸에 들어간다.
    assert out["rowText"][0][-n:] == ["5", "7", "9"], out["rowText"][0]
    assert out["rowText"][1][-n:] == ["1", "2", ""], out["rowText"][1]
    assert out["rowText"][2][-n:] == ["", "", ""], out["rowText"][2]
    print("OK  test_para_columns — After Value 가 DUT 별 %d칸" % n)


def test_normal_unchanged():
    """(2) para_duts 가 없으면 종전 15컬럼 그대로."""
    out = _probe(NORMAL_GL, "normal_goodlog")
    assert out["cols"] == 15, out["cols"]
    assert out["topSpans"] == ["5", "3", "1", "1", "5"], out["topSpans"]
    assert out["subHead"][-1] == "Value", out["subHead"]
    assert all(c == 15 for c in out["cells"]), out["cells"]
    # Value 는 종전대로 after_value 한 칸.
    assert out["rowText"][0][-1] == "5", out["rowText"][0]
    assert out["rowText"][2][-1] == "", out["rowText"][2]
    print("OK  test_normal_unchanged — 비para 렌더 15컬럼 유지")


def test_note_keys_unchanged():
    """(3) gl: 코멘트 키는 para 여부와 무관하게 같다 — 기존 코멘트 유실 방지(규칙 12)."""
    para = _probe(PARA_GL, "para_keys")
    normal = _probe(NORMAL_GL, "normal_keys")
    expect = [f"gl:ITEM_A{SEP}ITEM_A", f"gl:NEW_ONE{SEP}", f"gl:{SEP}GONE"]
    assert para["noteKeys"] == expect, para["noteKeys"]
    assert normal["noteKeys"] == expect, normal["noteKeys"]
    print("OK  test_note_keys_unchanged — gl: 키 불변")


def test_static_contract():
    """Edge 없이도 도는 소스 검사 — 하드코딩 15 가 되살아나지 않았는지."""
    src = _JS.read_text(encoding="utf-8")
    assert "const COLS = 15;" not in src, "goodlog COLS 가 다시 상수 15 로 고정됐습니다"
    assert "gl.para_duts" in src, "para_duts 분기가 없습니다"
    # 저장 키 규약 문자열은 그대로여야 한다.
    assert 'return "gl:" + (r.after_item_name || "") + CMP_NOTE_SEP' in src, \
        "glNoteKey 규약이 바뀌었습니다 (규칙 12 — 기존 코멘트 유실)"
    print("OK  test_static_contract")


if __name__ == "__main__":
    test_static_contract()
    if not edge_path():
        print("SKIP  Edge 없음 — 렌더 검증 생략")
        sys.exit(0)
    test_para_columns()
    test_normal_unchanged()
    test_note_keys_unchanged()
    print("\nALL PASS  tests/test_para_goodlog_js.py")
