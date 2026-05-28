"""Honey 자동 업데이트 실행기 (Windows, onedir + 설치본 방식).

배포가 onedir(폴더형) + Inno Setup 설치본(HoneySetup-x.y.z.exe) 으로 바뀌면서,
업데이트는 "단일 exe 교체" 가 아니라 "새 설치본을 받아 조용히(/SILENT) 재설치"
한다. 설치본이 설치 폴더 전체(_internal 포함)를 교체하고, 설치 끝에 [Run] 항목으로
Honey 를 자동 재실행한다.

frozen(PyInstaller) 에서만 의미가 있다 (스크립트 실행 중엔 교체 대상이 없음).
"""
import subprocess
import sys
from pathlib import Path

# DETACHED_PROCESS(0x08) | CREATE_NEW_PROCESS_GROUP(0x200):
# 부모(이 앱)가 종료돼도 설치본이 계속 진행되도록.
_DETACHED = 0x00000008 | 0x00000200


def is_frozen() -> bool:
    """PyInstaller 등으로 패키징된 exe 로 실행 중인지."""
    return bool(getattr(sys, "frozen", False))


def run_installer(installer_path, silent=True):
    """다운로드한 HoneySetup.exe 를 실행. silent=True 면 /SILENT(진행바만 표시).

    detached 로 띄우므로 즉시 반환한다. 호출 측은 곧바로 앱을 종료해
    설치본이 파일 락 없이 폴더를 교체하게 해야 한다. 설치본의 [Run] 항목이
    설치 완료 후 Honey 를 자동 재실행한다.
    """
    installer_path = str(Path(installer_path).resolve())
    args = [installer_path]
    if silent:
        # /SILENT: 마법사 페이지 없이 "설치 중" 진행바 창만 표시
        # /SUPPRESSMSGBOXES: 확인창 자동 응답, /NOCANCEL: 설치 중 취소 비활성
        args += ["/SILENT", "/SUPPRESSMSGBOXES", "/NOCANCEL"]
    subprocess.Popen(args, creationflags=_DETACHED, close_fds=True)
