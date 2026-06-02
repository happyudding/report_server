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

    def rename_sources(self, new_names) -> None:
        """각 source(input file)의 legend 명(Filename)을 순서대로 교체.

        new_names 길이가 부족하면 앞에서부터만 적용하고 나머지는 기존명 유지.
        빈 문자열은 무시(기존명 유지). 중복명은 _2, _3 … 접미사로 회피.
        """
        old = list(self._mass_data_map.values())
        used = set()
        renamed = []
        for i, md in enumerate(old):
            want = (str(new_names[i]).strip() if i < len(new_names) else "") or md.name
            base, cand, n = want, want, 2
            while cand in used:
                cand = f"{base}_{n}"
                n += 1
            md.name = cand
            used.add(cand)
            renamed.append(md)
        self._mass_data_map = {md.name: md for md in renamed}

    def subjects(self) -> list:
        """모든 source 의 subject 이름 합집합 (등장 순서 유지).

        파일마다 subject 구성이 다를 수 있어(diff compare) 첫 파일만 보면 다른
        파일에만 있는 항목이 누락된다. item 선택 UI 가 common/only-A/only-B 를
        모두 볼 수 있도록 각 파일 순서대로 합집합을 만든다. (동일 구성이면 첫 파일
        목록과 동일.)
        """
        names, seen = [], set()
        for md in self._mass_data_map.values():
            for s in md.subjects:
                s = str(s)
                if s not in seen:
                    seen.add(s)
                    names.append(s)
        return names

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

    def split_for_diff(self) -> Optional[dict]:
        """diff compare: 2개 파일의 subject 를 common/a_only/b_only 서브그룹으로 분할.

        파일이 2개가 아니거나 두 파일 subject 집합이 동일하면 None (기존 단일 모드).
        반환: {common, a_only, b_only, classification}. 각 서브그룹은 해당 분류의
        subject 만 남긴 df_honey_group (source 이름은 보존).
        """
        classification = B.classify_subjects(self._mass_data_map)
        if classification is None:
            return None
        name_a, name_b = classification["name_a"], classification["name_b"]
        md_a, md_b = self._mass_data_map[name_a], self._mass_data_map[name_b]

        def _keep(md, subject_names):
            names0 = [str(s) for s in md.subjects]
            idx_list = [names0.index(s) for s in subject_names if s in names0]
            return md.select_subjects(idx_list)

        common_g = df_honey_group([
            _keep(md_a, classification["common"]),
            _keep(md_b, classification["common"]),
        ])
        a_only_g = df_honey_group([_keep(md_a, classification["a_only"])])
        b_only_g = df_honey_group([_keep(md_b, classification["b_only"])])

        return {
            "common": common_g,
            "a_only": a_only_g,
            "b_only": b_only_g,
            "classification": classification,
        }

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

    def fail_subject_names(self) -> list:
        """그룹 전체에서 fail 이 발생한 subject 이름 목록 (등장 순서 유지).

        파일마다 subject 구성이 다를 수 있어(diff) 위치 기반 id union 은 다른 파일의
        항목을 엉뚱한 이름으로 매핑한다. 각 파일의 자기 인덱스로 이름을 모아 합친다.
        """
        names, seen = [], set()
        for md in self._mass_data_map.values():
            subs = [str(s) for s in md.subjects]
            for i in md.fail_subject_ids():
                if 0 <= i < len(subs) and subs[i] not in seen:
                    seen.add(subs[i])
                    names.append(subs[i])
        return names

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
