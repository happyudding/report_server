"""Signature 판정 근거 — Issue Table 의 `?` 팝업이 쓰는 조립기.

**왜 필요한가.** 화면에는 "이 항목은 LOW_CPK" 라는 결론만 뜨고, *무슨 기준으로 어떤 값이
임계값을 넘어서* 그렇게 판정됐는지는 `/pe/eval` 관리자 트레이스에만 있었다. 정작 그
판정을 검토해야 하는 PTE/개발 담당자는 관리자가 아니라 근거를 볼 수 없었다.

**데이터는 이미 있다.** 업로드 때 `eval_export.collect_session_snapshot` 이 전 세션·전 item
을 `persist=True` 로 평가해 L1(raw_metrics)/L2(features)/발화(case_signature)/근거
(eval_evidence)를 `REPORT_EVAL_DB_PATH` 에 적재한다. 여기서는 **조회만** 한다 — 재평가하지
않는다.

주의해서 지킨 것 3가지:
- **`ingested_by='eval-snapshot'` run 만 본다.** 코멘트 export run(`web_report`)에도
  raw_metrics 가 있지만 그건 `tabs/cpk._stats` 산출물이라 엔진 판정 근거가 아니다.
  섞으면 "임계값을 넘었다는데 값이 안 맞는" 화면이 된다.
- **대표 케이스는 AI Comment 와 같은 규칙으로 고른다** (`ai_comment.rank_key`). 소스가
  여러 개인 세션에서 다른 케이스를 고르면 코멘트와 근거가 어긋난다.
- **근거가 없어도 룰 기준(조건식·임계값)은 항상 채운다.** "무슨 기준인가"는 DB 없이도
  답할 수 있어야 한다 — 스냅샷 이전 세션에서 팝업이 빈 화면이 되면 안 된다.

엔진은 직접 import 하지 않는다(불변 규칙 #8) — `web_report.eval_debug`(룰·임계값·조건 분해)
와 `web_report.eval_export`(DB 커넥션)만 경유한다. 임계값 초과 계산은 형제 모듈
[review.py](review.py) 의 것을 그대로 쓴다(같은 값을 두 번 계산하지 않는다).
"""
from __future__ import annotations

import logging

from web_report import ai_comment, eval_debug, eval_export

from . import review

logger = logging.getLogger(__name__)

# Issue Table row_key 접두 — 정본은 web_report/service.py `_ISSUE_KEY_PREFIXES` 와
# static/webreport/sheets.js `issueRowKey`. 저장 키라 바꾸면 안 된다(CLAUDE.md 규칙 12).
# Compare 탭 접두(CMPDIST|·CMPETC|)는 여기 없다 — 그 시트에는 Signature 컬럼 자체가
# 없어(엔진이 Before/After 비교를 평가하지 않는다) 근거를 물을 행이 생기지 않는다.
KEY_PREFIXES = ("Yield|", "CPK|", "TEMP|", "ETC|")

# 근거를 못 얻은 사유 → 화면 문구. 어떤 값이든 룰 기준은 함께 내려간다.
_MISSING_NOTE = {
    "temp_row": "Temperature 행은 서버가 RT limit 으로 다시 판정한 결과라 "
                "엔진 근거와 대응되지 않습니다 — 룰 기준만 표시합니다.",
    "no_eval_db": "평가 DB가 아직 만들어지지 않았습니다 — 룰 기준만 표시합니다.",
    "no_snapshot": "이 세션에는 평가 스냅샷이 없습니다(스냅샷 도입 이전 업로드) — "
                   "룰 기준만 표시합니다.",
    "case_not_found": "이 항목은 평가 스냅샷에 남지 않았습니다"
                      "(저장 기준 미달이거나 평가 대상 밖) — 룰 기준만 표시합니다.",
    "error": "근거를 읽는 중 오류가 발생했습니다 — 룰 기준만 표시합니다.",
}

