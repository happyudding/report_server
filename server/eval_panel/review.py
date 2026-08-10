"""Eval 표본함 — 룰별 소수 표본 검수 + 승인형 임계값 추천. `/pe/eval` 표본함 탭의 파일 계층.

**왜 이게 필요한가.** 세션 하나를 트레이스하면 발화가 수백 건이라 사람이 하나씩 볼 수
없다. 그렇다고 안 보면 "이 룰이 과하게 뜨는가" 를 판단할 근거가 없다. 그래서 룰마다
**최대 8건**(임계값 경계 3 / 중간 3 / 극단 2)만 뽑아 맞음·과다발화만 찍게 하고, 그
라벨이 일정량 쌓이면 임계값 **강화안**을 계산해 사람이 승인하면 적용한다.

설계상 못박은 것 3가지:
- **자동 반영 없음.** 추천은 계산까지만 하고, 적용은 기존 Thresholds 저장 API 를 그대로
  타서 백업·rules_rev·낙관적 잠금·감사 로그를 전부 거친다.
- **강화 방향만.** 느슨하게 만들어 **새 발화를 늘리는** 추천은 v1 에서 만들지 않는다 —
  검수하지 않은 케이스가 새로 뜨면 그건 라벨로 검증된 변경이 아니다.
- **룰별 기준 하드코딩 금지.** 층화 기준 metric 은 그 룰의 `when_metric` 첫 조건에서
  자동으로 뽑고, 그게 판정 기준이 아닌 룰(SUBPOP_GAP)만 yaml `review_metric` 으로 지목한다.

DB 스키마는 바꾸지 않는다 — 라벨은 기존 `label` 테이블에 `labeler='eval-review'` 로 넣고,
`human_status` 는 비운다(그래야 전체 status 채점 `eval_admin.scoring()` 과 섞이지 않는다).
"""
from __future__ import annotations

import logging
import re

from web_report import eval_debug, eval_export

logger = logging.getLogger(__name__)

REVIEW_LABELER = "eval-review"
SNAPSHOT_INGESTED_BY = "eval-snapshot"

# 룰당 표본 구성 — 경계(임계값 바로 위) / 중간 / 극단. 합 8건.
SAMPLE_EDGE = 3
SAMPLE_MID = 3
SAMPLE_FAR = 2
SAMPLE_MAX = SAMPLE_EDGE + SAMPLE_MID + SAMPLE_FAR

# 추천 생성 게이트 — 표본이 적으면 어떤 임계값을 골라도 우연이다.
MIN_LABELS = 20
MIN_PER_SIDE = 5
TARGET_PRECISION = 0.90

_COND_RE = re.compile(r"^(abs)?\s*([<>]=?)\s*(.+)$")

# 조회에 실을 지표 — when_metric 이 참조할 수 있는 저장 컬럼 전부.
_METRIC_COLS = ("spread_norm", "skewness", "kurtosis", "outlier_ratio", "bimodality_score",
                "density_gap", "cdf_gap", "spec_margin_low", "spec_margin_high",
                "limit_hit_ratio", "edge_fail_ratio", "center_fail_ratio",
                "quadrant_imbalance", "n_dut", "site_cpk_delta", "code_edge_hit",
                "ring_fail_ratio", "radial_gradient_norm", "x_gradient_norm",
                "y_gradient_norm", "n_modes", "modality_v2")
_RAW_COLS = ("cpk", "mean", "stdev", "min", "max", "fail_count", "total_count")


class ReviewError(ValueError):
    """사용자에게 그대로 보여줄 실패 (라우트가 400 으로 변환)."""


# ── 지표 조립 ────────────────────────────────────────────────────────────────

