"""더미 sheet_grids 픽스처 생성기.

Honey 클라이언트가 Excel COM 으로 추출해 서버로 보내는 sheet_grids 형식을 모방한다.
서버 xlsx_parser.parse_report_xlsx() 가 정상 동작하는지 검증하는 입력 샘플로 사용.

실행:
    python tests/sample_xlsx.py [output_path.json]   # grids JSON 저장
    python tests/sample_xlsx.py                       # parse 결과를 stdout 으로 검증

산출 형식 (서버 입력 계약):
    { "summary": {"origin":[1,1], "values":[[...]]},
      "yield":   {"origin":[1,1], "values":[[...]]},
      "issue_table": {"origin":[1,1], "values":[[...]]} }
실측 레이아웃 전제: A1 배너, B열부터 표 시작, summary B4='DEVICE'/B7='2. Yield',
yield B3='bin' 헤더, issue_table B3='Category'/C3='Bin' 헤더.
"""
import json
import sys
from pathlib import Path


def _grid(cells: dict, max_row: int, max_col: int) -> list:
    """{(row,col): value} (1-based) → max_row×max_col 2D 배열 (빈 칸 None)."""
    g = [[None] * max_col for _ in range(max_row)]
    for (r, c), v in cells.items():
        g[r - 1][c - 1] = v
    return g


def _summary_grid() -> dict:
    """B4='DEVICE' 앵커. Device Feature + 2.Yield + Major Fail Bins."""
    cells = {(1, 1): "📊 REPORT TITLE — sample-001", (3, 2): "1. Device Feature"}
    feat_hdr = ["DEVICE", "Customer", "PKG_Type", "GrossDie", "Process Line", "EVT_Version"]
    feat_val = ["S5E_TEST", "SLSI", "FCBGA", 280, "L1", "EVT0"]
    for i, (h, v) in enumerate(zip(feat_hdr, feat_val)):
        cells[(4, 2 + i)] = h     # B4..G4
        cells[(5, 2 + i)] = v     # B5..G5
    cells[(7, 2)] = "2. Yield"
    cells[(8, 2)] = "Lot NO"; cells[(8, 4)] = "Yield"
    cells[(8, 5)] = "Major Fail Bins"; cells[(8, 8)] = "Comment"
    cells[(9, 2)] = "-"; cells[(9, 4)] = 93.3
    fails = [("1st Fail", "subject_14", 0.4), ("2nd Fail", "subject_25", 0.4),
             ("3rd Fail", "subject_33", 0.4), ("4th Fail", "subject_03", 0.3),
             ("5th Fail", "subject_09", 0.3)]
    for i, (rank, subj, ratio) in enumerate(fails):
        r = 9 + i
        cells[(r, 5)] = rank; cells[(r, 6)] = subj; cells[(r, 7)] = ratio
    return {"origin": [1, 1], "values": _grid(cells, max_row=13, max_col=8)}


def _yield_grid() -> dict:
    """A1 배너, B3='bin' 헤더행, 데이터 4행~."""
    header = ["bin", "Item", "mass_data_a_count", "mass_data_a_yield",
              "mass_data_b_count", "mass_data_b_yield",
              "mass_data_c_count", "mass_data_c_yield", "avg", "comment"]
    rows = [
        [1, "Pass", 280, 93.33, 327, 93.43, 326, 93.14, 93.3, ""],
        [10, "subject_03", 1, 0.33, 3, 0.86, 3, 0.86, 0.68, ""],
        [11, "subject_09", 0, 0.0, 3, 0.86, 3, 0.86, 0.57, "전압 측정 실패"],
        [12, "subject_14", 0, 0.0, 3, 0.86, 3, 0.86, 0.57, "온도 한계 초과"],
    ]
    cells = {(1, 1): "Yield"}
    for i, h in enumerate(header):
        cells[(3, 2 + i)] = h          # B3..K3
    for ri, row in enumerate(rows):
        for ci, v in enumerate(row):
            cells[(4 + ri, 2 + ci)] = v
    return {"origin": [1, 1], "values": _grid(cells, max_row=3 + len(rows), max_col=11)}


def _issue_table_grid() -> dict:
    """A1 배너, B3='Category'/C3='Bin' 헤더행."""
    header = ["Category", "Bin", "Item", "avg",
              "mass_data_a_yield", "mass_data_b_yield", "mass_data_c_yield",
              "Distribution", "comment", "개발팀 1차 comment"]
    rows = [
        ["Yield", 1, "Pass", 93.3, 93.33, 93.43, 93.14, "", "", ""],
        ["", 10, "subject_03", 0.68, 0.33, 0.86, 0.86, "", "재현 가능", "PMU 확인 필요"],
        ["", 11, "subject_09", 0.57, 0.0, 0.86, 0.86, "", "고온 조건", "센서 보정 필요"],
    ]
    cells = {(1, 1): "Issue_table"}
    for i, h in enumerate(header):
        cells[(3, 2 + i)] = h          # B3..K3
    for ri, row in enumerate(rows):
        for ci, v in enumerate(row):
            cells[(4 + ri, 2 + ci)] = v
    return {"origin": [1, 1], "values": _grid(cells, max_row=3 + len(rows), max_col=11)}


def build_sample_grids() -> dict:
    return {
        "summary": _summary_grid(),
        "yield": _yield_grid(),
        "issue_table": _issue_table_grid(),
    }


if __name__ == "__main__":
    grids = build_sample_grids()
    if len(sys.argv) > 1:
        out = Path(sys.argv[1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(grids, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok] wrote {out}")
    else:
        # parser 검증 (server/ 를 import path 에 추가)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
        from xlsx_parser import parse_report_xlsx
        parsed = parse_report_xlsx(grids)
        print(json.dumps({
            "summary_keys": sorted(parsed["summary"].keys()),
            "yield_rows": len(parsed["yield_rows"]),
            "issue_rows": len(parsed["issue_rows"]),
            "sheet_data_sheets": sorted(parsed["sheet_data"].keys()),
        }, ensure_ascii=False, indent=2))
