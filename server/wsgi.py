import atexit
import faulthandler
import logging
import multiprocessing
import os
import socket
import sys
import threading
import time
from pathlib import Path

# web_report 컴퓨트 워커(ProcessPoolExecutor spawn)가 이 모듈을 __mp_main__ 으로
# 재임포트한다 — 자식 프로세스에서는 앱 조립·스케줄러(cleanup/백업/메트릭)·로그 tee 를
# 전부 건너뛴다 (중복 기동 방지). 워커는 web_report/database 만 직접 import 해 쓴다.
_IS_MP_CHILD = multiprocessing.parent_process() is not None

# 콘솔 인코딩(예: Windows cp949)이 로그 문자열의 비-인코딩 문자(em-dash 등)를
# 만나도 서버가 UnicodeEncodeError 로 죽지 않도록 stdout/stderr 를 UTF-8 로 강제.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_LOG_FILE = None
LOG_PATH = None
_LOG_DIR = None
_LOG_STAMP = None   # 이 프로세스 기동 타임스탬프 — server_/faulthandler_ 파일이 이름으로 짝지어진다
_FAULT_FILE = None  # faulthandler 전용 파일 핸들 (전역 보관 — GC 로 fd 가 닫히면 덤프 유실)
_LOG_MAX_BYTES = 0  # LOG_MAX_MB env — 활성 파일 크기 상한 (0 = 로테이션 비활성)
_log_bytes = 0      # 활성 파일 누적 기록량 (len(str) 근사 — 정확한 바이트 아님)
_log_lock = threading.Lock()


class _TeeStream:
    """콘솔 + 활성 로그 파일 동시 기록. 파일 핸들은 전역(_LOG_FILE) 간접 참조 —
    stdout/stderr 두 인스턴스가 공유하므로 로테이션 스왑이 양쪽에 동시 반영된다."""

    def __init__(self, console_stream):
        self._console = console_stream
        self.encoding = getattr(console_stream, "encoding", "utf-8")
        self.errors = getattr(console_stream, "errors", "replace")

    def write(self, data):
        self._console.write(data)
        _file_write(data)
        self.flush()

    def flush(self):
        self._console.flush()
        try:
            if _LOG_FILE is not None:
                _LOG_FILE.flush()
        except Exception:
            pass

    def isatty(self):
        return self._console.isatty()

    def __getattr__(self, name):
        return getattr(self._console, name)


def _file_write(data):
    """활성 로그 파일 기록 + 상한 초과 시 로테이션 (best-effort)."""
    global _log_bytes
    f = _LOG_FILE
    if f is None:
        return
    try:
        f.write(data)
        _log_bytes += len(data)
        if _LOG_MAX_BYTES and _log_bytes >= _LOG_MAX_BYTES:
            _rotate_log()
    except Exception:
        pass


def _rotate_log():
    """활성 로그를 닫고 새 server_<stamp>.txt 로 교체 — 장기 무재시작 운영에서
    단일 활성 파일이 무한 성장하는 것을 막는다. 실패 시 기존 파일로 계속 기록."""
    global _LOG_FILE, LOG_PATH, _log_bytes
    with _log_lock:
        if _LOG_FILE is None or not _LOG_MAX_BYTES or _log_bytes < _LOG_MAX_BYTES:
            return  # 다른 스레드가 이미 교체함
        try:
            new_path = _LOG_DIR / f"server_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            new_file = new_path.open("a", encoding="utf-8", buffering=1)
        except Exception:
            _log_bytes = 0  # 재시도 폭주 방지 — 다음 상한 도달 때 다시 시도
            return
        old = _LOG_FILE
        _LOG_FILE = new_file
        LOG_PATH = new_path
        _log_bytes = 0
        try:
            old.close()
        except Exception:
            pass
        _prune_old_logs(_LOG_DIR)