def _derived(row: dict) -> dict:
    """저장하지 않는 파생 지표 — 엔진 `signatures.build_ctx_values` 와 같은 식.

    조건이 이 이름들을 참조하므로 표본 정렬·재판정에서도 같은 값을 써야 한다.
    (`limit_missing` 은 lsl/usl 이 저장되지 않아 재현할 수 없으므로 뺀다 — 그 값을 쓰는
    MISSING_LIMIT 은 현재 비활성이고, 활성화되면 그 룰만 표본 정렬에서 빠진다.)
    """
    out = dict(row)
    low, high = row.get("spec_margin_low"), row.get("spec_margin_high")
    margins = [m for m in (low, high) if m is not None]
    if margins:
        out["spec_margin_min"] = min(margins)
    if low is not None and high is not None and (low + high) > 0:
        out["center_bias"] = (high - low) / (low + high)
    ratio, n_dut = row.get("outlier_ratio"), row.get("n_dut")
    if ratio is not None and n_dut:
        out["outlier_count"] = round(ratio * n_dut)
    grads = [abs(row[k]) for k in ("radial_gradient_norm", "x_gradient_norm", "y_gradient_norm")
             if row.get(k) is not None]
    if grads:
        out["gradient_norm_abs_max"] = max(grads)
    return out


def _rule_criterion(sig: dict, thresholds: dict):
    """이 룰의 표본 층화·재판정 기준 → (metric, op, threshold_key) | None.

    우선순위: yaml `review_metric`(판정 기준이 when_metric 이 아닌 룰용) → `when_metric`
    중 **임계값 키를 참조하는** 첫 조건. 리터럴 비교(">0")는 옮길 값이 없으므로 건너뛴다.
    """
    review = sig.get("review_metric") or {}
    for metric, key in review.items():
        if key in thresholds:
            return str(metric), ">", str(key)
    for metric, cond in (sig.get("when_metric") or {}).items():
        m = _COND_RE.match(str(cond).strip())
        if not m:
            continue
        ref = m.group(3).strip()
        if ref in thresholds:
            return str(metric), m.group(2), ref
    return None


def _exceedance(actual, op: str, threshold) -> float | None:
    """임계값을 얼마나 넘었나 — 크면 명백, 0 에 가까우면 경계. 방향 무관 단일 축.

    ">" 계열은 actual/threshold - 1, "<" 계열은 1 - actual/threshold 로 부호를 맞춘다.
    threshold 가 0 이면 비율이 성립하지 않으므로 절대차로 떨어뜨린다.
    """
    if actual is None or threshold is None:
        return None
    try:
        actual, threshold = float(actual), float(threshold)
    except (TypeError, ValueError):
        return None
    if threshold == 0:
        return actual if op.startswith(">") else -actual
    ratio = actual / threshold
    return ratio - 1.0 if op.startswith(">") else 1.0 - ratio


def _passes(actual, op: str, threshold) -> bool:
    """조건 1개 재판정 — 엔진 `signatures._eval_condition` 과 같은 의미(결측=False)."""
    if actual is None or threshold is None:
        return False
    try:
        actual, threshold = float(actual), float(threshold)
    except (TypeError, ValueError):
        return False
    return {">": actual > threshold, ">=": actual >= threshold,
            "<": actual < threshold, "<=": actual <= threshold}.get(op, False)


# ── 표본 조회 ────────────────────────────────────────────────────────────────

_QUEUE_SQL = f"""
SELECT ev.eval_id, ev.case_id, ev.run_id, ev.status, ev.confidence,
       ev.data_completeness, ev.engine_version,
       fc.bin, fc.item_class, fc.product_name, fc.lot_id, fc.revision,
       im.item_name_raw, im.item_canonical, im.value_type, im.unit,
       pm.product_type, pm.family_product,
       ir.session_id, ir.analysis_key, ir.source_file,
       {','.join('rm.' + c for c in _RAW_COLS)}, rm."yield" AS yield_rate,
       {','.join('f.' + c for c in _METRIC_COLS)}
  FROM case_signature cs
  JOIN evaluation ev ON ev.eval_id = cs.eval_id
  JOIN ingest_run ir ON ir.run_id = ev.run_id
  JOIN fail_case fc ON fc.case_id = ev.case_id
  JOIN item_master im ON im.item_id = fc.item_id
  LEFT JOIN product_master pm ON pm.product_name = fc.product_name
  LEFT JOIN raw_metrics rm ON rm.case_id = ev.case_id AND rm.run_id = ev.run_id
  LEFT JOIN features f ON f.case_id = ev.case_id AND f.run_id = ev.run_id
                      AND f.engine_version = ev.engine_version
 WHERE cs.signature = ? AND ir.ingested_by = ?
"""


