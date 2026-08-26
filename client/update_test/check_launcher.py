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
import time
from pathlib import Path

CLIENT_DIR = Path(__file__).resolve().parent.parent
SYS32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
STUB_OK = SYS32 / "hostname.exe"      # exit 0
STUB_CRASH = SYS32 / "where.exe"      # 인자 없이 실행하면 exit 2
STUB_WAIT = SYS32 / "ping.exe"        # 여러 번 ping 시 수 초간 생존


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
    for stub in (STUB_OK, STUB_CRASH, STUB_WAIT):
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

        print("[4-2] 직접 설치 중단 폴더는 파일이 있어도 실행하지 않는다")
        root = work / "installing"
        root.mkdir()
        shutil.copy2(CLIENT_DIR / "launcher.py", root / "launcher.py")
        make_version(root, "9.0.0", STUB_OK)
        partial = make_version(root, "9.0.1", STUB_OK)
        (partial / ".installing").write_text(
            '{"install_id":"partial","version":"9.0.1","release":"r1"}',
            encoding="utf-8")
        write_current(root, "9.0.1\n9.0.0\n")
        check("중단 폴더를 건너뛰고 구버전 실행", run_launcher(root) == 0)
        log = launcher_log(root)
        check("중단된 9.0.1 미실행", "launch 9.0.1" not in log)
        check("정상 9.0.0 실행", "launch 9.0.0" in log)

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

        check_running_process_detection(work)
        check_apply_update(work)

        print("\nALL OK")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def check_running_process_detection(work):
    """본체/런처/Qt 역할을 구분하고 고아 Qt만 자동 정리한다."""
    sys.path.insert(0, str(CLIENT_DIR))
    import importlib

    launcher = importlib.import_module("launcher")
    print("[5-1] 업데이트 전 실행 중 Honey 탐지")
    root = work / "running"
    app_dir = root / "versions" / "9.0.0"
    app_dir.mkdir(parents=True)
    exe = app_dir / "HoneyApp.exe"
    shutil.copy2(STUB_WAIT, exe)
    proc = subprocess.Popen(
        [str(exe), "127.0.0.1", "-n", "20"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000)
    try:
        time.sleep(0.5)
        found = launcher.running_honey_processes(root)
        check("같은 루트 HoneyApp.exe 탐지", any(p["pid"] == proc.pid for p in found))
        check("HoneyApp.exe를 실제 앱으로 분류",
              any(p["pid"] == proc.pid and p["role"] == "app" for p in found))
        logs = []
        check("업데이트가 없으면 실행 중 앱의 중복 실행을 막음",
              launcher.handle_running_without_update(
                  root, logs.append, False, launcher_wait_sec=0) is False
              and proc.poll() is None)
        check("무UI에서는 실행 프로세스를 종료하지 않고 업데이트 보류",
              launcher.ask_and_close_running_honey(root, found, logs.append, False) is False
              and proc.poll() is None)
        check("동의 후 강제 종료에 쓰는 Windows 핸들 경로",
              launcher._terminate_process(proc.pid))
        proc.wait(timeout=10)
        check("탐지한 Honey 프로세스 종료 확인", proc.poll() is not None)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)

    print("[5-1a] 종료 동의 후 본체와 Qt 보조 프로세스를 함께 정리한다")
    root = work / "running-accepted"
    app_dir = root / "versions" / "9.0.0"
    app_dir.mkdir(parents=True)
    app_exe = app_dir / "HoneyApp.exe"
    helper_exe = app_dir / "QtWebEngineProcess.exe"
    shutil.copy2(STUB_WAIT, app_exe)
    shutil.copy2(STUB_WAIT, helper_exe)
    app_proc = subprocess.Popen(
        [str(app_exe), "127.0.0.1", "-n", "20"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000)
    helper_proc = subprocess.Popen(
        [str(helper_exe), "127.0.0.1", "-n", "20"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000)
    real_confirm = launcher._confirm_close_running_honey
    real_post_close = launcher._post_close_to_processes
    try:
        time.sleep(0.5)
        found = launcher.running_honey_processes(root)
        launcher._confirm_close_running_honey = lambda _text: True
        launcher._post_close_to_processes = lambda pids: [
            launcher._terminate_process(pid) for pid in pids]
        logs = []
        check("종료 동의 시 업데이트 계속",
              launcher.ask_and_close_running_honey(
                  root, found, logs.append, True) is True)
        app_proc.wait(timeout=10)
        helper_proc.wait(timeout=10)
        check("동의 후 HoneyApp.exe 종료", app_proc.poll() is not None)
        check("동의 후 Qt 보조 프로세스 종료", helper_proc.poll() is not None)
    finally:
        launcher._confirm_close_running_honey = real_confirm
        launcher._post_close_to_processes = real_post_close
        for running_proc in (app_proc, helper_proc):
            if running_proc.poll() is None:
                running_proc.terminate()
                running_proc.wait(timeout=10)

    print("[5-2] 고아 Qt 보조 프로세스는 최신 버전 실행을 막지 않는다")
    root = work / "orphan-helper"
    app_dir = make_version(root, "9.0.0", STUB_OK)
    write_current(root, "9.0.0\n")
    helper_exe = app_dir / "QtWebEngineProcess.exe"
    shutil.copy2(STUB_WAIT, helper_exe)
    helper = subprocess.Popen(
        [str(helper_exe), "127.0.0.1", "-n", "20"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000)
    try:
        time.sleep(0.5)
        found = launcher.running_honey_processes(root)
        check("QtWebEngineProcess.exe를 helper로 분류",
              any(p["pid"] == helper.pid and p["role"] == "helper" for p in found))
        logs = []
        result = launcher.handle_running_without_update(
            root, logs.append, False, launcher_wait_sec=0)
        helper.wait(timeout=10)
        check("고아 Qt가 있어도 런처 실행 계속", result is None)
        check("고아 Qt 자동 종료", helper.poll() is not None)
        check("최신 실행 후보 유지", launcher.candidates(root)[0] == "9.0.0")
        check("고아 Qt 정리 로그", any("고아 Qt" in line for line in logs))
    finally:
        if helper.poll() is None:
            helper.terminate()
            helper.wait(timeout=10)

    print("[5-3] 다른 Honey.exe가 작업 중이면 안내 후 중복 실행을 막는다")
    root = work / "other-launcher"
    root.mkdir()
    other_exe = root / "Honey.exe"
    shutil.copy2(STUB_WAIT, other_exe)
    other = subprocess.Popen(
        [str(other_exe), "127.0.0.1", "-n", "20"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=0x08000000)
    try:
        time.sleep(0.5)
        logs = []
        _processes, blocked = launcher.wait_for_other_launcher(
            root, logs.append, False, timeout_sec=0)
        check("다른 런처 중복 실행 차단", blocked and other.poll() is None)
        check("무반응 대신 사유 로그", any("계속 실행 중" in line for line in logs))
    finally:
        other.terminate()
        other.wait(timeout=10)

    print("[5-4] 업데이트 생략 경로에서도 고아 Qt를 통과시킨다")
    for label, argv, noupdate, offline in (
            ("skip-update", ["--skip-update"], False, False),
            ("no-ui", [], False, False),
            ("noupdate.txt", [], True, False),
            ("offline", [], False, True)):
        root = work / f"orphan-{label}"
        app_dir = make_version(root, "9.0.0", STUB_OK)
        write_current(root, "9.0.0\n")
        if noupdate:
            (root / "noupdate.txt").write_text("1\n", encoding="utf-8")
        helper_exe = app_dir / "QtWebEngineProcess.exe"
        shutil.copy2(STUB_WAIT, helper_exe)
        helper = subprocess.Popen(
            [str(helper_exe), "127.0.0.1", "-n", "20"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000)
        real_fetch = launcher.app_update.fetch_manifest
        if offline:
            launcher.app_update.fetch_manifest = lambda _url: (_ for _ in ()).throw(
                OSError("offline-test"))
        try:
            time.sleep(0.5)
            show_ui = label != "no-ui"
            result = launcher.try_update(root, argv, show_ui=show_ui)
            helper.wait(timeout=10)
            check(f"{label}: 실행 계속", result is None)
            check(f"{label}: 고아 Qt 종료", helper.poll() is not None)
        finally:
            launcher.app_update.fetch_manifest = real_fetch
            if helper.poll() is None:
                helper.terminate()
                helper.wait(timeout=10)


def check_apply_update(work):
    """_apply_update 의 대상 준비 — 불완전 폴더를 rename 하지 않고 복구 경로로 보낸다."""
    sys.path.insert(0, str(CLIENT_DIR))
    import importlib

    launcher = importlib.import_module("launcher")
    au = importlib.import_module("transport.app_update")

    print("[6] _apply_update 직접 설치 준비")
    root = work / "apply"
    (root / "versions").mkdir(parents=True)
    (root / "updates").mkdir()
    logs = []

    def logf(message):
        logs.append(message)

    # 불완전/잠긴 폴더를 치우지 않고 그대로 둔 채 전체 zip 설치로 진행해야 한다.
    locked = root / "versions" / "9.9.9"
    locked.mkdir()
    (locked / "HoneyApp.exe").write_bytes(b"x")
    holder = open(locked / "locked.dll", "wb")
    holder.write(b"in-use")
    holder.flush()
    downloads = []
    real_download = au.download
    au.download = lambda *a, **k: downloads.append(a[0]) or (_ for _ in ()).throw(
        RuntimeError("download-stub"))
    try:
        try:
            launcher._apply_update(
                root, "http://127.0.0.1:1", "9.9.9", {"file": "x.zip"}, None,
                lambda *a: True, logf)
            raise AssertionError("다운로드 스텁 예외가 전달되지 않았다")
        except RuntimeError as exc:
            check("repair 뒤 설치 다운로드로 진행", str(exc) == "download-stub")
    finally:
        au.download = real_download
        holder.close()
    check("전체 zip 다운로드를 한 번 시도", len(downloads) == 1)
    check("대상 폴더를 rename/delete 하지 않음",
          locked.exists() and (locked / "locked.dll").read_bytes() == b"in-use"
          and not list((root / "versions").glob("*.old-*")))

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
