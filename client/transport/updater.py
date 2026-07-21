"""Honey automatic updater for PyInstaller onedir ZIP releases.

The app downloads Honey-<version>.zip, extracts it to a temporary directory,
then starts a detached batch file. The batch file waits until the current
Honey.exe process exits, stages the new _internal next to the live one
(_internal.new), swaps it in with directory renames, copies the root files,
and starts Honey.exe again. Any failure after the swap rolls back to the
previous _internal / Honey.exe — a half-updated install is never left behind.

설치 폴더에 쓰기 권한이 없으면 실행하지 않는다 (UAC 승격 없음 — 그 경우
클라이언트가 자동 설치 버튼을 비활성화하고 ZIP 다운로드만 안내한다).

진단 로그는 파이썬·배치 양쪽이 <Honey.exe 폴더>\\log\\update.log 한 파일에 남긴다
(종전엔 %TEMP%\\honey_update.log 였는데 현장 사용자가 %TEMP% 를 찾지 못했다).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

_DETACHED = 0x00000008 | 0x00000200
_BAT_CONFIRM_SEC = 5.0          # 배치 진입(flag 파일) 확인에 쓸 최대 대기
_LOG_ENCODING = "mbcs" if sys.platform == "win32" else "utf-8"
_LOG_MAX_BYTES = 1_000_000
_rotated = False


def is_frozen() -> bool:
    """Return True when running as a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def log_dir() -> Path:
    """업데이트 로그 폴더 = run_log 와 같은 곳 (<Honey.exe 폴더>\\log).

    배치의 루트 robocopy 는 /E (미러 아님) + /XD _internal log 라 이 폴더는
    업데이트로 지워지지 않는다. _internal swap 도 이 폴더를 건드리지 않는다.
    """
    if is_frozen():
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent   # client/
    return base / "log"


def log_path() -> Path:
    return log_dir() / "update.log"


def ulog(message: str) -> None:
    """업데이트 진단 한 줄. log/update.log append + stdout (run_log 가 tee).

    best-effort — 로깅 실패가 업데이트 자체를 막지 않는다. 배치도 같은 파일에
    ASCII 로만 append 하므로 파이썬/배치 기록이 한 파일에 시간순으로 쌓인다.
    파일 인코딩을 mbcs 로 고정하는 이유도 배치 echo 와 맞추기 위해서다.
    """
    global _rotated
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [py {os.getpid()}] {message}"
    try:
        print(f"[update] {line}")
    except Exception:
        pass
    try:
        target = log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        if not _rotated:
            _rotated = True
            if target.exists() and target.stat().st_size > _LOG_MAX_BYTES:
                target.replace(target.with_suffix(".log.old"))
        with open(target, "a", encoding=_LOG_ENCODING, errors="replace") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def log_startup_state(version: str = "") -> None:
    """앱 시작 시 1줄. 잔재 파일이 있으면 지난 배치가 중간에 죽었다는 직접 증거."""
    leftovers = []
    try:
        if is_frozen():
            app_dir = Path(sys.executable).resolve().parent
            leftovers = [n for n in ("_internal.new", "_internal.old", "Honey.exe.bak")
                         if (app_dir / n).exists()]
    except Exception:
        pass
    msg = f"START version={version} frozen={is_frozen()}"
    if leftovers:
        msg += f" LEFTOVER={leftovers} (이전 업데이트가 중간에 중단된 흔적)"
    ulog(msg)


