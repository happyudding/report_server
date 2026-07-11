"""타입 있는 세션 모델 (Phase 7, 2026-07-11) — dict 키 오타의 '조용한 None' 방지.

Session 은 Mapping 프로토콜을 구현해 기존 dict 소비 코드(`session.get(...)`,
`session["key"]`, `dict(session)`)와 완전 호환이면서, 새 코드는
`session.analysis_key` 처럼 속성 접근(오타 = AttributeError 즉시 발견)과 타입
힌트를 쓸 수 있다. 스키마에 새 컬럼이 생겨도 ``extra`` 로 흡수되어 깨지지 않는다.

읽기 전용 계약 — 값을 바꾸려면 dict(session) 사본을 떠서 쓸 것 (_public_session 패턴).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields


@dataclass
class Session(Mapping):
    session_id: str = ""
    id: int | None = None
    analysis_key: str | None = None
    file_name: str | None = None
    file_path: str | None = None
    content_hash: str | None = None
    status: str | None = None
    created_at: int | None = None
    updated_at: int | None = None
    error_message: str | None = None
    product_type: str | None = None
    process: str | None = None
    product: str | None = None
    revision: str | None = None
    edm_link: str | None = None
    dataset_id: str | None = None
    lot_id: str | None = None
    password: str | None = None
    is_debug: int | None = 0
    source: str | None = None
    is_important: int | None = 0
    is_private: int | None = 0
    uploaded_by: str | None = None
    client_host: str | None = None
    webreport_options: str | None = None
    mode: str | None = "Normal"
    extra: dict = field(default_factory=dict)   # 스키마에 새로 생긴 미지의 컬럼 흡수

    @classmethod
    def _field_names(cls) -> tuple:
        return tuple(f.name for f in fields(cls) if f.name != "extra")

    @classmethod
    def from_row(cls, row) -> "Session | None":
        """sqlite3.Row(또는 dict) → Session. None 이면 None (기존 get_session 계약)."""
        if row is None:
            return None
        d = dict(row)
        known = {name: d.pop(name) for name in cls._field_names() if name in d}
        return cls(**known, extra=d)

    # ── Mapping 프로토콜 (기존 dict 소비 코드 호환) ───────────────────────────
    def _mapping(self) -> dict:
        out = {name: getattr(self, name) for name in self._field_names()}
        out.update(self.extra)
        return out

    def __getitem__(self, key):
        return self._mapping()[key]

    def __iter__(self):
        return iter(self._mapping())

    def __len__(self):
        return len(self._mapping())

    def as_dict(self) -> dict:
        return self._mapping()
