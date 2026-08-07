"""버전 폴더 + 런처 방식 자동 업데이트 (테스트 단계 — 운영 배선 전).

설치 트리:

    Honey\\                       <- install_root()
    ├── Honey.exe                 런처 (versions 안의 앱을 띄운다)
    ├── current.txt               1행 현재 버전, 2행(선택) 이전 버전
    ├── log\\                     실행/업데이트 로그 (버전과 무관하게 보존)
    ├── updates\\                 다운로드 zip 임시 보관
    └── versions\\<ver>\\HoneyApp.exe + _internal\\ + honey.env

업데이트는 **실행 중인 파일을 하나도 건드리지 않는다** — 새 버전을
versions\\<ver>.tmp-<pid> 에 풀고, 완성되면 versions\\<ver> 로 rename 한 뒤
current.txt 만 원자적으로 바꾸고 런처를 다시 띄운다. 구 batch 스왑 방식
(transport/updater.py)이 겪던 DLL 잠금·robocopy 실패·백신 차단이 구조적으로 없다.

이 모듈은 updater.py 를 대체할 목적이지만, 아직은 **병행 추가**다 — updater.py 와
기존 릴리스 파이프라인은 그대로 두고, honey_main 은 HONEY_UPDATE_TEST=1 일 때만
이쪽 경로를 쓴다.

함수 대부분이 root 를 인자로 받는다 — 빌드본 없이도 가짜 트리로 검증할 수 있게 하기
위해서다 (client/update_test/check_app_update.py).
"""
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

APP_EXE_NAME = "HoneyApp.exe"
LAUNCHER_EXE_NAME = "Honey.exe"
VERSIONS_DIRNAME = "versions"
UPDATES_DIRNAME = "updates"
CURRENT_FILENAME = "current.txt"

_LOG_MAX_BYTES = 1_000_000
_rotated = False


class InstallCancelled(Exception):
    """progress_cb 가 False 를 돌려줘 압축 해제를 중단했다."""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_root():
    """versioned 레이아웃이면 설치 루트, 아니면 None.

    None = 구 배치 레이아웃이거나 개발 실행 — 호출부는 기존 수동 ZIP 흐름을 쓴다.
    """
    if not is_frozen():
        return None
    exe = Path(sys.executable).resolve()
    if exe.name.lower() != APP_EXE_NAME.lower():
        return None
    version_dir = exe.parent
    if version_dir.parent.name.lower() != VERSIONS_DIRNAME:
        return None
    return version_dir.parent.parent


def is_versioned_layout() -> bool:
    return install_root() is not None


# ── 로그 ────────────────────────────────────────────────────────────────────
def log_dir() -> Path:
    """versioned 면 루트\\log (버전 폴더가 지워져도 남는다), 아니면 updater 와 같은 곳."""
    root = install_root()
    if root is not None:
        return root / "log"
    if is_frozen():
        return Path(sys.executable).resolve().parent / "log"
    return Path(__file__).resolve().parent.parent / "log"   # client/log


def log_path() -> Path:
    return log_dir() / "update.log"


