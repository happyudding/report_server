"""df_honey_group — 여러 mass_data(df_honey) 를 하나의 report dataset 으로 묶는다.

여러 입력 sheet/CSV(각각 한 mass_data 단위)를 모아 item select(공통 subject 필터)
적용 + group-level 분석을 제공한다. 순수 Python.

각 mass_data 는 df_honey 인스턴스(단일 df 보유)이며, subject 선택/행 필터는
df_honey 의 select_subjects / subset_rows 슬라이싱 메서드에 위임한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from . import _builders as B
from .csvfile_to_df import DF_YIELD_COLUMNS
from .df_honey import df_honey


class df_honey_group:
    def __init__(self, mass_data_list: list):
        # {source_name: mass_data(df_honey)}
        self._mass_data_map = {md.name: md for md in mass_data_list}

    # ------------------------------------------------------------------ 구성

    @classmethod
    def from_csvs(cls, paths, report_meta=None, progress_cb=None) -> "df_honey_group":
        """paths 목록을 순서대로 로드해 그룹 생성.

        progress_cb(done, total, filename) — 각 파일 로드 시작 전 호출.
        done == total 이면 완료 신호. csvfile_to_df 는 변경 없음.
        """
        paths = list(paths)
        n = len(paths)
        mass_data_list = []
        for i, p in enumerate(paths):
            filename = Path(p).name
            if progress_cb:
                progress_cb(i, n, filename)
            # 파일 내부 서브콜백 — 브랜치 교체 후 csvfile_to_df 가 지원하면 자동 동작
            sub_cb = (lambda s, t, _i=i, _n=n, _f=filename: progress_cb(_i, _n, _f, s, t)
                      ) if progress_cb else None
            mass_data_list.append(df_honey.from_csv(p, report_meta=report_meta,
                                                     progress_cb=sub_cb))
        if progress_cb:
            progress_cb(n, n, "")
        return cls(mass_data_list)

    @property
    def combined_df_yield(self) -> pd.DataFrame:
        """각 source 의 df_yield 를 이어붙인 전체 yield 집계 DataFrame."""
        frames = [md.df_yield for md in self._mass_data_map.values()
                  if md.df_yield is not None and not md.df_yield.empty]
        if not frames:
            return pd.DataFrame(columns=DF_YIELD_COLUMNS)
        return pd.concat(frames, ignore_index=True)

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

    def raw_frames(self):
        """각 source(input file)의 df_honey 포맷 DataFrame 을 (sheet명, df) 리스트로.

        sheet명 = source 이름(input file stem). Raw Data 시트 출력용 — df_honey 에
        적재된 포맷(subject 헤더 + Units/Lower/Upper/Lower/Upper limit + 데이터)을
        Source 열·제목 없이 그대로 내보낸다.
        """
        return [(md.name, md.to_df()) for md in self._mass_data_map.values()]

    def __len__(self):
        return len(self._mass_data_map)

    def __repr__(self):
        return f"df_honey_group(mass_data={self.names()})"
