"""Honey automatic updater for PyInstaller onedir ZIP releases.

The app downloads Honey-<version>.zip, extracts it to a temporary directory,
then starts a detached batch file. The batch file waits until the current
Honey.exe process exits, copies the extracted onedir payload over the app
directory, and starts Honey.exe again.
"""
import ctypes
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


def _launch_normal(bat_path: Path) -> None:
    subprocess.Popen(
        ["cmd.exe", "/c", str(bat_path)],
        creationflags=_DETACHED,
        close_fds=True,
    )


def _launch_elevated(bat_path: Path) -> None:
    """UAC 승격으로 batch 실행. 취소·실패 시 RuntimeError."""
    # SW_HIDE = 0 (콘솔창 숨김). "runas" 가 UAC 프롬프트를 띄운다.
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", "cmd.exe", f'/c "{bat_path}"', None, 0
    )
    if rc <= 32:
        raise RuntimeError(f"관리자 권한 승격 실패 또는 취소됨 (code {rc})")


def apply_update_zip(zip_path) -> None:
    """Apply a downloaded Honey ZIP release after the current app exits."""
    if not is_frozen():
        raise RuntimeError("ZIP update can only be applied from a built Honey.exe")

    zip_path = Path(zip_path).resolve()
    app_dir = Path(sys.executable).resolve().parent
    app_exe = app_dir / "Honey.exe"

    extract_root = Path(tempfile.mkdtemp(prefix="honey_update_"))
    _safe_extract(zip_path, extract_root)
    payload_dir = _find_payload_dir(extract_root)

    bat_path = Path(tempfile.gettempdir()) / f"honey_update_{os.getpid()}.bat"
    bat_text = f"""@echo off
setlocal
set "SRC={payload_dir}"
set "DST={app_dir}"
set "EXE={app_exe}"

:wait_for_exit
tasklist /FI "PID eq {os.getpid()}" 2>NUL | find "{os.getpid()}" >NUL
if not errorlevel 1 (
  timeout /t 1 /nobreak >NUL
  goto wait_for_exit
)

robocopy "%SRC%" "%DST%" /E /R:2 /W:1 /NFL /NDL /NJH /NJS /NP
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 exit /b %RC%

start "" "%EXE%"
exit /b 0
"""
    bat_path.write_text(bat_text, encoding="mbcs")
    if _is_writable(app_dir):
        _launch_normal(bat_path)
    else:
        _launch_elevated(bat_path)
