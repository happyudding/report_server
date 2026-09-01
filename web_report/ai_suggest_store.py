# -*- coding: utf-8 -*-
"""AI Comment [제안] suggestion 영구 저장 (docs/23 핵심 결정 ① — 캐시가 아니다).

dist_pack_store 패턴: ``<akey>/cache`` 밖이라 총량 상한 축출 대상에서 빠지고, aicmt
캐시가 비워져 콜드 재빌드가 돌아도(룰 불변이면 프롬프트 sha 가 같아) 재병합으로 생존한다.
akey 디렉터리 안이라 세션 삭제(storage_gateway 가 akey 째 삭제) 시 함께 정리된다.

레이아웃::

    <upload_root>/web_report/<analysis_key>/ai_suggest/<chash12>_<mode>.json          (원본)
    <upload_root>/web_report/<analysis_key>/ai_suggest/<chash12>_<mode>_p<dig8>.json  (전처리)

형식::

    {"schema": 1, "items": {"<item_raw>": {"sha": "<12hex>", "suggestion": str,
                                           "by": str, "ts": int,
                                           "raw": str}}}

``raw`` 는 sanitize **이전** 의 LLM 원문이다(2026-09-01, 선택 키 — 없으면 종전 형식).
관리자 검수에서 "모델이 이상하게 답한 것"과 "서버 sanitize 가 잘라낸 것"을 구분하려면
둘 다 있어야 한다 — 저장된 문장만 보면 그 구분이 원리적으로 불가능하다. 상한을 걸어
(``MAX_RAW_CHARS``) 파일이 무한정 커지지 않게 하고, sanitize 결과와 **같으면 아예 저장
하지 않는다**(대부분의 정상 케이스에서 용량 증가 0).

- 파일 이름 규약은 dist_pack_store._gen_name 과 동일 — chash 가 바뀌면(raw 편집)
  구조적으로 구세대가 조회되지 않고, ``delete_stale`` 이 회수한다.
- 읽기 실패는 조용히 {} — 호출부는 병합 없이 진행(폴백 무해).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_ITEMS = 500   # 파일당 item 상한 — 초과분은 오래된 ts 부터 버린다
# 원문(raw) 보존 상한 — sanitize 상한(1000)보다 넉넉히 두되 무한은 아니다. 형식 이탈
# (서두·코드펜스·JSON 통째)이 보이는 데는 이 길이면 충분하고, 넘어가는 응답은 어차피
# 앞부분만 봐도 이상을 안다.
MAX_RAW_CHARS = 4000


def _store_root(upload_root: Path, analysis_key) -> Path:
    return Path(upload_root) / "web_report" / str(analysis_key) / "ai_suggest"


def _chash12(content_hash) -> str:
    return (str(content_hash) or "none")[:12] or "none"


def _gen_name(content_hash, mode, prep_digest: str = "") -> str:
    name = f"{_chash12(content_hash)}_{str(mode or 'Normal')}"
    if prep_digest:
        name += f"_p{str(prep_digest)[:8]}"
    return name


def store_path(upload_root: Path, analysis_key, content_hash, mode,
               prep_digest: str = "") -> Path:
    return (_store_root(upload_root, analysis_key)
            / (_gen_name(content_hash, mode, prep_digest) + ".json"))


def load(upload_root: Path, analysis_key, content_hash, mode,
         prep_digest: str = "") -> dict:
    """저장된 items dict — {item: {"sha","suggestion","by","ts"}}. 없음/손상 → {}."""
    path = store_path(upload_root, analysis_key, content_hash, mode, prep_digest)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        _log.warning("ai_suggest load failed: %s", path, exc_info=True)
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, dict) else {}


def save_merge(upload_root: Path, analysis_key, content_hash, mode,
               items: dict, by: str = "", prep_digest: str = "") -> int:
    """items 를 기존 파일에 upsert 저장 (tmp→replace 원자 교체). 반환 = 저장 후 총 건수.

    merge(upsert)라 반복 push 가 멱등이다. 호출부(service)가 keyed_lock 으로 동시 push
    를 직렬화하지만, tmp 이름에 pid 를 넣어 프로세스 경계의 동시 쓰기에서도 서로의
    임시 파일을 지우지 않는다(dist_pack_store 규약).
    """
    path = store_path(upload_root, analysis_key, content_hash, mode, prep_digest)
    merged = load(upload_root, analysis_key, content_hash, mode, prep_digest)
    now = int(time.time())
    for item, row in (items or {}).items():
        if not item or not isinstance(row, dict):
            continue
        suggestion = str(row.get("suggestion") or "")
        entry = {"sha": str(row.get("sha") or ""),
                 "suggestion": suggestion,
                 "by": str(row.get("by") or by or ""),
                 "ts": int(row.get("ts") or now)}
        # cases = LLM 이 요약한 [사례] 블록 (2026-09-02 두 블록 계약). 없으면 키를 만들지
        # 않는다 — 옛 저장분(제안만)과 파일 모양이 같아 하위호환이 유지된다.
        cases = str(row.get("cases") or "")
        if cases:
            entry["cases"] = cases
        # 원문은 sanitize 결과와 **다를 때만** 남긴다 — 같으면 정보가 0인데 파일만 2배가
        # 된다(정상 케이스가 대부분이라 실질 용량 증가는 거의 없다).
        raw = str(row.get("raw") or "")
        if raw and raw != suggestion:
            entry["raw"] = raw[:MAX_RAW_CHARS]
        merged[str(item)] = entry
    if len(merged) > MAX_ITEMS:
        keep = sorted(merged.items(), key=lambda kv: kv[1].get("ts") or 0,
                      reverse=True)[:MAX_ITEMS]
        merged = dict(keep)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"schema": SCHEMA_VERSION, "items": merged},
                                  ensure_ascii=False, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        _log.warning("ai_suggest save failed: %s", path, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return 0
    return len(merged)


def delete_stale(upload_root: Path, analysis_key, keep_content_hash) -> int:
    """현 content_hash 세대가 아닌 저장 파일 삭제 (raw 교체 후 회수 — dist_pack 과 동일).

    같은 chash 의 mode·전처리 variant 는 전부 남는다(prefix 판정).
    """
    root = _store_root(upload_root, analysis_key)
    keep_prefix = _chash12(keep_content_hash) + "_"
    removed = 0
    try:
        entries = list(root.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return 0
    except Exception:
        _log.warning("ai_suggest stale scan failed akey=%.12s", str(analysis_key),
                     exc_info=True)
        return 0
    for entry in entries:
        if not entry.is_file() or entry.name.startswith(keep_prefix):
            continue
        try:
            entry.unlink()
            removed += 1
        except Exception:  # noqa: BLE001
            pass
    if removed:
        _log.info("ai_suggest stale removed akey=%.12s files=%d",
                  str(analysis_key), removed)
    return removed
