"""버전 폴더 + 런처 방식 자동 업데이트 (테스트 단계 — 운영 배선 전).

설치 트리:

    Honey\\                       <- install_root()
    ├── Honey.exe                 런처 (versions 안의 앱을 띄운다)
    ├── current.txt               1행 현재 버전, 2행(선택) 이전 버전
    ├── log\\                     실행/업데이트 로그 (버전과 무관하게 보존)
    ├── updates\\                 다운로드 zip 임시 보관
    └── versions\\<ver>\\HoneyApp.exe + _internal\\ + honey.env

새 버전은 versions\\<ver> 에 직접 쓰되 `.installing`/`.ready` 작업 ID 가 일치하기
전에는 실행 후보로 보지 않는다. 설치가 중단되면 기존 버전으로 실행하고 다음 시도에서
같은 폴더를 복구한다. 실행 중인 Honey가 있으면 런처가 사용자 동의를 받아 먼저 종료한다.

이 모듈은 updater.py(구 batch 스왑)를 대체한다. 어느 경로를 쓸지는 환경변수가 아니라
**설치 구조**가 정한다 — install_root() 가 versioned 레이아웃을 인식하면 이쪽이다.
(HONEY_UPDATE_TEST 게이트는 2026-08-12 제거됐다.)

**기존 버전 폴더를 통째로 지우거나 rename 하지 않는다** (2026-08-26). 완성돼 있으면
채택(adopt)하고, 깨진 잔재면 그 자리에서 누락·손상 파일을 다시 채운다. 구버전은 새 버전이
검증되고 current.txt 가 전환될 때까지 전혀 건드리지 않는다.

함수 대부분이 root 를 인자로 받는다 — 빌드본 없이도 가짜 트리로 검증할 수 있게 하기
위해서다 (client/update_test/check_app_update.py).
"""
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

APP_EXE_NAME = "HoneyApp.exe"
LAUNCHER_EXE_NAME = "Honey.exe"
VERSIONS_DIRNAME = "versions"
UPDATES_DIRNAME = "updates"
CURRENT_FILENAME = "current.txt"
MANIFEST_FILENAME = ".files.json"     # 버전 폴더 안의 파일 목록 캐시 (델타 판정용)
INSTALLING_FILENAME = ".installing"   # 직접 설치 작업 ID (ready 와 일치하기 전엔 실행 금지)
READY_FILENAME = ".ready"             # 전체 검증이 끝난 직접 설치 작업 ID
FAILCOUNT_FILENAME = ".update_fail"   # "<버전> <연속 실패 횟수> <런처 빌드>"
ELEVATED_RESULT_FILENAME = ".elevated_result.json"   # 승격 프로세스 → 부모 진단 채널

_NET_TIMEOUT = 5        # 서버 질의 — 앱 기동을 붙잡으면 안 되므로 짧게
_DOWNLOAD_TIMEOUT = 60  # 데이터 수신 — 큰 파일 대비
_DOWNLOAD_RETRIES = 2   # 네트워크 오류만 재시도 (취소·해시 불일치는 즉시 포기)

_LOG_MAX_BYTES = 1_000_000
_rotated = False


class InstallCancelled(Exception):
    """progress_cb 가 False 를 돌려줘 압축 해제를 중단했다."""


class DownloadCancelled(Exception):
    """progress_cb 가 False 를 돌려줘 다운로드를 중단했다 (런처 진행창의 취소)."""


class LocalWriteError(Exception):
    """설치 폴더의 파일 조작(생성·쓰기·교체)이 권한/잠금으로 실패했다.

    **네트워크 실패와 반드시 구분해야 한다.** 이것이 나면 전체 zip 을 다시 받아도
    같은 자리에서 또 실패하므로(2026-08-26 현장: 델타 실패 → 331MB 재다운로드 →
    같은 rename 에서 재실패), 호출부는 폴백하지 말고 권한 상승 경로로 가야 한다.

    path 는 실제로 막힌 경로다 — 사용자에게 그대로 보여줘야 조치할 수 있다.
    """

    def __init__(self, message, path=None, winerror=None, readonly=None):
        super().__init__(message)
        self.path = str(path) if path else ""
        self.winerror = winerror
        self.readonly = readonly

    def details(self):
        """report_failure context / 실패창에 넣을 진단 조각."""
        out = {"failing_path": self.path}
        if self.winerror is not None:
            out["winerror"] = self.winerror
        if self.readonly is not None:
            out["readonly"] = "1" if self.readonly else "0"
        return out


def _is_readonly(path) -> bool:
    try:
        return not (os.stat(path).st_mode & stat.S_IWRITE)
    except OSError:
        return False


