"""eval.db read-only 접근 계층.

eval_engine 을 import 하지 않는다(불변 규칙 #8) — sqlite `mode=ro` 로 직접 열어
SELECT 만 한다. 스키마 계약의 정본은 `eval_analyzer/eval_engine/store.py` 의 SCHEMA 이고,
이 모듈은 그 테이블/컬럼 이름에만 의존한다(DDL 을 실행하지 않으므로 스키마를 바꾸지 않는다).

**경로 우선순위** (검증은 실측 eval.db 를 가진 외부 담당자가 하므로 override 가 필요하다):
1. `set_db_path()` 로 명시 지정 (CLI `--eval-db`)
2. `EVAL_DB_PATH` 환경변수 (eval_analyzer 쪽 관례와 동일한 이름)
3. `config.REPORT_EVAL_DB_PATH` (server 기본 — 코멘트 export 대상 파일)

파일이 없으면 예외를 던지지 않고 **빈 결과**를 돌려준다. 개발 PC 에는 eval.db 가 없는
것이 정상이고, 그때 챗봇 전체가 죽으면 안 되기 때문이다(어떤 경로를 봤는지는
`db_path()` 로 사용자에게 알려준다).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

_override: Path | None = None


def set_db_path(path) -> None:
    """조회 대상 eval.db 를 명시 지정 (CLI `--eval-db`). None 이면 override 해제."""
    global _override
    _override = Path(path).expanduser().resolve() if path else None


def db_path() -> Path:
    if _override is not None:
        return _override
    env = os.getenv("EVAL_DB_PATH")
    if env:
        return Path(env).expanduser().resolve()
    import config  # server/ 는 sys.path 에 있다 (upload_webreport.py 와 동일 규약)
    return Path(config.REPORT_EVAL_DB_PATH)


def available() -> bool:
    return db_path().exists()


@contextmanager
def ro_conn():
    """read-only 커넥션. 파일이 없으면 None 을 yield 한다(호출부가 빈 결과로 처리).

    mode=ro 라 실수로 CREATE/INSERT 를 하면 예외가 난다 — 조회 전용 보장.
    """
    path = db_path()
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def query(sql: str, params=()) -> list[dict]:
    """SELECT 1회 → list[dict]. DB 가 없으면 빈 리스트."""
    with ro_conn() as conn:
        if conn is None:
            return []
        return [dict(r) for r in conn.execute(sql, list(params)).fetchall()]
