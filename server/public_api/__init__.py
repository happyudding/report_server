"""공개 REST API 등록 진입점 — /pe/api/v1.

사내망 타 서버/스크립트가 이 서버의 데이터를 읽어 가는 통로다. **무인증·읽기 전용**이며,
서버 부하가 작은 조회(단순 SELECT / 메모리 dict)만 노출한다. 접근 규약은 README.md.

**기능 하나 = 하위 폴더 하나 = Blueprint 하나**로 관리한다. 새 기능을 붙일 때는
`public_api/<기능>/routes.py` 에 Blueprint 를 만들고 아래 register 함수에 등록 2줄만
추가한다 (기존 기능 파일은 건드리지 않는다).

현재 범위는 기준정보(product_info)와 HONEY 기능 도움말(help)이다. ENGR 이력 조회는
나중에 같은 방식으로 추가한다 — 그때 지켜야 할 비공개 세션 차단 규약은 README.md
"향후 확장" 절 참조.

eval_panel/__init__.py 와 같은 이유로 이 파일은 경량 유지 — routes 는 register 시점에만
import 한다.
"""
import logging

_log = logging.getLogger(__name__)

URL_PREFIX = "/pe/api/v1"

# Blueprint 이름 규약. 관리자 패널 'public API' 탭의 계측은 Flask endpoint 이름의
# 이 접두로만 공개 API 요청을 알아본다 (metrics.py ENDPOINT_PREFIX). 이름이 어긋난
# 기능은 계측에서 통째로 빠지므로 등록 시점에 경고한다 — 등록 자체는 막지 않는다
# (모니터링 규약 때문에 기능이 죽으면 안 된다).
BLUEPRINT_PREFIX = "public_api_"


def _register(app, blueprint, path):
    """Blueprint 등록 + 이름 규약 검사. 새 기능은 이 함수로만 등록한다."""
    if not blueprint.name.startswith(BLUEPRINT_PREFIX):
        _log.warning("[public-api] Blueprint 이름 '%s' 이(가) '%s' 로 시작하지 않는다 — "
                     "관리자 패널 public API 탭 계측에서 누락된다 (README '기능 추가 규칙')",
                     blueprint.name, BLUEPRINT_PREFIX)
    app.register_blueprint(blueprint, url_prefix=f"{URL_PREFIX}/{path}")


def register_public_api(app):
    from public_api.product_info.routes import product_info_bp
    from public_api.help.routes import help_bp
    _register(app, product_info_bp, "product-info")
    _register(app, help_bp, "help")

    _log.info("[public-api] registered at %s/", URL_PREFIX)
    return True
