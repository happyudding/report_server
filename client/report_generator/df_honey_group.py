"""df_honey_group — 여러 mass_data(df_honey) 를 하나의 report dataset 으로 묶는다.

여러 입력 sheet/CSV(각각 한 mass_data 단위)를 모아 item select(공통 subject 필터)
적용 + group-level 분석을 제공한다. 순수 Python.

각 mass_data 는 df_honey 인스턴스(단일 df 보유)이며, subject 선택/행 필터는
df_honey 의 select_subjects / subset_rows 슬라이싱 메서드에 위임한다.
"""
from __future__ import annotations

from typing import Optional

from . import _builders as B
from .df_honey import df_honey


class df_honey_group:
    def __init__(self, mass_data_list: list):
        # {source_name: mass_data(df_honey)}
        self._mass_data_map = {md.name: md for md in mass_data_list}

    # ------------------------------------------------------------------ 구성

    @classmethod
    def from_csvs(cls, paths, report_meta=None) -> "df_honey_group":
        mass_data_list = [df_honey.from_csv(p, report_meta=report_meta) for p in paths]
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

    def select_items(self, selected_items) -> "df_honey_group":
        """선택 subject 만 남긴 새 그룹 반환 (selected_items=None/[] 이면 self)."""
        if not selected_items:
            return self
        sel_set = set(selected_items)
        filtered = []
        for mass_data in self._mass_data_map.values():
            keep = [i for i, s in enumerate(mass_data.subjects) if s in sel_set]
            filtered.append(mass_data.select_subjects(keep))
        return df_honey_group(filtered)

    def filter_rows_by_bin(self, bin_value) -> "df_honey_group":
        """meta.Bin == bin_value 인 행만 남긴 새 그룹 (예: Bin1 Only)."""
        target = B._fmt_type(bin_value)
        filtered = []
        for mass_data in self._mass_data_map.values():
            binc = mass_data.meta["Bin"].map(B._fmt_type)
            filtered.append(mass_data.subset_rows(binc == target))
        return df_honey_group(filtered)

    def split_by_dut(self) -> "df_honey_group":
        """단일 mass_data 를 DUT 값별로 분할 (DUT 가 source/legend 가 됨).

        DUT 정리 모드: 입력 파일이 1개일 때만 가능.
        """
        if len(self._mass_data_map) != 1:
            raise ValueError("DUT 정리는 입력 파일이 1개일 때만 가능합니다.")
        mass_data = next(iter(self._mass_data_map.values()))
        duts = mass_data.meta["DUT"].map(B._fmt_type)
        new_list = []
        for dut in duts.unique():
            label = str(dut) if str(dut).strip() else "(blank)"
            new_list.append(mass_data.subset_rows(duts == dut, name=f"DUT {label}"))
        if not new_list:
            raise ValueError("DUT 정리: 분할할 DUT 값이 없습니다.")
        return df_honey_group(new_list)

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

    def major_fail_subjects(self, top: int = 5) -> list:
        return B.build_major_fail_subjects(self._mass_data_map, top=top)

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

    def raw_table(self):
        """df_honey 에 적재된 원본 측정 데이터를 단일 테이블로 반환.

        Returns (header, rows):
          header = ['Source', <meta 컬럼...>, <subject 이름...>]
          rows   = [[source, *meta_values, *score_values], ...]
        그룹은 동일 subject 를 가정하므로 첫 source 의 컬럼을 헤더로 쓰고,
        여러 source 는 행을 위아래로 이어붙인다 (Source 열로 구분).
        """
        header = None
        rows = []
        for name, md in self._mass_data_map.items():
            meta_cols = list(md.meta.columns)
            subjects = list(md.subjects)
            if header is None:
                header = ["Source"] + meta_cols + subjects
            meta_vals = md.meta.reset_index(drop=True).values.tolist()
            score_vals = md.scores.reset_index(drop=True).values.tolist()
            n = max(len(meta_vals), len(score_vals))
            for i in range(n):
                mrow = list(meta_vals[i]) if i < len(meta_vals) else [None] * len(meta_cols)
                srow = list(score_vals[i]) if i < len(score_vals) else [None] * len(subjects)
                rows.append([name] + mrow + srow)
        return (header or ["Source"]), rows

    def __len__(self):
        return len(self._mass_data_map)

    def __repr__(self):
        return f"df_honey_group(mass_data={self.names()})"