def _local_error(message, path, exc=None):
    """OSError 를 LocalWriteError 로 승격 (실패 경로·winerror·읽기전용 여부 첨부)."""
    winerror = getattr(exc, "winerror", None)
    detail = f"{message}: {path}"
    if exc is not None:
        detail = f"{detail} ({type(exc).__name__}: {exc})"
    return LocalWriteError(detail, path=path, winerror=winerror,
                           readonly=_is_readonly(path))


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


def write_current(root, current, previous=None, attempts=5, delay=0.5):
    """current.txt 를 원자적으로 교체 (임시 파일 + os.replace).

    중간에 프로세스가 죽어도 반쯤 쓰인 파일이 남지 않는다 — 런처가 이 파일 하나로
    어떤 버전을 띄울지 정하므로 손상되면 앱이 안 뜬다.
    """
    root = Path(root)
    body = current if not previous else f"{current}\n{previous}"
    tmp = root / f".{CURRENT_FILENAME}.tmp-{os.getpid()}"
    target = root / CURRENT_FILENAME
    try:
        tmp.write_text(body + "\n", encoding="utf-8")
    except OSError as exc:
        raise _local_error("버전 전환 파일을 쓸 수 없습니다", tmp, exc) from exc
    for attempt in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            if attempt + 1 >= attempts:
                raise _local_error("현재 버전을 전환할 수 없습니다", target, exc) from exc
            ulog(f"CURRENT replace 재시도 {attempt + 1}/{attempts} ({exc})")
            time.sleep(delay)


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


# ── 삭제 / 대상 폴더 준비 ────────────────────────────────────────────────────
def _rmtree_robust(path, attempts=3, delay=0.5) -> bool:
    """읽기전용 해제 + 제한 재시도로 지운다. 끝내 못 지우면 False (예외 아님).

    쓰는 쪽이 "치워두고 나중에 지운다" 전략이라 삭제 실패가 치명적이지 않다 —
    실패를 예외로 올리면 업데이트가 멈추는데, 정작 새 버전 설치에는 지장이 없다.
    백신 실시간 감시가 갓 만든 파일을 잠깐 잡고 있는 경우가 흔해 재시도를 둔다.
    """
    path = Path(path)

    def _retry(func, target, _exc):
        # 읽기전용 속성은 관리자 권한과 무관하게 삭제를 막는다 — 풀고 한 번 더.
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    for attempt in range(attempts):
        if not path.exists():
            return True
        try:
            # onexc 는 3.12+, onerror 는 그 이전 — 런처가 어느 파이썬으로 빌드되든 돌아야 한다.
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_retry)
            else:
                shutil.rmtree(path, onerror=lambda f, t, _i: _retry(f, t, None))
        except OSError as exc:
            ulog(f"RMTREE 실패({attempt + 1}/{attempts}) {path}: {exc}")
        if not path.exists():
            return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    return not path.exists()


def _read_install_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _release_key(version, release_id=None):
    return str(release_id or f"version:{version}")


def version_ready(version_dir, version=None, release_id=None) -> bool:
    """직접 설치 폴더의 작업이 끝났는가. 마커가 없는 구 릴리스는 호환상 True."""
    version_dir = Path(version_dir)
    installing_path = version_dir / INSTALLING_FILENAME
    if not installing_path.exists():
        return True
    installing = _read_install_state(installing_path)
    ready = _read_install_state(version_dir / READY_FILENAME)
    if not installing or not ready:
        return False
    expected_version = str(version or version_dir.name)
    expected_release = str(release_id) if release_id else None
    return (installing.get("install_id")
            and installing.get("install_id") == ready.get("install_id")
            and str(installing.get("version")) == expected_version
            and str(ready.get("version")) == expected_version
            and str(installing.get("release")) == str(ready.get("release"))
            and (expected_release is None
                 or str(installing.get("release")) == expected_release))


def _write_install_state(path, payload, message):
    path = Path(path)
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        raise _local_error(message, path, exc) from exc


def _begin_direct_install(final, version, release_id=None):
    final = Path(final)
    try:
        final.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _local_error("새 버전 폴더를 만들 수 없습니다", final, exc) from exc
    state = {"schema": 1, "install_id": uuid.uuid4().hex,
             "version": str(version), "release": _release_key(version, release_id)}
    _write_install_state(final / INSTALLING_FILENAME, state,
                         "설치 상태 파일을 쓸 수 없습니다")
    return state


def _manifest_key(files):
    return sorted((f.get("p"), f.get("h")) for f in files)


