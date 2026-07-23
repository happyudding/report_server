"""Distribution pack 영구 저장 (2026-07-23).

disk_cache 와 달리 **캐시가 아니다** — 총량 상한 축출 대상에서 빠지고(축출 스캔은
``<akey>/cache`` 만 훑는다), 세션 재조회·서버 재시작 후에도 남아 서버가 Distribution 을
다시 정렬하지 않게 한다. Honey 가 업로드에 첨부한 pack 만 저장하며, 없으면 조회는 기존
tables 재계산 폴백으로 동작한다(구세션 무변경). Honey 가 첨부한 원본 pack 외에, 조회
전처리(preprocess)를 켠 세션용 variant 는 서버가 1회 만들어 같은 규칙으로 저장한다.

레이아웃::

    <upload_root>/web_report/<analysis_key>/dist_pack/<chash12>_<mode>/            (원본)
    <upload_root>/web_report/<analysis_key>/dist_pack/<chash12>_<mode>_p<dig8>/    (전처리)
        index.json
        chunk_0.gz, chunk_1.gz, ...

- akey 디렉토리 안이라 세션 삭제(storage_gateway 가 akey 째 삭제) 시 함께 정리된다.
- 디렉토리 이름에 content_hash 를 넣어 raw_data 편집으로 chash 가 바뀌면 **구조적으로**
  구 pack 이 조회되지 않는다(잘못된 재사용 불가). 구 세대 디렉토리는 ``delete_stale`` 로
  회수한다 — chash prefix 로 판정하므로 variant 도 같은 세대면 함께 남는다.
- 전처리 spec 이 바뀌면 chash 는 그대로고 digest 만 바뀌므로 ``delete_stale`` 이 못 지운다.
  그 경우는 ``delete_variant`` 로 표적 회수한다.
- 읽기 실패는 조용히 None — 호출부가 기존 계산 폴백으로 넘어간다.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

_log = logging.getLogger(__name__)

_INDEX_NAME = "index.json"


def _pack_root(upload_root: Path, analysis_key) -> Path:
    return Path(upload_root) / "web_report" / str(analysis_key) / "dist_pack"


def _chash12(content_hash) -> str:
    return (str(content_hash) or "none")[:12] or "none"


def _gen_name(content_hash, mode, prep_digest: str = "") -> str:
    """pack 디렉토리 이름. ``prep_digest`` 가 있으면 전처리 variant 이름이 된다.

    digest 가 빈 문자열이면 종전과 완전히 같은 이름 — 기존 저장분·호출부 무변경.
    """
    name = f"{_chash12(content_hash)}_{str(mode or 'Normal')}"
    if prep_digest:
        name += f"_p{str(prep_digest)[:8]}"
    return name


def pack_dir(upload_root: Path, analysis_key, content_hash, mode,
             prep_digest: str = "") -> Path:
    return (_pack_root(upload_root, analysis_key)
            / _gen_name(content_hash, mode, prep_digest))


def save(upload_root: Path, analysis_key, content_hash, mode, index_text: str,
         chunks: dict, prep_digest: str = "") -> bool:
    """index 텍스트 + {chunk_id: gzip bytes} 저장. 성공 여부 반환.

    임시 디렉토리에 다 쓴 뒤 교체해 부분 저장 상태가 조회되지 않게 한다. tmp 이름에 pid 를
    넣어 두 프로세스(클라 pack 수신 vs 서버 variant 생성)가 같은 키를 동시에 써도 서로의
    임시 디렉토리를 지우지 않게 한다.
    """
    target = pack_dir(upload_root, analysis_key, content_hash, mode, prep_digest)
    tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
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


def load_index(upload_root: Path, analysis_key, content_hash, mode,
               prep_digest: str = "") -> dict | None:
    """저장된 index → 검증된 dict. 없거나 손상이면 None (호출부는 폴백)."""
    from .dist_pack import parse_pack_index

    path = (pack_dir(upload_root, analysis_key, content_hash, mode, prep_digest)
            / _INDEX_NAME)
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
                     chunk_id: int, prep_digest: str = "") -> dict | None:
    """chunk 1개의 items dict. 없거나 손상이면 None."""
    from .dist_pack import load_chunk_items as _decode

    path = (pack_dir(upload_root, analysis_key, content_hash, mode, prep_digest)
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
    (dedup 형제 세션이 서로 다른 mode 로 같은 원본을 공유할 수 있다). 어느 쪽이든 남기는
    세대의 전처리 variant(``..._p<dig8>``)는 함께 남는다.
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
        if keep_exact:
            keep = (entry.name == keep_exact
                    or entry.name.startswith(keep_exact + "_p"))
        else:
            keep = entry.name.startswith(keep_prefix)
        if keep:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    if removed:
        _log.info("dist pack stale removed akey=%.12s dirs=%d", str(analysis_key), removed)
    return removed


def delete_variant(upload_root: Path, analysis_key, content_hash, mode,
                   prep_digest: str) -> bool:
    """전처리 variant 1개 표적 삭제 (spec 변경으로 digest 가 바뀔 때 용량 회수).

    chash 가 그대로라 ``delete_stale`` 로는 지워지지 않는 세대를 위한 것이다. digest 가
    비면 원본 pack 을 가리키므로 아무것도 하지 않는다(원본은 Honey 가 올린 것이라 서버가
    지우면 재생성할 수 없다).
    """
    if not prep_digest:
        return False
    target = pack_dir(upload_root, analysis_key, content_hash, mode, prep_digest)
    if not target.is_dir():
        return False
    shutil.rmtree(target, ignore_errors=True)
    _log.info("dist pack variant removed akey=%.12s dir=%s", str(analysis_key), target.name)
    return True
