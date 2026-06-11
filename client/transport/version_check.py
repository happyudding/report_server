"""Version check and release package download helpers.

Flow:
1) fetch_latest(base_url) -> version.json dict.
2) is_newer(remote, CURRENT_VERSION) tells the UI whether to ask the user.
3) download_to(target, url, expected_sha256, progress_cb) downloads Honey ZIP.
4) updater.apply_update_zip() applies the ZIP after the app exits.
"""
import hashlib
from pathlib import Path

import requests

from .config import REQUEST_TIMEOUT_SEC, SERVER_BASE_URL


class DownloadCancelled(Exception):
    """Raised when progress_cb returns False."""


def fetch_latest(base_url=None) -> dict:
    base = (base_url or SERVER_BASE_URL).rstrip("/")
    url = f"{base}/honey/version"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    return resp.json()


def is_newer(remote: str, local: str) -> bool:
    """Compare simple semver strings in a.b.c form."""
    if not remote or not local:
        return False
    try:
        ra = tuple(int(x) for x in remote.split("."))
        la = tuple(int(x) for x in local.split("."))
    except ValueError:
        return remote != local
    return ra > la


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
