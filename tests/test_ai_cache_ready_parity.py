# -*- coding: utf-8 -*-
"""AI Comment 캐시의 값싼 판정 ↔ 본문 판정이 **같은 답**을 낸다 — 2026-09-02 무한 로딩 방지.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_ai_cache_ready_parity.py

**무엇이 깨졌었나**: `_ai_cache_ready`(값싼 폴링 판정)는 파일 **존재만** 보는데
(`disk_cache.ai_comment_exists`, stat 1회), `_ai_comment_cached`(본문)는 로드한 dict 에
`_AI_RESULT_KEYS` 가 다 있어야 히트로 쳤다. 2026-09-01 그 목록에 `precedents`·
`precedent_counts` 가 추가되자, 그 키 없이 쓰인 캐시 파일을 가진 세션이 이렇게 됐다:

  · 콜드 빌드 `_ai_comment_cached` → 키 부족 → miss → `ai_comment_pending=True`
  · `_pending_kinds` 는 `not _ai_cache_ready` 로 판정 → 파일이 있으니 **빈 튜플**
  · `_pending_report_ready` → False → `report_is_cold` → **영원히 202**
  · 백그라운드 `run_ai_comment_build` 도 `_ai_cache_ready`=True → "할 일 없음" 즉시 종료

즉 **스스로 낫지 않는 무한 로딩**. 잡이 예외로 죽는 게 아니라 정상 종료라 실패 카운터·
503 차단·재시도 안전망이 전부 우회되고, 사용자에겐 에러 없이 "무한 로딩"으로만 보였다.
CLAUDE.md 규칙 17 ② "두 판정이 같은 답을 내야 한다" 위반.

고친 방법: 키 검증을 `disk_cache.load_ai_comment(require_keys=)` 로 내리고, 모자라면
**손상 파일과 똑같이 파일을 지운다**. 그러면 다음 `_ai_cache_ready` 가 False 가 되어
백그라운드 잡이 다시 돌고 스스로 복구된다.

검증 항목:
  (a) 키가 온전한 파일 — 두 판정 모두 히트
  (b) 키가 모자란 파일 — 본문 판정이 miss 이고, **파일이 지워져** 값싼 판정도 False
      (= 두 판정이 같은 답. 지우지 않으면 여기서 True/False 로 갈린다)
  (c) 손상(디코드 실패) 파일 — 종전과 같이 지우고 miss (회귀 없음)
  (d) require_keys 를 안 주면 검증하지 않는다 (기존 호출자 동작 불변)

pytest 미사용(tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="ai_cache_parity_"))
os.environ.setdefault("REPORT_UPLOAD_DIR", str(_TMP))

from web_report import disk_cache  # noqa: E402

# service 의 실제 상수를 그대로 쓴다 — 목록이 또 늘어나도 이 테스트가 따라간다.
from web_report.service import _AI_RESULT_KEYS  # noqa: E402

KEY = ("akey", "chash", "mode", 13)


def _full_result() -> dict:
    """_AI_RESULT_KEYS 를 모두 갖춘 정상 결과."""
    return {k: ({} if k.endswith("s") or k == "comments" else []) for k in _AI_RESULT_KEYS}


def _path() -> Path:
    return disk_cache._path_for(_TMP, "aicmt", KEY, ".json.gz")


def _write_raw(obj) -> None:
    disk_cache.save_ai_comment(_TMP, KEY, obj)


def _cleanup_key() -> None:
    p = _path()
    if p.exists():
        p.unlink()


def test_a_full_file_hits_both():
    _cleanup_key()
    _write_raw(_full_result())
    assert disk_cache.ai_comment_exists(_TMP, KEY), "값싼 판정이 파일을 못 봤다"
    got = disk_cache.load_ai_comment(_TMP, KEY, _AI_RESULT_KEYS)
    assert got is not None, "키가 온전한데 본문 판정이 miss"
    assert disk_cache.ai_comment_exists(_TMP, KEY), "정상 파일을 지웠다"
    print("  (a) 온전한 파일 - 두 판정 모두 히트 OK")


def test_b_missing_keys_drops_file():
    """핵심 — 키가 모자라면 파일을 지워 두 판정이 같은 답이 된다."""
    _cleanup_key()
    partial = _full_result()
    partial.pop("precedents", None)          # 2026-09-01 에 추가된 키를 뺀 옛 파일
    partial.pop("precedent_counts", None)
    _write_raw(partial)
    assert disk_cache.ai_comment_exists(_TMP, KEY), "사전 조건: 파일이 있어야 한다"

    got = disk_cache.load_ai_comment(_TMP, KEY, _AI_RESULT_KEYS)
    assert got is None, "키가 모자란데 본문 판정이 히트로 쳤다"
    # 이것이 회귀 방지의 본체 — 지우지 않으면 값싼 판정만 True 로 남아 무한 202 가 된다.
    assert not disk_cache.ai_comment_exists(_TMP, KEY), \
        "키 부족 파일이 남았다 — 값싼 판정(True)과 본문 판정(miss)이 갈려 무한 로딩이 된다"
    print("  (b) 키 부족 파일 - miss + 파일 삭제로 두 판정 일치 OK")


def test_c_corrupt_file_still_dropped():
    _cleanup_key()
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"not-a-gzip")
    assert disk_cache.load_ai_comment(_TMP, KEY, _AI_RESULT_KEYS) is None
    assert not p.exists(), "손상 파일을 지우지 않았다 (종전 동작 회귀)"
    print("  (c) 손상 파일 - 종전대로 삭제 + miss OK")


def test_d_no_require_keys_skips_validation():
    """require_keys 미지정이면 검증하지 않는다 — 기존 호출자 동작 불변."""
    _cleanup_key()
    partial = {"comments": {}}
    _write_raw(partial)
    got = disk_cache.load_ai_comment(_TMP, KEY)
    assert got == partial, "require_keys 없이 부른 조회가 내용을 바꿨다"
    assert disk_cache.ai_comment_exists(_TMP, KEY), \
        "require_keys 없이 불렀는데 파일을 지웠다"
    print("  (d) require_keys 미지정 - 검증·삭제 안 함 OK")


def main() -> int:
    print("AI Comment 캐시 판정 일치 검증")
    try:
        test_a_full_file_hits_both()
        test_b_missing_keys_drops_file()
        test_c_corrupt_file_still_dropped()
        test_d_no_require_keys_skips_validation()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print("\n전부 통과")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
