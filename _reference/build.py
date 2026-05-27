import sys
from pathlib import Path

from analysis.dataset_builder import build_dataset
from config import SERVER_BASE_URL


# 빌드에 사용할 입력 CSV 절대경로 리스트.
# 수시로 다른 데이터로 빌드하려면 이 리스트만 수정.
INPUT_PATHS = [
    r"F:\COINAPI\plotly\data\a_school_updated_call.csv",
    r"F:\COINAPI\plotly\data\b_school_updated_call.csv",
    r"F:\COINAPI\plotly\data\c_school_updated_call.csv",
]

# INPUT_PATHS = [
#     r"F:\COINAPI\plotly\data\a_school.csv",
#     r"F:\COINAPI\plotly\data\b_school.csv",
#     r"F:\COINAPI\plotly\data\c_school.csv",
# ]


def main():
    dataset_id = sys.argv[1] if len(sys.argv) > 1 else "current"

    paths = [Path(p) for p in INPUT_PATHS]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise SystemExit("File(s) not found:\n  - " + "\n  - ".join(missing))

    inputs = {p.name: p for p in paths}
    print(f"Building dataset '{dataset_id}' from {len(inputs)} files:")
    for name in inputs:
        print(f"  - {name}")
    r = build_dataset(dataset_id, inputs)
    print(f"\nDone: {r['n_subjects']} subjects, {r['n_schools']} schools, {r['elapsed_s']}s")
    timings = r.get("timings", {})
    if timings:
        print("Timings:")
        for key, value in timings.items():
            print(f"  - {key}: {value}s")
    if "chart_bytes" in r and "svg_bytes" in r:
        print(f"Sizes: charts={r['chart_bytes'] / 1024 / 1024:.2f} MB, thumbs={r['svg_bytes'] / 1024 / 1024:.2f} MB")
    print(f"View at: {SERVER_BASE_URL}/view/{dataset_id}")
    print(f"Dash at: {SERVER_BASE_URL}/dash/{dataset_id}")


if __name__ == "__main__":
    main()
