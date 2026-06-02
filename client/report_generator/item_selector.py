"""ItemSelector — item select / 공통 filter 설정.

분석 대상 subject 목록과 (선택적) 공통 행 필터를 보관한다. 순수 데이터 객체.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ItemSelector:
    selected_items: Optional[list] = None   # subject 이름 리스트. None/[] → 전체
    # 공통 filter: meta 컬럼 동등 비교 {"Bin": "1", ...} (선택)
    meta_equals: dict = field(default_factory=dict)

    @classmethod
    def fail_only(cls, group) -> "ItemSelector":
        """fail 발생 subject 만 선택하는 selector (UI 기본값).

        파일별 subject 구성이 달라도(diff) 각 파일 자기 인덱스로 fail 이름을 모은다
        (group.fail_subject_names). 첫 파일에 위치 매핑하던 기존 방식은 B 에만 있는
        항목을 엉뚱하게 매핑/누락시켰다.
        """
        return cls(selected_items=group.fail_subject_names())

    def resolved_items(self, group) -> list:
        """실제 적용될 subject 목록 (None 이면 전체)."""
        if self.selected_items:
            return list(self.selected_items)
        return group.subjects()
