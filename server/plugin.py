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
    from admin_routes import admin_bp

    app.register_blueprint(report_bp)
    app.register_blueprint(honey_bp)
    app.register_blueprint(admin_bp)
    _init_report(app)

    if root_redirect:
        from flask import redirect

        @app.route("/")
        def _report_root():
            return redirect("/pe/report/")
