"""eval 룰 관리자 패널 등록 진입점 — /pe/eval.

eval_analyzer 의 thresholds(제품군/family 오버레이)·signatures 를 브라우저에서
편집하고(서버 재시작 없이 즉시 반영), 세션 1건의 L0~L6 평가 과정을 단계별로
들여다본다. 접근 게이트는 admin 과 같은 비밀번호(REPORT_ADMIN_PASSWORD)이며
쿠키는 경로가 다른 별도 토큰이다 (admin_panel.eval_gate_token).

admin_panel/__init__.py 와 같은 이유로 이 파일은 경량 유지 — 무거운 routes 는
register 시점에만 import 한다.
"""
import logging

_log = logging.getLogger(__name__)

URL_PREFIX = "/pe/eval"


def register_eval_panel(app):
    from eval_panel.routes import eval_panel_bp
    app.register_blueprint(eval_panel_bp, url_prefix=URL_PREFIX)
    _log.info("[eval-panel] registered at %s/", URL_PREFIX)
    return True
