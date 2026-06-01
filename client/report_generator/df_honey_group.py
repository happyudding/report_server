"""DfHoneyGroup — 여러 mass_data(DfHoney) 를 하나의 report dataset 으로 묶는다.

여러 입력 sheet/CSV(각각 한 mass_data 단위)를 모아 item select(공통 subject 필터)
적용 + group-level 분석을 제공한다. 순수 Python.
"""
from __future__ import annotations

from typing import Optional

from . import _builders as B
from .df_honey import DfHoney


class DfHoneyGroup:
    def __init__(self, mass_data_list: list):
        # {source_name: mass_data(DfHoney)}
        self._mass_data_map = {md.name: md for md in mass_data_list}

    # ------------------------------------------------------------------ 구성

    @classmethod
    def from_csvs(cls, paths, report_meta=None) -> "DfHoneyGroup":
        mass_data_list = [DfHoney.from_csv(p, report_meta=report_meta) for p in paths]
        return cls(mass_data_list)

    @property
    def mass_data_map(self) -> dict:
        return self._mass_data_map

    def names(self) -> list:
        return list(self._mass_data_map.keys())

    def subjects(self) -> list:
        """첫 source 기준 subject 이름 목록 (그룹은 동일 subject 가정)."""
        if not self._mass_data_map:
            return []
        return list(next(iter(self._mass_data_map.values())).subjects)

    def validate(self) -> dict:
        """{source_name: [issues...]} (정상이면 빈 리스트)."""
        return {name: md.validate() for name, md in self._mass_data_map.items()}

    # ------------------------------------------------------------------ 필터

    def select_items(self, selected_items) -> "DfHoneyGroup":
        """선택 subject 만 남긴 새 그룹 반환 (selected_items=None/[] 이면 self)."""
        if not selected_items:
            return self
        sel_set = set(selected_items)
        filtered = []
        for name, mass_data in self._mass_data_map.items():
            keep = [i for i, s in enumerate(mass_data.subjects) if s in sel_set]
            new_scores = (mass_data.scores.iloc[:, keep].copy() if keep
                          else mass_data.scores.iloc[:, 0:0].copy())
            new_scores.columns = list(range(len(keep)))
            filtered.append(DfHoney(
                name=name,
                subjects=[mass_data.subjects[i] for i in keep],
                units=[mass_data.units[i] if i < len(mass_data.units) else "" for i in keep],
                lower_limits=[mass_data.lower_limits[i] if i < len(mass_data.lower_limits) else None for i in keep],
                upper_limits=[mass_data.upper_limits[i] if i < len(mass_data.upper_limits) else None for i in keep],
                scores=new_scores,
                meta=mass_data.meta,
                report_meta=mass_data.report_meta,
            ))
        return DfHoneyGroup(filtered)

    @staticmethod
    def _subset_rows(mass_data, name, mask) -> DfHoney:
        """row mask(bool Series/array) 로 mass_data 의 행을 잘라 새 DfHoney 생성."""
        m = mask.to_numpy() if hasattr(mask, "to_numpy") else mask
        new_scores = mass_data.scores[m].reset_index(drop=True).copy()
        new_scores.columns = list(range(new_scores.shape[1]))
        new_meta = mass_data.meta[m].reset_index(drop=True).copy()
        return DfHoney(
            name=name,
            subjects=list(mass_data.subjects),
            units=list(mass_data.units),
            lower_limits=list(mass_data.lower_limits),
            upper_limits=list(mass_data.upper_limits),
            scores=new_scores,
            meta=new_meta,
            report_meta=mass_data.report_meta,
        )

    def filter_rows_by_bin(self, bin_value) -> "DfHoneyGroup":
        """meta.Bin == bin_value 인 행만 남긴 새 그룹 (예: Bin1 Only)."""
        target = B._fmt_type(bin_value)
        filtered = []
        for name, mass_data in self._mass_data_map.items():
            binc = mass_data.meta["Bin"].map(B._fmt_type)
            mask = binc == target
            filtered.append(self._subset_rows(mass_data, name, mask))
        return DfHoneyGroup(filtered)

    def split_by_dut(self) -> "DfHoneyGroup":
        """단일 mass_data 를 DUT 값별로 분할 (DUT 가 source/legend 가 됨).

        DUT 정리 모드: 입력 파일이 1개일 때만 가능.
        """
        if len(self._mass_data_map) != 1:
            raise ValueError("DUT 정리는 입력 파일이 1개일 때만 가능합니다.")
        mass_data = next(iter(self._mass_data_map.values()))
        duts = mass_data.meta["DUT"].map(B._fmt_type)
        new_list = []
        for dut in duts.unique():
            mask = duts == dut
            label = str(dut) if str(dut).strip() else "(blank)"
            new_list.append(self._subset_rows(mass_data, f"DUT {label}", mask))
        if not new_list:
            raise ValueError("DUT 정리: 분할할 DUT 값이 없습니다.")
        return DfHoneyGroup(new_list)

    # ------------------------------------------------------------------ 분석

    def cpk(self) -> list:
        return B.build_cpk(self._mass_data_map)

    def yield_rate(self) -> list:
        return B.build_yield(self._mass_data_map)

    def fail_items(self) -> list:
        return B.build_fail_items(self._mass_data_map)["rows"]

    def issue_table(self) -> list:
        return B.build_issue_table(self._mass_data_map)

    def summary(self) -> list:
        return B.build_summary_rows(self._mass_data_map)

    def distribution(self, subject_idx, source_name: Optional[str] = None):
        """누적분포. source_name 지정 시 (xs, ys), None 이면 {name: (xs, ys)}."""
        if source_name:
            md = self._mass_data_map[source_name]
            return B.cumulative_distribution_full(B.to_numeric_clean(md.scores.iloc[:, subject_idx]))
        return {
            name: B.cumulative_distribution_full(B.to_numeric_clean(md.scores.iloc[:, subject_idx]))
            for name, md in self._mass_data_map.items()
        }

    def fail_subject_ids(self) -> list:
        """그룹 전체에서 fail 이 발생한 subject_id 목록 (item select 기본값)."""
        ids = set()
        for md in self._mass_data_map.values():
            ids.update(md.fail_subject_ids())
        return sorted(ids)

    def __len__(self):
        return len(self._mass_data_map)

    def __repr__(self):
        return f"DfHoneyGroup(mass_data={self.names()})"
