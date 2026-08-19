"""XlsxWriter 엔진 e2e — 실제 .xlsx 를 만들고 stdlib 로 뜯어 검증. Excel 불필요.

    python tests/test_excel_xlsxwriter.py [--keep]

서버 없이 돌도록 fetch_* 를 합성 payload 로 대체한다(웹 payload 와 같은 키 구조).
확인 항목: 시트 구성 / 표 값 / 색(fill) / 병합 / 열너비·행높이 / 차트 이미지 개수·해상도 /
시트 1개가 실패해도 파일이 만들어지는지.
"""
import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "client")):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np                                       # noqa: E402

_fails = []
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def check(name, got, want):
    ok = got == want
    if not ok:
        _fails.append(f"{name}\n     got : {got!r}\n     want: {want!r}")
    print(f"  [{'ok' if ok else 'FAIL'}] {name}")


def check_true(name, cond, detail=""):
    if not cond:
        _fails.append(f"{name} — {detail}")
    print(f"  [{'ok' if cond else 'FAIL'}] {name}{'' if cond else ' — ' + detail}")


# ── 합성 payload (웹 /full 응답과 같은 키 구조) ──────────────────────────────

SOURCES = ["LOT_A", "LOT_B"]
POINTS = 800            # source 당 die(=ECDF 고유값) 수 — 벤치가 늘려 잡는다


def set_scale(n_sources=None, n_points=None):
    """벤치용 규모 조절 — 소스 수/die 수를 바꾼다(테스트 기본값은 건드리지 않는다)."""
    global SOURCES, POINTS
    if n_sources:
        SOURCES = [f"LOT_{chr(65 + i)}" for i in range(n_sources)]
    if n_points:
        POINTS = int(n_points)


def _per_source(base, spread=1.0):
    """소스별 값 — 소스 수가 몇 개든 같은 규칙으로 채운다."""
    return {s: round(base + i * spread * 0.3, 2) for i, s in enumerate(SOURCES)}


def _yield_rows():
    pass_pct = _per_source(93.0, -1.0)
    rows = [dict({"step": "P2", "bin": "1", "TNO": "", "Item": "Pass", "avg": 92.0},
                 **{f"{s}_yield": v for s, v in pass_pct.items()},
                 **{f"{s}_count": int(v * 10) for s, v in pass_pct.items()})]
    for i, (b, item, base) in enumerate([("4", "VDD_FAIL", 5.0),
                                         ("7", "IDD_FAIL", 1.5),
                                         ("9", "LEAK_FAIL", 0.5)]):
        vals = _per_source(base)
        rows.append(dict({"step": "P2", "bin": b, "TNO": str(100 + i), "Item": item,
                          "avg": round(sum(vals.values()) / len(vals), 2)},
                         **{f"{s}_yield": v for s, v in vals.items()},
                         **{f"{s}_count": int(v * 10) for s, v in vals.items()}))
    return rows


def _issue_rows(n_cpk=1):
    """Issue Table 행 — n_cpk 로 CPK 섹션 항목 수를 늘려 대규모 세션을 흉내낸다."""
    def srcv(base):
        return {f"{s}_yield": v for s, v in _per_source(base).items()}

    rows = [
        dict({"Category": "Yield", "Step": "P2", "Bin": "4", "TNO": "100",
              "Item": "VDD_FAIL", "avg": 5.5, "Status": "Open",
              "PTE comment": "*[중요] 재현 확인", "개발 comment": "", "_grp": "g1"},
             **srcv(5.0)),
        dict({"Step": "P2", "Bin": "4", "TNO": "101", "Item": "VDD_FAIL_SUB", "avg": 2.0,
              "Status": "", "_detail": True, "PTE comment": "상세 코멘트",
              "개발 comment": "", "_grp": "g1"}, **srcv(2.0)),
        dict({"Step": "P2", "Bin": "7", "TNO": "102", "Item": "IDD_FAIL", "avg": 1.75,
              "Status": "Close", "PTE comment": "", "개발 comment": "조치 완료",
              "_grp": "g2"}, **srcv(1.5)),
        dict({"Category": "CPK", "Step": "P2", "Bin": "", "TNO": "", "Item": "",
              "avg": "cpk", "Status": ""},
             **{f"{s}_yield": None for s in SOURCES}),
    ]
    for i in range(max(1, n_cpk)):
        subject = "VDD_TEST" if i == 0 else (f"ITEM_{i:03d}_VOLT" if i % 5 else
                                             f"ITEM_{i:03d}_전압")
        rows.append(dict({"Step": "P2", "Bin": "", "TNO": str(200 + i), "Item": subject,
                          "avg": 1.10, "Status": "Open" if i % 3 == 0 else "",
                          "PTE comment": "", "개발 comment": ""}, **srcv(1.05)))
    rows.append(dict({"Category": "ETC", "Step": "P2", "Bin": "9", "TNO": "300",
                      "Item": "LEAK_FAIL", "avg": 0.75, "Status": "Open",
                      "PTE comment": "", "개발 comment": ""}, **srcv(0.5)))
    return rows


