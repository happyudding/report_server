import json
import sys
import time
from pathlib import Path

from analysis import page_builder
from analysis import table_builder
from analysis.chart_payload import build_payload
from config import DATASETS_DIR
from analysis.data_loader import load_table
from analysis.preprocess import cumulative_distribution_full, to_numeric_clean
from analysis.svg_builder import build_subject_svg

COLOR_PALETTE = [
    "#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A",
    "#19D3F3", "#FF6692", "#B6E880", "#FF97FF", "#FECB52",
]
JSON_KWARGS = {"ensure_ascii": False, "separators": (",", ":")}


def _save_upload(src, dest):
    if isinstance(src, (bytes, bytearray)):
        dest.write_bytes(bytes(src))
    elif hasattr(src, "save"):
        src.save(str(dest))
    else:
        dest.write_bytes(Path(src).read_bytes())


def _idx_or(seq, i, default=None):
    return seq[i] if i < len(seq) else default


def _elapsed_since(start):
    return round(time.perf_counter() - start, 2)


def _log(message):
    print(f"[build] {message}", flush=True)


def _progress(label, current, total, start):
    elapsed = time.perf_counter() - start
    pct = current / total * 100 if total else 100
    rate = current / elapsed if elapsed > 0 else 0
    remain = (total - current) / rate if rate > 0 else 0
    if not sys.stdout.isatty() and current < total and current % 100 != 0:
        return
    prefix = "\r" if sys.stdout.isatty() else ""
    print(
        f"{prefix}[build] {label}: {current}/{total} ({pct:5.1f}%) "
        f"elapsed {elapsed:6.1f}s ETA {remain:6.1f}s",
        end="" if sys.stdout.isatty() and current < total else "\n",
        flush=True,
    )


