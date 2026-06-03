import os
import socket
import sys
import time

# 콘솔 인코딩(예: Windows cp949)이 로그 문자열의 비-인코딩 문자(em-dash 등)를
# 만나도 서버가 UnicodeEncodeError 로 죽지 않도록 stdout/stderr 를 UTF-8 로 강제.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _log(msg):
    print(f"[wsgi] {msg}", flush=True)


def _lan_ips():
    ips = set()
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return sorted(ips)


_t0 = time.perf_counter()
_log("importing Flask ...")
from flask import Flask, redirect

_log(f"importing blueprints ... ({time.perf_counter() - _t0:.2f}s)")
from report.report_extension import report_bp
from honey_routes import honey_bp
from admin_routes import admin_bp

_log(f"creating app ... ({time.perf_counter() - _t0:.2f}s)")
app = Flask(__name__)
app.register_blueprint(report_bp)
app.register_blueprint(honey_bp)
app.register_blueprint(admin_bp)


@app.route("/")
def _root():
    return redirect("/pe/report/")


_log(f"app ready in {time.perf_counter() - _t0:.2f}s")

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"

    _log("===== Accessible URLs =====")
    _log(f"Local (이 PC)               : http://127.0.0.1:{port}/pe/report/")
    if host in ("0.0.0.0", "::", ""):
        ips = _lan_ips()
        if ips:
            for ip in ips:
                _log(f"LAN (같은 네트워크 다른 PC) : http://{ip}:{port}/pe/report/")
        else:
            _log("LAN: IPv4 주소를 찾지 못함 (ipconfig 로 직접 확인)")
        _log("** 처음 외부 PC 에서 접근 시 Windows 방화벽 허용 필요할 수 있음:")
        _log(f'   New-NetFirewallRule -DisplayName "report-server {port}" -Direction Inbound -LocalPort {port} -Protocol TCP -Action Allow')
    else:
        _log(f"(HOST={host} 으로 bind — LAN 노출 안 됨. LAN 접근하려면 HOST 환경변수 제거)")
    _log("===========================")

    _log(f"starting server on http://{host}:{port} (debug={debug})")
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)