def _payload_valid(version_dir, remote_files=None, full_hashes=False) -> bool:
    version_dir = Path(version_dir)
    if not (version_dir / APP_EXE_NAME).is_file():
        return False
    local_files = read_file_manifest(version_dir)
    if not local_files:
        return False
    if remote_files is not None and _manifest_key(local_files) != _manifest_key(remote_files):
        return False
    for entry in local_files:
        rel, size, expected_hash = entry.get("p"), entry.get("s"), entry.get("h")
        if not rel:
            return False
        try:
            target = _safe_target(version_dir, rel)
            if size is not None and target.stat().st_size != int(size):
                return False
            if expected_hash and (full_hashes or rel.lower() == APP_EXE_NAME.lower()):
                if sha256_file(target) != expected_hash:
                    return False
        except (OSError, ValueError):
            return False
    return True


def verify_version_dir(version_dir, remote_files=None, version=None,
                       release_id=None, full_hashes=False) -> bool:
    """실행 가능한 완료 폴더인가. 설치 완료 시에는 전 파일 해시까지 확인한다."""
    version_dir = Path(version_dir)
    return (version_ready(version_dir, version, release_id)
            and _payload_valid(version_dir, remote_files, full_hashes))


def _finish_direct_install(final, state, remote_files=None):
    final = Path(final)
    files = remote_files or read_file_manifest(final)
    if not files:
        files = build_file_manifest(final)
    if not files:
        raise RuntimeError(f"설치 결과 파일 매니페스트가 비었습니다: {final}")
    write_file_manifest(final, files)
    if not _payload_valid(final, files, full_hashes=True):
        raise RuntimeError(f"새 버전 전체 파일 검증에 실패했습니다: {final}")
    ready = dict(state)
    ready["verified_at"] = datetime.now().isoformat(timespec="seconds")
    _write_install_state(final / READY_FILENAME, ready,
                         "설치 완료 상태를 기록할 수 없습니다")
    if not version_ready(final, state["version"], state["release"]):
        raise RuntimeError(f"새 버전 완료 상태를 확인할 수 없습니다: {final}")
    return final


def prepare_target(root, version, remote_files=None, release_id=None) -> str:
    """설치 전 대상 확인. 반환 'adopted'|'repair'|'absent'.

    완성된 폴더는 채택하고, 불완전한 폴더는 삭제/rename 없이 같은 자리에서 복구한다.
    """
    root = Path(root)
    versions = root / VERSIONS_DIRNAME
    try:
        versions.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _local_error("versions 폴더를 만들 수 없습니다", versions, exc) from exc

    # versions\ 아래에 실제로 쓸 수 있는지 — can_write(root) 는 루트만 보므로
    # 하위 폴더에만 걸린 ACL 을 못 잡는다 (현장에서 통과시켜 놓고 뒤에서 터졌다).
    probe = versions / f".wtest-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        probe.mkdir()
    except OSError as exc:
        raise _local_error("versions 폴더에 쓸 수 없습니다", versions, exc) from exc
    finally:
        try:
            probe.rmdir()
        except OSError:
            pass

    final = versions / version
    if not final.exists():
        return "absent"

    if verify_version_dir(final, remote_files, version, release_id):
        ulog(f"PREPARE adopt {final} (완성된 폴더 — 재설치하지 않는다)")
        return "adopted"
    ulog(f"PREPARE repair {final} (불완전 폴더 — 제자리 복구)")
    return "repair"


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


def _guard_running_version(final, version):
    """실행 중인 버전 폴더는 건드리지 않는다 (전체·델타 양쪽 공통)."""
    running = Path(sys.executable).resolve().parent if is_frozen() else None
    if running is not None and final.resolve() == running:
        raise RuntimeError(f"실행 중인 버전({version})은 다시 설치할 수 없습니다")


def install_version(root, version, zip_path, progress_cb=None,
                    remote_files=None, release_id=None):
    """새 버전을 versions\\<version> 에 설치하고 그 경로를 반환.

    최종 폴더에 직접 풀고 완료 마커는 전체 해시 검증 뒤에만 기록한다. 중간 실패 폴더는
    실행되지 않으며 다음 시도에서 같은 자리에 다시 풀어 복구한다.
    """
    root = Path(root)
    versions = root / VERSIONS_DIRNAME
    versions.mkdir(parents=True, exist_ok=True)
    final = versions / version
    _guard_running_version(final, version)
    state = _begin_direct_install(final, version, release_id)
    ulog(f"INSTALL direct extract -> {final} install_id={state['install_id'][:8]}")
    try:
        extract_payload(zip_path, final, progress_cb)
        if not (final / APP_EXE_NAME).exists():
            raise RuntimeError(f"압축 해제 결과에 {APP_EXE_NAME} 이 없습니다")
        final = _finish_direct_install(final, state, remote_files)
    except (InstallCancelled, DownloadCancelled, LocalWriteError):
        raise
    except OSError as exc:
        raise _local_error("새 버전 파일을 쓸 수 없습니다", final, exc) from exc
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


