"""사이드 진단 리스너 — waitress 와 독립된 최소 HTTP 서버.

왜 별도 리스너인가: 기존 진단 라우트(/pe/report/_threads)는 waitress 스레드 풀
(WAITRESS_THREADS, 기본 13)을 공유한다. 정작 진단이 절실한 상황 —— 스레드가 전부
heavy 작업에 묶여 /healthz 조차 30초 안에 못 돌려주는 상태 —— 에서는 그 라우트도
같이 굶어 응답하지 못한다. watchdog 이 "healthz_timeout" 만 남기고 재기동해 버리면
어떤 요청이 스레드를 잡고 있었는지 증거가 사라진다.

이 모듈은 표준 라이브러리만으로 별도 소켓·별도 스레드에서 돌아 그 순간에도 응답한다.
watchdog 이 재기동 직전(Save-Snapshot)에 여기서 스레드 덤프를 받아 부검 파일에 남긴다.

  GET /alive    -> {"pid","uptime_s","threads","inflight"}  프로세스 생존·응답성
  GET /threads  -> 전 스레드 stack trace (text/plain)

스택·파일 경로가 노출되므로 127.0.0.1 에만 bind 한다 (HOST env 를 따라가지 않는다).
"""

import json
import multiprocessing
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_START_TS = time.time()


def dump_threads_text() -> str:
    """모든 스레드의 stack trace 덤프. /pe/report/_threads 와 공용."""
    out = [
        f"pid={os.getpid()} uptime_s={int(time.time() - _START_TS)} "
        f"threads={threading.active_count()} inflight={_inflight()}"
    ]
    tid_to_name = {t.ident: t.name for t in threading.enumerate()}
    for tid, frame in sys._current_frames().items():
        out.append(f"=== Thread {tid} ({tid_to_name.get(tid, '?')}) ===")
        out.append("".join(traceback.format_stack(frame)))
    return "\n".join(out)


def _inflight():
    """처리 중인 요청 수. metrics 를 못 읽으면 None (진단 부가 정보라 실패 무시)."""
    try:
        from admin_panel.metrics import current_inflight
        return current_inflight()
    except Exception:
        return None


def _inflight_rows(limit=5):
    """진행 중 요청 중 오래 걸린 순으로 최대 limit 건 — "무엇이 걸렸나".

    terminate 의 drain(drain_wait.ps1)이 이걸 읽어 "10건" 대신 라우트와 경과를 찍는다.
    개수만 보고는 기다릴지 끊을지 판단할 수 없기 때문이다. 응답이 커지지 않게 상위
    몇 건만 준다 — 멈춘 스레드들은 대개 같은 지점이라 표본 몇 개면 충분하다.
    """
    try:
        from admin_panel.metrics import inflight_detail
        return (inflight_detail() or [])[:limit]
    except Exception:
        return []


class _Server(ThreadingHTTPServer):
    # 기본값(True)이면 Windows 에서는 이미 리스닝 중인 포트에도 bind 가 성공해버려,
    # 죽어가는 구 프로세스와 새 프로세스가 같은 포트를 나눠 갖는다 — watchdog 이 받은
    # 덤프가 어느 프로세스 것인지 알 수 없게 된다. 겹치면 차라리 실패로 드러내야 한다.
    allow_reuse_address = False
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        if self.path.startswith("/alive"):
            body = json.dumps({
                "pid": os.getpid(),
                "uptime_s": int(time.time() - _START_TS),
                "threads": threading.active_count(),
                "inflight": _inflight(),
                "requests": _inflight_rows(),
            }).encode()
            self._send(200, "application/json", body)
        elif self.path.startswith("/threads"):
            self._send(200, "text/plain; charset=utf-8", dump_threads_text().encode("utf-8"))
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def _send(self, code, ctype, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def log_message(self, fmt, *args):
        pass   # 서버 로그를 5분마다 오염시키지 않는다


def start_diag_listener(main_port: int) -> None:
    """진단 리스너를 데몬 스레드로 기동. 어떤 실패에서도 예외를 올리지 않는다 —
    이건 부가 진단 장치이므로 포트 충돌 따위로 서버 본체 기동을 막아선 안 된다.

    포트: DIAG_PORT env 우선, 미설정이면 main_port+1. DIAG_PORT=0 이면 비활성.

    bind 는 백그라운드에서 몇 번 재시도한다 — watchdog 재기동 직후에는 방금 죽인 구
    프로세스가 포트를 놓기 전일 수 있는데, 하필 그때가 진단이 가장 필요한 순간이다.
    """
    try:
        if multiprocessing.parent_process() is not None:
            return   # 컴퓨트 워커에서는 기동하지 않는다
        raw = os.getenv("DIAG_PORT", "")
        port = int(raw) if raw.strip() else main_port + 1
        if port <= 0:
            return
        threading.Thread(target=_serve_forever, args=(port,),
                         name="diag-listener", daemon=True).start()
    except Exception as e:
        print(f"[diag] listener disabled: {e}", flush=True)


def _serve_forever(port: int) -> None:
    for attempt in range(5):
        try:
            srv = _Server(("127.0.0.1", port), _Handler)
        except OSError as e:
            if attempt == 4:
                print(f"[diag] listener disabled (:{port} 사용 중): {e}", flush=True)
                return
            time.sleep(1.0)
            continue
        print(f"[diag] listener on http://127.0.0.1:{port}/ (alive, threads)", flush=True)
        try:
            srv.serve_forever()
        except Exception as e:
            print(f"[diag] listener stopped: {e}", flush=True)
        return
