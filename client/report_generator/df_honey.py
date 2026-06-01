"""df_honey — 하나의 mass_data 단위(반도체 웨이퍼/로트 측정 sheet·CSV) 분석 객체.

**df_honey 포맷 단일 DataFrame(`self.df`) 하나만 보유**하고, 분석에 필요한
subjects/units/limits/scores/meta 는 그 df 에서 파생하는 cached property 로 노출한다.
입력을 df 하나로 표준화해 슬라이싱(subject 선택 / 행 필터)과 코드 재사용을 단순화한다.

df 레이아웃 (csv_loader.csvfile_to_df = normalize_raw 결과):
    행 0 = subject 이름(헤더), 1 = Units, 2 = Lower Limit, 3 = Upper Limit,
    4~5 = limit 중복행, 6~ = 데이터.
    열 0~4 = meta(DUT/XCoord/YCoord/Bin/Serial), 5~ = subject 측정값.

PyQt/xlwings 비의존 순수 Python.
"""
from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import _builders as B
from . import csv_loader
from .constants import (
    DATA_START_ROW, LOWER_LIMIT_ROW, META_COLUMNS, N_META_COLUMNS,
    SUBJECT_NAME_ROW, UNITS_ROW, UPPER_LIMIT_ROW,
)
from .models import ReportMeta


class df_honey:
    def __init__(self, df: pd.DataFrame, name: str,
                 report_meta: Optional[ReportMeta] = None):
        self.df = df.reset_index(drop=True)
        self.name = name
        self.report_meta = report_meta or ReportMeta()

    # ------------------------------------------------------------------ 생성

    @classmethod
    def from_csv(cls, path, report_meta: Optional[ReportMeta] = None,
                 name: Optional[str] = None) -> "df_honey":
        path = Path(path)
        df = csv_loader.csvfile_to_df(path)
        rm = report_meta or ReportMeta()
        if not rm.source_path:
            rm.source_path = str(path)
        if not rm.sheet_name:
            rm.sheet_name = path.stem
        return cls(df, name=name or path.stem, report_meta=rm)

    @classmethod
    def from_dataframe(cls, raw_df: pd.DataFrame, name: str = "data",
                       report_meta: Optional[ReportMeta] = None) -> "df_honey":
        """header=None 으로 읽은 raw DataFrame → df_honey."""
        norm = csv_loader.normalize_raw(raw_df)
        return cls(norm, name=name, report_meta=report_meta or ReportMeta())

    def to_df(self) -> pd.DataFrame:
        """보유 중인 df_honey 포맷 단일 DataFrame 반환."""
        return self.df

    # ------------------------------------------------------------------ df 파생 컴포넌트

    @cached_property
    def subjects(self) -> list:
        return [str(s) for s in self.df.iloc[SUBJECT_NAME_ROW, N_META_COLUMNS:].tolist()]

    @cached_property
    def units(self) -> list:
        return [str(u) if pd.notna(u) else ""
                for u in self.df.iloc[UNITS_ROW, N_META_COLUMNS:].tolist()]

    @cached_property
    def lower_limits(self) -> list:
        return pd.to_numeric(self.df.iloc[LOWER_LIMIT_ROW, N_META_COLUMNS:],
                             errors="coerce").tolist()

    @cached_property
    def upper_limits(self) -> list:
        return pd.to_numeric(self.df.iloc[UPPER_LIMIT_ROW, N_META_COLUMNS:],
                             errors="coerce").tolist()

    @cached_property
    def _block(self) -> pd.DataFrame:
        return self.df.iloc[DATA_START_ROW:].reset_index(drop=True)

    @cached_property
    def meta(self) -> pd.DataFrame:
        meta = self._block.iloc[:, :N_META_COLUMNS].copy()
        meta.columns = META_COLUMNS
        return meta

    @cached_property
    def scores(self) -> pd.DataFrame:
        scores = self._block.iloc[:, N_META_COLUMNS:].copy()
        scores.columns = range(len(self.subjects))
        return scores

    # ------------------------------------------------------------------ 슬라이싱 (단일 df 기반)

    def select_subjects(self, keep_idx) -> "df_honey":
        """선택 subject 인덱스만 남긴 새 df_honey (meta 5열 + 선택 subject열).

        df_honey 의 모든 파생 property 가 위치(iloc) 기반이라, 단일 df 를 열 슬라이싱
        하는 것만으로 subject 선택이 일관되게 반영된다.
        """
        cols = list(range(N_META_COLUMNS)) + [N_META_COLUMNS + int(i) for i in keep_idx]
        new_df = self.df.iloc[:, cols].copy()
        return df_honey(new_df, name=self.name, report_meta=self.report_meta)

    def subset_rows(self, mask, name: Optional[str] = None) -> "df_honey":
        """데이터행 mask(bool, 길이=데이터행 수) 로 행 필터 → 헤더행 0~5 유지 새 df_honey."""
        m = mask.to_numpy() if hasattr(mask, "to_numpy") else np.asarray(mask)
        head = self.df.iloc[:DATA_START_ROW]
        data = self.df.iloc[DATA_START_ROW:]
        kept = data[m]
        new_df = pd.concat([head, kept], ignore_index=True)
        return df_honey(new_df, name=name or self.name, report_meta=self.report_meta)

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

    def _as_mass_data_map(self):
        """단일 mass_data 를 {name: self} map 으로 (builder 호환 어댑터)."""
        return {self.name: self}

    def cpk(self, subject_idx=None) -> list:
        rows = B.build_cpk(self._as_mass_data_map())
        if subject_idx is None:
            return rows
        subject = self.subjects[subject_idx]
        return [r for r in rows if r["subject"] == subject]

    def yield_rate(self) -> list:
        return B.build_yield(self._as_mass_data_map())

    def fail_items(self) -> dict:
        return B.build_fail_items(self._as_mass_data_map())

    def fail_values(self) -> list:
        return B.build_issue_table(self._as_mass_data_map())

    def summary(self) -> list:
        return B.build_summary_rows(self._as_mass_data_map())

    def major_fail_subjects(self, top: int = 5) -> list:
        return B.build_major_fail_subjects(self._as_mass_data_map(), top=top)

    def distribution(self, subject_idx) -> tuple:
        values = B.to_numeric_clean(self.scores.iloc[:, subject_idx])
        return B.cumulative_distribution_full(values)

    def fail_subject_ids(self) -> list:
        """fail 이 발생한 subject_id 목록 (item select 기본값용)."""
        mask = B._fail_mask(self)
        sums = mask.sum(axis=0)
        return [int(i) for i, c in sums.items() if int(c) > 0]

    def __repr__(self):
        return f"df_honey(name={self.name!r}, subjects={len(self.subjects)}, rows={len(self.scores)})"