_BIMODALITY_NOTE = "이 룰만 조건식(when_metric)이 아니라 분포 모양 판정(modality)을 쓴다."
_UNKNOWN_NOTE = "다른 룰이 하나도 발화하지 않았을 때 붙는 표시 — 임계값 조건이 없다."


def _parse_row_key(row_key: str):
    """row_key → (kind, bin, item). 규약 밖이면 ValueError."""
    key = str(row_key or "").strip()
    if not any(key.startswith(p) for p in KEY_PREFIXES):
        raise ValueError(f"unsupported row key: {key[:40]}")
    kind, _, rest = key.partition("|")
    if kind == "Yield":
        bin_s, _, item = rest.partition("|")
        try:
            bin_ = int(str(bin_s).strip())
        except (TypeError, ValueError):
            bin_ = None
        return kind, bin_, item
    return kind, None, rest


def _rules_only(signature_ids, product_type, family_product, item_class=None):
    """DB 근거 없이 룰 정의만으로 채운 rules[] — 모든 폴백 경로가 이걸 쓴다."""
    return _build_rules(signature_ids, product_type, family_product,
                        item_class=item_class, ctx={}, sources={}, fired={})


def _criterion(sig, thresholds):
    """팝업 헤더 한 줄 요약 — 이 룰의 대표 기준 (metric, op, 임계값 키, 값)."""
    found = review._rule_criterion(sig, thresholds)
    if not found:
        return None
    metric, op, key = found
    return {"metric": metric, "op": op, "threshold_key": key,
            "threshold": thresholds.get(key)}


def _build_rules(signature_ids, product_type, family_product, *, item_class,
                 ctx, sources, fired):
    """요청받은 signature 각각 → 정의 + 조건 분해(+ 실측값이 있으면 함께)."""
    scoped = {str(s.get("id")): s
              for s in eval_debug.signatures_scoped(product_type, family_product)
              if s.get("id")}
    thresholds = eval_debug.effective_thresholds(product_type, family_product, item_class)
    bimodality_id, unknown_id = eval_debug.subpop_gap_id(), eval_debug.unknown_id()

    out = []
    for sig_id in signature_ids:
        sig = scoped.get(sig_id)
        if not sig:
            # 카탈로그 밖 값 — 룰이 지워졌거나 사람이 옛 id 로 확정해 둔 경우.
            out.append({"id": sig_id, "unknown_rule": True, "fired": sig_id in fired,
                        "role": fired.get(sig_id), "conditions": []})
            continue
        if sig_id == bimodality_id:
            conds, special = eval_debug.subpop_conditions(ctx, thresholds), "bimodality"
        elif sig_id == unknown_id:
            conds, special = [], "unknown"
        else:
            conds = eval_debug.condition_details(sig.get("when_metric"), ctx, thresholds)
            special = None
        for cond in conds:
            cond["exceedance"] = review._exceedance(cond.get("actual"), cond.get("op") or ">",
                                                    cond.get("ref_value"))
            cond["value_source"] = sources.get(cond.get("metric"))
        out.append({
            "id": sig_id,
            "enabled": sig.get("enabled") is not False,
            "status_hint": sig.get("status_hint"),
            "issue_category": sig.get("issue_category"),
            "phenomenon_ko": sig.get("phenomenon_ko"),
            "action_ko": sig.get("action_ko"),
            "criterion": _criterion(sig, thresholds),
            "special": special,
            "special_note": (_BIMODALITY_NOTE if special == "bimodality"
                             else _UNKNOWN_NOTE if special == "unknown" else None),
            "fired": sig_id in fired,
            "role": fired.get(sig_id),
            "conditions": conds,
        })
    return out


# ── 스냅샷 조회 ──────────────────────────────────────────────────────────────

