"""launcher.py 동작 확인 — 가짜 설치 트리 + 시스템 exe 를 스텁으로 써서 검증한다.

실행: python client\\update_test\\check_launcher.py
성공하면 마지막 줄에 ALL OK.

스텁 규칙 (컴파일러 없이 exe 종료코드를 만들기 위해 시스템 exe 를 복사해 쓴다):
  정상 종료(0) 스텁 = hostname.exe   → 즉시 0 으로 끝난다 = "정상 기동 후 사용자가 닫음"
  비정상 종료 스텁  = where.exe(인자 없음) → 즉시 2 로 끝난다 = "기동 직후 크래시"

launcher.py 는 실행 파일이 아닐 때 자기 파일이 있는 폴더를 설치 루트로 보므로,
테스트는 launcher.py 를 가짜 루트로 복사해 그 자리에서 돌린다.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CLIENT_DIR = Path(__file__).resolve().parent.parent
SYS32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
STUB_OK = SYS32 / "hostname.exe"      # exit 0
STUB_CRASH = SYS32 / "where.exe"      # 인자 없이 실행하면 exit 2


def make_version(root, version, stub):
    d = root / "versions" / version
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stub, d / "HoneyApp.exe")
    return d


def write_current(root, text):
    (root / "current.txt").write_text(text, encoding="utf-8")


def read_current(root):
    try:
        return (root / "current.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def run_launcher(root, *args):
    proc = subprocess.run(
        [sys.executable, str(root / "launcher.py"), "--no-ui", *args],
        capture_output=True, text=True, timeout=120,
    )
    return proc.returncode


def launcher_log(root):
    try:
        return (root / "log" / "launcher.log").read_text(encoding="utf-8")
    except OSError:
        return ""


def check(name, condition):
    assert condition, f"FAILED: {name}"
    print(f"  ok  {name}")


def main():
    for stub in (STUB_OK, STUB_CRASH):
        if not stub.exists():
            raise SystemExit(f"스텁으로 쓸 시스템 exe 가 없습니다: {stub}")

    work = Path(tempfile.mkdtemp(prefix="honey_launchertest_"))
    print(f"작업 폴더: {work}")
    try:
        print("[1] 정상 버전 실행")
        root = work / "ok"
        root.mkdir()
        shutil.copy2(CLIENT_DIR / "launcher.py", root / "launcher.py")
        make_version(root, "9.0.0", STUB_OK)
        write_current(root, "9.0.0\n")
        check("종료코드 0", run_launcher(root) == 0)
        check("로그에 launch 9.0.0", "launch 9.0.0" in launcher_log(root))

        print("[2] 새 버전 크래시 → 이전 버전 폴백 + current.txt 롤백")
        root = work / "rollback"
        root.mkdir()
        shutil.copy2(CLIENT_DIR / "launcher.py", root / "launcher.py")
        make_version(root, "9.0.0", STUB_OK)
        make_version(root, "9.0.1", STUB_CRASH)
        write_current(root, "9.0.1\n9.0.0\n")
        check("폴백 성공으로 종료코드 0", run_launcher(root) == 0)
        log = launcher_log(root)
        check("크래시 감지", "crash 9.0.1" in log)
        check("이전 버전 실행", "launch 9.0.0" in log)
        check("current.txt 롤백됨", read_current(root) == "9.0.0")

        print("[3] current.txt 없음 → versions 스캔 폴백")
        root = work / "nocurrent"
        root.mkdir()
        shutil.copy2(CLIENT_DIR / "launcher.py", root / "launcher.py")
        make_version(root, "9.0.0", STUB_OK)
        make_version(root, "8.0.0", STUB_OK)
        check("종료코드 0", run_launcher(root) == 0)
        check("최신 버전을 골랐다", "launch 9.0.0" in launcher_log(root))

        print("[4] 실행 가능한 버전 없음")
        root = work / "empty"
        root.mkdir()
        shutil.copy2(CLIENT_DIR / "launcher.py", root / "launcher.py")
        (root / "versions").mkdir()
        check("종료코드 1", run_launcher(root) == 1)
        check("로그에 no runnable version", "no runnable" in launcher_log(root))

        print("[5] --wait-pid: 살아 있는 프로세스를 기다린다")
        root = work / "waitpid"
        root.mkdir()
        shutil.copy2(CLIENT_DIR / "launcher.py", root / "launcher.py")
        make_version(root, "9.0.0", STUB_OK)
        write_current(root, "9.0.0\n")
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(3)"],
            creationflags=0x08000000)
        try:
            import time
            t0 = time.monotonic()
            rc = run_launcher(root, "--wait-pid", str(sleeper.pid))
            waited = time.monotonic() - t0
        finally:
            sleeper.wait()
        check(f"대기 후 정상 실행 (rc={rc}, {waited:.1f}s)", rc == 0)
        check("실제로 기다렸다 (2초 이상)", waited >= 2.0)
        check("로그에 wait for pid", f"wait for pid {sleeper.pid}" in launcher_log(root))

        print("\nALL OK")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