def relaunch_via_launcher(root):
    """앱의 [지금 업데이트] — 런처를 업데이트 확인 모드로 띄운다 (호출부가 곧바로 종료).

    **current.txt 를 건드리지 않는다** (switch_and_relaunch 와 다른 점). 아직 아무것도
    설치하지 않았으므로 포인터를 바꿀 이유가 없고, 바꾸면 없는 버전을 가리키게 된다.

    왜 필요한가: 버전 폴더 레이아웃에서 설치는 런처만 한다. 그런데 사용자가 실행 중인
    앱을 작업표시줄에 고정하면 Windows 는 런처(Honey.exe)가 아니라 HoneyApp.exe 를
    고정해, 그 뒤로 런처가 한 번도 안 돌아 업데이트가 영영 멈춘다(2026-09-03 현장).
    이 함수가 그 PC 의 복귀 경로다.
    """
    root = Path(root)
    launcher = root / LAUNCHER_EXE_NAME
    if not launcher.exists():
        raise RuntimeError(f"런처를 찾을 수 없습니다: {launcher}")
    ulog(f"FORCE UPDATE -> relaunch {launcher}")
    subprocess.Popen(
        [str(launcher), "--wait-pid", str(os.getpid()), "--force-update"],
        cwd=str(root),
        close_fds=True,
    )


# ── 정리 ────────────────────────────────────────────────────────────────────
def startup_cleanup(root, keep_versions=()):
    """앱이 정상 기동한 뒤 부르는 잔재 정리 (best-effort, 전부 무시 가능한 실패).

    - versions\\ 에서 keep_versions 외 폴더 삭제 (현재/이전 2개만 유지)
    - 중단된 *.tmp-* / 치워 둔 *.old-* 폴더 삭제
    - updates\\ 의 zip 삭제 (설치 성공/실패 시 지우지만 놓친 것 수거)

    보호 폴더(Program Files 등)에서는 일반 권한으로 지워지지 않는다 — 그때는 조용히
    남겨 두고, 다음 승격 업데이트(launcher --elevated-update)가 같은 함수를 관리자
    권한으로 다시 돌려 수거한다.
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
        # 직접 설치가 중단된 폴더는 다음 업데이트에서 제자리 복구한다. 완료 마커가
        # 일치하지 않는 동안에는 실행 후보가 아니므로 남겨 둬도 현재 앱에는 영향이 없다.
        if (entry / INSTALLING_FILENAME).exists() and not version_ready(entry):
            ulog(f"CLEANUP preserve repairable={entry.name}")
            continue
        if _rmtree_robust(entry):
            removed.append(entry.name)
        else:
            ulog(f"CLEANUP 실패(무시) {entry.name}")
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
        if not (entry / APP_EXE_NAME).exists() or not version_ready(entry):
            continue
        if not re.fullmatch(r"\d+(\.\d+)*", entry.name):
            continue
        out.append(entry.name)
    out.sort(key=lambda v: tuple(int(x) for x in v.split(".")), reverse=True)
    return out


# ════════════════════════════════════════════════════════════════════════════
# 아래는 **런처(launcher.py)가 앱을 띄우기 전에** 업데이트를 끝내기 위한 것들이다.
# 런처는 PyQt6·requests 를 넣을 수 없으므로(넣으면 수백 MB) 전부 표준 라이브러리로만
# 쓴다. 앱 쪽 경로(version_check.requests)는 그대로 두고 여기서 사본을 만들지 않는다.
# ════════════════════════════════════════════════════════════════════════════

# ── 버전 비교 ───────────────────────────────────────────────────────────────
def is_newer(remote, local) -> bool:
    """a.b.c 비교. version_check.is_newer 가 이 함수를 재노출한다 (사본 금지)."""
    if not remote or not local:
        return False
    try:
        ra = tuple(int(x) for x in str(remote).split("."))
        la = tuple(int(x) for x in str(local).split("."))
    except ValueError:
        return remote != local
    return ra > la


# ── 설치 위치 쓰기 권한 ──────────────────────────────────────────────────────
def can_write(root) -> bool:
    """설치 루트에 실제로 써 본다 (업데이트가 여기에 폴더를 만든다).

    Program Files 처럼 권한이 필요한 곳에 설치된 PC 가 331MB 를 다 받고 나서야
    실패하는 것을 막는다 — 받기 전에 판정하려고 존재한다.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=str(root), prefix=".wtest-"):
            return True
    except OSError:
        return False


