"""wsgi._prune_old_logs 의 '최근 구간 무조건 보존' 정책 검증.

배경: watchdog 재기동이 폭주하면 기동 1회당 server_*.txt 1개가 생긴다(2026-07 관측
142회/일). 개수 상한(LOG_KEEP_FILES=30)만 두면 몇 시간 만에 원인 구간 로그가 밀려나
폭주가 스스로 증거를 지운다. LOG_MIN_KEEP_HOURS(기본 48) 안쪽은 개수·용량과 무관하게
보존하도록 바꾼 정책을 이 테스트가 고정한다.

실행:
    python tests/test_log_prune.py

wsgi.py 는 import 만으로 앱 조립까지 도는 무거운 모듈이라, ast 로 대상 함수만 떼어
독립 실행한다. pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import ast
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_prune():
    src = Path(_ROOT, "server", "wsgi.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_prune_old_logs")
    ns = {"os": os, "time": time}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "wsgi_prune", "exec"), ns)
    return ns["_prune_old_logs"]


def _mk(log_dir: Path, name: str, age_sec: float, size: int = 512):
    p = log_dir / name
    p.write_bytes(b"x" * size)
    t = time.time() - age_sec
    os.utime(p, (t, t))
    return p


def main():
    prune = _load_prune()
    tmp = Path(tempfile.mkdtemp(prefix="log_prune_test_"))
    try:
        # ── (a) 폭주 재현: 최근 16시간에 5분 간격 200개 + 과거 파일들 ──────────
        log_dir = tmp / "a"
        log_dir.mkdir()
        burst = [_mk(log_dir, "server_burst_%03d.txt" % i, i * 300) for i in range(200)]
        old_3d = _mk(log_dir, "server_3days.txt", 3 * 86400)       # 48h 밖, 14일 내
        old_20d = _mk(log_dir, "server_20days.txt", 20 * 86400)    # 14일 초과

        for k in ("LOG_KEEP_FILES", "LOG_KEEP_DAYS", "LOG_MIN_KEEP_HOURS", "LOG_KEEP_TOTAL_MB"):
            os.environ.pop(k, None)
        prune(log_dir)

        alive = [p.name for p in burst if p.exists()]
        assert len(alive) == 200, "48h 내 파일이 삭제됨 (%d/200 생존)" % len(alive)
        assert not old_20d.exists(), "14일 경과 파일이 남음"
        assert not old_3d.exists(), "48h 밖 + 개수 상한 초과 파일이 남음"
        print("[a] 폭주 200개 전부 보존, 14일 경과/상한 초과 과거분 삭제 OK")

        # ── (b) 총 용량 캡: 48h 밖은 캡을 넘으면 삭제, 48h 내는 캡 무관 보존 ──
        log_dir = tmp / "b"
        log_dir.mkdir()
        recent_big = _mk(log_dir, "server_recent_big.txt", 3600, size=300 * 1024)
        old_small = _mk(log_dir, "server_old_small.txt", 5 * 86400, size=1024)
        os.environ["LOG_KEEP_TOTAL_MB"] = "0.1"   # 100KB — recent_big 하나로 이미 초과
        os.environ["LOG_KEEP_FILES"] = "100"      # 개수는 넉넉히 (용량 규칙만 보게)
        try:
            prune(log_dir)
        finally:
            os.environ.pop("LOG_KEEP_TOTAL_MB", None)
            os.environ.pop("LOG_KEEP_FILES", None)

        assert recent_big.exists(), "48h 내 파일이 용량 캡으로 삭제됨"
        assert not old_small.exists(), "용량 캡 초과 구간의 과거 파일이 남음"
        print("[b] 용량 캡은 48h 밖에만 적용 OK")

        # ── (c) 기존 동작 회귀: 전부 오래된 경우 개수 상한이 그대로 먹는지 ────
        log_dir = tmp / "c"
        log_dir.mkdir()
        olds = [_mk(log_dir, "server_old_%03d.txt" % i, 3 * 86400 + i * 60) for i in range(40)]
        prune(log_dir)
        alive = [p for p in olds if p.exists()]
        assert len(alive) == 30, "개수 상한(30) 미적용: %d 생존" % len(alive)
        # 살아남은 것은 최신 30개여야 한다 (age 가 작은 쪽)
        assert all(p.name in {o.name for o in olds[:30]} for p in alive), "오래된 쪽이 남음"
        print("[c] 48h 밖 구간의 개수 상한 30 유지 OK")

        print("\nALL OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
