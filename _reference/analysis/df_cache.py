from __future__ import annotations

import hashlib
import os
import pickle
import random
import time
import uuid
from pathlib import Path

import pandas as pd

from config import REPORT_UPLOAD_DIR

_CACHE_ROOT = Path(REPORT_UPLOAD_DIR) / "_df_cache"
_SCHEMA_SUFFIX = ".v1.pkl"
_PICKLE_PROTOCOL = 5
_CHUNK = 64 * 1024
_CLEANUP_PROBABILITY = 0.05


def _cache_path(content_hash: str) -> Path:
    return _CACHE_ROOT / content_hash[:2] / f"{content_hash}{_SCHEMA_SUFFIX}"


def compute_file_hash(file_path) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def load_cached_df(content_hash: str):
    path = _cache_path(content_hash)
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as f:
            obj = pickle.load(f)
        if isinstance(obj, pd.DataFrame):
            return obj
        return None
    except (pickle.UnpicklingError, EOFError, OSError, AttributeError, ImportError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def store_df(content_hash: str, df: pd.DataFrame) -> None:
    path = _cache_path(content_hash)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}_{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "wb") as f:
            pickle.dump(df, f, protocol=_PICKLE_PROTOCOL)
        os.replace(tmp, path)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return

    if random.random() < _CLEANUP_PROBABILITY:
        try:
            cleanup()
        except Exception:
            pass


def cleanup(max_age_sec: int = 86400, max_bytes: int = 1_000_000_000) -> None:
    if not _CACHE_ROOT.is_dir():
        return
    now = time.time()
    entries = []
    for p in _CACHE_ROOT.rglob(f"*{_SCHEMA_SUFFIX}"):
        try:
            st = p.stat()
        except OSError:
            continue
        if now - st.st_mtime > max_age_sec:
            try:
                p.unlink()
            except OSError:
                pass
            continue
        entries.append((p, st.st_mtime, st.st_size))

    total = sum(s for _, _, s in entries)
    if total <= max_bytes:
        return
    entries.sort(key=lambda x: x[1])
    for p, _, size in entries:
        if total <= max_bytes:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            pass