# ── 서버 주소 ───────────────────────────────────────────────────────────────
def read_server_url(root, version=None, default=None):
    """런처용 서버 주소. HONEY_SERVER_URL > versions\\<ver>\\honey.env > config 기본값.

    transport/config.py 를 그대로 쓸 수 없는 이유: 그쪽은 frozen 일 때 **exe 옆**
    honey.env 를 보는데, 런처 기준으로는 그게 설치 루트라 실제 위치와 어긋난다.
    """
    env_url = os.environ.get("HONEY_SERVER_URL")
    if env_url:
        return env_url.rstrip("/")
    root = Path(root)
    candidates = []
    if version:
        candidates.append(root / VERSIONS_DIRNAME / version / "honey.env")
    candidates.append(root / "honey.env")
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "SERVER_BASE_URL" and value.strip():
                return value.strip().rstrip("/")
    if default is None:
        try:
            from . import config
            default = config.SERVER_BASE_URL
        except Exception:
            default = ""
    return str(default).rstrip("/")


# ── 서버 통신 (urllib) ───────────────────────────────────────────────────────
def _honey_headers():
    """신원 토큰 UA — version_check._honey_headers 와 같은 규칙.

    서버가 "누구의 PC 에서 실패했는지" 귀속하려면 이게 있어야 한다.
    """
    try:
        import client_identity
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    agent = "python-urllib"
    if user:
        agent = f"{agent} HoneyUser/{urllib.parse.quote(user, safe='')}"
    return {"User-Agent": agent}


def _open(url, timeout, data=None, extra_headers=None):
    headers = _honey_headers()
    if extra_headers:
        headers.update(extra_headers)
    return urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers), timeout=timeout)


def fetch_json(url, timeout=_NET_TIMEOUT, extra_headers=None):
    with _open(url, timeout, extra_headers=extra_headers) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def fetch_manifest(base_url, timeout=_NET_TIMEOUT):
    """/honey/version. probe=1 = 실행 집계 제외 — 집계(honey_run)는 앱이 계속 담당한다.

    no-cache 를 붙이는 이유: 사내 프록시가 낡은 응답을 돌려주면 그 PC 만 옛 버전을
    보고 "이미 최신" 으로 판정한다 — 에러가 아니라 업데이트가 조용히 멈춘다.
    """
    return fetch_json(f"{base_url.rstrip('/')}/honey/version?probe=1", timeout,
                      extra_headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})


def fetch_file_manifest(base_url, version, timeout=_NET_TIMEOUT):
    """/honey/files/<ver>. 서버가 안 주면 예외 → 호출부가 전체 zip 으로 폴백한다."""
    url = f"{base_url.rstrip('/')}/honey/files/{urllib.parse.quote(str(version))}"
    data = fetch_json(url, timeout)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files:
        raise RuntimeError("파일 매니페스트가 비어 있습니다")
    return files


