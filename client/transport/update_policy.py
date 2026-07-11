"""Update install-method helpers (auto/manual).

새 버전 감지 시 클라이언트가 [자동 설치] / [ZIP 다운로드] / [나중에] 를
사용자에게 물어 방식을 고른다. 서버는 방식을 강제하지 않는다.
- auto  : ZIP 다운로드 후 자동 덮어쓰기 설치 (updater.apply_update_zip).
          설치 폴더 쓰기 불가면(updater.can_write_app_dir) 이 옵션은 비활성.
- manual: ZIP 을 다운로드 폴더에 저장만 하고 탐색기로 열어줌 (설치는 수동).

이 모듈은 manual 다운로드에 필요한 경로/탐색기 헬퍼만 제공한다.
"""
import ctypes
import subprocess
from pathlib import Path

MODE_AUTO = "auto"
MODE_MANUAL = "manual"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]


def downloads_dir() -> Path:
    """사용자 다운로드 폴더. Known Folder API 실패 시 홈 폴더로 폴백."""
    try:
        # FOLDERID_Downloads {374DE290-123F-4565-9164-39C4925E467B}
        guid = _GUID(
            0x374DE290, 0x123F, 0x4565,
            (ctypes.c_uint8 * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
        )
        path_ptr = ctypes.c_wchar_p()
        rc = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        if rc == 0 and path_ptr.value:
            found = Path(path_ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
            if found.is_dir():
                return found
    except Exception:
        pass
    fallback = Path.home() / "Downloads"
    return fallback if fallback.is_dir() else Path.home()


def unique_dest(directory: Path, filename: str) -> Path:
    """directory/filename 이 이미 있으면 "이름 (1).zip" 식으로 회피."""
    dest = Path(directory) / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for n in range(1, 100):
        candidate = dest.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    return dest  # 100개 넘게 쌓였으면 그냥 덮어쓴다


def open_folder_select(path: Path) -> None:
    """탐색기를 열어 다운로드된 파일을 선택 상태로 보여준다. 실패해도 무시."""
    try:
        subprocess.Popen(["explorer", "/select,", str(path)])
    except OSError:
        pass
