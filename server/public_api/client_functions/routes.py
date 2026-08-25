"""client 기능의 서버 API 이전 자리 — **빈 스텁**. 등록되지 않는다.

이 Blueprint 는 `register_public_api()` 에 **일부러 등록하지 않았다**. 라우트가 하나도
없어 등록해도 무해하지만, 등록해 두면 관리자 API 탭·`/capabilities` 에 빈 기능이 실려
"있는데 안 되는 것"처럼 보인다. 구현이 들어오는 시점에 등록한다(README 참조).

소유·구현은 외부 담당자다. 여기 라우트를 채울 때 지킬 규약은 README.md 와
`../web_report/CONTRACT.md` §4 에 있다 — 특히 `viewer=None` 금지와
요청 스레드 동기 대기 금지.
"""
from __future__ import annotations

from flask import Blueprint

# 이름 규약: public_api_ 접두 (관리자 패널 계측이 이 접두로 공개 API 를 식별한다)
client_functions_bp = Blueprint("public_api_client_functions", __name__)

# 라우트 없음 — 구현 시 여기에 @client_functions_bp.get(...) 를 추가하고
# public_api/__init__.py 의 register_public_api() 에 _register 2줄을 넣는다.
