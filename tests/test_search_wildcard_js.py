"""탭 검색어 `%` 와일드카드 회귀 — core.js searchTerms/searchMatch/searchMatchAny.

실행:
    server\\.venv\\Scripts\\python.exe tests\\test_search_wildcard_js.py

**왜 이 파일이 생겼나** (2026-08-28): web_report 탭 안의 검색칸(Issue Table·Yield·CPK·
Distribution·Trim·STDF·Raw Data·ETC 등 14곳)이 각자 `String(x).toLowerCase().includes(q)`
를 복붙해 쓰고 있었다. `%` 와일드카드(`BB%ABC` = BB 뒤 어딘가에 ABC)를 넣으면서 매칭
로직을 core.js 한 곳으로 모았다.

이 코드는 눈으로 보면 단순한데 실제로는 조용히 틀리는 함정이 있다:

  (1) **필드 이어붙이기** — CPK 의 subject|source 나 Trim 의 name|group|normalized 를
      한 문자열로 이어 붙이고 매칭하면 `A%B` 가 subject 의 A 와 source 의 B 로 **갈라져**
      매칭된다. 사용자가 의도하지 않은 행이 남고, 에러가 아니라 "이상한 결과"로 나온다.
  (2) **조각 겹침** — `AA%AA` 를 각 조각 독립 indexOf 로 보면 "AAB"(AA 1개)가 걸린다.
      두 번째 조각은 첫 조각이 **끝난 지점부터** 찾아야 한다.
  (3) **기존 동작 보존** — `%` 가 없는 검색어는 종전 부분일치와 100% 같아야 한다.
      운영 중인 서버라 기존 사용자의 검색이 달라지면 안 된다.
  (4) **정규식 미사용** — 항목명에 `.`·`(`·`[`·`+`·`*` 가 흔해 이스케이프 실수 하나로
      오매칭이 난다. 순차 indexOf 구현을 소스로 고정한다.

검증하는 것:
  (a) `%` 없는 검색어 = 종전 부분일치 (대소문자 무시·trim 포함)
  (b) `BB%ABC` 가 순서를 지키고, 순서가 뒤집힌 문자열은 안 걸린다
  (c) 다중 `%`·양끝 `%`·연속 `%%` 가 모두 허용되고 빈 조각은 무시된다
  (d) 조각이 겹치지 않는다 (`AA%AA` vs "AAB")
  (e) searchMatchAny 가 필드를 **각각** 본다 (이어붙이기 금지)
  (f) 빈 검색어 = 필터 없음 (전체 통과)
  (g) 정규식·이어붙이기 패턴이 소스에 없다 (정적 검사)
  (h) 소비자 14곳이 실제로 헬퍼를 쓴다 (복붙 잔재 검출)

Edge 가 없으면 정적 검사만 하고 나머지는 SKIP 한다(이 저장소에는 node 가 없다).
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_cpk_len8_js import _JS, edge_path, run_probe  # noqa: E402

# (검색어, 대상문자열, 기대) — 하나의 문자열을 대상으로 하는 searchMatch 케이스.
MATCH_CASES = [
    # ── (a) `%` 없음 = 종전 부분일치 ────────────────────────────────
    ("BB", "XXBBYY", True),
    ("BB", "XXBYY", False),
    ("bb", "XXBBYY", True),          # 대소문자 무시
    ("BB", "xxbbyy", True),
    ("  BB  ", "XXBBYY", True),      # 앞뒤 공백 trim
    ("A B", "xxA Byy", True),        # 중간 공백은 리터럴

    # ── (b) `BB%ABC` 순서 보장 ──────────────────────────────────────
    ("BB%ABC", "XXBBYYABCZZ", True),
    ("BB%ABC", "BBABC", True),        # 사이 0글자도 허용
    ("BB%ABC", "BB_1_ABC_2", True),
    ("BB%ABC", "ABCxxBB", False),     # 순서 역전
    ("BB%ABC", "BBAB", False),        # 두 번째 조각 없음
    ("BB%ABC", "ABC", False),         # 첫 조각 없음
    ("bb%abc", "XXBBYYABCZZ", True),  # 대소문자 무시

    # ── (c) 다중 % · 양끝 % · 연속 %% ───────────────────────────────
    ("A%B%C", "1A2B3C4", True),
    ("A%B%C", "1A2C3B4", False),      # C 가 B 보다 앞
    ("%ABC", "xxABCyy", True),        # 앞 % = 빈 조각 무시
    ("ABC%", "xxABCyy", True),        # 뒤 % = 빈 조각 무시
    ("%ABC%", "xxABCyy", True),
    ("A%%B", "1A2B3", True),          # 연속 %% = 빈 조각 무시
    ("%", "아무거나", True),           # 조각이 하나도 없다 = 필터 없음
    ("%%%", "아무거나", True),

    # ── (d) 조각 겹침 금지 ──────────────────────────────────────────
    ("AA%AA", "AAB", False),          # AA 가 1개뿐 — 겹쳐 세면 오매칭
    ("AA%AA", "AABAA", True),
    ("AA%AA", "AAAA", True),          # 인접 2회는 OK
    ("AA%AA", "AAA", False),          # 3글자로는 2회가 안 나온다

    # ── (f) 빈 검색어 = 전체 통과 ───────────────────────────────────
    ("", "무엇이든", True),
    ("   ", "무엇이든", True),

    # ── 실사용에 가까운 항목명 (정규식 메타문자 포함) ────────────────
    ("VDD%(V)", "VDD_CORE_MEAS(V)", True),
    ("IDD%[uA]", "IDD_STBY[uA]", True),
    (".", "A.B", True),               # 정규식이면 아무거나 매칭될 문자
    (".", "AB", False),               # 리터럴이므로 안 걸려야 한다
    ("A.C", "ABC", False),
    ("A.C", "A.C", True),
    ("A+B", "A+B", True),
    ("A+B", "AAB", False),
    ("(X)", "F(X)", True),
    ("[1]", "ARR[1]", True),
    ("A%.%B", "A_._B", True),
]

# (검색어, 필드목록, 기대) — searchMatchAny (필드를 각각 본다).
ANY_CASES = [
    ("A", ["ABC", "ZZZ"], True),
    ("A", ["XXX", "YAY"], True),
    ("A", ["XXX", "YYY"], False),
    # (e) 핵심: 필드를 이어붙이면 True 가 되는 케이스 — 반드시 False 여야 한다.
    ("ITEM%SRC", ["ITEM_A", "SRC_1"], False),
    ("ITEM%A", ["ITEM_A", "SRC_1"], True),      # 한 필드 안에서 순서 성립
    ("SRC%1", ["ITEM_A", "SRC_1"], True),
    ("", ["아무거나"], True),
    ("A", [], False),                            # 필드가 없으면 못 건다
    ("", [], True),                              # 단 빈 검색어는 필터 없음
]

# (검색어, 기대 조각) — searchTerms
TERMS_CASES = [
    ("BB%ABC", ["bb", "abc"]),
    ("  BB % ABC  ", ["bb", "abc"]),   # 조각별 trim
    ("%ABC", ["abc"]),
    ("ABC%", ["abc"]),
    ("A%%B", ["a", "b"]),
    ("", []),
    ("%", []),
    ("   ", []),
    ("ABC", ["abc"]),
]


# ── 정적 검사 (Edge 없이도 돈다) ─────────────────────────────────────────────

def test_helpers_defined():
    """헬퍼 3개는 core.js 에 있어야 한다 — core.js 가 첫 로드라 전 모듈이 쓸 수 있다."""
    src = (_JS / "core.js").read_text(encoding="utf-8")
    for fn in ("function searchTerms(", "function searchMatch(", "function searchMatchAny("):
        assert fn in src, f"core.js 에 {fn} 정의가 없습니다"


def test_no_regexp_matching():
    """정규식으로 만들지 않는다 — 항목명의 . ( ) [ ] + * 가 오매칭을 부른다."""
    src = (_JS / "core.js").read_text(encoding="utf-8")
    body = src[src.index("function searchTerms("):src.index("// ── 콜드 202")]
    for token in ("RegExp", "new RegExp", "/.*/"):
        assert token not in body, (
            f"검색 헬퍼가 정규식({token})을 쓰고 있습니다 — 항목명에 흔한 "
            ". ( ) [ ] + * 가 이스케이프 없이 메타문자로 해석돼 조용히 오매칭됩니다.")


def test_consumers_use_helper():
    """검색칸을 가진 모듈이 실제로 헬퍼를 쓰는지 — 복붙 잔재(includes)를 검출한다.

    한 파일이라도 옛 `.toLowerCase().includes(term)` 로 남으면 그 탭만 `%` 가 안 먹는데,
    에러가 아니라 "그 탭에서만 검색이 이상함"으로 나타나 발견이 늦다.
    """
    for name in ("yield_issue.js", "cpk.js", "distribution.js", "stdf_map.js",
                 "raw_data.js", "edit_mode.js", "trim.js"):
        src = (_JS / name).read_text(encoding="utf-8")
        assert "searchTerms(" in src, f"{name}: searchTerms 를 쓰지 않습니다"


def test_no_field_concat_in_trim_palette():
    """Trim 팔레트 data-name 은 필드를 U+001F 로 구분한다 — 공백 연결이면 (1) 이 재발한다."""
    src = (_JS / "trim.js").read_text(encoding="utf-8")
    assert "\\u001f" in src, (
        "trim.js 팔레트 검색키가 필드 구분자(U+001F)를 쓰지 않습니다 — "
        "필드를 이어붙이면 'A%B' 가 name 의 A 와 group 의 B 로 갈라져 매칭됩니다.")


def test_mention_keeps_literal_percent():
    """`@` 멘션(셀 편집 중 항목 찾기)은 `%` 를 **리터럴로** 둔다 — 검색칸이 아니라
    코멘트 본문을 치는 자리라 "수율 50%" 처럼 % 가 그냥 문자로 온다(2026-08-28 결정).
    나중에 "여기만 빠뜨렸네" 하고 헬퍼로 바꾸지 않도록 사유와 함께 고정한다."""
    src = (_JS / "edit_mode.js").read_text(encoding="utf-8")
    body = src[src.index("function showMention("):]
    body = body[:body.index("\nfunction ", 1)]
    assert "searchMatch(" not in body and "searchTerms(" not in body, (
        "showMention 이 `%` 와일드카드를 쓰고 있습니다 — 이 자리는 코멘트 본문 입력이라 "
        "% 가 리터럴이어야 합니다(core.js 헬퍼를 의도적으로 쓰지 않는 유일한 곳).")


def test_no_es_module():
    """분할 JS 는 classic script 순서 로드다 — import/export 를 쓰면 전부 죽는다."""
    src = (_JS / "core.js").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|export)\s", src, re.M), "core.js: ES module 금지"


# ── 브라우저 검사 ────────────────────────────────────────────────────────────

def test_search_match():
    """searchMatch: 순서 보장 · 조각 겹침 금지 · 정규식 메타문자 리터럴 취급."""
    harness = """<pre id="res"></pre><script>
    const CASES = %s;
    const out = CASES.map(([q, text]) => {
      try { return searchMatch(text, searchTerms(q)); }
      catch (e) { return "ERR:" + String(e); }
    });
    document.getElementById("res").textContent = JSON.stringify(out);
    </script>""" % json.dumps([[q, t] for q, t, _ in MATCH_CASES], ensure_ascii=False)
    got = json.loads(run_probe(harness, "search_match"))
    bad = [(q, t, exp, g) for (q, t, exp), g in zip(MATCH_CASES, got) if g != exp]
    assert not bad, "searchMatch 불일치:\n" + "\n".join(
        f"  q={q!r} text={t!r} 기대={exp} 실제={g}" for q, t, exp, g in bad)


def test_search_match_any():
    """searchMatchAny: 필드를 **각각** 본다 (이어붙이면 ITEM%SRC 가 오매칭)."""
    harness = """<pre id="res"></pre><script>
    const CASES = %s;
    const out = CASES.map(([q, fields]) => {
      try { return searchMatchAny(fields, searchTerms(q)); }
      catch (e) { return "ERR:" + String(e); }
    });
    document.getElementById("res").textContent = JSON.stringify(out);
    </script>""" % json.dumps([[q, f] for q, f, _ in ANY_CASES], ensure_ascii=False)
    got = json.loads(run_probe(harness, "search_match_any"))
    bad = [(q, f, exp, g) for (q, f, exp), g in zip(ANY_CASES, got) if g != exp]
    assert not bad, "searchMatchAny 불일치:\n" + "\n".join(
        f"  q={q!r} fields={f!r} 기대={exp} 실제={g}" for q, f, exp, g in bad)


def test_search_terms():
    """searchTerms: 소문자화 · 조각별 trim · 빈 조각 제거."""
    harness = """<pre id="res"></pre><script>
    const CASES = %s;
    const out = CASES.map(q => {
      try { return searchTerms(q); } catch (e) { return "ERR:" + String(e); }
    });
    document.getElementById("res").textContent = JSON.stringify(out);
    </script>""" % json.dumps([q for q, _ in TERMS_CASES], ensure_ascii=False)
    got = json.loads(run_probe(harness, "search_terms"))
    bad = [(q, exp, g) for (q, exp), g in zip(TERMS_CASES, got) if g != exp]
    assert not bad, "searchTerms 불일치:\n" + "\n".join(
        f"  q={q!r} 기대={exp} 실제={g}" for q, exp, g in bad)


def test_null_safe():
    """null/undefined 입력에 던지지 않는다 — 검색칸이 아직 없는 렌더 시점에 불린다."""
    harness = """<pre id="res"></pre><script>
    const out = [];
    try {
      out.push(searchTerms(null), searchTerms(undefined));
      out.push(searchMatch(null, ["a"]), searchMatch(undefined, ["a"]));
      out.push(searchMatch("abc", null), searchMatch("abc", undefined));
      out.push(searchMatchAny(null, ["a"]), searchMatchAny(["abc"], null));
    } catch (e) { out.push("ERR:" + String(e)); }
    document.getElementById("res").textContent = JSON.stringify(out);
    </script>"""
    got = json.loads(run_probe(harness, "search_null"))
    assert got == [[], [], False, False, True, True, False, True], got


def test_distribution_wildcard_end_to_end():
    """실소비자 1곳(distSuggestions)이 `%` 를 실제로 태우는지 — 헬퍼만 맞고 호출부가
    옛 코드로 남는 회귀를 잡는다."""
    # ⚠ distIndex 를 var 로 **선선언하지 않는다** — distribution.js 가 `let distIndex`
    #   로 스스로 선언해, 하네스가 미리 선언하면 그 스크립트 전체가 SyntaxError 로
    #   죽는다(core.js DATA/SESSION_ID 와 같은 함정 — test_cpk_len8_js run_probe 주석).
    harness = """<pre id="res"></pre><script>
    distIndex = [
      {subject: "VDD_CORE_MEAS(V)", test_num: "1"},
      {subject: "VDD_IO_MEAS(V)",   test_num: "2"},
      {subject: "IDD_STBY[uA]",     test_num: "3"},
      {subject: "MEAS_VDD_CORE",    test_num: "4"}
    ];
    const pick = q => distSuggestions(q, 0).map(r => r.subject);
    document.getElementById("res").textContent = JSON.stringify({
      plain:    pick("VDD"),
      wild:     pick("VDD%MEAS"),
      ordered:  pick("MEAS%VDD"),
      noMatch:  pick("VDD%NOPE"),
      empty:    pick("")
    });
    </script>"""
    got = json.loads(run_probe(harness, "search_dist", extra_js=("distribution.js",)))
    assert got["plain"] == ["VDD_CORE_MEAS(V)", "VDD_IO_MEAS(V)", "MEAS_VDD_CORE"], got
    # VDD 뒤에 MEAS 가 오는 것만 — MEAS_VDD_CORE 는 순서가 반대라 빠진다.
    assert got["wild"] == ["VDD_CORE_MEAS(V)", "VDD_IO_MEAS(V)"], got
    assert got["ordered"] == ["MEAS_VDD_CORE"], got
    assert got["noMatch"] == [], got
    assert got["empty"] == [], got          # 빈 검색어는 종전대로 후보 없음


def test_issue_row_dom_wildcard():
    """sheetRowMatches: Issue Table 은 **렌더된 DOM 행 텍스트**를 훑는 유일한 경로다.

    다른 소비자는 데이터 배열을 거르지만 여기만 실제 `<tr>` 을 본다 — Item 셀 + comment
    셀(data-raw 우선)을 한 문자열로 이어 붙이므로, `%` 가 Item 과 comment 에 걸쳐
    매칭되는 것이 **의도된 동작**이다(행 전체가 하나의 검색 대상).
    """
    harness = """<pre id="res"></pre><script>
    // stripCommentFormat 은 sheets.js 소관이라 하네스에서 최소 대역(원문 통과)만 둔다 —
    // 이 테스트가 보려는 것은 서식 제거가 아니라 `%` 조각의 순서 매칭이다.
    function stripCommentFormat(s) { return String(s == null ? "" : s); }
    const mk = (item, comment, raw) => {
      const tr = document.createElement("tr");
      for (let i = 0; i < 3; i++) tr.appendChild(document.createElement("td"));
      const it = document.createElement("td");
      it.setAttribute("data-col", "Item");
      it.textContent = item;
      tr.appendChild(it);
      const cm = document.createElement("td");
      cm.className = "st-comment";
      cm.textContent = comment;
      if (raw != null) cm.dataset.raw = raw;
      tr.appendChild(cm);
      return tr;
    };
    const hit = (q, item, comment, raw) => sheetRowMatches(mk(item, comment, raw), searchTerms(q));
    document.getElementById("res").textContent = JSON.stringify({
      plain:      hit("VDD",          "VDD_CORE(V)", "",        null),
      wild:       hit("VDD%CORE",     "VDD_X_CORE",  "",        null),
      wildRev:    hit("CORE%VDD",     "VDD_X_CORE",  "",        null),
      inComment:  hit("재측정",        "VDD_CORE",    "재측정 필요", null),
      acrossCell: hit("VDD%재측정",    "VDD_CORE",    "재측정 필요", null),
      rawWins:    hit("원문",          "VDD_CORE",    "치환된표시",  "원문코멘트"),
      rawWild:    hit("원문%코멘트",    "VDD_CORE",    "치환된표시",  "원문코멘트"),
      noHit:      hit("VDD%NOPE",     "VDD_CORE",    "재측정 필요", null),
      emptyAll:   hit("",             "VDD_CORE",    "",        null)
    });
    </script>"""
    got = json.loads(run_probe(harness, "search_issue_row",
                               extra_js=("yield_issue.js",)))
    assert got["plain"] is True, got
    assert got["wild"] is True, got
    assert got["wildRev"] is False, got          # 순서 역전은 안 걸린다
    assert got["inComment"] is True, got         # comment 도 검색 대상
    assert got["acrossCell"] is True, got        # 행 전체가 한 대상 (의도된 동작)
    assert got["rawWins"] is True, got           # data-raw 우선
    assert got["rawWild"] is True, got
    assert got["noHit"] is False, got
    assert got["emptyAll"] is True, got          # 빈 검색어 = 전 행 유지


def main() -> int:
    static = [test_helpers_defined, test_no_regexp_matching, test_consumers_use_helper,
              test_no_field_concat_in_trim_palette, test_mention_keeps_literal_percent,
              test_no_es_module]
    browser = [test_search_terms, test_search_match, test_search_match_any,
               test_null_safe, test_distribution_wildcard_end_to_end,
               test_issue_row_dom_wildcard]
    failed = 0
    for fn in static:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    if not edge_path():
        print("  SKIP 브라우저 검사 — Edge 를 찾지 못했습니다")
    else:
        for fn in browser:
            try:
                fn()
                print(f"  ok   {fn.__name__}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL {fn.__name__}: {e}")
    print("FAILED" if failed else "PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
