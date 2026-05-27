"""Honey 자동 업데이트 실행기 (Windows).

실행 중인 exe 는 OS 가 이미지 락을 걸고 있어 그 자리에서 덮어쓸 수 없다.
그래서 외부 updater.bat 를 detached 로 띄우고 → 이 앱이 종료(락 해제)되기를
기다렸다가 → staged 새 exe 로 교체 → 재실행 → 자기 자신(bat) 삭제 한다.

frozen(PyInstaller onefile) 에서만 의미가 있다. 스크립트(`python honey_main.py`)
실행 중에는 교체 대상 exe 가 없으므로 is_frozen() == False.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# updater.bat:
#   %1 = staged 새 exe (교체 원본)
#   %2 = target  = 현재 실행 exe (교체 대상, 처음엔 락 상태)
# move 가 락 때문에 실패하면(errorlevel 1) ~0.5s 간격으로 재시도, 약 60s 후 포기.
_BAT_TEMPLATE = r"""@echo off
setlocal
set "NEW=%~1"
set "TARGET=%~2"
set /a TRIES=0

:waitloop
ping -n 2 127.0.0.1 >nul
move /y "%NEW%" "%TARGET%" >nul 2>&1
if not errorlevel 1 goto launch
set /a TRIES+=1
if %TRIES% GEQ 120 goto cleanup
goto waitloop

:launch
start "" /D "%~dp2" "%TARGET%"

:cleanup
del "%~f0"
"""


def is_frozen() -> bool:
    """PyInstaller 등으로 패키징된 exe 로 실행 중인지."""
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> Path:
    """교체 대상이 되는 현재 실행 exe 경로 (frozen 일 때 유효)."""
    return Path(sys.executable).resolve()


def staging_path(target_exe=None) -> Path:
    """새 exe 를 받아둘 임시 경로. move 가 같은 볼륨에서 일어나도록
    target 과 같은 디렉터리에 둔다 (cross-volume move 회피)."""
    target = Path(target_exe).resolve() if target_exe else current_exe_path()
    return target.with_name(target.stem + ".new.exe")


def apply_update(new_exe, target_exe=None) -> Path:
    """staged new_exe 로 target_exe 를 교체하는 updater.bat 생성·실행.

    detached 로 띄우므로 이 함수는 즉시 반환한다. 호출 측은 곧바로 앱을
    종료해야 락이 풀려 교체가 진행된다.

    Returns: 생성된 updater.bat 경로.
    """
    if target_exe is None:
        target_exe = current_exe_path()
    new_exe = Path(new_exe).resolve()
    target_exe = Path(target_exe).resolve()

    bat_path = Path(tempfile.gettempdir()) / "honey_update.bat"
    bat_path.write_text(_BAT_TEMPLATE, encoding="ascii")

    # DETACHED_PROCESS(0x08) | CREATE_NEW_PROCESS_GROUP(0x200):
    # 부모(이 앱)가 종료돼도 bat 가 계속 살아 있도록, 콘솔 창 없이 실행.
    creationflags = 0x00000008 | 0x00000200
    subprocess.Popen(
        ["cmd", "/c", str(bat_path), str(new_exe), str(target_exe)],
        creationflags=creationflags,
        close_fds=True,
        cwd=str(bat_path.parent),
    )
    return bat_path
