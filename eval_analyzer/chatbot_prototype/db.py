"""eval.db read-only 접근 헬퍼.

챗봇은 eval.db 를 **읽기 전용**으로만 연다(쓰기 소유권은 eval_engine.store).
경로는 eval_engine.config.DB_PATH 를 단일 출처로 재사용한다.
langchain 비의존 — 순수 stdlib + eval_engine.
"""
import sqlite3
from contextlib import contextmanager

from eval_engine import config


class DBNotFound(RuntimeError):
    """eval.db 파일이 아직 없음(엔진이 한 번도 적재 안 함)."""


@contextmanager
def ro_conn():
    """read-only sqlite 커넥션. mode=ro 라 CREATE/INSERT 시 예외.

    DB 파일이 없으면 DBNotFound (빈 파일 생성 방지 — mode=ro 는 없는 파일 못 염).
    """
    if not config.DB_PATH.exists():
        raise DBNotFound(f"eval.db 없음: {config.DB_PATH} (엔진으로 먼저 적재 필요)")
    uri = f"file:{config.DB_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
