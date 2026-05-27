import sys

from config import INPUT_DIR, SCHOOL_FILES_GLOB
from dataset_builder import build_dataset


def main():
    dataset_id = sys.argv[1] if len(sys.argv) > 1 else "current"
    paths = sorted(INPUT_DIR.glob(SCHOOL_FILES_GLOB))
    if not paths:
        raise SystemExit(f"No files matching {SCHOOL_FILES_GLOB!r} in {INPUT_DIR}")
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
    print(f"View at: http://127.0.0.1:8000/view/{dataset_id}")


if __name__ == "__main__":
    main()
