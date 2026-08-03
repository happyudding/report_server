"""pytest 공용 fixture. eval.db 오염 방지 — 모든 DB 테스트는 tmp 로 격리."""
import pytest


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """config.DB_PATH/DATA_DIR 를 tmp 로 바꾸고 init_db. store.get_conn() 은
    호출 시점에 config.DB_PATH 를 읽으므로 monkeypatch 로 충분."""
    from eval_engine import config, store
    db = tmp_path / "eval.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db)
    store.init_db()
    return db
