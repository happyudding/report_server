from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    UPPER_LIMIT_ROW, LOWER_LIMIT_ROW, META_COLUMNS, N_META_COLUMNS,
    DATA_START_ROW, SUBJECT_NAME_ROW, UNITS_ROW,
)
from analysis.preprocess import cumulative_distribution_full, to_numeric_clean
from analysis.table_builder import (
    PASS_BIN,
    _build_cpk,
    _build_fail_items,
    _build_yield,
    _fmt_num,
    _fmt_type,
    _subject_columns,
)


class df_honey:
    """단일 파일/df 단위 분석 객체. ExcelData 속성과 호환되어 table_builder 함수와 바로 연동."""

    def __init__(self, name, subjects, units, lower_limits, upper_limits, scores, meta):
        self.name = name
        self.subjects = subjects
        self.units = units
        self.lower_limits = lower_limits
        self.upper_limits = upper_limits
        self.scores = scores    # int-indexed columns (0, 1, 2, ...)
        self.meta = meta        # columns: DUT, XCoord, YCoord, Bin

    @classmethod
    def from_file(cls, path) -> "df_honey":
        """CSV / Excel 파일 경로 → df_honey."""
        from analysis.data_loader import load_table
        path = Path(path)
        exc = load_table(path)
        return cls(
            name=path.stem,
            subjects=exc.subjects,
            units=exc.units,
            lower_limits=exc.lower_limits,
            upper_limits=exc.upper_limits,
            scores=exc.scores,
            meta=exc.meta,
        )

    @classmethod
    def from_df(cls, df: pd.DataFrame, name: str = "data") -> "df_honey":
        """Raw DataFrame (CSV와 동일한 포맷, header=None으로 읽은 상태) → df_honey."""
        row = lambda r: df.iloc[r, N_META_COLUMNS:]
        subjects = [str(s) for s in row(SUBJECT_NAME_ROW).tolist()]
        units = [str(u) if pd.notna(u) else "" for u in row(UNITS_ROW).tolist()]
        lo = pd.to_numeric(row(LOWER_LIMIT_ROW), errors="coerce").tolist()
        hi = pd.to_numeric(row(UPPER_LIMIT_ROW), errors="coerce").tolist()
        block = df.iloc[DATA_START_ROW:].reset_index(drop=True)
        meta = block.iloc[:, :N_META_COLUMNS].copy()
        meta.columns = META_COLUMNS
        scores = block.iloc[:, N_META_COLUMNS:].copy()
        scores.columns = range(len(subjects))
        return cls(name=name, subjects=subjects, units=units,
                   lower_limits=lo, upper_limits=hi, scores=scores, meta=meta)

    # ------------------------------------------------------------------
    # internal

    def _as_schools(self):
        return {self.name: self}

    # ------------------------------------------------------------------
    # 분석 메서드

    def cpk(self, subject_idx=None) -> list:
        """CPK 통계. subject_idx 지정 시 해당 subject만, None이면 전체."""
        rows = _build_cpk(self._as_schools())
        if subject_idx is None:
            return rows
        subject = self.subjects[subject_idx]
        return [r for r in rows if r["subject"] == subject]

    def yield_rate(self) -> list:
        """Bin별 수율 breakdown."""
        return _build_yield(self._as_schools())

    def distribution(self, subject_idx) -> tuple:
        """지정 subject의 누적분포 (xs, ys) numpy arrays."""
        values = to_numeric_clean(self.scores.iloc[:, subject_idx])
        return cumulative_distribution_full(values)

    def fail_items(self) -> dict:
        """수율 + fail subject 목록."""
        return _build_fail_items(self._as_schools())

    def fail_values(self) -> list:
        """비합격 DUT별 벗어난 측정값 상세 (lower/upper limit 초과 레코드)."""
        subjects_list = _subject_columns(self)
        n_sub = len(subjects_list)

        meta = self.meta.reset_index(drop=True).copy()
        meta["Bin"] = meta["Bin"].map(_fmt_type)

        non_pass_mask = meta["Bin"] != PASS_BIN
        if not non_pass_mask.any():
            return []

        meta_np = meta[non_pass_mask].reset_index(drop=True)
        scores_np = self.scores[non_pass_mask].reset_index(drop=True)
        numeric = scores_np.apply(pd.to_numeric, errors="coerce")

        lo_arr = [self.lower_limits[i] if i < len(self.lower_limits) else None for i in range(n_sub)]
        hi_arr = [self.upper_limits[i] if i < len(self.upper_limits) else None for i in range(n_sub)]

        fail_lo = pd.DataFrame(False, index=numeric.index, columns=numeric.columns)
        fail_hi = pd.DataFrame(False, index=numeric.index, columns=numeric.columns)

        for idx in range(n_sub):
            lo, hi = lo_arr[idx], hi_arr[idx]
            col_s = numeric.iloc[:, idx]
            if lo is not None and pd.notna(lo):
                fail_lo.iloc[:, idx] = (col_s < float(lo)).fillna(False)
            if hi is not None and pd.notna(hi):
                fail_hi.iloc[:, idx] = (col_s > float(hi)).fillna(False)

        fail_any = fail_lo | fail_hi
        failing = fail_any.stack()
        failing = failing[failing]
        if len(failing) == 0:
            return []

        rows = []
        for row_i, col_i in failing.index:
            lo, hi = lo_arr[col_i], hi_arr[col_i]
            is_lo = bool(fail_lo.at[row_i, col_i])
            meta_row = meta_np.iloc[row_i]
            rows.append({
                "source": self.name,
                "dut": _fmt_type(meta_row["DUT"]),
                "x_coord": _fmt_type(meta_row["XCoord"]),
                "y_coord": _fmt_type(meta_row["YCoord"]),
                "bin": _fmt_type(meta_row["Bin"]),
                "subject": subjects_list[col_i],
                "value": _fmt_num(numeric.at[row_i, col_i]),
                "lower_limit": _fmt_num(lo) if (lo is not None and pd.notna(lo)) else "N/A",
                "upper_limit": _fmt_num(hi) if (hi is not None and pd.notna(hi)) else "N/A",
                "fail": "< lo" if is_lo else "> hi",
            })
        return rows

    def summary(self) -> list:
        """item × bin 단위 summary rows (build_summary_rows 결과)."""
        from report.report_analysis_service import build_summary_rows
        return build_summary_rows(self._as_schools())

    def __repr__(self):
        return (
            f"df_honey(name={self.name!r}, "
            f"subjects={len(self.subjects)}, "
            f"rows={len(self.scores)})"
        )


