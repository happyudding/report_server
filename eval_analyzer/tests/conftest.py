"""pytest 공용 fixture. eval.db 오염 방지 — 모든 DB 테스트는 tmp 로 격리."""
import pytest


@pytest.fixture(autouse=True)
def all_signatures_enabled(request, monkeypatch):
    """운영 배포용 `enabled: false` 를 테스트에서는 무시한다.

    signatures.yaml 의 on/off 는 "지금 어떤 룰을 굴릴지"라는 배포 설정이지 룰 로직이
    아니다. 룰을 껐다 켤 때마다 로직 테스트가 깨지면 나중에 다시 켤 때 기댈 안전망이
    사라지므로, 테스트는 항상 전 룰이 켜진 상태를 본다.
    비활성 메커니즘 자체는 test_rules_scope.py 가 `rules_as_deployed` 마커로 이 fixture
    를 끄고 검증한다.
    """
    if request.node.get_closest_marker("rules_as_deployed"):
        return
    from eval_engine.pipeline import signatures
    from eval_engine.pipeline._rules import signatures_doc

    def _all_enabled():
        """signatures.yaml 문서에서 `enabled` 키만 걷어낸 사본 — 키 부재 = 활성."""
        doc = signatures_doc()
        return {**doc,
                "signatures": [{k: v for k, v in s.items() if k != "enabled"}
                               for s in doc.get("signatures") or []]}

    monkeypatch.setattr(signatures, "signatures_doc", _all_enabled)


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
