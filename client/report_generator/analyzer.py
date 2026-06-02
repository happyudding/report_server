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

    yield_rows = work.yield_rate()
    cpk_rows = work.cpk()
    fail_item_rows = work.fail_items()
    issue_rows = B.build_issue_summary(mass_data_map)  # bin별 most-fail item 요약
    summary_rows = work.summary()
    major_fail_subject_rows = B.build_major_fail_subjects(mass_data_map)

    subjects_meta = _subjects_meta_from_group(work)

    distributions = _build_distributions(work, subjects_meta)

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

    _apply_diff_compare(work, result)
    return result


def _apply_diff_compare(group: df_honey_group, result: AnalysisResult) -> None:
    """2개 파일 subject 불일치 시 diff compare 결과를 result 에 채운다.

    공통(common) CPK/Distribution 은 이름 기반으로 재계산해 메인 시트(cpk/distribution)
    의 위치 기반 오정렬을 바로잡고, a_only/b_only 는 별도 필드에 담는다. Yield/Fail
    Item/Issue Table/Summary 는 병합 기준 기존 계산을 그대로 둔다.
    """
    split = group.split_for_diff()
    if split is None:
        return
    cl = split["classification"]
    common_g, a_only_g, b_only_g = split["common"], split["a_only"], split["b_only"]

    result.diff_classification = cl

    # 메인 cpk/distribution 을 common subjects 기준(이름 매칭)으로 재계산
    result.cpk_rows = B.build_cpk_for_subjects(common_g.mass_data_map, cl["common"])
    result.subjects = _subjects_meta_from_group(common_g)
    result.distributions = _build_distributions(common_g, result.subjects)

    # a_only / b_only
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