class df_honey_group:
    """여러 df_honey를 묶어 비교/통합 분석하는 객체."""

    def __init__(self, honeys: list):
        self._schools = {h.name: h for h in honeys}

    # ------------------------------------------------------------------
    # 통합 분석

    def cpk(self) -> list:
        """전체 source 통합 CPK (per-source + total 행 포함)."""
        return _build_cpk(self._schools)

    def yield_rate(self) -> list:
        """전체 source 통합 수율."""
        return _build_yield(self._schools)

    def fail_items(self) -> dict:
        """전체 source 통합 fail item."""
        return _build_fail_items(self._schools)

    def summary(self) -> list:
        """item × bin 단위 summary rows."""
        from report.report_analysis_service import build_summary_rows
        return build_summary_rows(self._schools)

    def distribution(self, subject_idx, school_name=None):
        """누적분포. school_name 지정 시 (xs, ys), None이면 {name: (xs, ys)} dict."""
        if school_name:
            h = self._schools[school_name]
            values = to_numeric_clean(h.scores.iloc[:, subject_idx])
            return cumulative_distribution_full(values)
        return {
            name: cumulative_distribution_full(
                to_numeric_clean(h.scores.iloc[:, subject_idx])
            )
            for name, h in self._schools.items()
        }

    def compare_cpk(self) -> pd.DataFrame:
        """source별 CPK를 나란히 비교하는 DataFrame (index=subject, columns=source)."""
        rows = _build_cpk(self._schools)
        df = pd.DataFrame(rows)
        return df.pivot_table(index="subject", columns="source", values="cpk", aggfunc="first")

    def names(self) -> list:
        return list(self._schools.keys())

    def __len__(self):
        return len(self._schools)

    def __repr__(self):
        return f"df_honey_group(schools={self.names()})"
