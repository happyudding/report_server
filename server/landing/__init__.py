"""서버 랜딩 페이지 — /pe.

제품군 바로가기(→ /pe/report/?pt=<PT>) · Honey 다운로드 · 현황 수치를 담은 첫 화면.
정적 HTML 한 장이라 라우트가 이것뿐이고, 화면이 쓰는 데이터는 report_bp 의
GET /pe/report/api/landing 하나다 (그쪽에 둔 이유는 routes_misc.py 주석 참조).

'/pe' 와 '/pe/' 를 같은 뷰에 둘 다 매핑한다 — 룰이 '/pe' 하나뿐이면 '/pe/' 요청이
404 가 된다(Werkzeug 의 자동 슬래시 보정은 반대 방향뿐). 두 룰을 다 달면 308 홉도 없다.
"""
import logging

from flask import Blueprint

from config import REPORT_LANDING_HTML
from report.static_pages import send_html_gzip

_log = logging.getLogger(__name__)

landing_bp = Blueprint("landing", __name__)


@landing_bp.get("/pe")
@landing_bp.get("/pe/")
def landing_page():
    # gzip + ETag + mtime 기반 RAM 캐시 (HTML 을 고치면 재시작 없이 즉시 반영).
    # CSRF 쿠키는 여기서 발급되지 않는다 — 랜딩 JS 가 /pe/report/api/landing 을
    # 부르는 순간 report_bp.after_request 가 심는다.
    return send_html_gzip(REPORT_LANDING_HTML)


def register_landing(app):
    app.register_blueprint(landing_bp)
    _log.info("[landing] registered at /pe")
    return True
