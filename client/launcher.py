"""Honey 런처 — versions\\<ver>\\HoneyApp.exe 를 골라 실행한다.

설치 루트(런처가 있는 폴더) 구조는 transport/app_update.py 주석 참조.
런처는 **자동 업데이트가 실행 중인 파일을 건드리지 않게** 하기 위한 고정점이다.
업데이트는 versions\\ 아래 새 폴더를 만들고 current.txt 만 바꾸므로, 런처 자신은
거의 바뀌지 않는다.

의존성은 표준 라이브러리뿐이다 (PyQt6 등을 import 하면 런처가 수백 MB 가 된다).

동작:
  1) --wait-pid <N> 이 있으면 그 프로세스가 끝날 때까지 최대 30초 기다린다
     (업데이트 직후 구버전 앱의 종료를 기다리는 용도).
  2) current.txt 를 읽어 실행 후보 목록을 만든다 (손상/누락이면 versions 스캔).
  3) 후보를 순서대로 실행한다. 15초 안에 비정상 종료하면 다음 후보로 넘어가고,
     current.txt 를 이전 버전으로 되돌린다 (= 자동 롤백).

--no-ui 는 자동 테스트 전용이다 (실패 안내창이 클릭을 기다리며 멈추지 않게).
"""
import ctypes
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

APP_EXE_NAME = "HoneyApp.exe"
CURRENT_FILENAME = "current.txt"
VERSIONS_DIRNAME = "versions"
STARTUP_WATCH_SEC = 15.0     # 이 시간 살아 있으면 정상 기동으로 본다
WAIT_PARENT_SEC = 30.0


def root_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def log(root: Path, message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [launcher {os.getpid()}] {message}"
    try:
        log_dir = root / "log"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "launcher.log", "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def wait_for_pid(pid: int, timeout_sec: float) -> None:
    """해당 PID 가 끝날 때까지 대기. 이미 없으면 즉시 반환 (best-effort)."""
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout_sec * 1000))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def read_current(root: Path):
    # utf-8-sig: 사람이 메모장으로 고쳐 BOM 이 붙어도 첫 줄을 잃지 않는다
    # (BOM 이 붙으면 버전 문자열이 '﻿9.0.1' 이 돼 폴더를 못 찾는다).
    try:
        text = (root / CURRENT_FILENAME).read_text(encoding="utf-8-sig")
    except OSError:
        return None, None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cur = lines[0] if lines else None
    prev = lines[1] if len(lines) > 1 else None
    return cur, prev


def write_current(root: Path, current: str, previous=None) -> None:
    body = current if not previous else f"{current}\n{previous}"
    tmp = root / f".{CURRENT_FILENAME}.tmp-{os.getpid()}"
    tmp.write_text(body + "\n", encoding="utf-8")
    os.replace(tmp, root / CURRENT_FILENAME)


def installed_versions(root: Path):
    """실행 가능한 버전 폴더를 최신순으로 (current.txt 가 없거나 깨졌을 때의 폴백)."""
    out = []
    try:
        entries = list((root / VERSIONS_DIRNAME).iterdir())
    except OSError:
        return out
    for entry in entries:
        if (entry / APP_EXE_NAME).exists() and re.fullmatch(r"\d+(\.\d+)*", entry.name):
            out.append(entry.name)
    out.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return out


def candidates(root: Path):
    """실행을 시도할 버전 순서. current → prev → 나머지 최신순."""
    cur, prev = read_current(root)
    order = []
    for ver in (cur, prev):
        if ver and ver not in order:
            order.append(ver)
    for ver in installed_versions(root):
        if ver not in order:
            order.append(ver)
    return [v for v in order if (root / VERSIONS_DIRNAME / v / APP_EXE_NAME).exists()]


def message_box(text: str, enabled: bool = True) -> None:
    """실패 안내창. enabled=False 는 자동 테스트용 — 안내창은 클릭할 때까지 막힌다."""
    if not enabled:
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Honey", 0x10)
    except Exception:
        pass


def main(argv) -> int:
    root = root_dir()
    show_ui = "--no-ui" not in argv

    if "--wait-pid" in argv:
        try:
            pid = int(argv[argv.index("--wait-pid") + 1])
        except (IndexError, ValueError):
            pid = 0
        if pid:
            log(root, f"wait for pid {pid}")
            wait_for_pid(pid, WAIT_PARENT_SEC)

    order = candidates(root)
    if not order:
        log(root, f"no runnable version under {root / VERSIONS_DIRNAME}")
        message_box(f"실행할 Honey 버전을 찾지 못했습니다.\n\n{root / VERSIONS_DIRNAME}\n\n"
                    "설치 파일(zip)을 다시 풀어 주세요.", show_ui)
        return 1

    cur, _prev = read_current(root)
    for index, version in enumerate(order):
        exe = root / VERSIONS_DIRNAME / version / APP_EXE_NAME
        log(root, f"launch {version}")
        try:
            proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        except OSError as exc:
            log(root, f"launch failed {version}: {exc}")
            continue
        try:
            rc = proc.wait(timeout=STARTUP_WATCH_SEC)
        except subprocess.TimeoutExpired:
            log(root, f"ok {version} (running)")
            return 0
        if rc == 0:
            log(root, f"ok {version} (exited 0)")
            return 0
        # 기동 직후 비정상 종료 = 이 버전이 깨졌다. 다음 후보로 내려가면서
        # current.txt 도 그 후보로 되돌려 다음 실행부터 바로 정상 버전이 뜨게 한다.
        log(root, f"crash {version} rc={rc}")
        fallback = order[index + 1] if index + 1 < len(order) else None
        if fallback and version == cur:
            try:
                write_current(root, fallback, None)
                log(root, f"rollback current -> {fallback}")
            except OSError as exc:
                log(root, f"rollback failed: {exc}")

    message_box("Honey 를 실행하지 못했습니다.\n\n"
                f"자세한 내용: {root / 'log' / 'launcher.log'}", show_ui)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
