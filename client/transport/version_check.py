"""Version check and release package download helpers.

Flow:
1) fetch_latest(base_url) -> version.json dict.
2) is_newer(remote, CURRENT_VERSION) tells the UI whether to ask the user.
3) download_to(target, url, expected_sha256, progress_cb) downloads Honey ZIP.
4) updater.apply_update_zip() applies the ZIP after the app exits.

fetch_announcement(base_url) 은 위 흐름과 별개로, 최신 버전을 실행 중일 때
릴리스 공지(announcement.txt 원문)를 가져온다.
"""
import hashlib
from pathlib import Path
from urllib.parse import quote

import requests

from .app_update import is_newer  # noqa: F401  (재노출 — 아래 주석 참조)
from .config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL
from .retry import get_with_retry


class DownloadCancelled(Exception):
    """Raised when progress_cb returns False."""


def _honey_headers():
    """신원 토큰 UA — excel_download._fetch 와 동일 규칙(HoneyUser/<percent-encoded 계정>).

    서버가 /honey/version 호출을 'Honey 실행'으로 사용자별 집계하는 데 쓴다
    (server/honey_routes.py). 수집 실패 시 토큰 없이 진행 — 서버는 IP 로 집계."""
    try:
        import client_identity
        user = client_identity.collect().get("user", "")
    except Exception:
        user = ""
    return {"User-Agent": f"python-requests HoneyUser/{quote(user, safe='')}"} if user else {}


def fetch_latest(base_url=None) -> dict:
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/honey/version"
    resp = get_with_retry(url, timeout=REQUEST_TIMEOUT_SEC, headers=_honey_headers())
    resp.raise_for_status()
    return resp.json()


def fetch_announcement(base_url=None) -> str:
    """릴리스 공지 원문(/honey/announcement)을 서버가 준 그대로 돌려준다.

    공지는 실패해도 앱 동작에 영향이 없고 다음 실행 때 다시 시도하면 되므로
    get_with_retry(최대 30초 지연) 대신 짧은 타임아웃으로 한 번만 요청한다.
    """
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    resp = requests.get(f"{base}/honey/announcement", timeout=(5, 15))
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


# is_newer 는 위에서 app_update 것을 재노출한다 (`version_check.is_newer` 호출부 무변경).
# 정본을 app_update 에 둔 이유: 런처는 requests 를 넣을 수 없어 이 모듈을 import 하지
# 못하는데, 런처와 앱이 서로 다른 "더 새 버전" 판정을 쓰면 안 되기 때문이다.


def download_to(target_path, url, expected_sha256=None, base_url=None, progress_cb=None):
    """Stream a file to target_path and optionally verify sha256.

    progress_cb(downloaded:int, total:int) -> bool|None is called for each chunk.
    Returning False cancels the download and deletes the partial file.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if not url.startswith("http"):
        base = (base_url or SERVER_BASE_URL).rstrip("/")
        url = f"{base}{url}" if url.startswith("/") else f"{base}/{url}"

    h = hashlib.sha256()
    try:
        download_timeout = tuple(t * 2 for t in REQUEST_TIMEOUT_SEC)
        with requests.get(url, stream=True, timeout=download_timeout) as resp:
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
