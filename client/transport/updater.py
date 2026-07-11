"""Honey automatic updater for PyInstaller onedir ZIP releases.

The app downloads Honey-<version>.zip, extracts it to a temporary directory,
then starts a detached batch file. The batch file waits until the current
Honey.exe process exits, copies the extracted onedir payload over the app
directory, and starts Honey.exe again.

설치 폴더에 쓰기 권한이 없으면 실행하지 않는다 (UAC 승격 없음 — 그 경우
클라이언트가 자동 설치 버튼을 비활성화하고 ZIP 다운로드만 안내한다).
"""
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

_DETACHED = 0x00000008 | 0x00000200


def is_frozen() -> bool:
    """Return True when running as a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


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


def _launch_normal(bat_path: Path) -> None:
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat_path)],
        creationflags=_DETACHED,
        close_fds=True,
    )


def apply_update_zip(zip_path) -> None:
    """Apply a downloaded Honey ZIP release after the current app exits."""
    if not is_frozen():
        raise RuntimeError("ZIP update can only be applied from a built Honey.exe")

    zip_path = Path(zip_path).resolve()
    app_dir = Path(sys.executable).resolve().parent
    app_exe = app_dir / "Honey.exe"

    if not _is_writable(app_dir):
        # 자동 설치 버튼은 can_write_app_dir 로 이미 비활성이지만 방어적으로 재확인
        raise RuntimeError(
            "설치 폴더에 쓰기 권한이 없습니다. 수동 업데이트(ZIP 다운로드)를 이용하세요.")

    extract_root = Path(tempfile.mkdtemp(prefix="honey_update_"))
    _safe_extract(zip_path, extract_root)
    payload_dir = _find_payload_dir(extract_root)

    bat_path = Path(tempfile.gettempdir()) / f"honey_update_{os.getpid()}.bat"
    bat_text = f"""@echo off
setlocal
set "SRC={payload_dir}"
set "DST={app_dir}"
set "EXE={app_exe}"
set "LOG=%TEMP%\\honey_update.log"

echo [%date% %time%] update start (pid {os.getpid()}) >> "%LOG%"

set /a TRIES=0
:wait_for_exit
tasklist /FI "PID eq {os.getpid()}" /NH 2>NUL | find "{os.getpid()}" >NUL 2>&1
if errorlevel 1 goto copy_files
set /a TRIES+=1
if %TRIES% GEQ 120 goto wait_timeout
rem timeout 대신 ping 슬립 - 콘솔 입력 핸들이 없어도 동작 (1회 약 1초)
ping -n 2 127.0.0.1 >NUL 2>&1
goto wait_for_exit

:wait_timeout
echo [%date% %time%] FAILED: Honey (pid {os.getpid()}) 종료 대기 시간 초과 - 업데이트 중단 >> "%LOG%"
exit /b 1

:copy_files
rem _internal 은 /MIR 로 구버전 잔재 파일까지 제거, 루트는 log/ 등 보존 위해 /E
robocopy "%SRC%\\_internal" "%DST%\\_internal" /MIR /R:3 /W:2 /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] robocopy _internal RC=%RC% >> "%LOG%"
if %RC% GEQ 8 goto copy_failed

robocopy "%SRC%" "%DST%" /E /XD _internal /R:3 /W:2 /NFL /NDL /NJH /NJS /NP >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] robocopy root RC=%RC% >> "%LOG%"
if %RC% GEQ 8 goto copy_failed

start "" "%EXE%"
exit /b 0

:copy_failed
echo [%date% %time%] 업데이트 파일 복사 실패 (robocopy RC=%RC%) >> "%LOG%"
echo 수동 설치: "%SRC%" 안의 내용을 "%DST%" 에 복사한 뒤 Honey.exe 를 실행하세요. >> "%LOG%"
start "" notepad "%LOG%"
exit /b 1
"""
    # cmd 는 ANSI(cp949) 배치만 안전하게 해석. 인코딩 불가 문자는 ? 로 치환해
    # (주석/메시지에만 존재) 업데이트 전체가 크래시하지 않게 한다.
    bat_path.write_text(bat_text, encoding="mbcs", errors="replace")
    _launch_normal(bat_path)