def download(url, dest, expected_sha256=None, progress_cb=None,
             timeout=_DOWNLOAD_TIMEOUT, retries=_DOWNLOAD_RETRIES):
    """스트리밍 다운로드 + sha256 검증. progress_cb(done,total) 가 False 면 취소.

    끊긴 연결·타임아웃만 재시도한다 — 취소는 사용자 의사이고, sha256 불일치는
    서버 파일이 잘못됐다는 뜻이라 다시 받아도 같은 결과다.
    """
    last = None
    for attempt in range(max(1, retries + 1)):
        try:
            return _download_once(url, dest, expected_sha256, progress_cb, timeout)
        except (urllib.error.URLError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                raise            # 404 등은 재시도해도 같다 (전체 zip 폴백이 맞는 상황)
            last = exc
            if attempt + 1 >= max(1, retries + 1):
                break
            wait = 1 + 2 * attempt
            ulog(f"DOWNLOAD 재시도 {attempt + 1} ({type(exc).__name__}: {exc}) — {wait}s 후")
            time.sleep(wait)
    raise last


def _download_once(url, dest, expected_sha256=None, progress_cb=None,
                   timeout=_DOWNLOAD_TIMEOUT):
    """단발 다운로드. 실패·취소 시 **받다 만 파일을 반드시 지운다** — 남겨두면
    다음 실행이 그것을 온전한 것으로 착각할 여지가 생긴다.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with _open(url, timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with dest.open("wb") as fh:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress_cb is not None and progress_cb(done, total) is False:
                        raise DownloadCancelled()
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    if expected_sha256 and digest.hexdigest().lower() != str(expected_sha256).lower():
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 불일치: {dest.name}")
    return dest


# ── 파일 매니페스트 / 델타 ───────────────────────────────────────────────────
def sha256_file(path, chunk=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def build_file_manifest(app_dir):
    """앱 폴더를 훑어 [{p,s,h}, ...] 를 만든다 (릴리스 빌드가 쓰는 형식과 동일)."""
    app_dir = Path(app_dir)
    files = []
    for path in sorted(app_dir.rglob("*")):
        if (not path.is_file()
                or path.name in {MANIFEST_FILENAME, INSTALLING_FILENAME, READY_FILENAME}):
            continue
        files.append({"p": path.relative_to(app_dir).as_posix(),
                      "s": path.stat().st_size,
                      "h": sha256_file(path)})
    return files


def read_file_manifest(version_dir):
    """버전 폴더에 저장해 둔 파일 목록. 없거나 깨졌으면 None (→ 전체 zip 폴백).

    이 캐시가 델타의 핵심이다 — 없으면 752MB 를 다시 해싱해야 하는데 그건 수십 초라
    업데이트를 빠르게 만들려는 목적과 정면으로 어긋난다.
    """
    try:
        data = json.loads((Path(version_dir) / MANIFEST_FILENAME)
                          .read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, list) and files else None


def write_file_manifest(app_dir, files):
    (Path(app_dir) / MANIFEST_FILENAME).write_text(
        json.dumps({"files": files}, ensure_ascii=False), encoding="utf-8")


def _safe_target(base, rel):
    """매니페스트의 경로는 서버가 준 값이다 — zip-slip 과 같은 방어를 건다."""
    base_dir = Path(base).resolve()
    target = (base_dir / rel).resolve()
    if os.path.commonpath([str(base_dir), str(target)]) != str(base_dir):
        raise RuntimeError(f"unsafe path in manifest: {rel}")
    return target


def plan_delta(remote_files, source_dir, local_files):
    """(재사용 [(entry, 로컬상대경로)], 받을 [entry]).

    해시로 맞추므로 파일이 폴더를 옮겨도 재사용된다. 캐시에 있다고 적혀 있어도 실제
    파일이 없으면 받는 쪽으로 보낸다.
    """
    have = {}
    for entry in (local_files or []):
        key = entry.get("h")
        if key and key not in have:
            have[key] = entry.get("p")
    reuse, fetch = [], []
    source_dir = Path(source_dir)
    for entry in remote_files:
        src_rel = have.get(entry.get("h"))
        if src_rel and (source_dir / src_rel).is_file():
            reuse.append((entry, src_rel))
        else:
            fetch.append(entry)
    return reuse, fetch


def copy_reusable(src, dst):
    """구버전 파일을 새 버전에 독립 복사한다.

    직접 설치 폴더는 실패 뒤 같은 파일을 덮어써 복구한다. 하드링크를 쓰면 그 덮어쓰기가
    정상 구버전까지 바꿀 수 있으므로 롤백 버전과 파일 실체를 절대 공유하지 않는다.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return "copy"


def _entry_valid(path, entry) -> bool:
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size != int(entry.get("s") or 0):
            return False
        expected = entry.get("h")
        return not expected or sha256_file(path) == expected
    except (OSError, ValueError, TypeError):
        return False


def install_delta(root, version, base_url, remote_files, source_dir,
                  local_files=None, progress_cb=None, release_id=None):
    """변경분만 받아 versions\\<version> 을 조립한다. 반환 (설치 경로, 통계).

    최종 폴더에 직접 조립한다. 완료 전에는 상태 마커가 일치하지 않아 실행되지 않고,
    다음 시도는 이미 정상인 파일을 건너뛰고 나머지만 복구한다.
    """
    root = Path(root)
    versions = root / VERSIONS_DIRNAME
    versions.mkdir(parents=True, exist_ok=True)
    final = versions / version
    _guard_running_version(final, version)   # 전체 zip 경로와 같은 가드 (2026-08-26 대칭화)
    reuse, fetch = plan_delta(remote_files, source_dir, local_files)
    fetch_bytes = sum(int(e.get("s") or 0) for e in fetch)
    stats = {"reuse": len(reuse), "fetch": len(fetch), "bytes": fetch_bytes,
             "linked": 0, "copied": 0, "recovered": 0}
    ulog(f"DELTA plan reuse={len(reuse)} fetch={len(fetch)} "
         f"bytes={fetch_bytes / 1024 / 1024:.1f}MB")
    state = _begin_direct_install(final, version, release_id)
    try:
        for entry, src_rel in reuse:
            target = _safe_target(final, entry["p"])
            if _entry_valid(target, entry):
                stats["recovered"] += 1
                continue
            copy_reusable(Path(source_dir) / src_rel, target)
            stats["copied"] += 1
        done = 0
        for entry in fetch:
            target = _safe_target(final, entry["p"])
            if _entry_valid(target, entry):
                stats["recovered"] += 1
                done += int(entry.get("s") or 0)
                continue
            url = (f"{base_url.rstrip('/')}/honey/file/{urllib.parse.quote(str(version))}"
                   f"?path={urllib.parse.quote(entry['p'])}")

            def _cb(chunk_done, _total, _base=done):
                if progress_cb is None:
                    return None
                return progress_cb(_base + chunk_done, fetch_bytes)

            download(url, target, entry.get("h"), _cb)
            done += int(entry.get("s") or 0)
        if not (final / APP_EXE_NAME).exists():
            raise RuntimeError(f"조립 결과에 {APP_EXE_NAME} 이 없습니다")
        write_file_manifest(final, remote_files)
        final = _finish_direct_install(final, state, remote_files)
    except (DownloadCancelled, InstallCancelled, LocalWriteError):
        raise
    except OSError as exc:
        raise _local_error("새 버전 파일을 쓸 수 없습니다", final, exc) from exc
    ulog(f"DELTA done {final} linked={stats['linked']} copied={stats['copied']} "
         f"fetched={stats['fetch']} recovered={stats['recovered']}")
    return final, stats


# ── 실패 보고 (관리자 🚨 진단 사건 탭) ────────────────────────────────────────
def report_failure(base_url, message, context=None, version=""):
    """업데이트 실패를 서버에 남긴다 — 완전 무음, 실패해도 앱 실행에 영향 없음.

    payload 규약은 error_report.report_error 와 같다. 서버는 별도 수정 없이
    POST /pe/report/api/client_diagnostic 로 받아 감사 로그 + 진단 사건에 남긴다.
    """
    event_id = uuid.uuid4().hex[:12]
    try:
        payload = {"event_id": event_id, "kind": "update_failed",
                   "message": str(message)[:500], "version": str(version or ""),
                   "mode": "minimal"}
        if context:
            payload["context"] = {k: str(v)[:200] for k, v in context.items()}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{base_url.rstrip('/')}/pe/report/api/client_diagnostic"
        with _open(url, 3, data=data,
                   extra_headers={"Content-Type": "application/json"}):
            pass
    except Exception:
        pass
    return event_id


# ── 반복 실패 방지 ──────────────────────────────────────────────────────────
# 파일 포맷은 "<버전> <횟수> <런처빌드>" 3토큰이다. 2토큰(구 포맷)이거나 빌드가 다르면
# 0 으로 본다 — **런처를 고쳐 배포하면 과거 실패 기록이 자동으로 풀린다**. 이게 없으면
# 3회 실패로 포기 상태가 된 PC 는 고친 런처를 받아도 영영 재시도하지 않는다.
# 빈 빌드값도 반드시 토큰 하나를 차지해야 한다 — 공백으로 쓰면 split 이 2토큰으로
# 읽어 카운터가 매번 0 으로 리셋되고, 반복 실패 방지 자체가 무력해진다.
_BUILD_NONE = "-"


def read_fail_count(root, version, launcher_build=""):
    """그 버전의 연속 실패 횟수. 다른 버전·다른 런처 빌드의 기록이면 0."""
    try:
        text = (Path(root) / FAILCOUNT_FILENAME).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return 0
    parts = text.split()
    if len(parts) != 3 or parts[0] != str(version):
        return 0
    if parts[2] != (str(launcher_build) or _BUILD_NONE):
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def bump_fail_count(root, version, launcher_build=""):
    count = read_fail_count(root, version, launcher_build) + 1
    try:
        (Path(root) / FAILCOUNT_FILENAME).write_text(
            f"{version} {count} {str(launcher_build) or _BUILD_NONE}\n", encoding="utf-8")
    except OSError:
        pass
    return count


def clear_fail_count(root):
    try:
        (Path(root) / FAILCOUNT_FILENAME).unlink()
    except OSError:
        pass


# ── 권한 상승 (ACL 로 막힌 PC 전용 보조 경로) ────────────────────────────────
# 평소에는 쓰이지 않는다. prepare_target 이 LocalWriteError 를 냈을 때만 런처가
# 사용자 동의를 받아 자기 자신을 한 번 승격 실행한다. 승격 프로세스는 **업데이트만**
# 하고 끝나며, 앱(HoneyApp.exe)은 언제나 일반 권한으로 실행된다.
ERROR_CANCELLED = 1223          # UAC 창에서 '아니오' — 실패가 아니라 사용자 선택이다
ERROR_ALREADY_EXISTS = 183


def acquire_update_mutex(root):
    """설치 루트별 mutex. 이미 다른 프로세스가 쥐고 있으면 None.

    같은 폴더를 두 프로세스가 동시에 업데이트하면 서로의 파일 쓰기와 상태 마커를 밟는다.
    반환된 핸들은 프로세스가 살아 있는 동안 들고 있어야 한다(GC 되면 풀린다).
    """
    try:
        key = hashlib.sha1(str(Path(root).resolve()).lower().encode("utf-8")).hexdigest()[:16]
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, True, f"Local\\HoneyUpd_{key}")
        if not handle:
            return None
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return None
        return handle
    except Exception:   # noqa: BLE001 - mutex 를 못 만들어도 업데이트는 진행돼야 한다
        return None


