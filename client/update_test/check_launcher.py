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


def make_version(root, version, stub, internal=True):
    """가짜 버전 폴더. internal=False 면 **반쯤 지워진 폴더**를 흉내낸다.

    실제 onedir 빌드에는 반드시 _internal 이 있고, 런처는 그 유무로 깨진 잔재를
    실행 후보에서 뺀다 (launcher.runnable).
    """
    d = root / "versions" / version
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(stub, d / "HoneyApp.exe")
    if internal:
        (d / "_internal").mkdir(exist_ok=True)
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

        print("[4-1] 반쯤 지워진 버전 폴더는 실행 후보에서 뺀다")
        root = work / "broken"
        root.mkdir()
        shutil.copy2(CLIENT_DIR / "launcher.py", root / "launcher.py")
        make_version(root, "9.0.0", STUB_OK)
        make_version(root, "9.0.1", STUB_OK, internal=False)   # rmtree 실패 잔재
        write_current(root, "9.0.1\n9.0.0\n")
        check("깨진 폴더를 건너뛰고 정상 버전 실행", run_launcher(root) == 0)
        log = launcher_log(root)
        check("9.0.1 을 실행하지 않았다", "launch 9.0.1" not in log)
        check("9.0.0 을 실행했다", "launch 9.0.0" in log)

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

        check_apply_update(work)

        print("\nALL OK")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_apply_update(work):
    """_apply_update 의 실패 분류 — 서버·UI 없이 함수만 직접 부른다.

    여기서 지키려는 계약은 하나다: **로컬 권한 실패는 전체 zip 으로 폴백하지 않는다.**
    폴백하면 331MB 를 받고 같은 자리에서 또 실패한다(2026-08-26 현장 증상).
    """
    sys.path.insert(0, str(CLIENT_DIR))
    import importlib

    launcher = importlib.import_module("launcher")
    au = importlib.import_module("transport.app_update")

    print("[6] _apply_update 실패 분류")
    root = work / "apply"
    (root / "versions").mkdir(parents=True)
    (root / "updates").mkdir()
    logs = []

    def logf(message):
        logs.append(message)

    # 잠긴 폴더 → prepare_target 이 LocalWriteError → 다운로드 시도 자체가 없어야 한다.
    locked = root / "versions" / "9.9.9"
    locked.mkdir()
    (locked / "HoneyApp.exe").write_bytes(b"x")
    holder = open(locked / "locked.dll", "wb")
    holder.write(b"in-use")
    holder.flush()
    downloads = []
    real_download = au.download
    au.download = lambda *a, **k: downloads.append(a[0]) or real_download(*a, **k)
    try:
        launcher._apply_update(
            root, "http://127.0.0.1:1", "9.9.9", {"file": "x.zip"}, None,
            lambda *a: True, logf)
        raise AssertionError("잠긴 폴더인데 설치가 진행됐다")
    except au.LocalWriteError as exc:
        check("LocalWriteError 가 그대로 올라온다", str(exc.path).endswith("9.9.9"))
    finally:
        au.download = real_download
        holder.close()
    check("전체 zip 을 받지 않았다 (중복 실패 제거)", not downloads)

    # 완성된 폴더 → adopt. 역시 아무것도 받지 않는다.
    done = root / "versions" / "9.8.0"
    (done / "_internal").mkdir(parents=True)
    (done / "HoneyApp.exe").write_bytes(b"app")
    au.write_file_manifest(done, au.build_file_manifest(done))
    downloads.clear()
    au.download = lambda *a, **k: downloads.append(a[0]) or real_download(*a, **k)
    try:
        mode = launcher._apply_update(
            root, "http://127.0.0.1:1", "9.8.0", {"file": "x.zip"}, None,
            lambda *a: True, logf)
    finally:
        au.download = real_download
    check("완성된 폴더는 adopt", mode == "adopt")
    check("adopt 는 다운로드 0", not downloads)


if __name__ == "__main__":
    main()
