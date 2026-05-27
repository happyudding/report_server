"""더미 .xlsx 생성기.

외부 report generator 가 만드는 실제 산출물 형식을 모방한다.
xlsx_parser.parse_report_xlsx() 가 정상 동작하는지 검증하는 입력 샘플로 사용.

실행:
    python tests/sample_xlsx.py [output_path]
기본 출력: tests/sample.xlsx
"""
import sys
from datetime import datetime
from pathlib import Path

try:
    import openpyxl
except ImportError:
    print("openpyxl 미설치. pip install openpyxl", file=sys.stderr)
    sys.exit(1)


def build_sample_xlsx(out_path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    _write_summary(wb.create_sheet("summary"))
    _write_yield(wb.create_sheet("yield"))
    _write_cpk(wb.create_sheet("cpk"))
    _write_fail_data(wb.create_sheet("fail_data"))
    _write_fail_values(wb.create_sheet("fail_values"))
    _write_issue_table(wb.create_sheet("issue_table"))
    _write_distribution(wb.create_sheet("distribution"))
    _write_histogram(wb.create_sheet("histogram"))

    wb.save(str(out_path))
    print(f"[ok] wrote {out_path}")


def _write_summary(ws):
    ws["A1"] = "Plotly Data Dashboard — sample-001"
    ws["A3"] = "Feature"
    ws.append([])  # row 4 (anchor 위치 보정용)
    ws["A4"] = ""
    ws["A5"] = "Dataset ID"; ws["B5"] = "Total DUT"; ws["C5"] = "Pass Count"; ws["D5"] = "Fail Types"
    ws["A6"] = "sample-001"; ws["B6"] = 1200; ws["C6"] = 1180; ws["D6"] = "Bin2, Bin3"

    ws["A8"] = "Yield Summary"
    ws["A9"] = "Overall Pass Yield (Bin 1): 98.3%"

    ws["A11"] = "Major Fail Bins"
    ws["A12"] = "rank"; ws["B12"] = "bin"; ws["C12"] = "main_fail"; ws["D12"] = "ratio"; ws["E12"] = "comment"
    ws["A13"] = 1; ws["B13"] = "Bin2"; ws["C13"] = "VDD_LOW"; ws["D13"] = "1.0%"; ws["E13"] = "전압 측정 실패"
    ws["A14"] = 2; ws["B14"] = "Bin3"; ws["C14"] = "TEMP_HI"; ws["D14"] = "0.7%"; ws["E14"] = "온도 한계 초과"

    ws["A16"] = "Evaluation Summary"
    ws["A17"] = "Yield";    ws["B17"] = "CPK"; ws["C17"] = "Temp";    ws["D17"] = "ETC"
    ws["A18"] = "정상";     ws["B18"] = "양호"; ws["C18"] = "정상";   ws["D18"] = "-"


def _write_yield(ws):
    ws.append(["bin", "count", "portion(%)", "avg", "Main Fail Subject", "comment"])
    ws.append([1, 1180, 98.3, 1.01, "-", "PASS"])
    ws.append([2,   12,  1.0,  0.85, "VDD_LOW", "전압 측정 실패"])
    ws.append([3,    8,  0.7,  0.72, "TEMP_HI", "온도 한계 초과"])
    ws.append(["Total", 1200, 100.0, "", "", ""])


def _write_cpk(ws):
    ws.append(["subject", "lower_limit", "upper_limit", "units", "n", "average", "stdev", "cpk"])
    ws.append(["VDD_LOW", 0.9, 1.1, "V", 1200, 1.005, 0.012, 1.85])
    ws.append(["TEMP_HI", -40, 85, "C", 1200, 32.5, 12.3, 1.42])


def _write_fail_data(ws):
    ws["A1"] = "see fail_values sheet for detail"


def _write_fail_values(ws):
    ws.append(["source", "dut", "x_coord", "y_coord", "bin",
               "subject", "value", "lower_limit", "upper_limit", "fail"])
    ws.append(["A.csv", 101, 3, 5, 2, "VDD_LOW", 0.82, 0.9, 1.1, "< lo"])
    ws.append(["A.csv", 102, 4, 6, 3, "TEMP_HI", 92.0, -40, 85, "> hi"])


def _write_issue_table(ws):
    ws.append(["bin", "subject", "average",
               "Distribution", "Issue Point", "Comment",
               "개발팀 1차 Comment", "PTE 1차 comment"])
    ws.append([2, "VDD_LOW", 0.85, "", "전압 회로 점검", "재현 가능", "PMU 확인 필요", "추적 중"])
    ws.append([3, "TEMP_HI", 92.0, "", "온도 센서 캘리브레이션", "고온 조건",
               "센서 보정 필요", "분석 중"])


def _write_distribution(ws):
    ws["A1"] = "(image placeholder)"


def _write_histogram(ws):
    ws["A1"] = "(image placeholder)"


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "sample.xlsx")
    build_sample_xlsx(out)
