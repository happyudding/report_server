"""사이드 진단 리스너(server/diag_listener.py) 검증.

배경: watchdog 이 24h 에 49회 재기동하면서 원인은 healthz_timeout 뿐이었다. 정작
스레드가 전부 묶인 그 순간 기존 진단 라우트(/pe/report/_threads)도 같은 waitress 풀에서
굶어 증거를 못 남긴다. 이 테스트의 핵심은 [b] — **메인 포트가 굶는 동안에도 사이드
리스너는 응답하고, 덤프에 스레드를 잡고 있는 스택이 보인다**는 것을 실제로 재현한다.

실행:
    python tests/test_diag_listener.py

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "server"))

import diag_listener  # noqa: E402


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(port, path, timeout=3):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")


def _wait_listening(port, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _get(port, "/alive", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise AssertionError(f"리스너가 {timeout}s 안에 뜨지 않음 (:{port})")


def test_listener_basics():
    port = _free_port()
    os.environ["DIAG_PORT"] = str(port)
    diag_listener.start_diag_listener(0)
    _wait_listening(port)

    code, body = _get(port, "/alive")
    assert code == 200, code
    info = json.loads(body)
    assert info["pid"] == os.getpid(), info
    assert info["threads"] >= 1, info

    code, body = _get(port, "/threads")
    assert code == 200, code
    assert "MainThread" in body, body[:200]
    assert "=== Thread" in body, body[:200]

    try:
        _get(port, "/nope")
        raise AssertionError("404 가 아님")
    except urllib.error.HTTPError as e:
        assert e.code == 404, e.code

    # 같은 포트로 재기동 시도 — 예외가 올라오면 서버 본체 기동이 막힌다(절대 금지)
    diag_listener.start_diag_listener(0)

    # 점유된 포트에 두 번째 리스너가 '조용히 붙는' 일은 없어야 한다. Windows 는
    # SO_REUSEADDR 이 켜져 있으면 중복 bind 를 허용해서, 죽어가는 구 프로세스와
    # 덤프 대상이 뒤섞인다 — allow_reuse_address=False 로 막았는지 직접 확인.
    try:
        diag_listener._Server(("127.0.0.1", port), diag_listener._Handler)
        raise AssertionError("점유된 포트에 중복 bind 가 성공함 (덤프 대상 모호)")
    except OSError:
        pass
    print("[a] /alive · /threads · 404 · 포트충돌 무해·중복bind 차단 OK")


def test_survives_thread_exhaustion():
    """waitress 스레드를 전부 점유시켜 메인 포트를 굶긴 상태에서도
    사이드 리스너가 응답하고, 덤프에 그 대기 스택이 보이는지."""
    try:
        from flask import Flask
        from waitress import serve
    except ImportError as e:
        print(f"[b] SKIP — flask/waitress 없음 ({e})")
        return

    app = Flask(__name__)
    release = threading.Event()

    @app.get("/hang")
    def hang():
        release.wait(20)
        return "done"

    @app.get("/healthz")
    def healthz():
        return "ok"

    main_port = _free_port()
    threading.Thread(
        target=lambda: serve(app, host="127.0.0.1", port=main_port, threads=2),
        daemon=True).start()
    time.sleep(0.5)

    # 스레드 2개를 전부 점유
    for _ in range(2):
        threading.Thread(
            target=lambda: _get(main_port, "/hang", timeout=20),
            daemon=True).start()
    time.sleep(1.0)

    try:
        # 메인 포트는 굶는다 = watchdog 이 보는 healthz_timeout 증상
        try:
            _get(main_port, "/healthz", timeout=2)
            raise AssertionError("스레드 고갈 재현 실패 — healthz 가 응답함")
        except (socket.timeout, urllib.error.URLError, TimeoutError):
            pass

        # 그 순간에도 사이드 리스너는 즉시 응답하고 대기 스택을 보여준다
        t0 = time.time()
        code, body = _get(int(os.environ["DIAG_PORT"]), "/threads", timeout=3)
        assert code == 200 and time.time() - t0 < 3, (code, time.time() - t0)
        assert body.count("release.wait") >= 2 or body.count("hang") >= 2, body[-3000:]
    finally:
        release.set()
    print("[b] 스레드 고갈 중에도 사이드 리스너 응답 + 대기 스택 채집 OK")


def main():
    test_listener_basics()
    test_survives_thread_exhaustion()
    print("\nALL OK")


if __name__ == "__main__":
    sys.exit(main())