def release_update_mutex(handle):
    if not handle:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.ReleaseMutex(ctypes.c_void_p(handle))
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        pass


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:   # noqa: BLE001
        return False


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.c_void_p),
                ("lpVerb", ctypes.c_wchar_p),
                ("lpFile", ctypes.c_wchar_p),
                ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p),
                ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_wchar_p),
                ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", ctypes.c_ulong),
                ("hIcon", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p)]


def run_elevated(exe, args, cwd=None, timeout_sec=900):
    """UAC 로 exe 를 실행하고 끝날 때까지 기다린다. 반환 ('ok'|'cancelled'|'error', 정보).

    subprocess 로는 승격할 수 없다 — ShellExecuteEx 의 'runas' 동사만 UAC 를 띄운다.
    """
    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = str(exe)
    info.lpParameters = subprocess.list2cmdline([str(a) for a in args])
    info.lpDirectory = str(cwd) if cwd else None
    info.nShow = 1   # SW_SHOWNORMAL — 승격 프로세스의 진행창이 보여야 한다
    try:
        if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
            code = ctypes.windll.kernel32.GetLastError()
            if code == ERROR_CANCELLED:
                return "cancelled", "사용자가 관리자 권한 요청을 취소했습니다"
            return "error", f"권한 상승 실패 (code={code})"
    except Exception as exc:   # noqa: BLE001
        return "error", f"권한 상승 실패 ({type(exc).__name__}: {exc})"

    handle = info.hProcess
    if not handle:
        return "error", "권한 상승 프로세스 핸들을 얻지 못했습니다"
    try:
        kernel32 = ctypes.windll.kernel32
        waited = kernel32.WaitForSingleObject(ctypes.c_void_p(handle),
                                              int(timeout_sec * 1000))
        if waited != 0:
            return "error", f"업데이트가 {int(timeout_sec)}초 안에 끝나지 않았습니다"
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        return ("ok" if code.value == 0 else "error"), f"exit={code.value}"
    finally:
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        except Exception:
            pass