def ulog(message: str) -> None:
    """업데이트 진단 한 줄 (best-effort). batch 가 없어져 인코딩은 utf-8 로 통일."""
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
        with open(target, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


# ── current.txt ─────────────────────────────────────────────────────────────
def read_current(root):
    """(현재 버전, 이전 버전|None). 파일이 없거나 비면 (None, None)."""
    # utf-8-sig: 사람이 메모장으로 고쳐 BOM 이 붙어도 첫 줄을 잃지 않게 (런처와 같은 규칙).
    try:
        text = (Path(root) / CURRENT_FILENAME).read_text(encoding="utf-8-sig")
    except OSError:
        return None, None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    cur = lines[0] if lines else None
    prev = lines[1] if len(lines) > 1 else None
    return cur, prev


def write_current(root, current, previous=None):
    """current.txt 를 원자적으로 교체 (임시 파일 + os.replace).

    중간에 프로세스가 죽어도 반쯤 쓰인 파일이 남지 않는다 — 런처가 이 파일 하나로
    어떤 버전을 띄울지 정하므로 손상되면 앱이 안 뜬다.
    """
    root = Path(root)
    body = current if not previous else f"{current}\n{previous}"
    tmp = root / f".{CURRENT_FILENAME}.tmp-{os.getpid()}"
    tmp.write_text(body + "\n", encoding="utf-8")
    os.replace(tmp, root / CURRENT_FILENAME)


# ── zip 해제 ────────────────────────────────────────────────────────────────
def zip_payload_prefix(zf) -> str:
    """zip 안에서 앱 폴더(HoneyApp.exe 가 있는 곳)의 엔트리 prefix.

    zip 루트가 'Honey/versions/<ver>/' 든 'HoneyApp/' 든 상관없이 앱 본체만 뽑아낼 수
    있게 한다 (구 _find_payload_dir 의 zip 판). 후보가 여럿이면 가장 얕은 것.
    """
    candidates = [n for n in zf.namelist()
                  if n.rsplit("/", 1)[-1].lower() == APP_EXE_NAME.lower()]
    if not candidates:
        raise RuntimeError(f"업데이트 zip 에 {APP_EXE_NAME} 이 없습니다")
    best = min(candidates, key=lambda n: (n.count("/"), len(n)))
    return best[: len(best) - len(best.rsplit("/", 1)[-1])]


def extract_payload(zip_path, dest_dir, progress_cb=None):
    """zip 의 앱 폴더만 dest_dir 로 푼다. 진행률은 실제 바이트 기준.

    progress_cb(done_bytes, total_bytes) 가 False 를 반환하면 InstallCancelled.
    zip-slip(경로 탈출) 엔트리는 거부한다.
    """
    dest_root = Path(dest_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        prefix = zip_payload_prefix(zf)
        members = [m for m in zf.infolist() if m.filename.startswith(prefix)]
        total = sum(m.file_size for m in members if not m.is_dir())
        done = 0
        for member in members:
            rel = member.filename[len(prefix):]
            if not rel:
                continue
            target = (dest_root / rel).resolve()
            if os.path.commonpath([str(dest_root), str(target)]) != str(dest_root):
                raise RuntimeError(f"unsafe path in update zip: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
                    done += len(chunk)
                    if progress_cb is not None and progress_cb(done, total) is False:
                        raise InstallCancelled()


# ── 설치 ────────────────────────────────────────────────────────────────────
def check_disk(root, need_bytes):
    """(여유공간 충분?, 여유 MB, 필요 MB). zip 크기 미상이면 4GB 를 기준으로 본다.

    zip + 압축 해제본 + 여유를 합쳐 zip 크기의 4배를 요구한다.
    """
    need = int(need_bytes) * 4 if need_bytes else 4 * 1024 ** 3
    try:
        free = shutil.disk_usage(str(root)).free
    except OSError:
        return True, 0, need // (1024 * 1024)   # 확인 실패는 막지 않는다
    return free >= need, free // (1024 * 1024), need // (1024 * 1024)


def install_version(root, version, zip_path, progress_cb=None):
    """새 버전을 versions\\<version> 에 설치하고 그 경로를 반환.

    tmp 폴더에 다 푼 뒤 rename 이라, 실패·취소하면 tmp 만 지우면 되고 완성된
    버전 폴더가 반쯤 만들어진 채로 남지 않는다.
    """
    root = Path(root)
    versions = root / VERSIONS_DIRNAME
    versions.mkdir(parents=True, exist_ok=True)
    final = versions / version
    tmp = versions / f"{version}.tmp-{os.getpid()}"

    running = Path(sys.executable).resolve().parent if is_frozen() else None
    if running is not None and final.resolve() == running:
        raise RuntimeError(f"실행 중인 버전({version})은 다시 설치할 수 없습니다")

    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    ulog(f"INSTALL extract -> {tmp}")
    try:
        extract_payload(zip_path, tmp, progress_cb)
        if not (tmp / APP_EXE_NAME).exists():
            raise RuntimeError(f"압축 해제 결과에 {APP_EXE_NAME} 이 없습니다")
        if final.exists():
            # 이전에 중단된 같은 버전 잔재 — 부분 폴더는 믿지 않고 통째로 다시 넣는다.
            ulog(f"INSTALL replace existing {final}")
            shutil.rmtree(final)
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    ulog(f"INSTALL done {final}")
    return final


def switch_and_relaunch(root, new_version, old_version=None):
    """current.txt 를 새 버전으로 바꾸고 런처를 다시 띄운다 (호출부가 곧바로 종료).

    런처는 --wait-pid 로 이 프로세스의 종료를 기다린 뒤 새 버전을 실행한다.
    """
    root = Path(root)
    launcher = root / LAUNCHER_EXE_NAME
    if not launcher.exists():
        raise RuntimeError(f"런처를 찾을 수 없습니다: {launcher}")
    write_current(root, new_version, old_version)
    ulog(f"SWITCH current={new_version} prev={old_version} -> relaunch {launcher}")
    subprocess.Popen(
        [str(launcher), "--wait-pid", str(os.getpid())],
        cwd=str(root),
        close_fds=True,
    )


# ── 정리 ────────────────────────────────────────────────────────────────────
def startup_cleanup(root, keep_versions=()):
    """앱이 정상 기동한 뒤 부르는 잔재 정리 (best-effort, 전부 무시 가능한 실패).

    - versions\\ 에서 keep_versions 외 폴더 삭제 (현재/이전 2개만 유지)
    - 중단된 *.tmp-* 폴더 삭제
    - updates\\ 의 zip 삭제 (설치 성공/실패 시 지우지만 놓친 것 수거)
    """
    root = Path(root)
    keep = {v for v in keep_versions if v}
    removed = []
    versions = root / VERSIONS_DIRNAME
    try:
        entries = list(versions.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name in keep:
            continue
        try:
            shutil.rmtree(entry)
            removed.append(entry.name)
        except OSError as exc:
            ulog(f"CLEANUP 실패(무시) {entry.name}: {exc}")
    try:
        for zip_file in (root / UPDATES_DIRNAME).glob("*.zip"):
            zip_file.unlink()
            removed.append(zip_file.name)
    except OSError as exc:
        ulog(f"CLEANUP updates 실패(무시): {exc}")
    if removed:
        ulog(f"CLEANUP removed={removed}")
    return removed


def sorted_versions(root):
    """versions\\ 아래 실행 가능한 버전들을 최신순으로. 런처와 같은 규칙."""
    out = []
    try:
        entries = list((Path(root) / VERSIONS_DIRNAME).iterdir())
    except OSError:
        return out
    for entry in entries:
        if not (entry / APP_EXE_NAME).exists():
            continue
        if not re.fullmatch(r"\d+(\.\d+)*", entry.name):
            continue
        out.append(entry.name)
    out.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return out
