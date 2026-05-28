"""report_generator — Honey 로컬 리포트 분석/생성 엔진.

분석 계층(df_honey / df_honey_group / analyzer / item_selector)은 PyQt·xlwings
비의존 순수 Python. xlsx_writer 만 xlwings(Excel) 의존.
"""
from .analyzer import run as analyze
from .df_honey import DfHoney
from .df_honey_group import DfHoneyGroup
from .item_selector import ItemSelector
from .models import AnalysisResult, DistSeries, ReportMeta

__all__ = [
    "analyze",
    "DfHoney",
    "DfHoneyGroup",
    "ItemSelector",
    "AnalysisResult",
    "DistSeries",
    "ReportMeta",
]


def build_report(csv_paths, meta=None, selected_items=None, out_path=None, sheets=None):
    """전체 흐름 헬퍼: CSV 경로들 → 분석 → (out_path 지정 시) xlsx 생성.

    Returns: out_path 지정 시 생성 경로(str), 아니면 AnalysisResult.
    """
    group = DfHoneyGroup.from_csvs(csv_paths, report_meta=meta)
    selector = ItemSelector(selected_items=selected_items)
    result = analyze(group, meta=meta, selector=selector)
    if out_path is None:
        return result
    from . import xlsx_writer
    xlsx_writer.write(result, out_path, sheets=sheets)
    return out_path