def normalize_acl(root) -> bool:
    """설치 루트에 Users 수정 권한을 상속으로 부여 (승격 상태에서만 의미 있다).

    **설치보다 먼저** 불러야 한다 — 나중에 부르면 승격 프로세스가 만든 새 폴더가
    관리자 소유 그대로 남아 다음 업데이트가 또 막힌다. 이 한 번으로 그 PC 는
    이후 UAC 없이 업데이트된다(= 근본 해결).

    batch 를 만들지 않고 직접 실행한다 — .bat 은 cp949 인코딩 함정이 있고 여기서는
    필요가 없다.
    """
    try:
        # /Q 가 없으면 icacls 가 파일마다 "처리된 파일:" 을 콘솔에 직접 써서
        # (capture_output 으로도 안 막힌다) 승격 창 뒤에 검은 창이 번쩍인다.
        completed = subprocess.run(
            ["icacls", str(root), "/grant", "*S-1-5-32-545:(OI)(CI)M", "/T", "/C", "/Q"],
            capture_output=True, text=True, timeout=300,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = completed.returncode == 0
        output = f"{completed.stdout or ''} {completed.stderr or ''}".strip()
        ulog(f"ACL normalize rc={completed.returncode} {output[-400:]}")
        return ok
    except Exception as exc:   # noqa: BLE001 - 호출부가 실제 쓰기로 한 번 더 판정한다
        ulog(f"ACL normalize 실패: {type(exc).__name__}: {exc}")
        return False


def write_elevated_result(root, payload):
    """승격 프로세스 → 부모 진단 채널 (성공 판정은 current.txt 가 정본)."""
    try:
        target = Path(root) / UPDATES_DIRNAME
        target.mkdir(parents=True, exist_ok=True)
        (target / ELEVATED_RESULT_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def read_elevated_result(root):
    try:
        return json.loads((Path(root) / UPDATES_DIRNAME / ELEVATED_RESULT_FILENAME)
                          .read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def clear_elevated_result(root):
    try:
        (Path(root) / UPDATES_DIRNAME / ELEVATED_RESULT_FILENAME).unlink()
    except OSError:
        pass