_CASE_SQL = f"""
SELECT ev.eval_id, ev.case_id, ev.run_id, ev.status, ev.engine_version,
       ev.data_completeness, ir.created_at AS ingested_at, ir.source_file,
       fc.bin, fc.item_class,
       {','.join('rm.' + c for c in review._RAW_COLS)}, rm."yield" AS yield_rate,
       {','.join('f.' + c for c in review._METRIC_COLS)}
  FROM ingest_run ir
  JOIN evaluation ev ON ev.run_id = ir.run_id
  JOIN fail_case fc ON fc.case_id = ev.case_id
  JOIN item_alias ia ON ia.item_id = fc.item_id
  LEFT JOIN raw_metrics rm ON rm.case_id = ev.case_id AND rm.run_id = ev.run_id
  LEFT JOIN features f ON f.case_id = ev.case_id AND f.run_id = ev.run_id
                      AND f.engine_version = ev.engine_version
 WHERE ir.session_id = ? AND ir.ingested_by = ? AND ia.raw_name = ?
"""


def _fetch_cases(conn, session_id, item, bin_=None):
    """이 세션·item 의 스냅샷 case 들. item_alias.raw_name 으로 매칭한다.

    `item_master.item_name_raw` 를 쓰지 않는 이유 — 그 컬럼은 canonical 당 "마지막에 본
    raw name" 하나라 다른 제품이 덮으면 매칭이 조용히 깨진다. `item_alias.raw_name` 은
    PK 라 화면 문자열과 1:1 이다.

    ⚠ **bin 으로 거르지 않는다** (2026-08-19) — 엔진 case 가 item 당 1개이고
    `fail_case.bin` 은 대표 bin(참고값)일 뿐이라, bin 을 걸면 `Yield|5|X` 팝업이
    `case_not_found` 로 떨어진다. 종전에 같은 item 의 bin 행마다 **서로 다른 근거**가
    보이던 불일치도 이걸로 사라진다. `bin_` 인자는 호출부 호환으로 남기고 무시한다.
    """
    return conn.execute(_CASE_SQL,
                        [session_id, review.SNAPSHOT_INGESTED_BY, item]).fetchall()


def _signatures_of(conn, eval_ids):
    """eval_id → {signature: role}."""
    if not eval_ids:
        return {}
    marks = ",".join("?" * len(eval_ids))
    out = {}
    for row in conn.execute(
            f"SELECT eval_id, signature, role FROM case_signature WHERE eval_id IN ({marks})",
            list(eval_ids)):
        out.setdefault(row["eval_id"], {})[row["signature"]] = row["role"]
    return out


def _pick_case(rows, fired_by_eval, bimodality_id):
    """대표 case — AI Comment 와 **같은 규칙**(ai_comment.rank_key), 동률이면 최신 eval.

    `_modality_tag` 가 BIMODALITY 발화 시에만 비지 않으므로, DB 쪽 has_modality 는
    case_signature 에 그 id 가 있는지로 1:1 대응한다.
    """
    def key(row):
        fired = fired_by_eval.get(row["eval_id"], {})
        return (ai_comment.rank_key(row["status"], bimodality_id in fired), row["eval_id"])
    return max(rows, key=key)


def _ctx_values(row, evidence):
    """조건이 참조하는 값 사전 + 값별 출처. 저장 컬럼 → 파생 → evidence 순."""
    raw_vals = {c: row[c] for c in review._RAW_COLS}
    raw_vals["yield"] = row["yield_rate"]
    feat_vals = {c: row[c] for c in review._METRIC_COLS}
    base = dict(raw_vals)
    base.update(feat_vals)
    ctx = review._derived(base)

    sources = {}
    for name, group in (("raw_metrics", raw_vals), ("features", feat_vals)):
        for k, v in group.items():
            if v is not None:
                sources[k] = name
    for k, v in ctx.items():
        if v is not None and k not in sources:
            sources[k] = "derived"
    # evidence 보강 — v9(2026-08-19) 이전에 수집된 스냅샷은 판정지표 컬럼이 NULL 이라
    # 조인만으로는 값이 비어 있다. 엔진이 발화 근거를 signal_code 로 남겨 두므로
    # (`signatures._format_evidence`) 그 값으로 메운다(4자리 반올림, 발화한 case 한정).
    # 저장 컬럼이 우선이므로 재수집된 세션은 자동으로 정확한 값을 쓴다.
    for signal_code, value in evidence:
        k = str(signal_code or "").lower()
        if not k or value is None or ctx.get(k) is not None:
            continue
        ctx[k] = value
        sources[k] = "evidence"
    return ctx, sources


