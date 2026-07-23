"""Distribution pack 영구 저장 (2026-07-23).

disk_cache 와 달리 **캐시가 아니다** — 총량 상한 축출 대상에서 빠지고(축출 스캔은
``<akey>/cache`` 만 훑는다), 세션 재조회·서버 재시작 후에도 남아 서버가 Distribution 을
다시 정렬하지 않게 한다. Honey 가 업로드에 첨부한 pack 만 저장하며, 없으면 조회는 기존
tables 재계산 폴백으로 동작한다(구세션 무변경).

레이아웃::

    <upload_root>/web_report/<analysis_key>/dist_pack/<chash12>_<mode>/
        index.json
        chunk_0.gz, chunk_1.gz, ...

- akey 디렉토리 안이라 세션 삭제(storage_gateway 가 akey 째 삭제) 시 함께 정리된다.
- 디렉토리 이름에 content_hash 를 넣어 raw_data 편집으로 chash 가 바뀌면 **구조적으로**
  구 pack 이 조회되지 않는다(잘못된 재사용 불가). 구 세대 디렉토리는 ``delete_stale`` 로
  회수한다.
- 읽기 실패는 조용히 None — 호출부가 기존 계산 폴백으로 넘어간다.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

_log = logging.getLogger(__name__)

_INDEX_NAME = "index.json"


def _pack_root(upload_root: Path, analysis_key) -> Path:
    return Path(upload_root) / "web_report" / str(analysis_key) / "dist_pack"


def _chash12(content_hash) -> str:
    return (str(content_hash) or "none")[:12] or "none"


def _gen_name(content_hash, mode) -> str:
    return f"{_chash12(content_hash)}_{str(mode or 'Normal')}"


def pack_dir(upload_root: Path, analysis_key, content_hash, mode) -> Path:
    return _pack_root(upload_root, analysis_key) / _gen_name(content_hash, mode)


def save(upload_root: Path, analysis_key, content_hash, mode, index_text: str,
         chunks: dict) -> bool:
    """index 텍스트 + {chunk_id: gzip bytes} 저장. 성공 여부 반환.

    임시 디렉토리에 다 쓴 뒤 교체해 부분 저장 상태가 조회되지 않게 한다.
    """
    target = pack_dir(upload_root, analysis_key, content_hash, mode)
    tmp = target.with_name(target.name + ".tmp")
    try:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        for chunk_id, blob in chunks.items():
            (tmp / f"chunk_{int(chunk_id)}.gz").write_bytes(blob)
        (tmp / _INDEX_NAME).write_text(index_text, encoding="utf-8")
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        tmp.replace(target)
        return True
    except Exception:
        _log.warning("dist pack save failed akey=%.12s", str(analysis_key), exc_info=True)
        shutil.rmtree(tmp, ignore_errors=True)
        return False


def load_index(upload_root: Path, analysis_key, content_hash, mode) -> dict | None:
    """저장된 index → 검증된 dict. 없거나 손상이면 None (호출부는 폴백)."""
    from .dist_pack import parse_pack_index

    path = pack_dir(upload_root, analysis_key, content_hash, mode) / _INDEX_NAME
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception:
        _log.warning("dist pack index read failed: %s", path, exc_info=True)
        return None
    try:
        return parse_pack_index(text)
    except ValueError as exc:
        _log.warning("dist pack index invalid akey=%.12s: %s", str(analysis_key), exc)
        return None


def load_chunk_items(upload_root: Path, analysis_key, content_hash, mode,
                     chunk_id: int) -> dict | None:
    """chunk 1개의 items dict. 없거나 손상이면 None."""
    from .dist_pack import load_chunk_items as _decode

    path = (pack_dir(upload_root, analysis_key, content_hash, mode)
            / f"chunk_{int(chunk_id)}.gz")
    try:
        blob = path.read_bytes()
    except FileNotFoundError:
        return None
    except Exception:
        _log.warning("dist pack chunk read failed: %s", path, exc_info=True)
        return None
    try:
        return _decode(blob)
    except Exception:
        _log.warning("dist pack chunk invalid: %s", path, exc_info=True)
        return None


def delete_stale(upload_root: Path, analysis_key, keep_content_hash, keep_mode=None) -> int:
    """현 content_hash 세대가 아닌 pack 디렉토리 삭제 (raw 교체 후 용량 회수).

    ``keep_mode`` 를 주면 그 mode 세대만 남기고, 안 주면 같은 chash 의 모든 mode 를 남긴다
    (dedup 형제 세션이 서로 다른 mode 로 같은 원본을 공유할 수 있다).
    """
    root = _pack_root(upload_root, analysis_key)
    keep_prefix = _chash12(keep_content_hash) + "_"
    keep_exact = _gen_name(keep_content_hash, keep_mode) if keep_mode else None
    removed = 0
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return 0
    except Exception:
        _log.warning("dist pack stale scan failed akey=%.12s", str(analysis_key),
                     exc_info=True)
        return 0
    for entry in entries:
        if not entry.is_dir():
            continue
        keep = entry.name == keep_exact if keep_exact else entry.name.startswith(keep_prefix)
        if keep:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    if removed:
        _log.info("dist pack stale removed akey=%.12s dirs=%d", str(analysis_key), removed)
    return removed
