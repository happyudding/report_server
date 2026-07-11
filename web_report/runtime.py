"""web_report 의존성 주입 지점 (컴포지션 루트: server/report/report_extension.init_app).

web_report 는 저장소 구현(server/storage_gateway)을 직접 import 하지 않고
storage() 를 통해 접근한다 (포트 정의는 ports.StoragePort). configure() 는 서버
부팅 시 1회 호출된다. 미구성 시(단독 스크립트·테스트·마이그레이션 도구)에는
sys.path 에 server/ 가 있다는 전제로 storage_gateway 를 지연 import 하는 기본
어댑터로 폴백한다 — 기존 동작과 동일.
"""
from __future__ import annotations

_storage = None


def configure(storage) -> None:
    """StoragePort 구현 주입 (부팅 시 1회)."""
    global _storage
    _storage = storage


def storage():
    """주입된 StoragePort 구현 반환. 미구성 시 기본 어댑터 지연 로드."""
    global _storage
    if _storage is None:
        import storage_gateway   # 컴포지션 루트 미경유 폴백 (스크립트/테스트)
        _storage = storage_gateway
    return _storage