# ── 공개 API ─────────────────────────────────────────────────────────────────

def build(session_id: str, row_key: str, signature_ids, *,
          product_type: str, family_product: str, preprocessed: bool = False) -> dict:
    """Signature 근거 payload. 실패해도 룰 기준만은 반드시 채워 돌려준다."""
    ids = [str(s) for s in (signature_ids or [])]
    kind, bin_, item = _parse_row_key(row_key)
    out = {"key": row_key, "kind": kind, "bin": bin_, "item": item,
           "evidence_missing": None, "evidence_note": None,
           "ingested_at": None, "engine_version": None, "snapshot_status": None,
           "warnings": [], "rules": []}

    def fallback(reason, item_class=None):
        out["evidence_missing"] = reason
        out["evidence_note"] = _MISSING_NOTE.get(reason)
        out["rules"] = _rules_only(ids, product_type, family_product, item_class)
        return out

    if kind == "TEMP":
        return fallback("temp_row")

    conn = None
    try:
        # 조회 전용 커넥션 — 이 팝업은 조회자 전원이 누르는 읽기 경로라, 스키마 보장
        # (=쓰기 트랜잭션)을 하는 open_conn 을 쓰면 클릭마다 eval DB 쓰기 잠금을 다툰다.
        conn = eval_export.open_conn_ro()
        if conn is None:
            return fallback("no_eval_db")
        rows = _fetch_cases(conn, session_id, item, bin_)
        if not rows:
            # 세션 자체에 스냅샷이 없는 것과 이 항목만 없는 것은 사용자에게 다른 뜻이다.
            has_run = conn.execute(
                "SELECT 1 FROM ingest_run WHERE session_id=? AND ingested_by=? LIMIT 1",
                (session_id, review.SNAPSHOT_INGESTED_BY)).fetchone()
            return fallback("case_not_found" if has_run else "no_snapshot")

        fired_by_eval = _signatures_of(conn, [r["eval_id"] for r in rows])
        row = _pick_case(rows, fired_by_eval, eval_debug.subpop_gap_id())
        fired = fired_by_eval.get(row["eval_id"], {})
        evidence = conn.execute(
            "SELECT signal_code, value FROM eval_evidence WHERE eval_id=?",
            (row["eval_id"],)).fetchall()
        ctx, sources = _ctx_values(row, [(e["signal_code"], e["value"]) for e in evidence])

        out.update(ingested_at=row["ingested_at"], engine_version=row["engine_version"],
                   snapshot_status=row["status"],
                   rules=_build_rules(ids, product_type, family_product,
                                      item_class=row["item_class"], ctx=ctx,
                                      sources=sources, fired=fired))
        # 스냅샷은 업로드 시점 1회라 그 뒤 원본을 바꾸는 편집을 따라가지 않는다.
        if preprocessed:
            out["warnings"].append(
                "업로드 이후 전처리(항목 제외·outlier·셀 편집)가 적용되어 "
                "화면 값과 아래 근거가 다를 수 있습니다.")
        missing = [i for i in ids if i not in fired]
        if missing:
            out["warnings"].append(
                "스냅샷 당시에는 발화하지 않은 룰이 있습니다(" + ", ".join(missing) + ") — "
                "사람이 확정했거나 그 뒤 룰이 편집된 경우입니다.")
    except ValueError:
        raise
    except Exception:
        logger.exception("signature reason build failed (session=%s key=%s)",
                         session_id, row_key)
        return fallback("error")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out
