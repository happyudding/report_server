"""admin 대시보드 등록 진입점.

REPORT_ADMIN_SECRET(영숫자/_/- 3~64자) 을 URL 경로 조각으로 써서
/pe/admin-<secret>/ 아래에 admin_panel blueprint 를 등록한다.
기본값은 'pte' 라 별도 설정 없이 /pe/admin-pte/ 로 항상 접속된다.
경로를 숨기고 싶으면 REPORT_ADMIN_SECRET 에 임의 문자열을 지정한다.
빈 문자열/형식 불일치 시에는 등록하지 않는다.
"""
import logging
import re

import config

_log = logging.getLogger(__name__)

_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def register_admin_panel(app):
    secret = config.REPORT_ADMIN_SECRET
    if not secret:
        _log.info("[admin-panel] REPORT_ADMIN_SECRET 비어 있음 — admin 패널 비활성")
        return False
    if not _SECRET_RE.match(secret):
        _log.warning("[admin-panel] REPORT_ADMIN_SECRET 형식 불일치(영숫자/_/- 3~64자) — 등록 안 함")
        return False
    from admin_panel.routes import admin_panel_bp
    app.register_blueprint(admin_panel_bp, url_prefix=f"/pe/admin-{secret}")
    from admin_panel import metrics
    metrics.init_app(app)  # app 전역 in-flight 훅 + 리소스 샘플러 (패널과 운명 공유)
    _log.info("[admin-panel] registered at /pe/admin-%s/", secret)
    return True
