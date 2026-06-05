"""analyzer — group-level 분석 orchestration.

df_honey_group + ReportMeta + ItemSelector → AnalysisResult.
xlsx_writer 가 소비할 모든 결과(테이블 + distribution)를 한 번에 계산한다.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Optional

from . import _builders as B
from . import _profile
from .df_honey_group import df_honey_group
from .item_selector import ItemSelector
from .models import AnalysisResult, DistSeries, ReportMeta

_FLOW_PROFILE_ON = bool(os.environ.get("HONEY_FLOW_PROFILE"))


def _emit_profile_event(profile_cb, label: str, status: str,
                        elapsed: Optional[float] = None, error: Optional[str] = None) -> None:
    if profile_cb is None:
        return
    event = {
        "module": "analyzer",
        "label": label,
        "status": status,
    }
    if elapsed is not None:
        event["elapsed"] = elapsed
    if error:
        event["error"] = error
    try:
        profile_cb(event)
    except Exception:
        pass


@contextlib.contextmanager
def _flow_time(label: str, profile_cb=None):
    if not (_FLOW_PROFILE_ON or _profile.collecting() or profile_cb is not None):
        yield
        return
    _emit_profile_event(profile_cb, label, "start")
    depth = _profile.push()
    t0 = time.perf_counter()
    try:
        yield
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        _profile.pop("analyzer", label, elapsed, depth)
        _emit_profile_event(profile_cb, label, "error", elapsed, str(exc))
        if _FLOW_PROFILE_ON:
            print(f"[flow-profile] analyzer.{label}: ERROR after {elapsed:.3f}s ({exc})",
                  file=sys.stderr, flush=True)
        raise
    finally:
        if sys.exc_info()[0] is None:
            elapsed = time.perf_counter() - t0
            _profile.pop("analyzer", label, elapsed, depth)
            _emit_profile_event(profile_cb, label, "done", elapsed)
            if _FLOW_PROFILE_ON:
                print(f"[flow-profile] analyzer.{label}: {elapsed:.3f}s",
                      file=sys.stderr, flush=True)


def run(group: df_honey_group, meta: Optional[ReportMeta] = None,
        selector: Optional[ItemSelector] = None,
        profile_cb=None) -> AnalysisResult:
    """선택 item 적용 → 전 테이블 계산 → AnalysisResult."""
    meta = meta or ReportMeta()
    selector = selector or ItemSelector()

    with _flow_time("select_items", profile_cb):
        work = group.select_items(selector.selected_items)
    mass_data_map = work.mass_data_map
    if not mass_data_map:
        raise ValueError("분석할 데이터가 없습니다.")

    # diff 모드(2파일·subject 구성 상이) 선판정. cpk/distribution 은 위치(iloc) 기반
    # 빌더라 파일별 subject 수가 다르면 첫 파일 기준 idx 가 다른 파일에서 범위를
    # 벗어나 깨진다. diff 면 common subject 를 이름 기반으로 계산한다. (단일/동일
    # 모드면 split is None → 기존 위치 기반 경로 유지.)
    with _flow_time("split_for_diff", profile_cb):
        split = work.split_for_diff()

    with _flow_time("build_analysis_tables", profile_cb):
        with _flow_time("build_yield"):
            yield_rows = work.yield_rate()
        with _flow_time("build_fail_items"):
            fail_item_rows = work.fail_items()
        with _flow_time("build_issue_summary"):
            # fail_item_rows(=캐시) 재사용 — 내부 build_fail_items 재계산 회피
            issue_rows = B.build_issue_summary(mass_data_map, fail_items=fail_item_rows)
        with _flow_time("build_summary_rows"):
            summary_rows = work.summary()
        with _flow_time("build_major_fail_subjects"):
            major_fail_subject_rows = B.build_major_fail_subjects(mass_data_map)

        if split is None:
            with _flow_time("build_cpk"):
                cpk_rows = work.cpk()
            with _flow_time("subjects_meta"):
                subjects_meta = _subjects_meta_from_group(work)
            with _flow_time("build_distributions"):
                dist_source_data = work.dist_source_frames()
                distributions = _build_distributions(subjects_meta, dist_source_data)
        else:
            cl = split["classification"]
            common_g = split["common"]
            with _flow_time("build_cpk_common"):
                cpk_rows = B.build_cpk_for_subjects(common_g.mass_data_map, cl["common"])
            with _flow_time("subjects_meta_common"):
                subjects_meta = _subjects_meta_from_group(common_g)
            with _flow_time("build_distributions_common"):
                dist_source_data = common_g.dist_source_frames()
                distributions = _build_distributions(subjects_meta, dist_source_data)

    total_dut = sum(len(md.scores) for md in mass_data_map.values())
    pass_yield = next((r["portion (%)"] for r in yield_rows if str(r["bin"]) == "1"), None)

    fail_value_rows = {}
    for name, md in mass_data_map.items():
        with _flow_time(f"fail_detail {name}", profile_cb):
            fail_value_rows[name] = md.fail_value_frame()

    with _flow_time("combined_df_yield", profile_cb):
        combined_df_yield = group.combined_df_yield
    result = AnalysisResult(
        meta=meta,
        sources=work.names(),
        subjects=subjects_meta,
        cpk_rows=cpk_rows,
        yield_rows=yield_rows,
        fail_item_rows=fail_item_rows,
        issue_rows=issue_rows,
        summary_rows=summary_rows,
        distributions=distributions,
        dist_source_data=dist_source_data,
        major_fail_subject_rows=major_fail_subject_rows,
        total_dut=total_dut,
        pass_yield=pass_yield,
        df_yield=combined_df_yield if not combined_df_yield.empty else None,
        fail_value_rows=fail_value_rows,
    )

    if split is not None:
        with _flow_time("diff_extras", profile_cb):
            _apply_diff_extras(result, split)
    return result


def _apply_diff_extras(result: AnalysisResult, split: dict) -> None:
    """diff compare 의 분류 메타 + a_only/b_only 전용 CPK/Distribution 을 채운다.

    공통(common) CPK/Distribution/subjects 는 run() 에서 이미 이름 기반으로 계산해
    메인 시트에 반영했으므로, 여기서는 분류 정보와 a_only/b_only 만 추가한다. Yield/
    Fail Item/Issue Table/Summary 는 병합 기준 기존 계산을 그대로 둔다.
    """
    cl = split["classification"]
    a_only_g, b_only_g = split["a_only"], split["b_only"]

    result.diff_classification = cl
    result.cpk_rows_a_only = B.build_cpk_for_subjects(a_only_g.mass_data_map, cl["a_only"])
    result.cpk_rows_b_only = B.build_cpk_for_subjects(b_only_g.mass_data_map, cl["b_only"])
    result.subjects_a_only = _subjects_meta_from_group(a_only_g)
    result.subjects_b_only = _subjects_meta_from_group(b_only_g)
    sd_a = a_only_g.dist_source_frames()
    sd_b = b_only_g.dist_source_frames()
    result.distributions_a_only = _build_distributions(result.subjects_a_only, sd_a)
    result.distributions_b_only = _build_distributions(result.subjects_b_only, sd_b)
    result.dist_source_data_a_only = sd_a
    result.dist_source_data_b_only = sd_b


def _subjects_meta_from_group(group: df_honey_group) -> list:
    """그룹 첫 source 의 subject 메타([{subject_id, subject, units, lower/upper_limit}])."""
    mass_data_map = group.mass_data_map
    if not mass_data_map:
        return []
    first = next(iter(mass_data_map.values()))
    return [
        {
            "subject_id": idx,
            "subject": subject,
            "units": first.units[idx] if idx < len(first.units) else "",
            "lower_limit": B._json_safe(first.lower_limits[idx] if idx < len(first.lower_limits) else None),
            "upper_limit": B._json_safe(first.upper_limits[idx] if idx < len(first.upper_limits) else None),
        }
        for idx, subject in enumerate(first.subjects)
    ]


def _build_distributions(subjects_meta: list, source_frames: list) -> list:
    """선택 subject 의 메타 DistSeries (traces 비움).

    차트 X/Y(모든 DUT 점)는 writer 가 dist_source_data(=source_frames)에서 열별 정렬 +
    rank/count 로 직접 산출하므로, 여기서는 고유값 ECDF 트레이스를 계산하지 않는다(다운샘플
    폐기). source_frames 에 non-NaN 데이터가 있는 subject 만 포함(기존 skip-empty 유지).
    """
    out = []
    for sm in subjects_meta:
        name = sm["subject"]
        has_data = any(name in f.columns and bool(f[name].notna().any())
                       for _n, f in source_frames)
        if not has_data:
            continue
        out.append(DistSeries(
            subject_id=sm["subject_id"],
            subject=name,
            unit=sm["units"],
            lower_limit=sm["lower_limit"],
            upper_limit=sm["upper_limit"],
            traces=[],
        ))
    return out
