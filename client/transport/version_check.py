"""버전 체크 / 자동 업데이트 헬퍼.

flow:
1) fetch_latest(base_url) → version.json dict
2) is_newer(remote, CURRENT_VERSION) 가 True 면 사용자에게 묻고
3) download_to(target, url, expected_sha256, progress_cb) 로 설치본(HoneySetup.exe) 다운로드
4) updater.run_installer() 로 조용히(/SILENT) 재설치 → 앱 종료 → 설치 후 자동 재실행
"""
import hashlib
from pathlib import Path

import requests

from .config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL


class DownloadCancelled(Exception):
    """progress_cb 가 False 를 반환해 사용자가 다운로드를 취소함."""


def fetch_latest(base_url=None) -> dict:
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/honey/version"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def is_newer(remote: str, local: str) -> bool:
    """semver 비교 (간이 — 'a.b.c' 형태 가정)."""
    if not remote or not local:
        return False
    try:
        ra = tuple(int(x) for x in remote.split("."))
        la = tuple(int(x) for x in local.split("."))
    except ValueError:
        return remote != local
    return ra > la


def download_to(target_path, url, expected_sha256=None, base_url=None, progress_cb=None):
    """target_path 로 streaming 다운로드. sha256 검증 옵션.

    progress_cb(downloaded:int, total:int) -> bool|None : 청크마다 호출.
      total 은 Content-Length (없으면 0). False 를 반환하면 DownloadCancelled.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if not url.startswith("http"):
        base = (base_url or SERVER_BASE_URL).rstrip("/")
        url = f"{base}{url}" if url.startswith("/") else f"{base}/{url}"

    h = hashlib.sha256()
    try:
        with requests.get(url, stream=True, timeout=REQUEST_TIMEOUT_SEC * 2) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            with target_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    h.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb is not None and progress_cb(downloaded, total) is False:
                        raise DownloadCancelled()
    except DownloadCancelled:
        target_path.unlink(missing_ok=True)
        raise

    if expected_sha256:
        actual = h.hexdigest()
        if actual.lower() != expected_sha256.lower():
            target_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"sha256 mismatch: expected={expected_sha256}, actual={actual}")
    return target_path
