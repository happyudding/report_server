"""콘솔 로그(stdout/stderr)를 Honey.exe 폴더의 log/ 에 실시간 기록.

windowed(console=False) 빌드에서는 콘솔 창이 없어 print 출력이 사라진다.
실행 시각으로 이름붙인 텍스트 파일에 stdout/stderr 를 tee(복제) 해 남긴다.
best-effort — 설정 실패가 앱 기동을 막지 않는다.
"""
import sys
from datetime import datetime
from pathlib import Path


def _base_dir():
    """로그를 둘 기준 폴더. frozen 은 Honey.exe 폴더, dev 는 client/ (config.py 패턴과 동일)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


class _Tee:
    """원본 스트림과 로그 파일에 동시에 쓰는 최소 file-like.

    원본 스트림은 windowed frozen 에서 None 일 수 있어 가드한다. 각 write 마다
    flush 해 실시간 누적을 보장한다. 쓰기 실패가 본 동작을 깨지 않도록 보호한다.
    """

    def __init__(self, original, logfile):
        self._original = original
        self._logfile = logfile

    def write(self, s):
        if self._original is not None:
            try:
                self._original.write(s)
            except Exception:
                pass
        try:
            self._logfile.write(s)
            self._logfile.flush()
        except Exception:
            pass

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass
        try:
            self._logfile.flush()
        except Exception:
            pass

    def isatty(self):
        return bool(self._original is not None and getattr(self._original, "isatty", lambda: False)())


def setup_run_logging():
    """log/<실행날짜시간>.txt 를 열고 sys.stdout/stderr 를 그 파일로 tee. 실패 시 None."""
    try:
        log_dir = _base_dir() / "log"
        log_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"{stamp}.txt"
        suffix = 1
        while path.exists():  # 같은 초 재실행 충돌 방지
            path = log_dir / f"{stamp}_{suffix}.txt"
            suffix += 1

        logfile = open(path, "w", encoding="utf-8", buffering=1)
        sys.stdout = _Tee(sys.stdout, logfile)
        sys.stderr = _Tee(sys.stderr, logfile)
        return path
    except Exception:
        return None
