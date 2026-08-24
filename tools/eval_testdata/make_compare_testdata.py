"""Compare 모드 검증용 합성 데이터 생성기 (before/after 쌍) — v1, 2026-08-20.

    server\\.venv\\Scripts\\python.exe tools\\eval_testdata\\make_compare_testdata.py

산출물 3종 (기본 ``data/`` 아래):
    compare_testdata_v1_before.csv   7-meta honeyform (Before)
    compare_testdata_v1_after.csv    7-meta honeyform (After)
    compare_testdata_v1_answer.csv   정답표 (item 별 기대 검출 + 목표/실측 지표)
    compare_testdata_v1_verify.csv   **서버 코드로 직접 돌린** 실측 대조 결과

두 CSV 를 Honey 로 **Compare 세션**(Before/After 배치)으로 업로드하면 Issue Table Compare
탭의 검출 결과가 정답표와 같아야 한다.

## 왜 eval_engine 사다리(L1~L5)와 다른가

`make_eval_testdata.py` 는 **단일 세션 안의 fail item 판정**(eval_engine signature 룰)을
겨냥한다. Compare 는 그 축이 아니다 — 검출 정본은 `web_report/tabs/compare.py _dist_focus`
이고 임계는 4개뿐이다:

    ① 양쪽 Cpk > 100                    → 제외 (여유 과대)
    ② 양쪽 σ 가 0/결측                   → 제외 (고정값)
    ③ 한쪽 Cpk < 1.33                    → **검출** (절대 품질 조건, 유의성 게이트 없음)
    ④ |stdev 증가율| ≥ 15% 이고 p_stdev < 0.05 → **검출**
    그 외                                 → 제외

그래서 레벨을 L1/L2/L3 **3단**으로 잡았다(사용자 지시):
    L1 = 차이 거의 없음 → 검출 안 됨   (양쪽 Cpk 1.4~1.7, |Δσ%| ≤ 6)
    L2 = 검출됨         → 임계 소폭 초과 (한 조건만 살짝 넘김)
    L3 = 심하게 검출됨   → 임계 크게 초과

## 두 가지 결정적 제약

1. **모집단은 Bin1(양품) die 뿐이다** (`compare._bin1_frame`). fail die 를 아무리 넣어도
   dist_shift 통계는 안 변한다 — 산포 차이는 Bin1 값 자체로 만들어야 한다. 그래서 이
   데이터는 대부분의 die 를 Bin1 로 두고, Bin 전이는 소수 die 에만 심는다.
2. **표본 모멘트 재스케일**(`_rescale`). 난수를 그냥 뽑으면 표본 σ 가 목표에서 ±3% 흔들려
   "+18% 겨냥"이 실측 14.9% 로 내려앉아 L2 가 검출되지 않는 일이 생긴다. z-score 정규화
   후 목표 μ·σ 로 되돌려 **모양은 유지하고 모멘트만 정확히** 맞춘다.

pytest 미사용 — 생성 직후 서버 코드(`build_dist_shift`/`build_goodlog`)를 **그대로 호출해**
기대와 대조하고, 불일치가 있으면 exit 1 로 끝난다(재구현 검증 금지 — 규칙 #13).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from web_report.honeyform import META_COLUMNS, META_ROW_LABELS, split_honeyform  # noqa: E402

try:                                              # 한국어 Windows 콘솔(cp949) 깨짐 방지
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ── 공통 규격 ────────────────────────────────────────────────────────────────
LSL, USL = 0.0, 10.0          # 전 항목 공통 spec (limit 변경 항목만 예외)
CENTER = 5.0                  # 중심값
UNIT = "V"
STEP = "P2"

LEVELS = (1, 2, 3)
LEVEL_KO = {1: "차이 거의 없음(미검출)", 2: "검출(임계 소폭 초과)", 3: "심하게 검출"}

# 검출 임계 — compare.py 상수와 같아야 한다. **여기서 다시 판정하지 않는다**(대조용 표기).
TH_CPK_LOW = 1.33
TH_STDEV_DELTA_PCT = 15.0
TH_ALPHA = 0.05
TH_CPK_HIGH = 100.0

# 산포 유형별 case 수 (합계 50) × 레벨 3 = 150 item.
# 유형 코드는 TNO 인코딩에 쓴다 (TNO = 유형×1000 + 레벨×100 + 순번).
SHAPES = [
    # (유형, 코드, case 수, 검출 경로)
    ("mean_shift", 1, 10, "cpk"),      # 평균이 밀려 Cpk 가 떨어진다
    ("sigma_up", 2, 10, "stdev"),      # 산포만 커진다 (Cpk 는 임계 위 유지)
    ("sigma_down", 3, 8, "stdev"),     # 산포가 줄어든다 — |Δσ%| 는 절대값이라 개선도 검출
    ("tail", 4, 8, "stdev"),           # 한쪽 꼬리 오염
    ("bimodal", 5, 8, "stdev"),        # 두 모드로 갈라짐
    ("low_cpk", 6, 6, "cpk"),          # 원래 여유가 없던 항목이 더 나빠진다
]
N_SHAPE_ITEMS = sum(c for _, _, c, _ in SHAPES) * len(LEVELS)   # 150

N_ADDED = 5          # After 에만 (new_items)
N_REMOVED = 5        # Before 에만 (goodlog removed)
N_LIMIT_CHG = 3      # HILIM / LOLIM / 둘 다
N_CONTROL = 37       # 정상 대조군 (before ≈ after, 미검출)

# Bin 전이 die (공통 좌표에서 Before→After Bin 이 달라지는 die)
BIN_PASS_TO_FAIL = 10        # Bin1 → Bin5
BIN_FAIL_TO_PASS = 5         # Bin3 → Bin1
BIN_FAIL_TO_FAIL = 5         # Bin3 → Bin7

WAFER_RADIUS = 18            # die ≈ 1009 (요청: 1000 행 규모)


# ── 값 합성 ──────────────────────────────────────────────────────────────────

def _rescale(v, mean, sd):
    """표본의 실제 평균·표준편차를 목표값으로 정확히 맞춘다(모양 보존).

    이게 없으면 표본 노이즈(σ 추정 CV ≈ 1/√(2(n−1)))로 "+18% 겨냥" 이 실측 14.9% 가 되어
    L2 가 임계 밑으로 떨어진다 — 경계 케이스를 의도대로 고정하는 핵심 장치다.
    """
    v = np.asarray(v, dtype=float)
    cur_sd = v.std(ddof=1)
    if cur_sd == 0:
        return np.full(v.size, mean, dtype=float)
    return (v - v.mean()) / cur_sd * sd + mean


def _shape_values(shape, n, rng, mean, sd):
    """유형별 **모양**만 만든다 — 모멘트는 _rescale 이 최종적으로 맞춘다."""
    if shape == "tail":
        # 한쪽(위쪽) 꼬리 오염: 소수 die 를 크게 띄운다. 값은 나중에 spec 안으로 클립되므로
        # Bin1 die 로 남는다(모집단 유지).
        base = rng.normal(0.0, 1.0, n)
        k = max(1, int(n * 0.04))
        idx = rng.choice(n, size=k, replace=False)
        base[idx] += rng.uniform(2.5, 4.0, k)
        return _rescale(base, mean, sd)
    if shape == "bimodal":
        # 두 봉우리 — 간격은 재스케일 후에도 상대적으로 유지된다.
        half = n // 2
        a = rng.normal(-1.0, 0.45, half)
        b = rng.normal(1.0, 0.45, n - half)
        return _rescale(np.concatenate([a, b]), mean, sd)
    return _rescale(rng.normal(0.0, 1.0, n), mean, sd)


def _sd_for_cpk(cpk, mean=CENTER, lsl=LSL, usl=USL):
    """목표 Cpk 를 만드는 σ. Cpk = min(usl−μ, μ−lsl) / (3σ)."""
    margin = min(usl - mean, mean - lsl)
    return margin / (3.0 * cpk)


def _plan_shape_case(shape, level, before_sd_hint=None):
    """(before μ,σ) / (after μ,σ) 목표값 + 기대 검출 + 근거.

    L1 은 **어느 조건에도 안 걸리게** 만든다: 양쪽 Cpk 를 1.4~1.7 대역에 두고
    (>1.33 이고 <100) |Δσ%| 를 6% 이하로 묶는다.
    """
    b_mu = CENTER
    if shape == "low_cpk":
        b_sd = _sd_for_cpk(1.45)                 # 원래도 여유가 빠듯한 항목
    else:
        b_sd = _sd_for_cpk(1.60)                 # 여유 있는 정상 항목
    if before_sd_hint is not None:
        b_sd = before_sd_hint
    a_mu, a_sd, reason = b_mu, b_sd, "none"

    if shape == "mean_shift":
        # 평균만 민다 → Cpk 가 떨어져 ③ 경로로 검출. σ 는 그대로라 ④ 는 안 탄다.
        a_mu = {1: CENTER + 0.20, 2: _mu_for_cpk(1.24, b_sd), 3: _mu_for_cpk(0.85, b_sd)}[level]
        reason = "cpk" if level > 1 else "none"
    elif shape == "sigma_up":
        a_sd = b_sd * {1: 1.06, 2: 1.19, 3: 1.60}[level]
        reason = "stdev" if level > 1 else "none"
    elif shape == "sigma_down":
        a_sd = b_sd * {1: 0.94, 2: 0.81, 3: 0.55}[level]
        reason = "stdev" if level > 1 else "none"
    elif shape in ("tail", "bimodal"):
        a_sd = b_sd * {1: 1.05, 2: 1.19, 3: 1.55}[level]
        reason = "stdev" if level > 1 else "none"
    elif shape == "low_cpk":
        a_sd = b_sd * {1: 1.02, 2: 1.15, 3: 1.65}[level]
        # L2 부터 Cpk 가 1.33 밑으로 내려간다(1.45/1.15 ≈ 1.26).
        reason = "cpk" if level > 1 else "none"
    return {"before": (b_mu, b_sd), "after": (a_mu, a_sd),
            "expect_focus": level > 1, "reason": reason}


def _mu_for_cpk(cpk, sd, lsl=LSL, usl=USL):
    """목표 Cpk 를 만드는 평균 (중심에서 **위쪽**으로 민다)."""
    return usl - 3.0 * sd * cpk


# ── 웨이퍼 좌표 ──────────────────────────────────────────────────────────────

def build_wafer(radius: int):
    """반경 radius 안의 정수격자 die 좌표 (0-based 양수로 옮겨 반환).

    ⚠ XPOS/YPOS 는 실데이터에서 **항상 양수**다(CLAUDE.md 규칙 #9) — 음수 좌표를 쓰면
    합성 데이터가 실데이터가 아니게 된다.
    """
    xs, ys = [], []
    for y in range(-radius, radius + 1):
        for x in range(-radius, radius + 1):
            if x * x + y * y <= radius * radius:
                xs.append(x + radius)
                ys.append(y + radius)
    return np.asarray(xs, dtype=int), np.asarray(ys, dtype=int)


# ── item 계획 ────────────────────────────────────────────────────────────────

def build_plan(rng):
    """전 item 의 (이름, 소속, 목표 파라미터, 기대) 목록."""
    items = []
    seq = 0

    for shape, code, count, path in SHAPES:
        for i in range(count):
            for level in LEVELS:
                seq += 1
                plan = _plan_shape_case(shape, level)
                items.append({
                    "name": f"{shape.upper()}_{i + 1:02d}_L{level}",
                    "group": "shape", "shape": shape, "level": level,
                    "tno": code * 1000 + level * 100 + (i + 1),
                    "lsl_b": LSL, "usl_b": USL, "lsl_a": LSL, "usl_a": USL,
                    "in_before": True, "in_after": True,
                    **plan,
                })

    # 정상 대조군 — before/after 파라미터가 같다(난수만 다시 뽑는다).
    for i in range(N_CONTROL):
        sd = _sd_for_cpk(1.60)
        items.append({
            "name": f"CONTROL_{i + 1:02d}", "group": "control", "shape": "normal",
            "level": 0, "tno": 7000 + i + 1,
            "lsl_b": LSL, "usl_b": USL, "lsl_a": LSL, "usl_a": USL,
            "in_before": True, "in_after": True,
            "before": (CENTER, sd), "after": (CENTER, sd),
            "expect_focus": False, "reason": "none",
        })

    # After 에만 있는 신규 item → compare.new_items + Issue Table Compare "신규" 행
    for i in range(N_ADDED):
        sd = _sd_for_cpk(1.55)
        items.append({
            "name": f"ADDED_ITEM_{i + 1:02d}", "group": "added", "shape": "normal",
            "level": 0, "tno": 8100 + i + 1,
            "lsl_b": None, "usl_b": None, "lsl_a": LSL, "usl_a": USL,
            "in_before": False, "in_after": True,
            "before": None, "after": (CENTER, sd),
            "expect_focus": False, "reason": "new_item",
        })

    # Before 에만 있는 삭제 item → goodlog removed
    for i in range(N_REMOVED):
        sd = _sd_for_cpk(1.55)
        items.append({
            "name": f"REMOVED_ITEM_{i + 1:02d}", "group": "removed", "shape": "normal",
            "level": 0, "tno": 8200 + i + 1,
            "lsl_b": LSL, "usl_b": USL, "lsl_a": None, "usl_a": None,
            "in_before": True, "in_after": False,
            "before": (CENTER, sd), "after": None,
            "expect_focus": False, "reason": "removed_item",
        })

    # Limit 변경 3건 — HILIM / LOLIM / 둘 다. 값 분포는 그대로 두고 규격만 바꾼다.
    # After Cpk 가 임계 위에 남도록 σ 를 넉넉히 잡아 **limit 변경만** 검출되게 한다.
    lim_specs = [
        ("HI", LSL, USL, LSL, 9.0),
        ("LO", LSL, USL, 1.0, USL),
        ("BOTH", LSL, USL, 0.8, 9.2),
    ]
    for i, (kind, lb, ub, la, ua) in enumerate(lim_specs[:N_LIMIT_CHG]):
        # After 기준으로도 Cpk ≥ 1.4 가 되게 σ 를 잡는다(좁아진 규격 기준).
        margin_a = min(ua - CENTER, CENTER - LSL if la == LSL else CENTER - la)
        sd = margin_a / (3.0 * 1.45)
        items.append({
            "name": f"LIMIT_CHG_{kind}", "group": "limit_change", "shape": "normal",
            "level": 0, "tno": 8300 + i + 1,
            "lsl_b": lb, "usl_b": ub, "lsl_a": la, "usl_a": ua,
            "in_before": True, "in_after": True,
            "before": (CENTER, sd), "after": (CENTER, sd),
            "expect_focus": False, "reason": "limit_change",
        })
    return items


# ── 프레임 조립 ──────────────────────────────────────────────────────────────

def build_frame(items, side, xs, ys, bins, rng):
    """한쪽(before/after) 7-meta honeyform DataFrame."""
    n = xs.size
    key_in = "in_before" if side == "before" else "in_after"
    key_par = "before" if side == "before" else "after"
    key_lsl = "lsl_b" if side == "before" else "lsl_a"
    key_usl = "usl_b" if side == "before" else "usl_a"

    present = [it for it in items if it[key_in]]
    meta_rows = {k: {} for k in META_ROW_LABELS}
    cols = {}
    for tseq, it in enumerate(present, start=1):
        mu, sd = it[key_par]
        v = _shape_values(it["shape"], n, rng, mu, sd)
        lsl, usl = it[key_lsl], it[key_usl]
        # spec 안으로 클립 — 값이 규격을 벗어나면 그 die 는 fail 이어야 하는데
        # (make_eval_testdata 의 불변 법칙), 여기서는 Bin1 모집단을 유지하는 것이 목적이라
        # 애초에 규격 밖 값을 만들지 않는다. 클립 폭은 규격에서 아주 살짝 안쪽.
        pad = (usl - lsl) * 0.001
        v = np.clip(v, lsl + pad, usl - pad)
        cols[it["name"]] = [f"{x:.6f}" for x in v]
        meta_rows["TSEQ"][it["name"]] = str(tseq)
        meta_rows["TNO"][it["name"]] = str(it["tno"])
        meta_rows["STEP"][it["name"]] = STEP
        meta_rows["UNIT"][it["name"]] = UNIT
        meta_rows["HILIM"][it["name"]] = f"{usl:g}"
        meta_rows["LOLIM"][it["name"]] = f"{lsl:g}"

    names = [it["name"] for it in present]
    head = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row["SERIAL"] = label
        row.update({nm: meta_rows[label].get(nm, "") for nm in names})
        head.append(row)

    body = {
        "SERIAL": [f"C{i:06d}" for i in range(n)],
        "SHOT": [str(i // 4 + 1) for i in range(n)],
        "DUT": [str(i % 4 + 1) for i in range(n)],
        "XPOS": [str(int(v)) for v in xs],
        "YPOS": [str(int(v)) for v in ys],
        "BIN": [str(int(b)) for b in bins],
        # fail die 의 FAILTNO — 이 데이터의 목적은 산포 비교라 대표 항목 하나로만 찍는다.
        "FAILTNO": ["" if int(b) == 1 else str(present[0]["tno"]) for b in bins],
    }
    body.update(cols)
    return pd.concat([pd.DataFrame(head, columns=META_COLUMNS + names),
                      pd.DataFrame(body, columns=META_COLUMNS + names)],
                     ignore_index=True)


def build_bins(n, rng):
    """(before bins, after bins) — 대부분 Bin1, 소수 die 만 전이시킨다.

    dist_shift 모집단이 Bin1 뿐이라 전이 die 는 통계에 거의 영향이 없다(±20/1000).
    """
    before = np.ones(n, dtype=int)
    after = np.ones(n, dtype=int)
    need = BIN_PASS_TO_FAIL + BIN_FAIL_TO_PASS + BIN_FAIL_TO_FAIL
    idx = rng.choice(n, size=need, replace=False)
    p = 0
    for _ in range(BIN_PASS_TO_FAIL):            # Bin1 → Bin5 (Pass→Fail)
        before[idx[p]] = 1; after[idx[p]] = 5; p += 1
    for _ in range(BIN_FAIL_TO_PASS):            # Bin3 → Bin1 (Fail→Pass)
        before[idx[p]] = 3; after[idx[p]] = 1; p += 1
    for _ in range(BIN_FAIL_TO_FAIL):            # Bin3 → Bin7 (Fail→Fail, 불일치)
        before[idx[p]] = 3; after[idx[p]] = 7; p += 1
    return before, after


# ── 검증 (서버 코드 직접 호출 — 재구현 금지) ────────────────────────────────

def verify(df_before, df_after, items):
    """실제 서버 경로로 검출을 계산해 기대와 대조. (rows, 불일치 수) 반환."""
    from web_report.tabs.compare import build_dist_shift, build_goodlog
    from web_report.tabs.cpk import build_cpk_rows

    t_before = split_honeyform(df_before, source="WF_BEFORE", file_name="before.csv")
    t_after = split_honeyform(df_after, source="WF_AFTER", file_name="after.csv")
    stat_items = sorted(set(t_after.item_columns) | set(t_before.item_columns))
    cpk_rows = build_cpk_rows([t_after, t_before], stat_items)
    dist = build_dist_shift([t_after, t_before], cpk_rows)
    gl = build_goodlog(t_after, t_before)

    by_item = {r["subject"]: r for r in dist["rows"]}
    a_set, b_set = set(t_after.item_columns), set(t_before.item_columns)
    new_items = sorted(a_set - b_set)
    gone_items = sorted(b_set - a_set)
    lim_changed = set((gl or {}).get("limit_change_map") or {})

    rows, bad = [], 0
    for it in items:
        name = it["name"]
        r = by_item.get(name)
        actual = bool(r and r["focus"])
        expect = bool(it["expect_focus"])
        # 존재 여부 기대 — 신규/삭제 item 은 dist_shift 대상이 아니다(공통 항목만).
        if it["group"] == "added":
            ok = (name in new_items) and (r is None)
            note = "new_items 포함" if ok else "new_items 누락"
        elif it["group"] == "removed":
            ok = (name in gone_items) and (r is None)
            note = "goodlog removed" if ok else "removed 누락"
        elif it["group"] == "limit_change":
            ok = (name in lim_changed) and actual == expect
            note = "limit_change_map 포함" if ok else "limit 변경 미검출"
        else:
            ok = actual == expect
            note = "" if ok else ("미검출(기대: 검출)" if expect else "오검출(기대: 미검출)")
        if not ok:
            bad += 1
        rows.append({
            "item": name, "group": it["group"], "shape": it["shape"],
            "level": it["level"], "level_ko": LEVEL_KO.get(it["level"], ""),
            "expect_focus": int(expect), "actual_focus": int(actual),
            "expect_reason": it["reason"],
            "before_avg": (r or {}).get("before", {}).get("average"),
            "before_stdev": (r or {}).get("before", {}).get("stdev"),
            "before_cpk": (r or {}).get("before", {}).get("cpk"),
            "after_avg": (r or {}).get("after", {}).get("average"),
            "after_stdev": (r or {}).get("after", {}).get("stdev"),
            "after_cpk": (r or {}).get("after", {}).get("cpk"),
            "stdev_delta_pct": (r or {}).get("stdev_delta_pct"),
            "meanshift_sigma": (r or {}).get("meanshift_sigma"),
            "p_stdev": (r or {}).get("p_stdev"),
            "ok": int(ok), "note": note,
        })

    counts = {
        "dist_total": dist["summary"]["total"], "dist_focus": dist["summary"]["focus"],
        "new_items": len(new_items), "removed_items": len(gone_items),
        "limit_changed": len(lim_changed),
    }
    return rows, bad, counts


def answer_rows(items):
    out = []
    for it in items:
        b = it.get("before") or (None, None)
        a = it.get("after") or (None, None)
        out.append({
            "item": it["name"], "group": it["group"], "shape": it["shape"],
            "level": it["level"], "level_ko": LEVEL_KO.get(it["level"], ""),
            "tno": it["tno"], "unit": UNIT,
            "before_lsl": it["lsl_b"], "before_usl": it["usl_b"],
            "after_lsl": it["lsl_a"], "after_usl": it["usl_a"],
            "target_before_mean": b[0], "target_before_stdev": b[1],
            "target_after_mean": a[0], "target_after_stdev": a[1],
            "expect_focus": int(bool(it["expect_focus"])), "expect_reason": it["reason"],
            "th_cpk_low": TH_CPK_LOW, "th_stdev_delta_pct": TH_STDEV_DELTA_PCT,
            "th_alpha": TH_ALPHA, "th_cpk_high": TH_CPK_HIGH,
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Compare 모드 검증용 before/after 합성 데이터")
    ap.add_argument("--out-dir", default=str(_ROOT / "data"))
    ap.add_argument("--prefix", default="compare_testdata_v1")
    ap.add_argument("--radius", type=int, default=WAFER_RADIUS)
    ap.add_argument("--seed", type=int, default=20260820)
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    xs, ys = build_wafer(args.radius)
    n = xs.size
    bins_b, bins_a = build_bins(n, rng)
    items = build_plan(rng)

    df_before = build_frame(items, "before", xs, ys, bins_b, rng)
    df_after = build_frame(items, "after", xs, ys, bins_a, rng)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p_before = out_dir / f"{args.prefix}_before.csv"
    p_after = out_dir / f"{args.prefix}_after.csv"
    p_answer = out_dir / f"{args.prefix}_answer.csv"
    p_verify = out_dir / f"{args.prefix}_verify.csv"

    df_before.to_csv(p_before, index=False, encoding="utf-8-sig")   # Excel 용 BOM
    df_after.to_csv(p_after, index=False, encoding="utf-8-sig")
    pd.DataFrame(answer_rows(items)).to_csv(p_answer, index=False, encoding="utf-8-sig")

    n_items_b = sum(1 for it in items if it["in_before"])
    n_items_a = sum(1 for it in items if it["in_after"])
    print(f"[생성] die {n} · item before {n_items_b} / after {n_items_a}")
    print(f"  {p_before}")
    print(f"  {p_after}")
    print(f"  {p_answer}")

    if args.no_verify:
        return 0

    rows, bad, counts = verify(df_before, df_after, items)
    pd.DataFrame(rows).to_csv(p_verify, index=False, encoding="utf-8-sig")
    print(f"  {p_verify}")
    print(f"[검증] 공통 항목 {counts['dist_total']} · 검출 {counts['dist_focus']} · "
          f"신규 {counts['new_items']} · 삭제 {counts['removed_items']} · "
          f"limit 변경 {counts['limit_changed']}")
    if bad:
        print(f"[실패] 기대와 다른 항목 {bad}개 — {p_verify} 의 ok=0 행을 보세요")
        for r in rows:
            if not r["ok"]:
                print(f"   - {r['item']:28s} {r['note']} "
                      f"(Δσ%={r['stdev_delta_pct']}, cpk_a={r['after_cpk']})")
        return 1
    print(f"[성공] 전 {len(rows)}개 항목이 기대와 일치")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
