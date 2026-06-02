"""analyzer — group-level 분석 orchestration.

df_honey_group + ReportMeta + ItemSelector → AnalysisResult.
xlsx_writer 가 소비할 모든 결과(테이블 + distribution)를 한 번에 계산한다.
"""
from __future__ import annotations

from typing import Optional

from . import _builders as B
from .df_honey_group import df_honey_group
from .item_selector import ItemSelector
from .models import AnalysisResult, DistSeries, ReportMeta


def run(group: df_honey_group, meta: Optional[ReportMeta] = None,
        selector: Optional[ItemSelector] = None) -> AnalysisResult:
    """선택 item 적용 → 전 테이블 계산 → AnalysisResult."""
    meta = meta or ReportMeta()
    selector = selector or ItemSelector()

    work = group.select_items(selector.selected_items)
    mass_data_map = work.mass_data_map
    if not mass_data_map:
        raise ValueError("분석할 데이터가 없습니다.")

    # diff 모드(2파일·subject 구성 상이) 선판정. cpk/distribution 은 위치(iloc) 기반
    # 빌더라 파일별 subject 수가 다르면 첫 파일 기준 idx 가 다른 파일에서 범위를
    # 벗어나 깨진다. diff 면 common subject 를 이름 기반으로 계산한다. (단일/동일
    # 모드면 split is None → 기존 위치 기반 경로 유지.)
    split = work.split_for_diff()

    yield_rows = work.yield_rate()
    fail_item_rows = work.fail_items()
    issue_rows = B.build_issue_summary(mass_data_map)  # bin별 most-fail item 요약
    summary_rows = work.summary()
    major_fail_subject_rows = B.build_major_fail_subjects(mass_data_map)

    if split is None:
        cpk_rows = work.cpk()
        subjects_meta = _subjects_meta_from_group(work)
        distributions = _build_distributions(work, subjects_meta)
    else:
        cl = split["classification"]
        common_g = split["common"]
        cpk_rows = B.build_cpk_for_subjects(common_g.mass_data_map, cl["common"])
        subjects_meta = _subjects_meta_from_group(common_g)
        distributions = _build_distributions(common_g, subjects_meta)

    total_dut = sum(len(md.scores) for md in mass_data_map.values())
    pass_yield = next((r["portion (%)"] for r in yield_rows if str(r["bin"]) == "1"), None)

    fail_value_rows = {
        name: md.get_fail_detail()
        for name, md in mass_data_map.items()
    }

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
        major_fail_subject_rows=major_fail_subject_rows,
        total_dut=total_dut,
        pass_yield=pass_yield,
        df_yield=combined_df_yield if not combined_df_yield.empty else None,
        fail_value_rows=fail_value_rows,
    )

    if split is not None:
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
    result.distributions_a_only = _build_distributions(a_only_g, result.subjects_a_only)
    result.distributions_b_only = _build_distributions(b_only_g, result.subjects_b_only)


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


def _build_distributions(group: df_honey_group, subjects_meta: list) -> list:
    """선택된 각 subject 의 source 별 CDF 트레이스."""
    out = []
    names = group.names()
    for sm in subjects_meta:
        idx = sm["subject_id"]
        traces = []
        for name in names:
            xs, ys = group.distribution(idx, source_name=name)
            if xs.size == 0:
                continue
            traces.append({"source": name, "xs": xs, "ys": ys})
        if not traces:
            continue
        out.append(DistSeries(
            subject_id=idx,
            subject=sm["subject"],
            unit=sm["units"],
            lower_limit=sm["lower_limit"],
            upper_limit=sm["upper_limit"],
            traces=traces,
        ))
    return out
