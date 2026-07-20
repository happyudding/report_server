"""admin 대시보드 등록 진입점.

REPORT_ADMIN_SECRET(영숫자/_/- 3~64자) 을 URL 경로 조각으로 써서
/pe/admin-<secret>/ 아래에 admin_panel blueprint 를 등록한다.
기본값은 'pte' 라 별도 설정 없이 /pe/admin-pte/ 로 항상 접속된다.
경로를 숨기고 싶으면 REPORT_ADMIN_SECRET 에 임의 문자열을 지정한다.
빈 문자열/형식 불일치 시에는 등록하지 않는다.
"""
import hashlib
import logging
import re

import config

_log = logging.getLogger(__name__)

_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")

# 게이트 쿠키 — routes.py(발급·검증)와 report/routes_voc.py(_is_admin)가 공유한다.
# 여기(config 만 import 하는 경량 모듈)에 두는 이유: routes_voc 가 admin_panel.routes
# 를 import 하면 admin 서브모듈 8개가 딸려 온다.
# VOC 용은 이름·경로가 다른 별도 쿠키 — admin 쿠키는 path=/pe/admin-<secret> 이라
# /pe/report/* 요청에 실려오지 않기 때문이다 (같은 이름 재발급은 브라우저에서 모호).
GATE_COOKIE_VOC = "pe_admin_gate_voc"
GATE_COOKIE_VOC_PATH = "/pe/report"


def gate_token():
    """게이트 쿠키 값 — 비밀번호 원문 대신 sha256 토큰(원문 노출 방지)."""
    return hashlib.sha256(("pe-admin-gate|" + config.REPORT_ADMIN_PASSWORD).encode()).hexdigest()


def voc_gate_token():
    """VOC 게이트 쿠키 값 — admin 게이트와 라벨이 달라 값이 다른 별도 토큰.

    이 쿠키는 path=/pe/report 라 web_report API·업로드 등 모든 요청에 실린다. admin
    토큰과 같은 값이면 평문 HTTP 구간에서 새어나갔을 때 그대로 admin 쿠키로 재사용되므로
    라벨을 분리한다(한쪽에서 다른 쪽을 유도 불가)."""
    return hashlib.sha256(("pe-voc-admin-gate|" + config.REPORT_ADMIN_PASSWORD).encode()).hexdigest()


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