def _dist_payload(n_items):
    items = {}
    rng = np.random.default_rng(11)
    for i in range(n_items):
        subject = f"ITEM_{i:03d}_전압" if i % 5 == 0 else f"ITEM_{i:03d}_VOLT"
        srcs = {}
        for s in SOURCES:
            v = np.sort(rng.normal(1.0, 0.03, POINTS))
            srcs[s] = {"x": v.tolist(), "y": np.linspace(0, 100, POINTS).tolist()}
        items[subject] = {"sources": srcs, "units": "V", "lo": 0.9, "hi": 1.1}
    # Issue Table 이 참조하는 항목들도 분포를 갖게 한다
    for subject in ("VDD_FAIL", "IDD_FAIL", "LEAK_FAIL", "VDD_TEST"):
        v = np.sort(rng.normal(1.0, 0.05, POINTS))
        items[subject] = {"sources": {s: {"x": v.tolist(),
                                          "y": np.linspace(0, 100, POINTS).tolist()}
                                      for s in SOURCES},
                          "units": "V", "lo": 0.9, "hi": 1.1}
    return {"format": "ecdf-columnar-v1", "items": items}


def _map_payload():
    maps = []
    for s in SOURCES:
        dies = []
        for y in range(1, 31):
            for x in range(1, 31):
                # die 키는 서버 Map_analysis._die 와 동일하게 "bin" (프런트/렌더러 공용)
                dies.append({"x": x, "y": y, "bin": 1 if (x + y) % 7 else 4})
        maps.append({"source": s, "step": "P2", "dies": dies})
    return {"format": "map-dies-v1", "maps": maps}


def _map_meta_rows():
    """/full 의 sheets["Map Analysis"] — dies 없는 경량 메타(schema v8) 구조 그대로.

    범례(build_global_bin_legend)가 bin_counts 를 쓰므로 서버와 같은 필드를 채운다.
    """
    rows = []
    for s in SOURCES:
        n_pass = sum(1 for y in range(1, 31) for x in range(1, 31) if (x + y) % 7)
        rows.append({"source": s, "file_name": f"{s}.csv", "step": "P2",
                     "x_min": 1, "x_max": 30, "y_min": 1, "y_max": 30, "total": 900,
                     "bin_counts": [{"bin": 1, "count": n_pass, "is_pass": True},
                                    {"bin": 4, "count": 900 - n_pass, "is_pass": False}]})
    return rows


