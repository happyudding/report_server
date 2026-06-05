"""df_honey — 하나의 mass_data 단위(반도체 웨이퍼/로트 측정 sheet·CSV) 분석 객체.

**df_honey 포맷 단일 DataFrame(`self.df`) 하나만 보유**하고, 분석에 필요한
subjects/units/limits/scores/meta 는 그 df 에서 파생하는 cached property 로 노출한다.
입력을 df 하나로 표준화해 슬라이싱(subject 선택 / 행 필터)과 코드 재사용을 단순화한다.

df 레이아웃 (csv_loader.csvfile_to_df = normalize_raw 결과):
    columns = subject 이름(DUT/XCoord/…/item1/item2…), 0 = Units,
    1 = Lower Limit, 2 = Upper Limit, 3~4 = limit 중복행, 5~ = 데이터.
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
    PASS_BIN, UNITS_ROW, UPPER_LIMIT_ROW,
)
from .csvfile_to_df import DF_YIELD_COLUMNS
from .models import ReportMeta

# FileName(=계열명) fallback 시 stem 절단 길이. Yield 의 sheetname(외부 honey_parse 가
# 만든 10자리 절단 이름)과 일관되도록 df_yield 가 없을 때 path.stem 을 이만큼 자른다.
_NAME_MAXLEN = 10


def _sheetname_from_df_yield(df_yield) -> Optional[str]:
    """df_yield 의 'sheetname' 컬럼에서 이 파일의 대표 sheetname(최빈값 1개) 반환.

    Yield 시트가 그대로 출력하는 10자리 절단·자동생성 이름. 비어있거나 컬럼이 없으면
    None — 호출자가 path.stem fallback 으로 처리.
    """
    if df_yield is None or getattr(df_yield, "empty", True):
        return None
    if "sheetname" not in df_yield.columns:
        return None
    vals = df_yield["sheetname"].dropna().astype(str)
    vals = vals[vals.str.strip() != ""]
    if vals.empty:
        return None
    return str(vals.mode().iloc[0])


class df_honey:
    def __init__(self, df: pd.DataFrame, name: str,
                 report_meta: Optional[ReportMeta] = None):
        self.df = df.reset_index(drop=True)
        self.name = name
        self.report_meta = report_meta or ReportMeta()
        self.df_yield: pd.DataFrame = pd.DataFrame(columns=DF_YIELD_COLUMNS)

    # FileName = 이 mass_data(input file)의 단일 표시 라벨. yield 컬럼명 / cpk source /
    # issue_table 컬럼명 / distribution legend 가 모두 이 값을 공유한다(네 출력이 항상
    # 동일 문자열). df_honey_group 이 생성 시 유일화하므로 같은 stem 두 파일도 a, a_2 로
    # 분리된다. self.name 의 별칭 — 기존 .name 참조 코드와 호환 유지.
    @property
    def FileName(self) -> str:
        return self.name

    @FileName.setter
    def FileName(self, value: str) -> None:
        self.name = value

    # ------------------------------------------------------------------ 생성

    @classmethod
    def from_csv(cls, path, report_meta: Optional[ReportMeta] = None,
                 name: Optional[str] = None,
                 progress_cb=None) -> "df_honey":
        path = Path(path)
        df, df_yield = csv_loader.csvfile_to_df(path, progress_cb=progress_cb)
        rm = report_meta or ReportMeta()
        if not rm.source_path:
            rm.source_path = str(path)
        # FileName(=계열명) = Yield 의 sheetname 과 통일. df_yield 가 비면 stem[:10] fallback.
        sheetname = _sheetname_from_df_yield(df_yield)
        if not rm.sheet_name:
            rm.sheet_name = sheetname or path.stem
        canonical = name or sheetname or path.stem[:_NAME_MAXLEN]
        instance = cls(df, name=canonical, report_meta=rm)
        instance.df_yield = df_yield
        return instance

    @classmethod
    def from_dataframe(cls, raw_df: pd.DataFrame, name: str = "data",
                       report_meta: Optional[ReportMeta] = None) -> "df_honey":
        """header=None 으로 읽은 raw DataFrame → df_honey."""
        norm = csv_loader.normalize_raw(raw_df)
        return cls(norm, name=name, report_meta=report_meta or ReportMeta())

    def to_df(self) -> pd.DataFrame:
        """보유 중인 df_honey 포맷 단일 DataFrame 반환."""
        return self.df

    def numeric_frame(self) -> pd.DataFrame:
        """측정행렬 DataFrame (행=DUT, 열=subject 이름, 값=수치). numeric_scores 재사용.

        distribution 차트가 all-DUT ECDF(열별 정렬 + rank/count)를 직접 산출하는 데 쓴다.
        """
        df = self.numeric_scores.copy()
        df.columns = list(self.subjects)
        return df

    # ------------------------------------------------------------------ df 파생 컴포넌트

    @cached_property
    def subjects(self) -> list:
        return [str(s) for s in self.df.columns[N_META_COLUMNS:].tolist()]

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

    # ------------------------------------------------------------------ fail 계산 캐시
    # 무거운 계산(numeric 변환 / fail mask)을 mass_data 단위로 1회만 평가·보존한다.
    # 모든 builder 가 이 attribute 만 참조하도록 통일 — 호출마다 재계산하지 않는다.
    # 슬라이싱(select_subjects/subset_rows)은 새 인스턴스를 만들므로 캐시도 새로 시작.

    @cached_property
    def numeric_scores(self) -> pd.DataFrame:
        """scores 전체를 수치로 1회 변환 (columns = range(n_sub) 유지)."""
        return self.scores.apply(pd.to_numeric, errors="coerce")

    @cached_property
    def _limit_arrays(self):
        """(lo, hi) float64 배열 — subject 열 정렬. 범위밖·None·비수치 → NaN."""
        n_sub = len(self.numeric_scores.columns)

        def _lim(seq, i):
            if i >= len(seq):
                return np.nan
            v = seq[i]
            if v is None:
                return np.nan
            try:
                return float(v)
            except (TypeError, ValueError):
                return np.nan

        lo = np.array([_lim(self.lower_limits, i) for i in range(n_sub)], dtype="float64")
        hi = np.array([_lim(self.upper_limits, i) for i in range(n_sub)], dtype="float64")
        return lo, hi

    @cached_property
    def fail_mask_lo(self) -> pd.DataFrame:
        """value < lower limit 인 셀 bool DataFrame (NaN·결측 limit → False)."""
        numeric = self.numeric_scores
        arr = numeric.to_numpy(dtype="float64", copy=False)
        lo, _ = self._limit_arrays
        with np.errstate(invalid="ignore"):
            fail = arr < lo
        return pd.DataFrame(fail, index=numeric.index, columns=numeric.columns, copy=False)

    @cached_property
    def fail_mask_hi(self) -> pd.DataFrame:
        """value > upper limit 인 셀 bool DataFrame (NaN·결측 limit → False)."""
        numeric = self.numeric_scores
        arr = numeric.to_numpy(dtype="float64", copy=False)
        _, hi = self._limit_arrays
        with np.errstate(invalid="ignore"):
            fail = arr > hi
        return pd.DataFrame(fail, index=numeric.index, columns=numeric.columns, copy=False)

    @cached_property
    def fail_mask_break(self) -> pd.DataFrame:
        """stop-on-fail 로 data 흐름이 뚝 끊긴 시점(말미 연속 NaN 런의 시작 열) bool DataFrame.

        각 DUT(행)에서 측정값이 끝까지 이어지지 않고 어느 item 부터 끝까지 모두 비어버린
        (NaN) 경우, 그 끊긴 첫 item 한 곳만 True. 전부 NaN 인 행과 PASS_BIN DUT 는 제외
        (옵션 미측정 오탐 방지). limit 위반과 별개의 fail 원인.
        """
        numeric = self.numeric_scores
        isnan = numeric.isna().to_numpy()
        n_rows, n_sub = isnan.shape
        out = np.zeros((n_rows, n_sub), dtype=bool)
        if n_rows == 0 or n_sub == 0:
            return pd.DataFrame(out, index=numeric.index, columns=numeric.columns, copy=False)
        # 말미 연속 NaN 런: col..끝 이 모두 NaN 인 위치 (오른쪽부터 cumprod)
        trailing = np.cumprod(isnan[:, ::-1], axis=1)[:, ::-1].astype(bool)
        # onset = 런의 시작(False→True 전환) 한 곳만
        prev = np.zeros_like(trailing)
        prev[:, 1:] = trailing[:, :-1]
        onset = trailing & ~prev
        onset[:, 0] = False   # col 0 onset = 전부 NaN 행 → 제외 (앞에 valid 값 없음)
        # PASS_BIN DUT 제외
        bins = self.meta["Bin"].map(B._fmt_type).to_numpy()
        onset &= (bins != PASS_BIN)[:, None]
        return pd.DataFrame(onset, index=numeric.index, columns=numeric.columns, copy=False)

    @cached_property
    def fail_mask(self) -> pd.DataFrame:
        """각 측정값의 fail 여부 bool DataFrame — limit 위반(lo∪hi) ∪ data 흐름 끊김(break).

        fail item 과 fail value 가 동일 정의를 공유한다.
        """
        return self.fail_mask_lo | self.fail_mask_hi | self.fail_mask_break

    # ------------------------------------------------------------------ 슬라이싱 (단일 df 기반)

    def select_subjects(self, keep_idx) -> "df_honey":
        """선택 subject 인덱스만 남긴 새 df_honey (meta 5열 + 선택 subject열).

        df_honey 의 모든 파생 property 가 위치(iloc) 기반이라, 단일 df 를 열 슬라이싱
        하는 것만으로 subject 선택이 일관되게 반영된다.
        """
        cols = list(range(N_META_COLUMNS)) + [N_META_COLUMNS + int(i) for i in keep_idx]
        new_df = self.df.iloc[:, cols].copy()
        new = df_honey(new_df, name=self.name, report_meta=self.report_meta)
        new.df_yield = self.df_yield.copy()
        return new

    def subset_rows(self, mask, name: Optional[str] = None) -> "df_honey":
        """데이터행 mask(bool, 길이=데이터행 수) 로 행 필터 → 헤더행 0~5 유지 새 df_honey."""
        m = mask.to_numpy() if hasattr(mask, "to_numpy") else np.asarray(mask)
        head = self.df.iloc[:DATA_START_ROW]
        data = self.df.iloc[DATA_START_ROW:]
        kept = data[m]
        new_df = pd.concat([head, kept], ignore_index=True)
        new = df_honey(new_df, name=name or self.name, report_meta=self.report_meta)
        new.df_yield = self.df_yield.copy()
        return new

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

    def get_fail_detail(self) -> list:
        """Bin != 1 DUT 의 항목별 한계 이탈 레코드.

        반환: [{"dut", "xcoord", "ycoord", "bin", "item", "value"}, ...]
        """
        return [
            {
                "dut":    r["dut"],
                "xcoord": r["x_coord"],
                "ycoord": r["y_coord"],
                "bin":    r["bin"],
                "item":   r["subject"],
                "value":  r["value"],
            }
            for r in self.fail_values()
        ]

    def summary(self) -> list:
        return B.build_summary_rows(self._as_mass_data_map())

    def major_fail_subjects(self, top: int = 5) -> list:
        return B.build_major_fail_subjects(self._as_mass_data_map(), top=top)

    def distribution(self, subject_idx) -> tuple:
        values = B.to_numeric_clean(self.scores.iloc[:, subject_idx])
        return B.cumulative_distribution_full(values)

    def fail_subject_ids(self) -> list:
        """fail 이 발생한 subject_id 목록 (item select 기본값용)."""
        mask = self.fail_mask
        sums = mask.sum(axis=0)
        return [int(i) for i, c in sums.items() if int(c) > 0]

    def __repr__(self):
        return f"df_honey(name={self.name!r}, subjects={len(self.subjects)}, rows={len(self.scores)})"