def _prune_old_logs(log_dir):
    """오래된 server_*.txt 정리 (best-effort) — 기동/로테이션마다 새 파일이라 무한 누적 방지.
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
    global _LOG_FILE, LOG_PATH, _LOG_DIR, _LOG_STAMP, _LOG_MAX_BYTES
    try:
        log_dir = Path(__file__).resolve().parent / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        _LOG_DIR = log_dir
        _LOG_MAX_BYTES = max(0, int(float(os.getenv("LOG_MAX_MB", "256")) * 1024 * 1024))
        _LOG_STAMP = time.strftime("%Y%m%d_%H%M%S")
        LOG_PATH = log_dir / f"server_{_LOG_STAMP}.txt"
        _LOG_FILE = LOG_PATH.open("a", encoding="utf-8", buffering=1)
        sys.stdout = _TeeStream(sys.stdout)
        sys.stderr = _TeeStream(sys.stderr)
        _prune_old_logs(log_dir)
    except Exception:
        LOG_PATH = None


def _prune_fault_files(log_dir):
    """빈 faulthandler 파일(크래시 없이 남은 0바이트)과 오래된 것 정리 (best-effort).
    열려 있는 파일은 Windows 공유 위반으로 unlink 실패 = 사용 중 보호로 자연히 스킵된다."""
    try:
        keep_days = float(os.getenv("LOG_KEEP_DAYS", "14"))
        cutoff = time.time() - keep_days * 86400
        for path in log_dir.glob("faulthandler_*.txt"):
            try:
                st = path.stat()
                if st.st_size == 0 or st.st_mtime < cutoff:
                    path.unlink()
            except Exception:
                pass
    except Exception:
        pass


def _enable_faulthandler():
    """네이티브 크래시(세그폴트/액세스 위반) 시 스택을 파일에 남긴다. 현재 서버에는
    faulthandler/excepthook 가 없어 C 확장(pyarrow/pandas) 크래시나 OS 강제종료 시
    흔적이 사라진다. server_ 로그와 stamp 를 공유해 크래시 덤프 ↔ 콘솔 로그가 이름으로 짝지어진다.
    tee 스트림은 fd 위임이 불안정하므로 전용 파일에 직접 쓴다(파일 객체는 전역 보관 — GC 방지)."""
    global _FAULT_FILE
    try:
        if _LOG_DIR is None or _LOG_STAMP is None:
            return
        _prune_fault_files(_LOG_DIR)
        path = _LOG_DIR / f"faulthandler_{_LOG_STAMP}.txt"
        _FAULT_FILE = path.open("a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_FILE)
    except Exception:
        pass


def _init_root_logging():
    """루트 로거 핸들러 설정 — 저장소 전역의 logging.getLogger 는 핸들러 미설정 상태라
    INFO 로그가 전량 소실되고 WARNING+ 만 무포맷으로 stderr 에 나온다. tee(sys.stderr)
    설치 이후에 호출해야 핸들러가 tee 스트림을 캡처해 파일에도 남는다."""
    try:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
        # boto3 계열은 INFO 가 시끄럽다 — 나머지는 진단 가치가 있어 그대로 둔다.
        for noisy in ("botocore", "urllib3", "s3transfer"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    except Exception:
        pass


if not _IS_MP_CHILD:
    _enable_console_log_file()
    _init_root_logging()   # tee 설치 이후 — basicConfig 가 tee stderr 를 캡처하도록
    _enable_faulthandler()
    atexit.register(lambda: _log("process exiting (interpreter shutdown)"))


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
app = None
if not _IS_MP_CHILD:
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
    # 웹 로그인 세션 쿠키 — 일반 브라우저가 singleID+PIN 으로 로그인한 신원을 유지한다.
    # SECURE 는 운영이 http(12.81.220.117:8080)라 False. TLS 도입 시 True 로 올릴 것.
    from datetime import timedelta
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False
    register_report_server(app, root_redirect=True)

    _log(f"app ready in {time.perf_counter() - _t0:.2f}s")

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
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

        threads = int(os.getenv("WAITRESS_THREADS", "13"))
        # waitress 기본 max_request_body_size 는 1GB — Flask MAX_CONTENT_LENGTH(기본 2048MB)
        # 보다 작아 대용량 업로드(parquet+dist blob 첨부)가 Flask 에 닿기 전에 413 으로
        # 끊긴다. 같은 env(MAX_CONTENT_LENGTH_MB) 하나로 양쪽 상한을 정합시킨다.
        body_limit = app.config["MAX_CONTENT_LENGTH"]
        _log(f"starting waitress on http://{host}:{port} "
             f"(threads={threads}, max_body={body_limit // (1024 * 1024)}MB)")
        serve(app, host=host, port=port, threads=threads,
              max_request_body_size=body_limit)