def _safe_extract(zip_path: Path, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            dest = (target_root / member.filename).resolve()
            if os.path.commonpath([str(target_root), str(dest)]) != str(target_root):
                raise RuntimeError(f"unsafe path in update zip: {member.filename}")
        zf.extractall(target_root)


def _find_payload_dir(extract_root: Path) -> Path:
    preferred = extract_root / "Honey"
    if (preferred / "Honey.exe").exists():
        return preferred
    if (extract_root / "Honey.exe").exists():
        return extract_root

    matches = list(extract_root.rglob("Honey.exe"))
    if not matches:
        raise RuntimeError("Honey.exe was not found in update zip")
    return matches[0].parent


def _is_writable(directory: Path) -> bool:
    """설치폴더에 직접 쓸 수 있는지 probe 파일로 확인."""
    probe = directory / f".honey_write_test_{os.getpid()}"
    try:
        with open(probe, "w"):
            pass
        probe.unlink()
        return True
    except OSError:
        return False


def can_write_app_dir() -> bool:
    """자동 설치 가능 여부. dev 모드는 True, frozen 이면 exe 폴더 쓰기 probe."""
    if not is_frozen():
        return True
    return _is_writable(Path(sys.executable).resolve().parent)


def _launch_normal(bat_path: Path, cmd_log: Path):
    """배치를 detached 로 띄우고 Popen 을 반환.

    stdout/stderr 를 파일로 받아 cmd.exe 자체 오류(배치 파일을 못 찾음, 실행 차단 등)를
    남긴다. 종전엔 리다이렉트가 없어 이런 오류가 어디에도 안 남았다.
    """
    out = open(cmd_log, "ab")
    try:
        return subprocess.Popen(
            ["cmd.exe", "/c", str(bat_path)],
            creationflags=_DETACHED,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
        )
    finally:
        out.close()   # 자식이 복제 핸들을 가지므로 부모 쪽은 닫는다


def apply_update_zip(zip_path) -> dict:
    """Apply a downloaded Honey ZIP release after the current app exits.

    반환: {"confirmed": bool, "child_pid": int, "bat": str, "log": str}
    confirmed=False 는 배치를 띄웠으나 진입 흔적(flag)을 못 봤다는 뜻 — 호출부는
    로그만 남기고 계속 진행한다(오탐으로 정상 업데이트를 막지 않기 위해).
    """
    ulog("==== AUTO UPDATE 시작 (실패하면 이 log 폴더를 담당자에게 보내주세요) ====")
    if not is_frozen():
        ulog("ABORT not frozen")
        raise RuntimeError("ZIP update can only be applied from a built Honey.exe")

    zip_path = Path(zip_path).resolve()
    app_dir = Path(sys.executable).resolve().parent
    app_exe = app_dir / "Honey.exe"

    try:
        zip_size = zip_path.stat().st_size
    except OSError:
        zip_size = -1
    ulog(f"ZIP {zip_path} size={zip_size}")
    ulog(f"APPDIR {app_dir}")
    try:
        ulog(f"DISK app_free_mb={shutil.disk_usage(app_dir).free // (1024 * 1024)} "
             f"temp={tempfile.gettempdir()} "
             f"temp_free_mb={shutil.disk_usage(tempfile.gettempdir()).free // (1024 * 1024)}")
    except OSError as exc:
        ulog(f"DISK check failed: {exc}")

    if not _is_writable(app_dir):
        # 자동 설치 버튼은 can_write_app_dir 로 이미 비활성이지만 방어적으로 재확인
        ulog("ABORT 설치 폴더 쓰기 권한 없음")
        raise RuntimeError(
            "설치 폴더에 쓰기 권한이 없습니다. 수동 업데이트(ZIP 다운로드)를 이용하세요.")

    extract_root = Path(tempfile.mkdtemp(prefix="honey_update_"))
    ulog(f"EXTRACT start -> {extract_root}")
    _t0 = time.monotonic()
    _safe_extract(zip_path, extract_root)
    ulog(f"EXTRACT done {time.monotonic() - _t0:.1f}s")
    payload_dir = _find_payload_dir(extract_root)
    ulog(f"PAYLOAD {payload_dir} internal={(payload_dir / '_internal').is_dir()}")

    logs = log_dir()
    flag_path = logs / f"update_bat_{os.getpid()}.flag"
    cmd_log = logs / "update_cmd.log"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        flag_path.unlink(missing_ok=True)   # pid 재사용 대비 잔재 제거
    except OSError as exc:
        ulog(f"LOGDIR prepare failed: {exc}")

    bat_path = Path(tempfile.gettempdir()) / f"honey_update_{os.getpid()}.bat"
    # 배치 echo 는 전부 ASCII 로만 쓴다 (notify_failed 의 사용자 안내 1줄만 한글) —
    # mbcs 인코딩이라 cp949 로 표현 못 하는 문자가 섞이면 ? 로 깨진다.
    bat_text = f"""@echo off
setlocal
set "SRC={payload_dir}"
set "DST={app_dir}"
set "EXE={app_exe}"
set "LOGDIR={logs}"
set "LOG=%LOGDIR%\\update.log"
set "RCLOG=%LOGDIR%\\update_robocopy.log"
set "FLAG={flag_path}"
set "STAGE=enter"
if not exist "%LOGDIR%" md "%LOGDIR%" 2>NUL
echo BAT_ENTER> "%FLAG%"
echo [%date% %time%] BAT enter parent_pid={os.getpid()} >> "%LOG%"
echo [%date% %time%] BAT src=%SRC% >> "%LOG%"

set "STAGE=wait_parent_exit"
set /a TRIES=0
:wait_for_exit
tasklist /FI "PID eq {os.getpid()}" /NH 2>NUL | find "{os.getpid()}" >NUL 2>&1
if errorlevel 1 goto stage_files
set /a TRIES+=1
if %TRIES% GEQ 120 goto wait_timeout
rem timeout 대신 ping 슬립 - 콘솔 입력 핸들이 없어도 동작 (1회 약 1초)
ping -n 2 127.0.0.1 >NUL 2>&1
goto wait_for_exit

:wait_timeout
rem 종전엔 여기서 exit /b 1 로 조용히 끝나 사용자가 아무것도 못 봤다 - 알림 경로로 보낸다.
echo [%date% %time%] BAT FAILED stage=wait_parent_exit reason=parent still alive >> "%LOG%"
goto notify_failed

:stage_files
echo [%date% %time%] BAT parent exited after %TRIES% tries >> "%LOG%"
rem 라이브 설치본을 건드리지 않는 스테이징(_internal.new)에 먼저 전체 복사 -
rem 여기서 실패하면 설치본 무손상으로 중단된다. 구버전 잔재 제거는 /MIR 대신
rem 디렉토리 통째 교체(swap)가 담당한다. 이전 실패 잔재부터 정리.
set "STAGE=cleanup_leftover"
if exist "%DST%\\_internal.new" rd /s /q "%DST%\\_internal.new"
if exist "%DST%\\_internal.old" rd /s /q "%DST%\\_internal.old"

set "STAGE=stage_internal"
robocopy "%SRC%\\_internal" "%DST%\\_internal.new" /E /R:3 /W:2 /NFL /NDL /NJH /NJS /NP >> "%RCLOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] BAT stage=stage_internal robocopy_rc=%RC% >> "%LOG%"
if %RC% GEQ 8 goto stage_failed

set "STAGE=backup_exe"
copy /y "%EXE%" "%EXE%.bak" >NUL
if errorlevel 1 goto stage_failed

rem swap: 동일 볼륨 rename 두 번 (순간) - 실패 지점별로 복원 경로가 있다
set "STAGE=move_internal_to_old"
move "%DST%\\_internal" "%DST%\\_internal.old" >NUL
if errorlevel 1 goto stage_failed

set "STAGE=move_new_to_internal"
move "%DST%\\_internal.new" "%DST%\\_internal" >NUL
if errorlevel 1 goto swap_failed
echo [%date% %time%] BAT stage=swap done >> "%LOG%"

rem 루트 파일(Honey.exe 등) 복사 - log/ 등 보존 위해 /E (미러 아님). log 는 방어적 명시 제외.
set "STAGE=copy_root"
robocopy "%SRC%" "%DST%" /E /XD _internal log /R:3 /W:2 /NFL /NDL /NJH /NJS /NP >> "%RCLOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] BAT stage=copy_root robocopy_rc=%RC% >> "%LOG%"
if %RC% GEQ 8 goto root_failed

set "STAGE=finalize"
rd /s /q "%DST%\\_internal.old"
del "%EXE%.bak" >NUL 2>&1
echo [%date% %time%] BAT end result=SUCCESS >> "%LOG%"
del "%FLAG%" >NUL 2>&1
start "" "%EXE%" >NUL 2>&1
exit /b 0

:stage_failed
rem 라이브 설치본 무손상 - 스테이징/백업 잔재만 정리하고 중단
echo [%date% %time%] BAT FAILED stage=%STAGE% (install untouched) >> "%LOG%"
if exist "%DST%\\_internal.new" rd /s /q "%DST%\\_internal.new"
if exist "%EXE%.bak" del "%EXE%.bak" >NUL 2>&1
goto notify_failed

:swap_failed
rem _internal 이 _internal.old 로 빠진 상태 - 구버전 원복
echo [%date% %time%] BAT FAILED stage=%STAGE% (rollback swap) >> "%LOG%"
move "%DST%\\_internal.old" "%DST%\\_internal" >NUL
if exist "%DST%\\_internal.new" rd /s /q "%DST%\\_internal.new"
if exist "%EXE%.bak" del "%EXE%.bak" >NUL 2>&1
goto notify_failed

:root_failed
rem 새 _internal 제거 후 구버전 _internal / Honey.exe 복원 (혼합 상태 방지)
echo [%date% %time%] BAT FAILED stage=%STAGE% (rollback root) >> "%LOG%"
rd /s /q "%DST%\\_internal"
move "%DST%\\_internal.old" "%DST%\\_internal" >NUL
copy /y "%EXE%.bak" "%EXE%" >NUL
del "%EXE%.bak" >NUL 2>&1
goto notify_failed

:notify_failed
echo [%date% %time%] BAT end result=FAILED stage=%STAGE% >> "%LOG%"
echo 수동 설치: "%SRC%" 안의 내용을 "%DST%" 에 복사한 뒤 Honey.exe 를 실행하세요. >> "%LOG%"
del "%FLAG%" >NUL 2>&1
start "" notepad "%LOG%"
exit /b 1
"""
    # cmd 는 ANSI(cp949) 배치만 안전하게 해석. 인코딩 불가 문자는 ? 로 치환해
    # (주석/메시지에만 존재) 업데이트 전체가 크래시하지 않게 한다.
    bat_path.write_text(bat_text, encoding="mbcs", errors="replace")
    ulog(f"BAT written {bat_path}")

    try:
        proc = _launch_normal(bat_path, cmd_log)
    except Exception as exc:
        ulog(f"POPEN FAILED {type(exc).__name__}: {exc}")
        raise RuntimeError(f"업데이트 실행 파일을 시작하지 못했습니다: {exc}") from exc
    ulog(f"POPEN ok child_pid={proc.pid}")

    # 배치가 실제로 진입했는지 flag 파일로 확인. 정상 배치는 부모(=현재 프로세스) 종료를
    # 최대 120초 기다리므로 이 구간에서 절대 끝나지 않는다 - 여기서 죽었다면 배치 실행
    # 자체가 막힌 것(보안 프로그램 차단 등)이다.
    confirmed = False
    deadline = time.monotonic() + _BAT_CONFIRM_SEC
    while time.monotonic() < deadline:
        if flag_path.exists():
            confirmed = True
            break
        rc = proc.poll()
        if rc is not None:
            ulog(f"BAT DIED early rc={rc} (log/update_cmd.log 확인)")
            raise RuntimeError(
                f"업데이트 설치 프로그램이 즉시 종료됐습니다 (종료코드 {rc}). "
                "보안 프로그램이 임시 폴더의 배치 실행을 차단했을 수 있습니다.")
        time.sleep(0.1)
    ulog(f"BAT enter confirmed={confirmed}")
    return {"confirmed": confirmed, "child_pid": proc.pid,
            "bat": str(bat_path), "log": str(log_path())}
