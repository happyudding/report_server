import os
import socket
import sys
import time
from pathlib import Path

# 콘솔 인코딩(예: Windows cp949)이 로그 문자열의 비-인코딩 문자(em-dash 등)를
# 만나도 서버가 UnicodeEncodeError 로 죽지 않도록 stdout/stderr 를 UTF-8 로 강제.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_LOG_FILE = None
LOG_PATH = None


class _TeeStream:
    def __init__(self, console_stream, file_stream):
        self._console = console_stream
        self._file = file_stream
        self.encoding = getattr(console_stream, "encoding", "utf-8")
        self.errors = getattr(console_stream, "errors", "replace")

    def write(self, data):
        self._console.write(data)
        try:
            self._file.write(data)
        except Exception:
            pass
        self.flush()

    def flush(self):
        self._console.flush()
        try:
            self._file.flush()
        except Exception:
            pass

    def isatty(self):
        return self._console.isatty()

    def __getattr__(self, name):
        return getattr(self._console, name)


def _prune_old_logs(log_dir):
    """오래된 server_*.txt 정리 (best-effort) — 기동마다 새 파일이라 무한 누적 방지.
    LOG_KEEP_FILES(기본 30) 초과분 + LOG_KEEP_DAYS(기본 14) 경과분을 삭제."""
    try:
        keep_files = int(os.getenv("LOG_KEEP_FILES", "30"))
        keep_days = float(os.getenv("LOG_KEEP_DAYS", "14"))
        cutoff = time.time() - keep_days * 86400
        logs = sorted(log_dir.glob("server_*.txt"), key=lambda p: p.stat().st_mtime)
        for i, path in enumerate(logs):
            if len(logs) - i > keep_files or path.stat().st_mtime < cutoff:
                try:
                    path.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def _enable_console_log_file():
    global _LOG_FILE, LOG_PATH
    try:
        log_dir = Path(__file__).resolve().parent / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        LOG_PATH = log_dir / f"server_{stamp}.txt"
        _LOG_FILE = LOG_PATH.open("a", encoding="utf-8", buffering=1)
        sys.stdout = _TeeStream(sys.stdout, _LOG_FILE)
        sys.stderr = _TeeStream(sys.stderr, _LOG_FILE)
        _prune_old_logs(log_dir)
    except Exception:
        LOG_PATH = None


_enable_console_log_file()


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
if LOG_PATH:
    _log(f"console log file: {LOG_PATH}")
_log("importing Flask ...")
from flask import Flask

_log(f"importing blueprints ... ({time.perf_counter() - _t0:.2f}s)")
from plugin import register_report_server

_log(f"creating app ... ({time.perf_counter() - _t0:.2f}s)")
app = Flask(__name__)
# 요청 본문 상한 — 미설정 시 무제한이라 대용량 업로드 폭주가 메모리 피크로 직결된다.
# upload_webreport 는 파일당 512MB 자체 검증만 있으므로 합산 상한을 여기서 건다 (초과 시 413).
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH_MB", "2048")) * 1024 * 1024
# 로그인 세션 쿠키(HMAC 서명) 키 — 재시작해도 로그인이 풀리지 않게 DB 폴더에 1회 생성·보관.
from config import REPORT_DB_PATH
_key_file = Path(REPORT_DB_PATH).parent / "secret_key"
try:
    app.config["SECRET_KEY"] = _key_file.read_text(encoding="utf-8").strip()
except OSError:
    app.config["SECRET_KEY"] = ""
if not app.config["SECRET_KEY"]:
    import secrets
    _key_file.parent.mkdir(parents=True, exist_ok=True)
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    _key_file.write_text(app.config["SECRET_KEY"], encoding="utf-8")
register_report_server(app, root_redirect=True)


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

    if debug:
        _log(f"starting Flask dev server on http://{host}:{port} (debug=True)")
        app.run(host=host, port=port, debug=True, use_reloader=False, threaded=True)
    else:
        # 동시 사용자(~10명) 운영용: Flask dev 서버 대신 waitress.
        # threads: 동시에 처리할 요청 수 (CPU-bound 계산은 web_report 캐시가 1회로 줄여줌).
        from waitress import serve

        threads = int(os.getenv("WAITRESS_THREADS", "8"))
        _log(f"starting waitress on http://{host}:{port} (threads={threads})")
        serve(app, host=host, port=port, threads=threads)
