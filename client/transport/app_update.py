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
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
FAILCOUNT_FILENAME = ".update_fail"   # "<버전> <연속 실패 횟수>"

_NET_TIMEOUT = 5        # 서버 질의 — 앱 기동을 붙잡으면 안 되므로 짧게
_DOWNLOAD_TIMEOUT = 60  # 데이터 수신 — 큰 파일 대비

_LOG_MAX_BYTES = 1_000_000
_rotated = False


class InstallCancelled(Exception):
    """progress_cb 가 False 를 돌려줘 압축 해제를 중단했다."""


class DownloadCancelled(Exception):
    """progress_cb 가 False 를 돌려줘 다운로드를 중단했다 (런처 진행창의 취소)."""


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


def fetch_json(url, timeout=_NET_TIMEOUT):
    with _open(url, timeout) as resp:
        return json.loads(resp.read().decode("utf-8-sig"))


def fetch_manifest(base_url, timeout=_NET_TIMEOUT):
    """/honey/version. probe=1 = 실행 집계 제외 — 집계(honey_run)는 앱이 계속 담당한다."""
    return fetch_json(f"{base_url.rstrip('/')}/honey/version?probe=1", timeout)


def fetch_file_manifest(base_url, version, timeout=_NET_TIMEOUT):
    """/honey/files/<ver>. 서버가 안 주면 예외 → 호출부가 전체 zip 으로 폴백한다."""
    url = f"{base_url.rstrip('/')}/honey/files/{urllib.parse.quote(str(version))}"
    data = fetch_json(url, timeout)
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list) or not files:
        raise RuntimeError("파일 매니페스트가 비어 있습니다")
    return files


def download(url, dest, expected_sha256=None, progress_cb=None, timeout=_DOWNLOAD_TIMEOUT):
    """스트리밍 다운로드 + sha256 검증. progress_cb(done,total) 가 False 면 취소.

    실패·취소 시 **받다 만 파일을 반드시 지운다** — 남겨두면 다음 실행이 그것을
    온전한 것으로 착각할 여지가 생긴다.
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
        if not path.is_file() or path.name == MANIFEST_FILENAME:
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


def link_or_copy(src, dst):
    """하드링크 우선(복사 0바이트), 안 되면 복사. 반환 'link' | 'copy'.

    볼륨이 다르거나 파일시스템이 지원하지 않으면 링크가 실패한다 — 그때는 복사로
    떨어질 뿐 업데이트가 깨지지 않아야 한다. PyInstaller 산출물은 실행 중 수정되지
    않으므로 이전 버전과 실체를 공유해도 안전하다.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def install_delta(root, version, base_url, remote_files, source_dir,
                  local_files=None, progress_cb=None):
    """변경분만 받아 versions\\<version> 을 조립한다. 반환 (설치 경로, 통계).

    전체 zip 방식과 마찬가지로 tmp 에 다 만든 뒤 rename 하므로, 중간에 실패하면
    tmp 만 지우면 되고 기존 버전은 그대로다.
    """
    root = Path(root)
    versions = root / VERSIONS_DIRNAME
    versions.mkdir(parents=True, exist_ok=True)
    final = versions / version
    tmp = versions / f"{version}.tmp-{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)

    reuse, fetch = plan_delta(remote_files, source_dir, local_files)
    fetch_bytes = sum(int(e.get("s") or 0) for e in fetch)
    stats = {"reuse": len(reuse), "fetch": len(fetch), "bytes": fetch_bytes,
             "linked": 0, "copied": 0}
    ulog(f"DELTA plan reuse={len(reuse)} fetch={len(fetch)} "
         f"bytes={fetch_bytes / 1024 / 1024:.1f}MB")
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        for entry, src_rel in reuse:
            how = link_or_copy(Path(source_dir) / src_rel, _safe_target(tmp, entry["p"]))
            stats["linked" if how == "link" else "copied"] += 1
        done = 0
        for entry in fetch:
            url = (f"{base_url.rstrip('/')}/honey/file/{urllib.parse.quote(str(version))}"
                   f"?path={urllib.parse.quote(entry['p'])}")

            def _cb(chunk_done, _total, _base=done):
                if progress_cb is None:
                    return None
                return progress_cb(_base + chunk_done, fetch_bytes)

            download(url, _safe_target(tmp, entry["p"]), entry.get("h"), _cb)
            done += int(entry.get("s") or 0)
        if not (tmp / APP_EXE_NAME).exists():
            raise RuntimeError(f"조립 결과에 {APP_EXE_NAME} 이 없습니다")
        write_file_manifest(tmp, remote_files)
        if final.exists():
            shutil.rmtree(final)
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    ulog(f"DELTA done {final} linked={stats['linked']} copied={stats['copied']} "
         f"fetched={stats['fetch']}")
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
def read_fail_count(root, version):
    """그 버전의 연속 실패 횟수. 다른 버전이 기록돼 있으면 0 (새 버전이면 리셋)."""
    try:
        text = (Path(root) / FAILCOUNT_FILENAME).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return 0
    parts = text.split()
    if len(parts) != 2 or parts[0] != str(version):
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def bump_fail_count(root, version):
    count = read_fail_count(root, version) + 1
    try:
        (Path(root) / FAILCOUNT_FILENAME).write_text(
            f"{version} {count}\n", encoding="utf-8")
    except OSError:
        pass
    return count


def clear_fail_count(root):
    try:
        (Path(root) / FAILCOUNT_FILENAME).unlink()
    except OSError:
        pass
