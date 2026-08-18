"""excel_download/_extra.py 순수 빌더 검증 — Excel/네트워크 없이 단독 실행.

    python tests/test_excel_extra_builders.py

값 로직이 웹 화면(map_select.js / sheets.js / compare.js)의 정본과 같은지 확인한다.
기대값은 웹 코드를 손으로 따라간 결과이며, 갈리면 여기서 먼저 터진다.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "client")):
    if p not in sys.path:
        sys.path.insert(0, p)

from excel_download import _extra          # noqa: E402

_fails = []


def check(name, got, want):
    if got != want:
        _fails.append(f"{name}\n     got : {got!r}\n     want: {want!r}")
        print(f"  [FAIL] {name}")
    else:
        print(f"  [ok] {name}")


# ── Issue Status 집계 ────────────────────────────────────────────────────────
def test_issue_status():
    print("\n[Issue Status]")
    issue = [
        {"Category": "Yield", "Status": "Open"},
        {"Status": "Close"},                       # 섹션 유지 → Yield
        {"Status": ""},                            # 빈 Status = 비대상
        {"Category": "CPK", "Status": "Close"},
        {"Category": "ETC", "Status": "진행중"},    # Close 아니면 open
    ]
    temp = [{"Category": "TEMP", "Status": "Open"}, {"Status": "Close"}]

    check("Normal 모드 = TEMP 행 없음",
          _extra.build_issue_status_rows(issue, None, "Normal"),
          [["Yield", 1, 1, "50.0%"], ["CPK", 0, 1, "100.0%"], ["ETC", 1, 0, "0.0%"]])
    check("Temperature 모드 = Temp 시트 합산",
          _extra.build_issue_status_rows(issue, temp, "Temperature"),
          [["Yield", 1, 1, "50.0%"], ["CPK", 0, 1, "100.0%"],
           ["TEMP", 1, 1, "50.0%"], ["ETC", 1, 0, "0.0%"]])
    check("이슈 0건 카테고리는 '-'",
          _extra.build_issue_status_rows([], None, "Normal"),
          [["Yield", 0, 0, "-"], ["CPK", 0, 0, "-"], ["ETC", 0, 0, "-"]])
    check("빈 입력에도 예외 없음", _extra.build_issue_status_rows(None, None, None)[0][0], "Yield")


# ── Engr Comment 평문화 ──────────────────────────────────────────────────────
def test_engr_plain():
    print("\n[Engr Comment]")
    check("평문은 그대로", _extra.engr_plain("수율 저하 확인"), "수율 저하 확인")
    check("rich 태그 제거",
          _extra.engr_plain('<!--rich--><span style="color:#d92d20">빨강</span> 본문'),
          "빨강 본문")
    check("br/div = 개행",
          _extra.engr_plain("<!--rich-->첫줄<br>둘째<div>셋째</div>"),
          "첫줄\n둘째\n셋째")
    check("script 는 내용까지 폐기",
          _extra.engr_plain("<!--rich-->안전<script>alert(1)</script>끝"), "안전끝")
    check("링크 토큰은 원문 유지",
          _extra.engr_plain("담당 @[chumji.kim] 참고 #[태그1]"),
          "담당 @[chumji.kim] 참고 #[태그1]")
    check("*[..] 서식 토큰만 strip", _extra.engr_plain("*[굵게] 본문"), "굵게 본문")
    check("None → 빈 문자열", _extra.engr_plain(None), "")


# ── Yield 상단 요약 ──────────────────────────────────────────────────────────
def test_yield_overview():
    print("\n[Yield overview]")
    ys_single = {"yield_pct": 93.5, "pass": 935, "fail": 65, "total": 1000, "tested": 1000,
                 "by_source": [{"source": "A", "yield_pct": 93.5, "pass": 935, "total": 1000}],
                 "by_step": [{"step": "P2", "avg_yield_pct": 93.5, "sources": []}]}
    out = _extra.build_yield_overview(ys_single, {"basis": "test"})
    check("소스 1개 → by_source 생략", out["by_source"], None)
    check("STEP 1개 → by_step 생략", out["by_step"], None)
    check("전체 요약 행", out["overall"]["rows"], [["93.50", 935, 1000, 65]])
    check("Total 라벨(test 기준)", out["overall"]["header"][2], "Total")

    ys_multi = {
        "yield_pct": 90.0, "pass": 1800, "fail": 200, "total": 2000, "tested": 2000,
        "by_source": [{"source": "A", "yield_pct": 92.0, "pass": 920, "total": 1000},
                      {"source": "B", "yield_pct": 88.0, "pass": 880, "total": 1000}],
        "by_step": [
            {"step": "P1", "avg_yield_pct": 95.0,
             "sources": [{"source": "A", "yield_pct": 95.0, "survivor": 950,
                          "entered": 1000, "fail": 50, "cum_fail": 50}]},
            {"step": "P2", "avg_yield_pct": 90.0,
             "sources": [{"source": "A", "yield_pct": 92.0, "survivor": 920,
                          "entered": 1000, "fail": 30, "cum_fail": 80},
                         {"source": "B", "yield_pct": 88.0, "survivor": 880,
                          "entered": 1000, "fail": 40, "cum_fail": 120}]}],
    }
    basis = {"basis": "gross", "gross_die": 1000,
             "by_source": [{"source": "A", "basis": "gross", "total": 1000},
                           {"source": "B", "basis": "test", "total": 1000}]}
    out = _extra.build_yield_overview(ys_multi, basis)
    check("Gross Die 라벨", out["overall"]["header"][2], "Gross Die")
    check("분모 캡션", out["overall"]["caption"], "분모: Gross Die 1000 · 측정 die 2000")
    check("소스별 표 행", out["by_source"]["rows"],
          [["A", "92.00%", "920 / 1000", "Gross 1000"],
           ["B", "88.00%", "880 / 1000", "Test 1000"]])
    check("STEP 표 행수 (1 + 2)", len(out["by_step"]["rows"]), 3)
    check("STEP 첫 셀에 avg 병기", out["by_step"]["rows"][0][0], "P1\navg 95.00%")
    check("2행짜리 STEP 만 병합 대상", out["by_step"]["merges"], [(1, 2)])
    check("Fail = step / cum", out["by_step"]["rows"][2][4], "40 / 120")
    check("빈 payload 안전", _extra.build_yield_overview(None, None)["overall"], None)


# ── 그라데이션 색 ────────────────────────────────────────────────────────────
def test_gradient():
    print("\n[Gradient]")
    # 웹 CSS hsl(0,78%,94% − yw·36%) 를 손으로 환산한 값 (HSL→RGB 정의 그대로):
    #   yw=0 → L=0.94: C=0.0936, m=0.8932 → R=252, G=B=228 → FCE4E4
    #   yw=1 → L=0.58: C=0.6552, m=0.2524 → R=231, G=B=64  → E74040
    check("ratio 0 = 가장 옅은 빨강", _extra.grad_fill_rgb(0), "FCE4E4")
    check("ratio 1 = 가장 진한 빨강", _extra.grad_fill_rgb(1), "E74040")
    check("범위 밖은 클램프", _extra.grad_fill_rgb(5), _extra.grad_fill_rgb(1))
    check("숫자 아님도 안전", _extra.grad_fill_rgb("x"), _extra.grad_fill_rgb(0))
    check("컬럼 최대", _extra.column_max([1, 5, None, "x", 3]), 5.0)
    check("비율 = 값/최대", _extra.grad_ratio(2.5, 5.0), 0.5)
    check("0 이하는 미채색", _extra.grad_ratio(0, 5.0), None)
    check("최대 초과는 1로 클램프", _extra.grad_ratio(9, 5.0), 1.0)
    check("양자화 16단계", _extra.quantize_ratio(0.51), 0.5)
    check("아주 작은 값도 최소 1단계", _extra.quantize_ratio(0.001), 0.0625)


# ── Compare ─────────────────────────────────────────────────────────────────
def test_compare():
    print("\n[Compare]")
    compare = {
        "equivalence": {
            "before": "B", "after": "A", "thresholds": {"avg_pct": 5, "cpk": 5},
            "summary": {"total": 2, "grade1": 1, "grade2": 0, "grade3": 1},
            "rows": [
                {"step": "P2", "subject": "VDD", "units": "V", "hilim": 1.2, "lolim": 0.8,
                 "before": {"average": 1.0, "stdev": 0.01, "cpk": 6.0},
                 "after": {"average": 1.01, "stdev": 0.01, "cpk": 5.5},
                 "delta_avg": 0.01, "delta_pct": 1.0, "grade": 1},
                {"step": "P2", "subject": "IDD", "units": "A", "hilim": 2.0, "lolim": 0.0,
                 "before": {"average": 1.0, "stdev": 0.1, "cpk": 3.0},
                 "after": {"average": 1.5, "stdev": 0.2, "cpk": 1.2},
                 "delta_avg": 0.5, "delta_pct": 50.0, "grade": 3},
            ]},
        "dist_shift": {
            "before": "B", "after": "A",
            "thresholds": {"cpk_low": 1.33, "stdev_delta_pct": 30, "alpha": 0.05},
            "summary": {"total": 1, "focus": 1},
            "rows": [{"subject": "IDD", "units": "A",
                      "after": {"average": 1.5, "stdev": 0.2, "cpk": 1.2, "n": 100},
                      "before": {"average": 1.0, "stdev": 0.1, "cpk": 3.0, "n": 100},
                      "meanshift_sigma": 5.0, "cpk_ratio_pct": 40.0,
                      "stdev_delta_pct": 100.0, "median_shift": 3.0, "focus": True}]},
        "goodlog": {
            "after_source": "A", "before_source": "B", "identical": False,
            "header": _extra.GOODLOG_HEADER,
            "rows": [{"after_item_name": "VDD", "after_lolimit": 0.8, "after_hilimit": 1.2,
                      "after_unit": "V", "after_value": 1.01,
                      "compare_item_name": True, "compare_lolimit": False,
                      "compare_hilimit": True, "comment": "", "gap": 12.0,
                      "Before_item_name": "VDD", "Before_lolimit": 0.7,
                      "Before_hilimit": 1.2, "Before_unit": "V", "Before_value": 0.9}]},
    }
    tables = _extra.build_compare_tables(compare)

    eq = tables["equivalence"]
    check("동일성 컬럼 14개", len(eq["header"]), 14)
    check("Grade 표기", eq["rows"][1][13], "Grade3")
    check("AVG차(%) 임계 초과 강조", eq["marks"].get((1, 12)), "bad")
    check("Grade3 동일성 셀 강조", eq["marks"].get((1, 13)), "bad")
    check("정상 행은 강조 없음", eq["marks"].get((0, 12)), None)
    check("After CPK 임계(5) 이상은 무표시", eq["marks"].get((0, 10)), None)   # 5.5 ≥ 5
    check("After CPK 임계 미만 경고", eq["marks"].get((1, 10)), "warn")        # 1.2 < 5

    ds = tables["dist_shift"]
    check("산포 비교 행", ds["rows"][0][:2], ["IDD", "A"])
    check("Stdev 증가율 강조", ds["marks"].get((0, 10)), "bad")
    check("focus 표기", ds["rows"][0][12], "주목")

    gl = tables["goodlog"]
    check("goodlog 컬럼 순서 고정", gl["header"], _extra.GOODLOG_HEADER)
    check("True → O(초록)", (gl["rows"][0][5], gl["marks"].get((0, 5))), ("O", "good"))
    check("False → X(빨강)", (gl["rows"][0][6], gl["marks"].get((0, 6))), ("X", "bad"))
    check("gap 10% 이상 강조", gl["marks"].get((0, 9)), "bad")
    check("compare 없음 → 빈 dict", _extra.build_compare_tables(None), {})


# ── 전처리 안내 ─────────────────────────────────────────────────────────────
def test_preprocess():
    print("\n[Preprocess]")
    check("전처리 없음 → 빈 행", _extra.build_preprocess_rows({"spec": {}}), [])
    check("None 안전", _extra.build_preprocess_rows(None), [])
    rows = _extra.build_preprocess_rows({
        "summary": "항목 2개 제외 · outlier ±3σ 제거",
        "spec": {"exclude_items": ["A", "B"], "outlier": {"k": 3},
                 "edits": [{"row": 1}], "rules": []},
        "yield_basis": "gross", "gross_die": 5000})
    labels = [r[0] for r in rows]
    check("구분 목록", labels,
          ["요약", "항목 제외 (2개)", "Outlier 제거", "셀 수정 (1건)", "수율 분모"])
    check("Gross Die 분모 표기", rows[-1][1], "Gross Die (5000)")
    many = _extra.build_preprocess_rows({"spec": {"exclude_items": [f"I{i}" for i in range(40)]}})
    check("항목 30개 초과는 '외 N개'", many[0][1].endswith("외 10개"), True)


if __name__ == "__main__":
    test_issue_status()
    test_engr_plain()
    test_yield_overview()
    test_gradient()
    test_compare()
    test_preprocess()
    print("\n" + "=" * 60)
    if _fails:
        print(f"실패 {len(_fails)}건")
        for f in _fails:
            print("  - " + f)
        sys.exit(1)
    print("전부 통과")
