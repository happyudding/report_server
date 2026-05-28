"""DfHoney — 단일 입력 sheet/CSV 에 대응하는 분석 단위.

규격화된 pandas DataFrame(meta + scores)과 메타데이터를 보유하고, sheet-level
기본 분석 메서드를 제공한다. PyQt/xlwings 비의존 순수 Python.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from . import _builders as B
from . import csv_loader
from .models import ReportMeta


class DfHoney:
    def __init__(self, name, subjects, units, lower_limits, upper_limits,
                 scores, meta, report_meta: Optional[ReportMeta] = None):
        self.name = name
        self.subjects = subjects
        self.units = units
        self.lower_limits = lower_limits
        self.upper_limits = upper_limits
        self.scores = scores      # 정수 인덱스 컬럼 (0..N-1)
        self.meta = meta          # columns: DUT, XCoord, YCoord, Bin, Serial
        self.report_meta = report_meta or ReportMeta()

    # ------------------------------------------------------------------ 생성

    @classmethod
    def from_csv(cls, path, report_meta: Optional[ReportMeta] = None,
                 name: Optional[str] = None) -> "DfHoney":
        path = Path(path)
        comp = csv_loader.load_components(path)
        rm = report_meta or ReportMeta()
        if not rm.source_path:
            rm.source_path = str(path)
        if not rm.sheet_name:
            rm.sheet_name = path.stem
        return cls(name=name or path.stem, report_meta=rm, **comp)

    @classmethod
    def from_dataframe(cls, raw_df: pd.DataFrame, name: str = "data",
                       report_meta: Optional[ReportMeta] = None) -> "DfHoney":
        """header=None 으로 읽은 raw DataFrame → DfHoney."""
        norm = csv_loader.normalize_raw(raw_df)
        comp = csv_loader.split_components(norm)
        return cls(name=name, report_meta=report_meta or ReportMeta(), **comp)

    # ------------------------------------------------------------------ 검증

    def validate(self) -> list:
        """schema 이슈 목록 반환 (빈 리스트면 정상)."""
        issues = []
        n = len(self.subjects)
        if n == 0:
            issues.append("subject(측정 항목)가 없습니다.")
        for attr in ("units", "lower_limits", "upper_limits"):
            if len(getattr(self, attr)) != n:
                issues.append(f"{attr} 길이({len(getattr(self, attr))})가 subject 수({n})와 다릅니다.")
        if self.scores.shape[1] != n:
            issues.append(f"scores 열 수({self.scores.shape[1]})가 subject 수({n})와 다릅니다.")
        if len(self.scores) == 0:
            issues.append("데이터 행이 없습니다.")
        for col in ("DUT", "XCoord", "YCoord", "Bin"):
            if col not in self.meta.columns:
                issues.append(f"meta 컬럼 누락: {col}")
        if "Bin" in self.meta.columns:
            binc = pd.to_numeric(self.meta["Bin"], errors="coerce")
            if binc.isna().all():
                issues.append("Bin 값을 숫자로 해석할 수 없습니다.")
        return issues

    def is_valid(self) -> bool:
        return not self.validate()

    # ------------------------------------------------------------------ 분석

    def _as_schools(self):
        return {self.name: self}

    def cpk(self, subject_idx=None) -> list:
        rows = B.build_cpk(self._as_schools())
        if subject_idx is None:
            return rows
        subject = self.subjects[subject_idx]
        return [r for r in rows if r["subject"] == subject]

    def yield_rate(self) -> list:
        return B.build_yield(self._as_schools())

    def fail_items(self) -> dict:
        return B.build_fail_items(self._as_schools())

    def fail_values(self) -> list:
        return B.build_issue_table(self._as_schools())

    def summary(self) -> list:
        return B.build_summary_rows(self._as_schools())

    def distribution(self, subject_idx) -> tuple:
        values = B.to_numeric_clean(self.scores.iloc[:, subject_idx])
        return B.cumulative_distribution_full(values)

    def fail_subject_ids(self) -> list:
        """fail 이 발생한 subject_id 목록 (item select 기본값용)."""
        mask = B._fail_mask_for_table(self)
        sums = mask.sum(axis=0)
        return [int(i) for i, c in sums.items() if int(c) > 0]

    def __repr__(self):
        return f"DfHoney(name={self.name!r}, subjects={len(self.subjects)}, rows={len(self.scores)})"
