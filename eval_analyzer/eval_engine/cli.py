"""얇은 테스트/보정 CLI (서버 아님).

용도:
  python -m eval_engine.cli init                 # eval.db 생성
  python -m eval_engine.cli run <sample.csv> [--meta '{...}']
                                                 # 샘플 raw 1개로 evaluate() 단독 검증
  python -m eval_engine.cli calibrate            # thresholds.yaml 재산출 (skeleton)
  python -m eval_engine.cli seed <background.csv> # 과거 라벨 seed 적재 (label/case_outcome)

run CSV 두 종류(헤더로 자동판별):
  A. degrade: meta_* 접두 컬럼 + item_name,bin,unit,yield,fail_count,total_count,lsl,usl
  B. raw per-DUT(df_honey 레이아웃): DUT,XCoord,YCoord,Bin,Serial,<items...>
     데이터 첫 3행 = UNITS / LOWER_LIMIT / UPPER_LIMIT 태그행, 이후 실측행.
     meta 는 --meta '{"product_name":...}' 인자로 주입.
seeds/background_seed_example.csv 형식 참고(24컬럼).
"""
import csv
import json
import sys

from . import store

try:  # Windows 콘솔(cp949)에서 한국어 코멘트 출력 보장
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def _to_float(s):
    """CSV 셀 → float. 빈 값은 None, 숫자가 아니면 **원본 문자열 그대로** 돌려준다."""
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return s


def _detect_csv_kind(header):
    """헤더만 보고 'raw'(DUT+Bin 있음) / 'degrade' 판별. run 명령이 파서를 고르는 기준."""
    return "raw" if ("DUT" in header and "Bin" in header) else "degrade"


