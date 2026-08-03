"""신규 정본 raw_df 포맷 샘플 CSV 생성기.

레이아웃(docs/REPORT_GENERATOR_DATA_REQUEST §1):
  columns : SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO, <item...>
  row0 TSEQ / row1 TNO / row2 UNIT / row3 HILIM(USL) / row4 LOLIM(LSL) / row5+ 측정
fail 식별(§2): serial 의 FAILTNO == item 의 TNO → 그 item·그 serial BIN 이 fail case.
제약: FAILTNO 는 serial 당 1개(stop-on-fail) → fail DUT 는 정확히 한 item 에서만 fail.

실행:  python tools/make_sample_raw_df.py
출력:  samples/sample_raw_df.csv  +  samples/sample_raw_df.meta.json
"""
import csv
import json
import math
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")
CSV_PATH = os.path.join(SAMPLES, "sample_raw_df.csv")
META_PATH = os.path.join(SAMPLES, "sample_raw_df.meta.json")

random.seed(20260702)

META_COLS = ["SERIAL", "SHOT", "DUT", "XPOS", "YPOS", "BIN", "FAILTNO"]
PASS_BIN = 1
N_DUT = 150

# item 정의: (name, unit, tno, lsl, usl, fail_bin, archetype)
ITEMS = [
    ("VREF_TRIM",    "V",    101, 1.0,  1.4,  18, "severe_outlier"),
    ("IDDQ_INIT",    "A",    102, 0.0,  15.0, 18, "edge_fail"),
    ("TRIM_BUCK_GM", "CODE", 103, 20.0, 40.0, 20, "wide_low_cpk"),
    ("BUCK_SCAN",    "P_F",  104, None, None, 40, "gross_fail"),
]


def _grid(n):
    """n 개 DUT 격자 좌표 + 반경."""
    side = int(math.ceil(math.sqrt(n)))
    c = (side - 1) / 2.0
    pts = []
    for i in range(n):
        x, y = i % side, i // side
        pts.append((x, y, math.hypot(x - c, y - c)))
    return pts


PTS = _grid(N_DUT)
RMAX = max(r for _, _, r in PTS)


def _nominal(item):
    """pass 측정값(spec 중앙 근처). P_F 는 0(=pass)."""
    _, unit, _, lsl, usl, _, _ = item
    if lsl is None or usl is None:
        return 0.0
    mid = (lsl + usl) / 2.0
    return random.gauss(mid, (usl - lsl) * 0.08)


def _fail_value(item):
    """fail 측정값(아키타입별)."""
    name, unit, tno, lsl, usl, fbin, arch = item
    if arch == "gross_fail":       # P_F: limit 없음, 값은 1(=fail 표식)
        return 1.0
    if arch == "wide_low_cpk":     # 넓은 산포로 usl 밖
        return usl + (usl - lsl) * 0.2
    return usl + (usl - lsl) * 0.25  # severe_outlier / edge_fail: usl 크게 초과


def _pick_fail_duts():
    """DUT index → (fail 시킬 item index) 배정. 한 DUT 는 최대 1 item fail."""
    assign = {}          # dut_idx -> item_idx
    used = set()
    for it_idx, item in enumerate(ITEMS):
        arch = item[6]
        # 후보 DUT 풀 (아키타입별 공간 특성)
        if arch == "edge_fail":
            pool = [i for i, (_, _, r) in enumerate(PTS) if r >= RMAX * 0.8]
            k = max(8, int(len(pool) * 0.6))
        elif arch == "gross_fail":
            pool = list(range(N_DUT))
            k = int(N_DUT * 0.55)           # >50% → GROSS_FAIL
        else:                                # severe_outlier / wide_low_cpk
            pool = list(range(N_DUT))
            k = 10
        pool = [i for i in pool if i not in used]
        random.shuffle(pool)
        chosen = pool[:k]
        for i in chosen:
            assign[i] = it_idx
            used.add(i)
    return assign


def build_rows():
    assign = _pick_fail_duts()
    # 메타행 5개 (item 컬럼에만 값, meta 7컬럼은 태그/공란)
    def meta_row(tag, cellfn):
        return [tag, "", "", "", "", "", ""] + [cellfn(it) for it in ITEMS]

    header = META_COLS + [it[0] for it in ITEMS]
    rows = [header]
    rows.append(meta_row("TSEQ", lambda it: it[2]))                       # TSEQ≈tno (미사용)
    rows.append(meta_row("TNO",  lambda it: it[2]))
    rows.append(meta_row("UNIT", lambda it: it[1]))
    rows.append(meta_row("HILIM", lambda it: "" if it[4] is None else it[4]))
    rows.append(meta_row("LOLIM", lambda it: "" if it[3] is None else it[3]))

    for dut in range(N_DUT):
        x, y, _ = PTS[dut]
        fail_item = assign.get(dut)
        if fail_item is None:
            b, failtno = PASS_BIN, ""
        else:
            b, failtno = ITEMS[fail_item][5], ITEMS[fail_item][2]
        vals = []
        for it_idx, item in enumerate(ITEMS):
            if it_idx == fail_item:
                vals.append(round(_fail_value(item), 4))
            else:
                vals.append(round(_nominal(item), 4))
        rows.append([dut + 1, 1, dut + 1, x, y, b, failtno] + vals)
    return rows


def main():
    os.makedirs(SAMPLES, exist_ok=True)
    rows = build_rows()
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)

    meta = {
        "product_name": "S5E_SAMPLE_001",
        "product_type": "PMIC",
        "family_product": "SOC",
        "revision": 0.0,
        "lot_id": "SMPLOT01",
        "wafer_number": 1,
        "source_file": "sample_raw_df.csv",
        "ingested_by": "make_sample_raw_df",
    }
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    n_fail = sum(1 for r in rows[6:] if r[6] != "")
    print(f"wrote {CSV_PATH}  ({len(rows)-6} DUT, {n_fail} fail DUT, {len(ITEMS)} items)")
    print(f"wrote {META_PATH}")


if __name__ == "__main__":
    main()
