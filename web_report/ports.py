"""web_report 가 바깥(서버 인프라)에 요구하는 포트 정의 (DIP — 2026-07-11).

web_report 는 구현체를 직접 import 하지 않는다:
- 저장소(StoragePort) 는 runtime.storage() 로 주입받는다
  (기본 구현: server/storage_gateway — report_extension.init_app 이 주입).
- DB(SessionRepo) 는 기존 관례대로 각 함수의 report_db 파라미터로 주입받는다
  (기본 구현: server/database/report_db 모듈).

둘 다 구조적 타이핑(Protocol) — 모듈 객체가 그대로 어댑터 역할을 한다.
새 메서드가 필요해지면 여기 시그니처를 먼저 추가하고 구현체를 맞출 것.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class StoragePort(Protocol):
    """parquet 원본·manifest 저장소 (S3 + 로컬 폴백 등 구현 세부는 어댑터 소관)."""

    def save_webreport_sources(self, analysis_key: str, content_hash: str,
                               sources: list, manifest: dict, upload_root: Path) -> dict:
        """반환 {"storage": "s3"|"local", "warnings": [...]} — 저장 위치를 명시한다."""
        ...

    def load_webreport_sources(self, analysis_key: str, upload_root: Path):
        """반환 (list[bytes], manifest dict). 기록된 저장 위치를 따라 읽는다."""
        ...

    def load_webreport_manifest(self, analysis_key: str, upload_root: Path) -> dict:
        ...


class SessionRepo(Protocol):
    """web_report 가 사용하는 세션/편집 DB 연산 (구현: database/report_db)."""

    def get_session(self, session_id: str) -> dict | None: ...

    def create_session(self, session_id: str, file_name: str, file_path, **meta) -> None: ...

    def update_session(self, session_id: str, **fields) -> None: ...

    def update_content_hash_for_analysis_key(self, analysis_key: str,
                                             content_hash: str) -> int:
        """같은 analysis_key 의 모든 세션 content_hash 일괄 갱신 (raw_data 편집용)."""
        ...

    def session_ids_for_analysis_key(self, analysis_key: str) -> list:
        """같은 analysis_key 를 공유하는 세션 id 목록 (dedup 형제 포함).

        물리 원본 교체 시 행 위치 기반 전처리 셀 패치를 형제까지 해제하는 데 쓴다."""
        ...

    def log_audit(self, action: str, **fields) -> None: ...

    def get_webreport_edit_rev(self, session_id: str) -> int: ...

    def get_webreport_edits(self, session_id: str) -> list: ...

    def apply_webreport_edits(self, session_id: str, changes: list,
                              updated_by=None) -> int: ...

    def note_base_token(self, blob) -> str | None:
        """Note 시트 blob 의 낙관적 잠금 base 토큰 (blob 이 None 이면 None)."""
        ...

    def save_note_sheet_checked(self, session_id: str, kind: str, item_key: str,
                                blob, base, updated_by=None, check: bool = True,
                                force: bool = False) -> tuple:
        """(ok, info) — base 불일치면 (False, {updated_by, updated_at, base}) 로
        쓰기 없이 거부하고, 통과하면 (True, {rev, base}). 검사와 쓰기는 한 트랜잭션."""
        ...
