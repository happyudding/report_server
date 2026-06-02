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

    first = next(iter(mass_data_map.values()))

    yield_rows = work.yield_rate()
    cpk_rows = work.cpk()
    fail_item_rows = work.fail_items()
    issue_rows = B.build_issue_summary(mass_data_map)  # bin별 most-fail item 요약
    summary_rows = work.summary()
    major_fail_subject_rows = B.build_major_fail_subjects(mass_data_map)

    subjects_meta = [
        {
            "subject_id": idx,
            "subject": subject,
            "units": first.units[idx] if idx < len(first.units) else "",
            "lower_limit": B._json_safe(first.lower_limits[idx] if idx < len(first.lower_limits) else None),
            "upper_limit": B._json_safe(first.upper_limits[idx] if idx < len(first.upper_limits) else None),
        }
        for idx, subject in enumerate(first.subjects)
    ]

    distributions = _build_distributions(work, subjects_meta)

    total_dut = sum(len(md.scores) for md in mass_data_map.values())
    pass_yield = next((r["portion (%)"] for r in yield_rows if str(r["bin"]) == "1"), None)

    fail_value_rows = {
        name: md.get_fail_detail()
        for name, md in mass_data_map.items()
    }

    combined_df_yield = group.combined_df_yield
    return AnalysisResult(
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
