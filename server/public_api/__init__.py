"""공개 REST API 등록 진입점 — /pe/api/v1.

사내망 타 서버/스크립트가 이 서버의 데이터를 읽어 가는 통로다. **무인증·읽기 전용**이며,
서버 부하가 작은 조회(단순 SELECT / 메모리 dict)만 노출한다. 접근 규약은 README.md.

**기능 하나 = 하위 폴더 하나 = Blueprint 하나**로 관리한다. 새 기능을 붙일 때는
`public_api/<기능>/routes.py` 에 Blueprint 를 만들고 아래 register 함수에 등록 2줄만
추가한다 (기존 기능 파일은 건드리지 않는다).

현재 범위는 기준정보(product_info)뿐이다. ENGR 이력·평가(eval.db) 조회는 나중에 같은
방식으로 추가한다 — 그때 지켜야 할 비공개 세션 차단 규약은 README.md "향후 확장" 절 참조.

eval_panel/__init__.py 와 같은 이유로 이 파일은 경량 유지 — routes 는 register 시점에만
import 한다.
"""
import logging

_log = logging.getLogger(__name__)

URL_PREFIX = "/pe/api/v1"


def register_public_api(app):
    from public_api.product_info.routes import product_info_bp
    app.register_blueprint(product_info_bp, url_prefix=f"{URL_PREFIX}/product-info")

    _log.info("[public-api] registered at %s/", URL_PREFIX)
    return True
