"""분석 결과 / 메타 데이터 모델.

cpk / yield / fail_item / issue / summary 는 이식한 builder 가 그대로 list[dict] 를
반환하므로 dict 형태를 유지한다 (불필요한 변환 제거). distribution 은 numpy 배열을
담아야 해서 DistSeries dataclass 로 표현한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd


@dataclass
class ReportMeta:
    """리포트 단위 메타. UI 입력값 + 원본 파일 정보."""
    product_type: str = ""
    product: str = ""
    lot_id: str = ""
    revision: str = ""
    process: str = ""
    source_path: str = ""          # 단일 파일 기준(그룹은 sources 로 별도 관리)
    sheet_name: str = ""


@dataclass
class DistSeries:
    """단일 subject 누적분포(CDF) — source 별 트레이스 묶음."""
    subject_id: int
    subject: str
    unit: str
    lower_limit: Optional[float]
    upper_limit: Optional[float]
    traces: list = field(default_factory=list)  # [{"source": str, "xs": np.ndarray, "ys": np.ndarray}]


@dataclass
class AnalysisResult:
    """group-level 분석 결과 + 메타 묶음. xlsx_writer 의 단일 입력."""
    meta: ReportMeta
    sources: list = field(default_factory=list)        # source 이름 순서
    subjects: list = field(default_factory=list)        # [{subject_id, subject, units, lower_limit, upper_limit}]
    cpk_rows: list = field(default_factory=list)         # list[dict]
    yield_rows: list = field(default_factory=list)       # list[dict]
    fail_item_rows: list = field(default_factory=list)   # list[dict] (fail_subjects 포함)
    issue_rows: list = field(default_factory=list)       # list[dict] (fail_values)
    summary_rows: list = field(default_factory=list)     # list[dict]
    distributions: list = field(default_factory=list)    # list[DistSeries]
    major_fail_subject_rows: list = field(default_factory=list)  # [{subject, fail_count, ratio}]
    total_dut: int = 0
    pass_yield: Optional[float] = None                   # Bin 1 portion (%)
    df_yield: Optional[pd.DataFrame] = None              # per-(step,Bin,Tno,item) yield 집계
    fail_value_rows: dict = field(default_factory=dict)  # {source_name: list[dict]}

    def summary_feature(self) -> dict:
        """summary 시트 Device Feature 섹션 값 (Fail Types 는 fail bin 번호 목록)."""
        fail_bins = sorted({str(r.get("bin")) for r in self.yield_rows
                            if str(r.get("bin")) != "1"},
                           key=lambda b: (0, int(b)) if b.isdigit() else (1, b))
        return {
            "Total DUT": self.total_dut,
            "Pass (Bin 1)": self._pass_count(),
            "Fail Types": ", ".join(fail_bins) if fail_bins else "-",
            "Sources": len(self.sources),
            "Subjects": len(self.subjects),
            "EVT Version": self.meta.revision or "-",
        }

    def _pass_count(self) -> int:
        for r in self.yield_rows:
            if str(r.get("bin")) == "1":
                return int(r.get("count") or 0)
        return 0

    def major_fail_bins(self, top: int = 5) -> list:
        """avg 내림차순 상위 fail bin (legacy summary Major Fail Bins 테이블용)."""
        fails = [r for r in self.yield_rows if str(r.get("bin")) != "1"]
        fails.sort(key=lambda r: -(r.get("avg") or 0.0))
        return fails[:top]

    def major_fail_subjects(self, top: int = 5) -> list:
        """subject별 총 fail 랭킹 상위 top (summary 시트 1st~5th Fail 용).

        [{"subject", "fail_count", "ratio"}]. analyzer 가 채운 행을 그대로 슬라이스.
        """
        return self.major_fail_subject_rows[:top]


def _to_native(value: Any):
    """numpy 스칼라 → python 기본형 (xlwings/Excel 호환)."""
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value