def _full_payload(n_items, n_cpk=1):
    dist_index = [{"subject": k, "test_num": str(1000 + i), "units": "V",
                   "lower_limit": 0.9, "upper_limit": 1.1,
                   "status": "fail" if i % 3 == 0 else "ok", "cpk": 1.0 + i % 3}
                  for i, k in enumerate(_dist_payload(n_items)["items"].keys())]
    yield_rows = _yield_rows()
    bin_groups = [{"rep": r} for r in yield_rows[1:]]
    return {
        "web_report": {
            "mode": "Normal",
            "sources": [{"name": s} for s in SOURCES],
            "dist_colors": [],
            "sheets": {
                "Yield": yield_rows,
                "CPK": [{"subject": "VDD_TEST", "lower_limit": 0.9, "upper_limit": 1.1,
                         "units": "V", "source": s, "n": 800, "min": 0.9, "median": 1.0,
                         "max": 1.1, "average": 1.0, "stdev": 0.03, "cpl": 1.1,
                         "cpu": 1.1, "cp": 1.1, "cpk": 1.05 if s == "LOT_A" else 1.5}
                        for s in SOURCES],
                "Issue Table": _issue_rows(n_cpk),
                "Fail Bin": [{"bin": "4", "item": "VDD_FAIL", "yield_pct": 5.5},
                             {"bin": "7", "item": "IDD_FAIL", "yield_pct": 1.75}],
                "Map Analysis": _map_meta_rows(),
            },
            "yield_bin_groups": bin_groups,
            "yield_step_groups": [{"step": "P2", "groups": bin_groups}],
            "yield_summary": {
                "yield_pct": 92.0, "pass": 1840, "fail": 160, "total": 2000,
                "tested": 2000,
                "by_source": [{"source": "LOT_A", "yield_pct": 93.0, "pass": 930,
                               "fail": 70, "total": 1000, "tested": 1000},
                              {"source": "LOT_B", "yield_pct": 91.0, "pass": 910,
                               "fail": 90, "total": 1000, "tested": 1000}],
                "by_step": [
                    {"step": "P1", "avg_yield_pct": 97.0,
                     "sources": [{"source": s, "yield_pct": 97.0, "survivor": 970,
                                  "entered": 1000, "fail": 30, "cum_fail": 30}
                                 for s in SOURCES]},
                    {"step": "P2", "avg_yield_pct": 92.0,
                     "sources": [{"source": s, "yield_pct": 92.0, "survivor": 920,
                                  "entered": 1000, "fail": 50, "cum_fail": 80}
                                 for s in SOURCES]}],
            },
            "yield_basis": {"basis": "gross", "gross_die": 1000,
                            "by_source": [{"source": s, "basis": "gross", "total": 1000}
                                          for s in SOURCES]},
            "summary_engr": {
                "yield": "<!--rich-->P2 <b>수율 저하</b> 확인 필요<br>재측정 예정",
                "cpk": "VDD_TEST cpk 1.05 로 낮음",
                "etc": "",
            },
            "distribution_index": dist_index,
            "compare": {
                "equivalence": {
                    "before": "LOT_B", "after": "LOT_A",
                    "thresholds": {"avg_pct": 5, "cpk": 5},
                    "summary": {"total": 1, "grade1": 0, "grade2": 0, "grade3": 1},
                    "rows": [{"step": "P2", "subject": "VDD_TEST", "units": "V",
                              "hilim": 1.1, "lolim": 0.9,
                              "before": {"average": 1.0, "stdev": 0.03, "cpk": 1.5},
                              "after": {"average": 1.5, "stdev": 0.06, "cpk": 1.05},
                              "delta_avg": 0.5, "delta_pct": 50.0, "grade": 3}]},
                "dist_shift": {
                    "before": "LOT_B", "after": "LOT_A",
                    "thresholds": {"cpk_low": 1.33, "stdev_delta_pct": 30, "alpha": 0.05},
                    "summary": {"total": 1, "focus": 1},
                    "rows": [{"subject": "VDD_TEST", "units": "V",
                              "after": {"average": 1.5, "stdev": 0.06, "cpk": 1.05, "n": 800},
                              "before": {"average": 1.0, "stdev": 0.03, "cpk": 1.5, "n": 800},
                              "meanshift_sigma": 16.6, "cpk_ratio_pct": 70.0,
                              "stdev_delta_pct": 100.0, "median_shift": 8.0,
                              "focus": True}]},
                "goodlog": {
                    "after_source": "LOT_A", "before_source": "LOT_B", "identical": False,
                    "header": None, "rows": [
                        {"after_item_name": "VDD_TEST", "after_lolimit": 0.9,
                         "after_hilimit": 1.1, "after_unit": "V", "after_value": 1.0,
                         "compare_item_name": True, "compare_lolimit": False,
                         "compare_hilimit": True, "comment": "", "gap": 12.5,
                         "Before_item_name": "VDD_TEST", "Before_lolimit": 0.8,
                         "Before_hilimit": 1.1, "Before_unit": "V", "Before_value": 0.89}]},
            },
        },
        "chart_notes": {
            "cdf:VDD_TEST": {
                "shapes": [{"type": "rect", "x0": 0.95, "x1": 1.05, "y0": 20, "y1": 80,
                            "xref": "x", "yref": "y",
                            "line": {"color": "#DC2626", "width": 2}}],
                "texts": [{"x": 1.0, "y": 50, "text": "확인 필요", "showarrow": True,
                           "ax": 30, "ay": -40, "xref": "x", "yref": "y",
                           "font": {"size": 12, "color": "#DC2626"}}],
                "comment": "산포 넓어짐 — 재측정 필요",
            },
        },
    }


