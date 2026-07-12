"""CSV(신규 정본 raw_df 포맷) → evaluate() → 콘솔 결과 testbench.

사용법:
  python tools/testbench_eval.py <csv> [--meta <meta.json>] [--persist]

동작:
  - CSV(레이아웃: SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO + TSEQ/TNO/UNIT/HILIM/LOLIM 메타행)를
    pandas DataFrame(raw_df)으로 읽어 evaluate() 에 넘긴다.
  - meta 는 --meta > 사이드카 <csv>.meta.json > 내장 기본값 순으로 로드.
  - 기본은 persist=False(preview, DB 미접근). --persist 시 임시 eval_testbench.db 에 적재.
  - case 별 status/signature/comment/precedents + 분포 요약을 콘솔에 출력.

다른 CSV 도 같은 포맷이면 경로만 바꿔 재사용.
"""
import argparse
import json
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:  # Windows 콘솔(cp949)에서 한국어 출력 보장
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

DEFAULT_META = {
    "product_name": "TESTBENCH_001",
    "product_type": "PMIC",
    "family_product": "SOC",
    "revision": 0.0,
    "lot_id": "TB_LOT",
    "wafer_number": 1,
    "ingested_by": "testbench_eval",
}


def load_meta(csv_path, meta_arg):
    meta = dict(DEFAULT_META)
    sidecar = os.path.splitext(csv_path)[0] + ".meta.json"
    path = meta_arg or (sidecar if os.path.exists(sidecar) else None)
    if path:
        with open(path, encoding="utf-8") as f:
            meta.update(json.load(f))
        meta["_meta_source"] = path
    else:
        meta["_meta_source"] = "(default)"
    meta.setdefault("source_file", os.path.basename(csv_path))
    return meta


def read_raw_df(csv_path):
    """정본 CSV → raw_df DataFrame. item 측정행만 숫자로 변환(메타행은 문자열 유지)."""
    import pandas as pd
    raw = pd.read_csv(csv_path, header=0, dtype=str, keep_default_na=False,
                      na_values=[""])
    if len(raw.columns) <= 7:
        raise ValueError("컬럼이 8개 미만 — 정본 포맷(meta 7 + item) 이 아님")
    if len(raw) < 6:
        raise ValueError("행이 부족 — 메타행 5(TSEQ/TNO/UNIT/HILIM/LOLIM) + 측정행 필요")
    item_cols = list(raw.columns[7:])
    meta_part = raw.iloc[:5]
    data_part = raw.iloc[5:].copy()
    for c in item_cols:
        data_part[c] = pd.to_numeric(data_part[c], errors="coerce")
    return pd.concat([meta_part, data_part], ignore_index=True)


def _fmt_precedents(precs):
    """precedent 전체를 (헤더, comment) 튜플로 반환 — cap 없음."""
    from eval_engine.pipeline._rules import outcome_label
    out = []
    for p in precs:
        if not p.get("action") and not p.get("result") and not p.get("comment"):
            continue
        prod = p.get("product_name")
        fam = p.get("family_product")
        prefix = ""
        if prod:
            prefix = f"{prod}({fam})@ " if fam else f"{prod}@ "
        act = outcome_label("action", p.get("action")).get("ko") or p.get("action")
        res = outcome_label("result", p.get("result")).get("ko") or p.get("result")
        out.append((prefix + f"{act}→{res}", p.get("comment")))
    return out


def print_report(result, meta, csv_path):
    cases = result["cases"]
    print("=" * 72)
    print(f"source     : {csv_path}")
    print(f"product    : {meta.get('product_name')} "
          f"({meta.get('product_type')}/{meta.get('family_product')}) "
          f"lot={meta.get('lot_id')} wafer={meta.get('wafer_number')}")
    print(f"meta source: {meta.get('_meta_source')}")
    print(f"run_id     : {result['run_id']}  |  engine={result['engine_version']}"
          f"  |  fail_case={len(cases)}")
    print("=" * 72)

    grouped = {}
    for c in cases:
        grouped.setdefault(c["item_canonical"], []).append(c)

    for item_name, item_cases in grouped.items():
        print(f"\n[{item_name}]")
        for c in item_cases:
            print(f"   bin={c['bin']}  class={c['item_class']}")
            print(f"   status={c['status']}  confidence={c['confidence']}"
                  f"  completeness={c['data_completeness']}")
            for s in c["signatures"]:
                tag = f"{s['id']} (primary)" if s["role"] == "primary" else s["id"]
                print(f"   - {tag}")
                ev = ", ".join(f"{e['signal_code']}={e['value']}" for e in s["evidence"])
                if ev:
                    print(f"       evidence : {ev}")
                if s.get("action_ko"):
                    print(f"       action   : {s['action_ko']}")
            print(f"   comment  : {c['comment']}")
            precs = _fmt_precedents(c["precedents"])
            for i, (head, cmt) in enumerate(precs, 1):
                print(f"   precedent{i}: {head}")
                if cmt:
                    print(f"       comment : {cmt}")

    print("\n" + "-" * 72)
    status_c = Counter(c["status"] for c in cases)
    sig_c = Counter(c["primary_signature"] for c in cases)
    print("status 분포     :", dict(status_c))
    print("signature 분포  :", dict(sig_c))
    print("=" * 72)


def main(argv=None):
    ap = argparse.ArgumentParser(description="raw_df CSV → evaluate() testbench")
    ap.add_argument("csv", help="정본 raw_df 포맷 CSV 경로")
    ap.add_argument("--meta", help="meta.json 경로 (기본: 사이드카 <csv>.meta.json)")
    ap.add_argument("--persist", action="store_true",
                    help="임시 eval_testbench.db 에 적재 (기본: preview)")
    args = ap.parse_args(argv)

    if args.persist:
        os.environ.setdefault(
            "EVAL_DB_PATH", os.path.join(ROOT, "data", "eval_testbench.db"))

    from eval_engine import api

    meta = load_meta(args.csv, args.meta)
    raw_df = read_raw_df(args.csv)
    run_input = {"meta": {k: v for k, v in meta.items() if not k.startswith("_")},
                 "raw_df": raw_df}
    result = api.evaluate(run_input, persist=args.persist)
    print_report(result, meta, args.csv)


if __name__ == "__main__":
    main()
