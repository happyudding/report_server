"""
report_server를 외부 Flask 앱에 붙이는 단일 진입점.

사용법:
    import sys
    sys.path.insert(0, r'F:/COINAPI/report_server/server')
    from plugin import register_report_server

    app = Flask(__name__)
    register_report_server(app)          # /pe/report, /honey, /pe/admin 등록
"""


def register_report_server(app, root_redirect=False):
    """
    report_server Blueprint 3개를 app에 등록하고 DB를 초기화한다.

    :param root_redirect: True이면 '/' → '/pe/report/' 리다이렉트 라우트도 추가.
                          외부 앱에 이미 '/' 라우트가 있으면 False(기본값) 사용.
    """
    from report.report_extension import report_bp, init_app as _init_report
    from honey_routes import honey_bp

    app.register_blueprint(report_bp)
    app.register_blueprint(honey_bp)
    _init_report(app)

    # admin 대시보드 — 기본 경로 /pe/admin-pte/ (REPORT_ADMIN_SECRET 로 경로 변경 가능).
    # 구 공개 /pe/admin(admin_routes.admin_bp) 은 admin_panel 로 흡수되어 등록하지 않는다.
    from admin_panel import register_admin_panel
    register_admin_panel(app)

    # eval 룰 패널 — /pe/eval (thresholds/signatures 편집 + L0~L6 트레이스).
    from eval_panel import register_eval_panel
    register_eval_panel(app)

    # 공개 REST API — /pe/api/v1 (무인증·읽기 전용, 사내망 타 서버용).
    from public_api import register_public_api
    register_public_api(app)

    # 운영 보조: /healthz + 전역 에러 핸들러 + DB 백업 스케줄러 (ops.py)
    from ops import init_ops
    init_ops(app)

    if root_redirect:
        from flask import redirect

        @app.route("/")
        def _report_root():
            return redirect("/pe/report/")
