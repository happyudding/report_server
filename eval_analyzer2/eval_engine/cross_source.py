from collections import Counter

from . import store
from .pipeline import recommend
from .pipeline._rules import signatures_doc, thresholds_for

SOURCE_ONLY_FAIL = "SOURCE_ONLY_FAIL"

def _signature_by_id() -> dict:
    return {s["id"] : s for s in signatures_doc()["signatures"]}

def _signature_text(signature_id: str) -> dict:
    s = _signature_by_id().get(signature_id)
    return {"phenomenon_ko" : s.get("phenomenon_ko") , "action_ko" : s.get("action_ko")} if s else {}

def _dominant_phenomenon(bad_rows : list[dict]) -> str:
    sig_ids = [r["primary_signature"] for r in bad_rows if r.get("primary_signature")]
    if not sig_ids:
        return "개별 case 진단 정보 없음"
    dominant_id, _count = Counter(sig_ids).most_common(1)[0]
    by_id = _signature_by_id()
    return by_id.get(dominant_id, {}).get("phenomenon_ko") or dominant_id

def _fail_rate(row: dict):
    fail, total = row.get("fail_count"), row.get("total_count")
    if not total:
        return None
    return (fail or 0) / total

def _group_by_item(rows: list[dict]) -> dict:
    groups = {}
    for r in rows:
        groups.setdefault(r["item_id"], []).append(r)
    return groups

def _representative_by_source(item_rows: list[dict]) -> dict:
    by_source = {}
    for r in item_rows:
        src=r.get("source_file")
        rate = _fail_rate(r)
        if src is None or rate is None:
            continue
        by_source.setdefault(src, []).append((rate, r))
    return {src:max(entries, key=lambda e: e[0]) for src, entries in by_source.items()}

def _split_by_gap(rep: dict, th: dict):
    if len(rep) < th["source_min_count"]:
        return None
    ordered = sorted(rep.items(), key=lambda kv : kv[1][0])
    rates = [rate for _src, (rate, _row) in ordered]
    best_idx, best_gap = None, -1.0
    for i in range(1, len(rates)):
        gap = rates[i] - rates[i - 1]
        if gap > best_gap:
            best_gap, best_idx = gap, i
    if best_gap < th["source_fail_rate_delta_warn"]:
        return None
    normal_sources = [src for src, _entry in ordered[:best_idx]]
    bad_sources = [src for src, _entry in ordered[best_idx:]]
    return normal_sources, bad_sources, best_gap

def _evaluate_item_group(item_rows: list[dict], th:dict):
    rep = _representative_by_source(item_rows)
    split = _split_by_gap(rep, th)
    if split is None:
        return None
    normal_sources, bad_sources, gap = split
    bad_rows = [r for r in item_rows if r.get("source_file") in bad_sources]
    bad_targets = [
        {"case_id" : r["case_id"], "run_id" : r["run_id"], "engine_version" : r.get("engine_version")}
        for r in bad_rows
    ]
    return {
        "normal_sources" : normal_sources, "bad_sources" : bad_sources,
        "source_fail_rate_gap" : gap, "bad_targets" : bad_targets,
        "dominant_phenomenon" : _dominant_phenomenon(bad_rows),
    }


def aggregate_cross_source(run_ids:list[int], * , engine_version:str | None = None, persist: bool = True) -> dict:
    """
    같은 wafer/lot 의 여러 run_id 를 item_id 기준으로 묶어 source only fail 패턴을 판정하고(persist 시) 불량 그룹들의 eval comment 를 갱신
    """

    return {"evaluations" :resultst}