def build_dataset(dataset_id, inputs, progress_cb=None):
    t0 = time.perf_counter()
    timings = {}
    out_dir = DATASETS_DIR / dataset_id
    input_dir, charts_dir, thumbs_dir = out_dir / "input", out_dir / "charts", out_dir / "thumbs"
    for d in (out_dir, input_dir, charts_dir, thumbs_dir):
        d.mkdir(parents=True, exist_ok=True)

    def emit(stage, current=0, total=0, **extra):
        if progress_cb:
            progress_cb({
                "dataset_id": dataset_id, "stage": stage,
                "current": current, "total": total,
                "elapsed_s": round(time.perf_counter() - t0, 2),
                **extra,
            })

    emit("save_inputs", 0, len(inputs))
    _log(f"start dataset={dataset_id}")
    _log("saving input CSV files")
    save_t0 = time.perf_counter()
    saved = []
    for filename, src in inputs.items():
        name = Path(filename).name
        if not name.lower().endswith(".csv"):
            continue
        dest = input_dir / name
        _save_upload(src, dest)
        saved.append(dest)
        emit("save_inputs", len(saved), len(inputs))
    if not saved:
        raise ValueError("No valid CSV uploads received (need .csv extension)")
    timings["save_inputs_s"] = _elapsed_since(save_t0)
    _log(f"saved {len(saved)} CSV files in {timings['save_inputs_s']}s")

    emit("load_csv", 0, len(saved))
    _log("loading CSV files")
    load_t0 = time.perf_counter()
    schools = {p.stem: load_table(p) for p in sorted(saved)}
    timings["load_csv_s"] = _elapsed_since(load_t0)
    _log(f"loaded {len(schools)} schools in {timings['load_csv_s']}s")
    emit("load_csv", len(schools), len(saved))
    names = list(schools.keys())
    color_map = {n: COLOR_PALETTE[i % len(COLOR_PALETTE)] for i, n in enumerate(names)}
    first = schools[names[0]]
    n_subjects = len(first.subjects)

    emit("table_json", 0, 1)
    _log("building table JSON artifacts")
    table_t0 = time.perf_counter()
    table_result = table_builder.build_table_artifacts(dataset_id, schools)
    timings["table_json_s"] = _elapsed_since(table_t0)
    _log(f"table JSON done in {timings['table_json_s']}s, raw rows={table_result['row_count']}")
    emit("table_json", 1, 1)

    cdf_s = 0.0
    payload_s = 0.0
    write_s = 0.0
    svg_s = 0.0
    write_svg_s = 0.0
    chart_bytes = 0
    svg_bytes = 0
    progress_t0 = time.perf_counter()
    _log(f"building JSON and SVG charts for {n_subjects} subjects")
    emit("cdf_svg", 0, n_subjects)
    for idx in range(n_subjects):
        traces = []
        for name in names:
            step_t0 = time.perf_counter()
            xs, ys = cumulative_distribution_full(to_numeric_clean(schools[name].scores.iloc[:, idx]))
            cdf_s += time.perf_counter() - step_t0
            traces.append({"school": name, "color": color_map[name], "xs": xs, "ys": ys})
        step_t0 = time.perf_counter()
        payload = build_payload(
            idx, first.subjects[idx], _idx_or(first.units, idx, ""),
            _idx_or(first.lower_limits, idx), _idx_or(first.upper_limits, idx), traces,
        )
        payload_s += time.perf_counter() - step_t0
        step_t0 = time.perf_counter()
        (charts_dir / f"{idx}.json").write_text(json.dumps(payload, **JSON_KWARGS), encoding="utf-8")
        write_s += time.perf_counter() - step_t0
        chart_bytes += (charts_dir / f"{idx}.json").stat().st_size

        step_t0 = time.perf_counter()
        svg = build_subject_svg(
            idx, first.subjects[idx], _idx_or(first.units, idx, ""),
            _idx_or(first.lower_limits, idx), _idx_or(first.upper_limits, idx), traces, payload["layout"],
        )
        svg_s += time.perf_counter() - step_t0
        step_t0 = time.perf_counter()
        (thumbs_dir / f"{idx}.svg").write_text(svg, encoding="utf-8")
        write_svg_s += time.perf_counter() - step_t0
        svg_bytes += (thumbs_dir / f"{idx}.svg").stat().st_size
        if (idx + 1) % 10 == 0 or idx + 1 == n_subjects:
            _progress("charts+svg", idx + 1, n_subjects, progress_t0)
            emit("cdf_svg", idx + 1, n_subjects)
    timings["cdf_s"] = round(cdf_s, 2)
    timings["payload_s"] = round(payload_s, 2)
    timings["write_json_s"] = round(write_s, 2)
    timings["svg_s"] = round(svg_s, 2)
    timings["write_svg_s"] = round(write_svg_s, 2)
    _log(f"chart JSON size: {chart_bytes / 1024 / 1024:.2f} MB")
    _log(f"SVG thumb size: {svg_bytes / 1024 / 1024:.2f} MB")

    emit("write_page", 0, 1)
    _log("writing HTML and build version")
    page_t0 = time.perf_counter()
    build_version = str(int(time.time()))
    (out_dir / "build_version.txt").write_text(build_version, encoding="utf-8")
    schools_info = [{"name": n, "color": color_map[n]} for n in names]
    page_builder.write_html(out_dir / "cumulative.html", first.subjects, schools_info,
                            dataset_id=dataset_id, build_version=build_version)
    timings["write_page_s"] = _elapsed_since(page_t0)

    elapsed_s = round(time.perf_counter() - t0, 2)
    timings["total_s"] = elapsed_s
    _log(f"done in {elapsed_s}s")
    emit("done", n_subjects, n_subjects, n_schools=len(names))
    return {
        "dataset_id": dataset_id, "build_version": build_version,
        "n_subjects": n_subjects, "n_schools": len(names), "schools": names,
        "elapsed_s": elapsed_s, "timings": timings,
        "chart_bytes": chart_bytes, "svg_bytes": svg_bytes,
        "raw_rows": table_result["row_count"],
    }