# ── xlsx 읽기 헬퍼 (stdlib 만) ───────────────────────────────────────────────

class Book:
    def __init__(self, path):
        self.zf = zipfile.ZipFile(path)
        self.names = set(self.zf.namelist())
        wb = ET.fromstring(self.zf.read("xl/workbook.xml"))
        self.sheets = [s.get("name") for s in wb.findall(".//m:sheets/m:sheet", NS)]
        shared = []
        if "xl/sharedStrings.xml" in self.names:
            sst = ET.fromstring(self.zf.read("xl/sharedStrings.xml"))
            for si in sst.findall("m:si", NS):
                shared.append("".join(t.text or "" for t in si.iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
        self.shared = shared
        styles = ET.fromstring(self.zf.read("xl/styles.xml"))
        self.fills = []
        for fill in styles.findall(".//m:fills/m:fill", NS):
            fg = fill.find("m:patternFill/m:fgColor", NS)
            self.fills.append((fg.get("rgb") if fg is not None else None))
        self.xf_fill = [int(xf.get("fillId") or 0)
                        for xf in styles.findall(".//m:cellXfs/m:xf", NS)]
        self.media = sorted(n for n in self.names if n.startswith("xl/media/"))

    def sheet_xml(self, name):
        idx = self.sheets.index(name) + 1
        return ET.fromstring(self.zf.read(f"xl/worksheets/sheet{idx}.xml"))

    def cells(self, name):
        """{"B3": (value, fill_rgb)} — 값은 문자열/숫자."""
        out = {}
        for c in self.sheet_xml(name).findall(".//m:sheetData/m:row/m:c", NS):
            ref, t = c.get("r"), c.get("t")
            v = c.find("m:v", NS)
            if t == "s" and v is not None:
                value = self.shared[int(v.text)]
            elif t == "inlineStr":
                is_el = c.find("m:is", NS)
                value = "".join(x.text or "" for x in is_el.iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")) \
                    if is_el is not None else None
            elif v is not None:
                try:
                    value = float(v.text)
                    if value == int(value):
                        value = int(value)
                except (TypeError, ValueError):
                    value = v.text
            else:
                value = None
            s = int(c.get("s") or 0)
            fill = self.fills[self.xf_fill[s]] if s < len(self.xf_fill) else None
            out[ref] = (value, fill)
        return out

    # xlsxwriter 는 Excel 이 표시하는 문자 폭에 여백(0.7109375)을 더해 저장한다 —
    # 지정한 값과 비교하려면 그 여백을 빼야 한다.
    _COL_PAD = 0.7109375

    def col_widths(self, name):
        return {int(c.get("min")): round(float(c.get("width")) - self._COL_PAD, 3)
                for c in self.sheet_xml(name).findall(".//m:cols/m:col", NS)}

    def row_heights(self, name):
        return {int(r.get("r")): float(r.get("ht"))
                for r in self.sheet_xml(name).findall(".//m:sheetData/m:row", NS)
                if r.get("ht")}

    def merges(self, name):
        return [m.get("ref") for m in
                self.sheet_xml(name).findall(".//m:mergeCells/m:mergeCell", NS)]

    def n_images(self, name):
        idx = self.sheets.index(name) + 1
        rels = f"xl/worksheets/_rels/sheet{idx}.xml.rels"
        if rels not in self.names:
            return 0
        root = ET.fromstring(self.zf.read(rels))
        draw = [r.get("Target") for r in root
                if r.get("Type", "").endswith("/drawing")]
        if not draw:
            return 0
        path = "xl/" + draw[0].replace("../", "")
        if path not in self.names:
            return 0
        d = ET.fromstring(self.zf.read(path))
        return sum(1 for _ in d.iter(
            "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}pic"))

    def png_size(self, member):
        head = self.zf.read(member)[:24]
        return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))


def _extra_header_index():
    """Issue Table 헤더에서 Status 컬럼의 0-based 인덱스 (열 문자 계산용)."""
    from excel_download import _extra
    return _extra.issue_header(SOURCES).index("Status")


def _has_color(book, member, rgb, tol=12):
    """PNG 안에 그 색 픽셀이 있는지 — 맵이 통째로 회색이던 사고를 잡는다."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    img = mpimg.imread(io.BytesIO(book.zf.read(member)), format="png")
    px = (img[:, :, :3] * 255).astype(int)
    want = np.array(rgb)
    return bool((np.abs(px - want).max(axis=2) <= tol).any())


def find_cell(cells, text, col=None):
    """값이 text 인 첫 셀 주소 (col 을 주면 그 열에서만)."""
    for ref, (value, _fill) in sorted(cells.items(), key=lambda kv: _ref_key(kv[0])):
        if col and not ref.startswith(col):
            continue
        if value == text:
            return ref
    return None


def _ref_key(ref):
    letters = "".join(ch for ch in ref if ch.isalpha())
    digits = int("".join(ch for ch in ref if ch.isdigit()) or 0)
    return (digits, letters)


# ── 실행 ────────────────────────────────────────────────────────────────────

def build(tmp, *, n_items=40, out_name="out.xlsx", break_sheet=None, engine="xlsxwriter",
          n_cpk=1):
    """서버 없이 run_excel_download 를 돌린다 — fetch_* 만 합성 payload 로 대체."""
    import excel_download as ed
    from excel_download import _fetch

    full = _full_payload(n_items, n_cpk)
    dist = _dist_payload(n_items)
    maps = _map_payload()

    def fake_fetch_report_data(server_base, session_id, bin1=False, status_cb=None):
        _fetch._merge_map_dies(full["web_report"], maps)
        return full, dist

    ed.fetch_report_data = fake_fetch_report_data
    ed.fetch_distribution_bin1 = lambda *a, **k: {}
    ed.fetch_temp_map = lambda *a, **k: {}

    if break_sheet:                     # 시트 1개를 일부러 실패시켜 격리를 확인
        from excel_download import _xlsx
        original = _xlsx.XlsxBook.write_cpk_sheet

        def boom(self, *a, **k):
            raise RuntimeError("의도적 실패(테스트)")
        _xlsx.XlsxBook.write_cpk_sheet = boom
        try:
            return _run(ed, tmp, out_name, engine)
        finally:
            _xlsx.XlsxBook.write_cpk_sheet = original
    return _run(ed, tmp, out_name, engine)


def _run(ed, tmp, out_name, engine):
    out = os.path.join(tmp, out_name)
    msgs = []
    result = ed.run_excel_download(
        "test-session-0001", "http://127.0.0.1:8080", out,
        status_cb=lambda state, msg: msgs.append((state, msg)),
        progress_cb=lambda pct, msg: msgs.append(("pct", f"{pct}% {msg}")),
        engine=engine)
    result["messages"] = msgs
    return result


def main(keep=False):
    tmp = tempfile.mkdtemp(prefix="exceldl_test_")
    try:
        print("\n[1] 정상 생성")
        res = build(tmp, n_items=40)
        path = res["out_path"]
        print(f"   엔진={res['engine']} 소요={res['elapsed']:.1f}s "
              f"항목={res['items']} 크기={os.path.getsize(path)/1e6:.1f}MB "
              f"경고={len(res['warnings'])}")
        for w in res["warnings"]:
            print(f"     ! {w}")
        bk = Book(path)

        check("시트 구성", bk.sheets,
              ["Summary", "Yield", "CPK", "Issue Table", "Compare",
               "Distribution", "Histogram", "Map Analysis"])
        check_true("경고 없음", not res["warnings"], str(res["warnings"]))

        # ── Summary ─────────────────────────────────────────────────────────
        print("\n[2] Summary — 웹 카드 파리티")
        cells = bk.cells("Summary")
        check("제목 배너", cells["A1"][0], "Summary")
        check_true("Yield Summary 표", find_cell(cells, "Yield Summary") is not None, "")
        check_true("Major Fail Bin 표", find_cell(cells, "Major Fail Bin") is not None, "")
        check_true("Issue Status 카드(신규)", find_cell(cells, "Issue Status") is not None, "")
        check_true("Engr Comment 카드(신규)", find_cell(cells, "Engr Comment") is not None, "")
        # Issue Status 값: Yield 2 Open? (VDD_FAIL Open, IDD_FAIL Close) → open1/close1
        ref = find_cell(cells, "Yield", col="B")
        prog_refs = [r for r, (v, _f) in cells.items() if v == "50.0%"]
        check_true("Yield 진행률 50.0% 기입", bool(prog_refs), str(sorted(cells.items())[:5]))
        engr = [v for (v, _f) in cells.values() if isinstance(v, str) and "수율 저하" in v]
        check_true("Engr 리치 HTML → 평문", bool(engr), "")
        check_true("Engr HTML 태그 제거", all("<b>" not in e for e in engr), str(engr))

        # ── Yield ───────────────────────────────────────────────────────────
        print("\n[3] Yield — 상단 요약 + 그라데이션")
        cells = bk.cells("Yield")
        check_true("Yield 요약 블록(신규)", find_cell(cells, "Yield 요약") is not None, "")
        check_true("STEP 별 수율 표(신규)", find_cell(cells, "STEP 별 수율") is not None, "")
        check_true("Source 별 수율 표(신규)", find_cell(cells, "Source 별 수율") is not None, "")
        check_true("분모 캡션", any(isinstance(v, str) and "Gross Die 1000" in v
                                 for v, _f in cells.values()), "")
        check_true("STEP 표 원본 표도 존재", find_cell(cells, "STEP P2") is not None, "")
        grad = {f for _v, f in cells.values() if f and f.startswith("FF") and
                f[2:4] in ("FC", "FB", "FA", "F9", "F8", "F7", "F6", "F5", "F4", "F3",
                           "F2", "F1", "F0", "EF", "EE", "ED", "EC", "EB", "EA", "E9",
                           "E8", "E7")}
        check_true("fail 빨강 그라데이션 여러 단계", len(grad) >= 2, f"fills={sorted(grad)}")

        # ── Issue Table ─────────────────────────────────────────────────────
        print("\n[4] Issue Table — Status 색·comment·썸네일")
        cells = bk.cells("Issue Table")
        vals = [v for v, _f in cells.values()]
        check_true("PTE comment 서식토큰 제거", any(
            isinstance(v, str) and v.startswith("중요 재현 확인") for v in vals),
            str([v for v in vals if isinstance(v, str) and "재현" in v]))
        check_true("접힌 detail comment 흡수", any(
            isinstance(v, str) and "VDD_FAIL_SUB: 상세 코멘트" in v for v in vals), "")
        fills = {f for _v, f in cells.values() if f}
        check_true("CPK 경고 연노랑", "FFFFF3B0" in fills, str(sorted(fills)))
        # 색이 **어느 칸에** 칠해지는지까지 고정한다 — 예전에 Status 색이 옆(source) 칸에
        # 칠해진 적이 있고, fill 존재 여부만 보면 그 사고를 못 잡는다.
        # 헤더: B=Category … L=Status (source 2개 기준), 데이터는 4행부터.
        status_col = "BCDEFGHIJKLMNOP"[_extra_header_index()]
        check("Status 헤더 위치", cells[f"{status_col}3"][0], "Status")
        check("Open 행 Status 값·색", cells[f"{status_col}4"], ("Open", "FFFFC7CE"))
        check("Close 행 Status 값·색", cells[f"{status_col}5"], ("Close", "FFDCFCE7"))
        prev_col = "BCDEFGHIJKLMNOP"[_extra_header_index() - 1]   # 마지막 source 컬럼
        check_true("옆 source 칸은 Status 색이 아니다",
                   cells[f"{prev_col}4"][1] not in ("FFFFC7CE", "FFDCFCE7"),
                   str(cells[f"{prev_col}4"]))
        check_true("Category 세로 병합", any(m.startswith("B") for m in
                                         bk.merges("Issue Table")),
                   str(bk.merges("Issue Table")))
        check_true("Issue 썸네일 이미지 부착", bk.n_images("Issue Table") > 0,
                   f"n={bk.n_images('Issue Table')}")

        # ── 차트 ────────────────────────────────────────────────────────────
        print("\n[5] Distribution / Histogram / Map — 이미지와 해상도")
        n_dist = bk.n_images("Distribution")
        n_hist = bk.n_images("Histogram")
        n_map = bk.n_images("Map Analysis")
        check_true("Distribution 차트 부착", n_dist >= 1, f"n={n_dist}")
        check_true("Histogram 차트 부착", n_hist >= 1, f"n={n_hist}")
        check("Distribution/Histogram 장수 동일", n_dist, n_hist)
        check("Map 이미지 = source 수", n_map, len(SOURCES))
        from excel_download._charts import DPI, NCOLS, CELL_W_IN
        big = max(bk.png_size(m) for m in bk.media)
        check_true(f"차트 PNG 해상도(DPI {DPI})",
                   big[0] >= int(NCOLS * CELL_W_IN * DPI * 0.9), f"max px={big}")
        check_true("숨김 항목 인덱스(Ctrl+F)", any(
            isinstance(v, str) and v.startswith("ITEM_")
            for v, _f in bk.cells("Distribution").values()), "")
        check_true("Map Bin Legend 표", find_cell(bk.cells("Map Analysis"), "Description")
                   is not None, "")
        # 맵이 전부 회색(= bin 미인식)으로 나오던 사고 방지 — Pass 초록이 실제로 칠해졌는지
        map_png = [m for m in bk.media if bk.png_size(m) == (660, 660)]
        check_true("웨이퍼 맵 Pass 색 존재", _has_color(bk, map_png[0], (12, 163, 12))
                   if map_png else False, "맵 PNG 에 PASS_COLOR(#0ca30c) 픽셀이 없다")

        # ── 신규 시트 ───────────────────────────────────────────────────────
        print("\n[6] Compare")
        cells = bk.cells("Compare")
        check_true("동일성 검증 표", find_cell(cells, "동일성 검증") is not None, "")
        check_true("산포 비교 표", find_cell(cells, "산포 비교") is not None, "")
        check_true("Good Log 표", find_cell(cells, "Good Log 비교") is not None, "")
        check_true("goodlog 불일치 X 표기", any(v == "X" for v, _f in cells.values()), "")

        # ── 레이아웃 ────────────────────────────────────────────────────────
        print("\n[7] Layout — 열너비·행높이·링크")
        widths = bk.col_widths("CPK")
        check("CPK TEST NAME 열너비 60", widths.get(2), 60.0)
        check("CPK 계열 열너비 15", widths.get(6), 15.0)
        heights = bk.row_heights("Yield")
        check_true("Yield 헤더행 40pt", 40.0 in heights.values(), str(heights))
        check_true("Yield 데이터행 22pt", 22.0 in heights.values(), str(heights))
        check_true("세션 링크(H1)", "H1" in bk.cells("Summary"), "")

        # ── 시트 실패 격리 ──────────────────────────────────────────────────
        print("\n[8] 시트 1개 실패해도 파일은 만들어진다")
        res2 = build(tmp, n_items=8, out_name="broken.xlsx", break_sheet="CPK")
        bk2 = Book(res2["out_path"])
        check_true("실패해도 파일 생성", os.path.exists(res2["out_path"]), "")
        check_true("경고 기록됨", any("CPK" in w for w in res2["warnings"]),
                   str(res2["warnings"]))
        cpk_cells = bk2.cells("CPK")
        check_true("실패 시트에 안내 문구", any(
            isinstance(v, str) and v.startswith("⚠") for v, _f in cpk_cells.values()),
            str(list(cpk_cells.values())[:4]))
        check_true("다른 시트는 정상", find_cell(bk2.cells("Summary"), "Yield Summary")
                   is not None, "")

        if keep:
            dest = ROOT / "tests" / "bench_results"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy(path, dest / "excel_download_sample.xlsx")
            print(f"\n샘플 보관: {dest / 'excel_download_sample.xlsx'}")
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            print(f"임시 폴더 유지: {tmp}")

    print("\n" + "=" * 60)
    if _fails:
        print(f"실패 {len(_fails)}건")
        for f in _fails:
            print("  - " + f)
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main(keep="--keep" in sys.argv))
