"""DfHoneyGroup — 여러 DfHoney 를 하나의 report dataset 으로 묶는다.

item select(공통 subject 필터) 적용 + group-level 분석 제공. 순수 Python.
"""
from __future__ import annotations

from typing import Optional

from . import _builders as B
from .df_honey import DfHoney


class DfHoneyGroup:
    def __init__(self, honeys: list):
        self._schools = {h.name: h for h in honeys}

    # ------------------------------------------------------------------ 구성

    @classmethod
    def from_csvs(cls, paths, report_meta=None) -> "DfHoneyGroup":
        honeys = [DfHoney.from_csv(p, report_meta=report_meta) for p in paths]
        return cls(honeys)

    @property
    def schools(self) -> dict:
        return self._schools

    def names(self) -> list:
        return list(self._schools.keys())

    def subjects(self) -> list:
        """첫 source 기준 subject 이름 목록 (그룹은 동일 subject 가정)."""
        if not self._schools:
            return []
        return list(next(iter(self._schools.values())).subjects)

    def validate(self) -> dict:
        """{source_name: [issues...]} (정상이면 빈 리스트)."""
        return {name: h.validate() for name, h in self._schools.items()}

    # ------------------------------------------------------------------ 필터

    def select_items(self, selected_items) -> "DfHoneyGroup":
        """선택 subject 만 남긴 새 그룹 반환 (selected_items=None/[] 이면 self)."""
        if not selected_items:
            return self
        sel_set = set(selected_items)
        filtered = []
        for name, table in self._schools.items():
            keep = [i for i, s in enumerate(table.subjects) if s in sel_set]
            new_scores = (table.scores.iloc[:, keep].copy() if keep
                          else table.scores.iloc[:, 0:0].copy())
            new_scores.columns = list(range(len(keep)))
            filtered.append(DfHoney(
                name=name,
                subjects=[table.subjects[i] for i in keep],
                units=[table.units[i] if i < len(table.units) else "" for i in keep],
                lower_limits=[table.lower_limits[i] if i < len(table.lower_limits) else None for i in keep],
                upper_limits=[table.upper_limits[i] if i < len(table.upper_limits) else None for i in keep],
                scores=new_scores,
                meta=table.meta,
                report_meta=table.report_meta,
            ))
        return DfHoneyGroup(filtered)

    # ------------------------------------------------------------------ 분석

    def cpk(self) -> list:
        return B.build_cpk(self._schools)

    def yield_rate(self) -> list:
        return B.build_yield(self._schools)

    def fail_items(self) -> list:
        return B.build_fail_items(self._schools)["rows"]

    def issue_table(self) -> list:
        return B.build_issue_table(self._schools)

    def summary(self) -> list:
        return B.build_summary_rows(self._schools)

    def distribution(self, subject_idx, source_name: Optional[str] = None):
        """누적분포. source_name 지정 시 (xs, ys), None 이면 {name: (xs, ys)}."""
        if source_name:
            h = self._schools[source_name]
            return B.cumulative_distribution_full(B.to_numeric_clean(h.scores.iloc[:, subject_idx]))
        return {
            name: B.cumulative_distribution_full(B.to_numeric_clean(h.scores.iloc[:, subject_idx]))
            for name, h in self._schools.items()
        }

    def fail_subject_ids(self) -> list:
        """그룹 전체에서 fail 이 발생한 subject_id 목록 (item select 기본값)."""
        ids = set()
        for h in self._schools.values():
            ids.update(h.fail_subject_ids())
        return sorted(ids)

    def __len__(self):
        return len(self._schools)

    def __repr__(self):
        return f"DfHoneyGroup(schools={self.names()})"