def _fetch_rule_rows(conn, signature: str, product_type=None, family_product=None,
                     unlabeled_only=True) -> list:
    """이 룰이 발화한 스냅샷 case 들. 최신 eval 우선, (analysis_key,item,bin) 중복 제거.

    같은 데이터를 여러 번 올린 dedup 형제 세션이 표본을 통째로 채우는 것을 막는다 —
    판정이 같으므로 검수해도 새로 알게 되는 것이 없다.
    """
    sql, params = _QUEUE_SQL, [signature, SNAPSHOT_INGESTED_BY]
    if product_type:
        sql += " AND pm.product_type = ?"
        params.append(product_type)
    if family_product:
        sql += " AND pm.family_product = ?"
        params.append(family_product)
    if unlabeled_only:
        sql += (" AND NOT EXISTS (SELECT 1 FROM label l WHERE l.eval_id = ev.eval_id"
                "                   AND l.labeler = ?)")
        params.append(REVIEW_LABELER)
    sql += " ORDER BY ev.eval_id DESC"

    seen, rows = set(), []
    for r in conn.execute(sql, params):
        row = dict(r)
        key = (row.get("analysis_key"), row.get("item_canonical"), row.get("bin"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(_derived(row))
    return rows


def _stratify(rows: list, metric: str, op: str, threshold) -> list:
    """경계 3 / 중간 3 / 극단 2 로 층화 추출. 정렬 키를 고정해 **재현 가능**하게 한다.

    경계만 보면 "애매한 것" 만 보게 되고 극단만 보면 "당연한 것" 만 본다. 둘 다 섞어야
    임계값을 어디로 옮길지 판단할 수 있다.
    ⚠ 이렇게 뽑은 표본의 precision 은 **전체 precision 이 아니다** — 경계 구간을 일부러
    과대표집했으므로 실제보다 낮게 나온다(화면·추천에 그렇게 표기한다).
    """
    scored = []
    for row in rows:
        exc = _exceedance(row.get(metric), op, threshold)
        if exc is None:
            continue
        row = dict(row)
        row["_exceedance"] = exc
        row["_metric"] = metric
        row["_metric_value"] = row.get(metric)
        row["_threshold_key"] = None      # 호출자가 채운다
        scored.append(row)
    # 동점에서 순서가 흔들리면 같은 화면을 두 번 열 때 표본이 달라진다 → case_id 로 고정.
    scored.sort(key=lambda r: (r["_exceedance"], str(r.get("case_id") or "")))
    if len(scored) <= SAMPLE_MAX:
        return scored

    picked, used = [], set()

    def take(idx):
        if 0 <= idx < len(scored) and idx not in used:
            used.add(idx)
            picked.append(scored[idx])

    for i in range(SAMPLE_EDGE):                      # 임계값 바로 위
        take(i)
    for i in range(1, SAMPLE_FAR + 1):                # 가장 명백한 쪽
        take(len(scored) - i)
    mid_lo, mid_hi = SAMPLE_EDGE, len(scored) - SAMPLE_FAR - 1
    if mid_hi > mid_lo:
        step = (mid_hi - mid_lo) / (SAMPLE_MID + 1)
        for k in range(1, SAMPLE_MID + 1):
            take(int(mid_lo + step * k))
    picked.sort(key=lambda r: (r["_exceedance"], str(r.get("case_id") or "")))
    return picked


def _active_signatures(product_type=None, family_product=None) -> list:
    """이 범위에서 실제로 평가되는 룰만 — 꺼진 룰의 표본을 검수시키지 않는다."""
    sigs = eval_debug.signatures_scoped(product_type or None, family_product or None)
    return [s for s in sigs if s.get("enabled") is not False]


def _sample_view(row: dict) -> dict:
    """검수 화면에 내려보낼 최소 필드 — 판단에 필요한 것만(응답을 가볍게)."""
    return {
        "eval_id": row["eval_id"], "case_id": row["case_id"],
        "session_id": row.get("session_id"), "analysis_key": row.get("analysis_key"),
        "source_index": _source_index(row.get("source_file")),
        "item": row.get("item_name_raw"), "item_canonical": row.get("item_canonical"),
        "bin": row.get("bin"), "unit": row.get("unit"), "value_type": row.get("value_type"),
        "product_name": row.get("product_name"), "product_type": row.get("product_type"),
        "family_product": row.get("family_product"), "lot_id": row.get("lot_id"),
        "status": row.get("status"), "confidence": row.get("confidence"),
        "data_completeness": row.get("data_completeness"),
        "cpk": row.get("cpk"), "yield": row.get("yield_rate"),
        "fail_count": row.get("fail_count"), "total_count": row.get("total_count"),
        "n_dut": row.get("n_dut"),
        "metric": row.get("_metric"), "metric_value": row.get("_metric_value"),
        "threshold_key": row.get("_threshold_key"),
        "threshold": row.get("_threshold"), "exceedance": row.get("_exceedance"),
    }


def _source_index(source_file):
    """'eval-snapshot#2' → 2 (골든셋 항목의 source 키). 형식이 다르면 None."""
    text = str(source_file or "")
    if "#" not in text:
        return None
    try:
        return int(text.rsplit("#", 1)[1])
    except ValueError:
        return None


def queue(product_type=None, family_product=None) -> dict:
    """활성 룰별 미검수 표본 (룰당 최대 8건) + 무판정 트랙 + 진행 현황.

    표본이 비는 사유를 구분해 내려보낸다 — "수집 안 됨" 과 "다 검수함" 은 다음에 할 일이
    전혀 다른데 빈 목록만 보면 구분할 수 없다.
    """
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return {"collected": False, "rules": [], "no_judgment": [],
                "note": "아직 수집된 평가 스냅샷이 없습니다 — 세션을 표본함에 수집하세요."}
    try:
        thresholds = eval_debug.effective_thresholds(product_type or None,
                                                     family_product or None)
        rules = []
        for sig in _active_signatures(product_type, family_product):
            sig_id = sig.get("id")
            crit = _rule_criterion(sig, thresholds)
            rows = _fetch_rule_rows(conn, sig_id, product_type, family_product)
            done = _labeled_count(conn, sig_id, product_type, family_product)
            entry = {"id": sig_id, "status_hint": sig.get("status_hint"),
                     "pending_total": len(rows), "labeled": done["n"],
                     "labeled_ok": done["ok"], "labeled_over": done["over"],
                     "samples": [], "criterion": None}
            if crit is None:
                entry["note"] = ("임계값을 참조하는 조건이 없어 표본을 층화할 수 없습니다 "
                                 "(signatures.yaml 에 review_metric 을 지정하세요).")
            else:
                metric, op, key = crit
                threshold = thresholds.get(key)
                entry["criterion"] = {"metric": metric, "op": op, "threshold_key": key,
                                      "threshold": threshold}
                for row in _stratify(rows, metric, op, threshold):
                    row["_threshold_key"], row["_threshold"] = key, threshold
                    entry["samples"].append(_sample_view(row))
            rules.append(entry)
        return {"collected": True, "rules": rules,
                "no_judgment": no_judgment(conn, product_type, family_product),
                "sample_max": SAMPLE_MAX, "min_labels": MIN_LABELS,
                "min_per_side": MIN_PER_SIDE}
    finally:
        conn.close()


def _labeled_count(conn, signature: str, product_type=None, family_product=None) -> dict:
    """이 룰의 검수 진행 — 총/맞음/과다발화. 추천 게이트(20건·양쪽 5건)의 근거."""
    sql = """SELECT l.engine_comment_accepted AS ok, COUNT(*) AS n
               FROM label l
               JOIN evaluation ev ON ev.eval_id = l.eval_id
               JOIN case_signature cs ON cs.eval_id = ev.eval_id
               JOIN fail_case fc ON fc.case_id = ev.case_id
               LEFT JOIN product_master pm ON pm.product_name = fc.product_name
              WHERE l.labeler = ? AND cs.signature = ?"""
    params = [REVIEW_LABELER, signature]
    if product_type:
        sql += " AND pm.product_type = ?"
        params.append(product_type)
    if family_product:
        sql += " AND pm.family_product = ?"
        params.append(family_product)
    sql += " GROUP BY l.engine_comment_accepted"
    ok = over = 0
    for r in conn.execute(sql, params):
        if r["ok"]:
            ok = r["n"]
        else:
            over = r["n"]
    return {"n": ok + over, "ok": ok, "over": over}


def no_judgment(conn, product_type=None, family_product=None, limit: int = 200) -> list:
    """무판정 트랙 — 통계가 비어 어떤 룰도 발화할 수 없는 항목 목록.

    `value_type=PF`(양불) 로 분류되면 L1/L2 가 cpk·산포를 전부 None 으로 비우고, 그러면
    모든 when_metric 조건이 결측→False 라 발화가 구조적으로 불가능하다. 대부분은 UNIT
    원문이 엔진 정확일치 표에 없어서 생긴 오분류이고, **임계값이 아니라 단위표 등록으로**
    고치는 문제라 여기부터 줄이는 것이 임계값 튜닝보다 효율이 높다.
    라벨 대상이 아니라 진단 목록이므로 item 단위로 접어서 보여준다.
    """
    sql = """SELECT im.item_name_raw AS item, im.unit, im.value_type,
                    pm.product_type, pm.family_product,
                    COUNT(*) AS n, MAX(ir.session_id) AS session_id
               FROM evaluation ev
               JOIN ingest_run ir ON ir.run_id = ev.run_id
               JOIN fail_case fc ON fc.case_id = ev.case_id
               JOIN item_master im ON im.item_id = fc.item_id
               LEFT JOIN product_master pm ON pm.product_name = fc.product_name
               LEFT JOIN raw_metrics rm ON rm.case_id = ev.case_id AND rm.run_id = ev.run_id
              WHERE ir.ingested_by = ?
                AND rm.cpk IS NULL
                AND NOT EXISTS (SELECT 1 FROM case_signature cs WHERE cs.eval_id = ev.eval_id)"""
    params = [SNAPSHOT_INGESTED_BY]
    if product_type:
        sql += " AND pm.product_type = ?"
        params.append(product_type)
    if family_product:
        sql += " AND pm.family_product = ?"
        params.append(family_product)
    sql += (" GROUP BY im.item_name_raw, im.unit, im.value_type, pm.product_type,"
            " pm.family_product ORDER BY n DESC LIMIT ?")
    params.append(int(limit))
    return [dict(r) for r in conn.execute(sql, params)]


# ── 라벨 저장 ────────────────────────────────────────────────────────────────

def save_review_label(eval_id: int, *, correct: bool, comment: str = "",
                      reviewer: str = "") -> dict:
    """검수 1건 저장 — `label` 재사용, `labeler='eval-review'`.

    `human_status` 는 **비운다.** 이 라벨은 "이 룰의 발화가 맞나" 이지 "전체 판정이
    무엇이어야 하나" 가 아니다. `eval_admin.scoring()` 이 `human_status IS NOT NULL` 로
    거르므로 전체 status 채점 표본과 섞이지 않는다(labeler 필터도 함께 건다).
    같은 case 재검수는 교체한다(기존 `save_human_label` 과 같은 DELETE→INSERT 관례).
    """
    conn = eval_export.open_conn(create=False)
    if conn is None:
        raise ReviewError("수집된 평가 스냅샷이 없습니다")
    try:
        row = conn.execute(
            "SELECT case_id FROM evaluation WHERE eval_id=?", (int(eval_id),)).fetchone()
        if row is None:
            raise ReviewError(f"없는 eval_id: {eval_id}")
        case_id = row["case_id"]
        conn.execute("DELETE FROM label WHERE eval_id=? AND labeler=?",
                     (int(eval_id), REVIEW_LABELER))
        store, _ = eval_export._engine()
        store.insert_label(case_id, int(eval_id), None, None, None,
                           1 if correct else 0, 0, (comment or "").strip()[:2000] or None,
                           REVIEW_LABELER, (reviewer or "")[:100], "manual", conn=conn)
        conn.commit()
        return {"eval_id": int(eval_id), "case_id": case_id, "correct": bool(correct)}
    finally:
        conn.close()


def golden_entry_for(eval_id: int) -> dict | None:
    """"맞음" 라벨을 골든셋 항목으로 굳히기 위한 재료 — (session_id, 합성 case dict).

    골든셋이 비어 있으면 "적용 전 회귀 실패 시 차단" 가드가 항상 통과해 무효가 된다.
    검수에서 맞다고 한 케이스가 곧 "이 발화는 유지돼야 한다" 이므로 그대로 기대값이 된다.
    `golden_io.add_case` 가 트레이스 케이스 dict 를 받으므로 같은 모양으로 맞춰 준다.
    """
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return None
    try:
        row = conn.execute(
            """SELECT ev.status, fc.bin, im.item_name_raw AS item,
                      ir.session_id, ir.source_file
                 FROM evaluation ev
                 JOIN ingest_run ir ON ir.run_id = ev.run_id
                 JOIN fail_case fc ON fc.case_id = ev.case_id
                 JOIN item_master im ON im.item_id = fc.item_id
                WHERE ev.eval_id=?""", (int(eval_id),)).fetchone()
        if row is None or not row["session_id"]:
            return None
        fired = [r["signature"] for r in conn.execute(
            "SELECT signature FROM case_signature WHERE eval_id=?", (int(eval_id),))]
        case = {"item_raw": row["item"], "bin": row["bin"],
                "source_index": _source_index(row["source_file"]),
                "status": row["status"],
                "signature_matrix": [{"id": s, "fired": True} for s in fired]}
        return {"session_id": row["session_id"], "case": case}
    finally:
        conn.close()


# ── 승인형 임계값 추천 ────────────────────────────────────────────────────────

def proposal(signature: str, product_type=None, family_product=None) -> dict:
    """검수 라벨로 임계값 **강화안** 1개를 계산한다 (적용은 하지 않는다).

    후보는 "과다발화라고 표시된 케이스의 지표값" 들이다 — 그 값 바로 위로 임계값을 올리면
    그 케이스가 발화에서 빠진다. 각 후보에 대해 라벨된 표본 전체를 재판정해
    precision(맞음/발화)과 **유지되는 맞음 건수**를 세고, precision 목표를 넘는 것 중
    맞음을 가장 많이 보존하는 값을 고른다.

    반환에 `blocked` 가 있으면 아직 만들 수 없다는 뜻이다(사유 포함).
    """
    conn = eval_export.open_conn(create=False)
    if conn is None:
        return {"blocked": "수집된 평가 스냅샷이 없습니다"}
    try:
        sig = next((s for s in _active_signatures(product_type, family_product)
                    if s.get("id") == signature), None)
        if sig is None:
            raise ReviewError(f"이 범위에서 활성 상태가 아닌 룰입니다: {signature}")
        thresholds = eval_debug.effective_thresholds(product_type or None,
                                                     family_product or None)
        crit = _rule_criterion(sig, thresholds)
        if crit is None:
            return {"blocked": "임계값을 참조하는 조건이 없어 옮길 값이 없습니다",
                    "signature": signature}
        metric, op, key = crit
        current = thresholds.get(key)
        if current is None:
            return {"blocked": f"임계값 {key} 를 읽을 수 없습니다", "signature": signature}

        labeled = _labeled_rows(conn, signature, product_type, family_product)
        ok = [r for r in labeled if r["_correct"]]
        over = [r for r in labeled if not r["_correct"]]
        if len(labeled) < MIN_LABELS or len(ok) < MIN_PER_SIDE or len(over) < MIN_PER_SIDE:
            return {"blocked": (f"표본 부족 — 검수 {len(labeled)}/{MIN_LABELS}건, "
                                f"맞음 {len(ok)}/{MIN_PER_SIDE}, "
                                f"과다발화 {len(over)}/{MIN_PER_SIDE}"),
                    "signature": signature, "labeled": len(labeled),
                    "ok": len(ok), "over": len(over)}

        base = _score_threshold(labeled, metric, op, current)
        candidates = []
        for row in over:                       # 과다발화 케이스를 하나씩 걷어내는 값
            value = row.get(metric)
            if value is None:
                continue
            cand = _tighten(value, op)
            if cand is None or not _is_stronger(cand, current, op):
                continue
            score = _score_threshold(labeled, metric, op, cand)
            score["value"] = round(cand, 6)
            candidates.append(score)
        if not candidates:
            return {"blocked": "강화 방향으로 옮길 후보가 없습니다 "
                               "(과다발화 케이스의 지표값이 이미 임계값 이하)",
                    "signature": signature}

        # 후보 정리 — 같은 값 중복 제거 후, precision 목표를 넘는 것 중 맞음 보존 최대.
        uniq = {c["value"]: c for c in sorted(candidates, key=lambda c: c["value"])}
        ranked = sorted(uniq.values(),
                        key=lambda c: (c["precision"] < TARGET_PRECISION,
                                       -c["kept_ok"], c["value"]))
        best = ranked[0]
        return {
            "signature": signature, "threshold_key": key, "metric": metric, "op": op,
            "current": current, "proposed": best["value"],
            "scope": {"product_type": product_type or "", "family_product": family_product or ""},
            "labeled": len(labeled), "ok": len(ok), "over": len(over),
            "before": base, "after": best,
            "target_precision": TARGET_PRECISION,
            "meets_target": best["precision"] >= TARGET_PRECISION,
            "removed": [_sample_view(_derived(r)) for r in over
                        if not _passes(r.get(metric), op, best["value"])],
            "lost_ok": [_sample_view(_derived(r)) for r in ok
                        if not _passes(r.get(metric), op, best["value"])],
            "note": ("표본 precision 은 경계 구간을 과대표집한 층화표본 기준이라 "
                     "전체 precision 보다 낮게 나옵니다."),
            "candidates": ranked[:10],
        }
    finally:
        conn.close()


def _labeled_rows(conn, signature, product_type=None, family_product=None) -> list:
    """이 룰의 검수 완료 표본 + 그 판정(맞음/과다발화). 재판정 시뮬레이션의 모집단."""
    rows = _fetch_rule_rows(conn, signature, product_type, family_product,
                            unlabeled_only=False)
    verdicts = {r["eval_id"]: r["ok"] for r in conn.execute(
        "SELECT eval_id, engine_comment_accepted AS ok FROM label WHERE labeler=?",
        (REVIEW_LABELER,))}
    out = []
    for row in rows:
        if row["eval_id"] in verdicts:
            row = dict(row)
            row["_correct"] = bool(verdicts[row["eval_id"]])
            out.append(row)
    return out


def _score_threshold(labeled: list, metric: str, op: str, threshold) -> dict:
    """이 임계값이면 표본에서 몇 건이 뜨고 그중 몇 건이 맞나."""
    fired = [r for r in labeled if _passes(r.get(metric), op, threshold)]
    kept_ok = sum(1 for r in fired if r["_correct"])
    total_ok = sum(1 for r in labeled if r["_correct"])
    return {"fired": len(fired), "kept_ok": kept_ok,
            "kept_over": len(fired) - kept_ok, "total_ok": total_ok,
            "precision": round(kept_ok / len(fired), 4) if fired else None}


def _tighten(value, op: str):
    """그 케이스를 발화에서 빼는 최소 임계값 — 값 자체(경계는 배타 비교라 값이 곧 컷)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_stronger(candidate, current, op: str) -> bool:
    """강화 방향인가 — ">" 룰은 임계값이 올라가야, "<" 룰은 내려가야 발화가 준다.

    v1 은 강화만 허용한다. 느슨하게 하면 검수하지 않은 케이스가 새로 발화하는데, 그건
    라벨로 검증된 변경이 아니라 그냥 미지의 영역을 넓히는 것이다.
    """
    if op.startswith(">"):
        return candidate > current
    return candidate < current