def _read_degrade_csv(path):
    """degrade CSV → run_input({"meta", "items"}). meta 는 첫 행의 `meta_*` 접두 컬럼에서 뽑는다."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError("empty CSV")
    first = rows[0]
    meta = {k[len("meta_"):]: first[k] for k in first if k.startswith("meta_")}
    meta.setdefault("revision", 0.0)
    items = []
    for r in rows:
        items.append({
            "item_name": r["item_name"], "bin": int(r["bin"]),
            "unit": r.get("unit") or None,
            "yield": _to_float(r.get("yield")),
            "fail_count": int(r["fail_count"]) if r.get("fail_count") else None,
            "total_count": int(r["total_count"]) if r.get("total_count") else None,
            "lsl": _to_float(r.get("lsl")), "usl": _to_float(r.get("usl")),
        })
    return {"meta": meta, "items": items}


def _read_raw_csv(path):
    """raw per-DUT CSV(df_honey 레이아웃) → 레거시 raw_table dict.

    첫 열의 태그(UNITS/LOWER_LIMIT/UPPER_LIMIT)로 한계행을 골라내고 나머지를 측정행으로
    본다. Bin 은 int, Serial 은 문자열 유지, 그 밖의 셀은 `_to_float`.
    ⚠ 정본 raw_df(7-meta) 경로가 아니라 레거시 raw_table 경로다 — 정본 포맷 E2E 는
    tools/testbench_eval.py 를 쓴다.
    """
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    meta_cols = [c for c in ("DUT", "XCoord", "YCoord", "Bin", "Serial", "Site") if c in header]
    item_cols = [c for c in header if c not in meta_cols]
    idx = {c: header.index(c) for c in header}
    units, lower, upper, data_rows = {}, {}, {}, []
    for r in rows[1:]:
        tag = r[0] if r else ""
        if tag == "UNITS":
            units = {c: (r[idx[c]] or None) for c in item_cols}
        elif tag == "LOWER_LIMIT":
            lower = {c: _to_float(r[idx[c]]) for c in item_cols}
        elif tag == "UPPER_LIMIT":
            upper = {c: _to_float(r[idx[c]]) for c in item_cols}
        else:
            row = {}
            for c in meta_cols:
                val = r[idx[c]]
                if c == "Serial":
                    row[c] = val
                elif c == "Bin":
                    fv = _to_float(val)
                    row[c] = int(fv) if isinstance(fv, float) else fv
                else:
                    row[c] = _to_float(val)
            for c in item_cols:
                row[c] = _to_float(r[idx[c]])
            data_rows.append(row)
    return {"meta_columns": meta_cols, "item_columns": item_cols,
            "units": units, "lower_limit": lower, "upper_limit": upper, "rows": data_rows}


def _cmd_run(argv):
    """`run <csv> [--meta '{...}']` — CSV 종류를 자동판별해 evaluate(persist=True) 후 JSON 출력.

    raw 경로는 meta 가 CSV 에 없으므로 CLI_TEST/PMIC 기본값을 깔고 `--meta` 로 덮어쓴다.
    api import 를 함수 안에서 하는 것은 init/seed 만 쓸 때 엔진 전체 로딩을 피하려는 것.
    """
    path = argv[1]
    meta_overrides = {}
    if "--meta" in argv:
        meta_overrides = json.loads(argv[argv.index("--meta") + 1])
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    kind = _detect_csv_kind(header)
    if kind == "degrade":
        run_input = _read_degrade_csv(path)
        run_input["meta"].update(meta_overrides)
    else:
        raw_table = _read_raw_csv(path)
        meta = {"product_name": "CLI_TEST", "product_type": "PMIC",
                "family_product": "PMIC_ETC", "revision": 0.0,
                "lot_id": "CLI_LOT", "wafer_number": 1, "source_file": path,
                "ingested_by": "cli"}
        meta.update(meta_overrides)
        run_input = {"meta": meta, "raw_table": raw_table}
    from . import api
    result = api.evaluate(run_input, persist=True)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def _cmd_seed(argv):
    """`seed <background.csv>` — 과거 라벨/조치를 선례 DB 에 직접 적재(평가 파이프라인 미경유).

    행마다 product/item 마스터 → fail_case → raw_metrics → label → case_outcome 을 손으로
    엮는다. 판정을 만드는 게 아니라 **선례검색이 인용할 과거 사례를 심는** 경로다.
    전 행을 커넥션 하나에 묶어 중간 실패 시 통째로 롤백된다.
    """
    path = argv[1]
    store.init_db()
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    revision = 0.0
    with store.get_conn() as conn:
        run_id = store.create_ingest_run(
            {"source_file": path, "ingested_by": "seed_cli"}, conn=conn)
        for r in rows:
            store.upsert_product_master({
                "product_name": r["product_name"], "family_product": r["family_product"],
                "product_type": r["product_type"]}, conn=conn)
            raw_name = r["item_name"]
            item_canonical = raw_name.strip().lower()
            category_major = "TRIM" if "TRIM" in raw_name.upper() else "NON_TRIM"
            value_type = r["value_type"]
            item_id = store.upsert_item_master(
                item_canonical, raw_name, None, None, category_major, None,
                value_type, None, conn=conn)
            lsl, usl = _to_float(r.get("lsl")), _to_float(r.get("usl"))
            if lsl is not None or usl is not None:
                store.upsert_item_spec(item_id, r["product_name"], revision, lsl, usl, conn=conn)
            bin_ = int(r["bin"])
            case_id = store.make_case_id(r["product_name"], r["lot_id"], r["wafer_number"],
                                         item_id, bin_, revision)
            item_class = f"{category_major}|{value_type}|{bin_}"
            store.upsert_fail_case(case_id, r["product_name"], r["lot_id"], r["wafer_number"],
                                   item_id, bin_, revision, item_class, conn=conn)
            store.link_run_case(run_id, case_id, conn=conn)
            store.save_raw_metrics(case_id, run_id, {
                "cpk": _to_float(r.get("cpk")), "mean": _to_float(r.get("mean")),
                "stdev": _to_float(r.get("stdev")), "yield": _to_float(r.get("yield_pct")),
                "fail_count": int(r["fail_count"]) if r.get("fail_count") else None,
                "total_count": int(r["total_count"]) if r.get("total_count") else None,
            }, conn=conn)
            label_id = store.insert_label(
                case_id, None, r.get("status"), r.get("root_cause_category"),
                r.get("signature_tag"), 0, 0, r.get("human_comment"),
                "seed_cli", None, "seed", conn=conn)
            store.insert_case_outcome(
                case_id, label_id, r.get("outcome_action"),
                r.get("outcome_condition") or None, r.get("outcome_result"),
                None, None, None, conn=conn)
    print(f"seeded {len(rows)} rows from {path}")


def _cmd_calibrate(argv):
    """`calibrate [--product-type X]` — 누적 features 분위수로 thresholds.yaml 갱신 후 결과 출력."""
    product_type = None
    if "--product-type" in argv:
        product_type = argv[argv.index("--product-type") + 1]
    from . import calibrate
    result = calibrate.recalibrate(product_type=product_type)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def main(argv=None):
    """서브커맨드 디스패처 — init / run / seed / calibrate. 그 밖은 모듈 docstring 을 출력한다."""
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "help"
    if cmd == "init":
        store.init_db()
        print(f"initialized {store.config.DB_PATH}")
    elif cmd == "run":
        _cmd_run(argv)
    elif cmd == "seed":
        _cmd_seed(argv)
    elif cmd == "calibrate":
        _cmd_calibrate(argv)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
