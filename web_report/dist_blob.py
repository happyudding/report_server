"""Distribution ECDF compact blob 공용 빌더 (2026-07-15).

서버(service.get_distribution 폴백 계산)와 Honey 클라이언트(업로드 시 프리컴퓨트,
client/honey_main.py)가 **같은 코드로 같은 값**을 만들기 위한 순수 모듈이다 —
캐시·저장소·DB 에 의존하지 않아 클라에서도 안전하게 import 한다(honeyform 과 동일).

클라가 업로드 multipart 에 dist_blob(전체)/dist_blob_bin1(양품만) gzip 을 첨부하면
ingest 가 검증 후 dist 캐시(disk+RAM)에 그대로 시딩한다 — 서버의 콜드 dist 빌드
(수십 초 CPU + RAM 스파이크)가 사라진다. 미첨부/검증 실패는 기존 서버 계산 폴백.

주의: 여기 계산 결과가 서버 폴백 계산(service.get_distribution)과 정준 JSON 으로
완전 일치해야 한다 — 두 경로 모두 compute_dist_compact 를 호출하므로 구조적으로
보장된다. 로직을 바꿀 땐 반드시 양쪽 일치 검증을 할 것.
"""
from __future__ import annotations

import gzip
import io
import json

DIST_BLOB_FORMAT = "ecdf-columnar-v1"
# build_distribution_compact 반환 dict 를 separators=(",",":") 로 직렬화한 선두 —
# ingest 가 전체 파싱 없이 포맷을 검증하는 데 쓴다 (키 순서는 삽입 순서 보존).
DIST_BLOB_PREFIX = b'{"format":"ecdf-columnar-v1","items":'


def compute_dist_compact(tables, selected_items, mode, *, bin1=False, only=None) -> dict:
    """HoneyformTable 리스트 → Distribution ECDF 컴팩트 dict (전 포인트, 다운샘플 없음).

    service.get_distribution 의 계산 본체와 동일: 모드 변형(DUT 분할) →
    selected_items 필터 → 무데이터 항목만 제외 → build_distribution_compact.
    Pass/Fail 항목은 하드 제외하지 않고 ECDF 를 포함한다 — 프런트 "P/F 없애기" 토글이
    표시를 제어하며, distribution_index(is_passfail 플래그)와 제외 기준을 맞춘다.
    tables 의 item_columns 를 in-place 필터하므로 호출자는 소모성 tables 를 넘길 것
    (서버는 캐시 클론, 클라는 방금 디코드한 임시 tables).

    ``only`` 는 항목 배치 조회(GET .../web_report/distribution_batch)용 항목 화이트리스트다.
    항목 집합을 **좁히기만** 하고 계산 자체는 동일하므로, 결과는 전체 계산 결과에서 그
    항목만 뽑은 것과 정준 JSON 으로 완전히 같다(다운샘플 아님 — 규칙 #6 무관). None 이면
    기존과 동일하게 전 항목을 계산한다.
    """
    from .tabs.common import empty_items
    from .tabs.distribution import build_distribution_compact
    from .validation import mode_tables, validate_mode

    tables = mode_tables(tables, validate_mode(mode))
    selected = {str(v) for v in (selected_items or []) if str(v)}
    if selected:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected]
    excluded = empty_items(tables)
    all_items = sorted({c for t in tables for c in t.item_columns if c not in excluded})
    if only is not None:
        wanted = {str(v) for v in only if str(v)}
        all_items = [c for c in all_items if c in wanted]
    return build_distribution_compact(tables, all_items, bin1_only=bin1)


def gzip_dist_blob(compact: dict, *, level: int = 6) -> bytes:
    """compact dict → 서버 dist 캐시와 동일 규약(separators, ensure_ascii=False)의
    JSON gzip bytes. 클라 프리컴퓨트는 1회 계산·전송이라 level 6 으로 전송량을 줄인다."""
    return gzip.compress(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=level)


def count_points(compact: dict) -> int:
    """compact dict 의 총 ECDF 포인트 수 (관측 로그용 — len 합산이라 저렴)."""
    total = 0
    for item in (compact.get("items") or {}).values():
        for src in (item.get("sources") or {}).values():
            total += len(src.get("x") or ())
    return total


def validate_dist_blob(blob: bytes) -> int:
    """클라 첨부 blob 의 gzip 무결성(CRC)과 포맷 선두를 검증한다. 반환 = 비압축 바이트 수.

    수백 MB 급 blob 을 ingest 에서 JSON 파싱하면 프리컴퓨트 이득이 사라지므로,
    스트림 해제(끝까지 읽으면 gzip 모듈이 CRC 를 검증)와 선두 프리픽스 확인만 한다.
    항목 값 자체의 정합성은 클라가 서버와 같은 compute_dist_compact 를 쓰는 것으로
    보장한다(검증 절차: 정준 JSON 일치). 실패 시 ValueError.
    """
    total = 0
    first = b""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(blob)) as f:
            chunk = f.read(1 << 16)
            first = chunk
            while chunk:
                total += len(chunk)
                chunk = f.read(1 << 20)
    except (OSError, EOFError) as exc:
        raise ValueError(f"invalid gzip dist blob: {exc}") from exc
    if not first.startswith(DIST_BLOB_PREFIX):
        raise ValueError("unexpected dist blob prefix (format mismatch)")
    return total
