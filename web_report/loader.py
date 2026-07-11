"""세션 → parquet 원본 다운로드·디코드 → HoneyformTable 리스트 (service.py 에서 분리).

storage_gateway 접근과 tables LRU 캐시 결합만 담당한다. 캐시 프리미티브는 cache.py,
정규화는 validation.py 참조.
"""
from __future__ import annotations

from pathlib import Path

from . import cache
from . import cache_policy
from . import runtime
from .honeyform import HoneyformTable, decode_split_honeyform_parquet


def clone_table(t: HoneyformTable) -> HoneyformTable:
    """캐시 원본 보호용 얕은 클론.

    build_report_payload 가 item_columns 를 in-place 필터하므로 리스트/메타 dict 는 복사하고,
    df/data 는 공유한다 — 호출자는 df/data 를 수정하지 않는다는 계약 (편집 경로는
    use_cache=False 로 캐시를 우회한다).
    """
    return HoneyformTable(
        source=t.source, file_name=t.file_name, df=t.df,
        item_columns=list(t.item_columns),
        tseq=dict(t.tseq),
        tno=dict(t.tno), step=dict(t.step), units=dict(t.units),
        hilim=dict(t.hilim), lolim=dict(t.lolim), data=t.data)


def download_decode_tables(analysis_key, upload_root: Path, *, keep_df: bool = True):
    """parquet 원본 다운로드 + 디코드. 반환 (tables, manifest).

    sources 와 함께 받은 manifest 를 manifest 캐시에 write-through 해 이어지는 warm 조회의
    S3 manifest GET 을 없앤다. keep_df=False 는 읽기 경로용 슬림 디코드 (honeyform 참조).
    """
    sources, manifest = runtime.storage().load_webreport_sources(
        analysis_key, upload_root=upload_root)
    cache.manifest_cache_put(analysis_key, manifest)

    sources_manifest = manifest.get("sources") or []
    tables = []
    for idx, data in enumerate(sources):
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or source_name)
        # decode+split 결합 경로 — to_numeric 중복 변환/재검증 제거 (결과 동일)
        tables.append(decode_split_honeyform_parquet(
            data, source=source_name, file_name=file_name, keep_df=keep_df))
    return tables, manifest


def load_tables(session_id: str, *, report_db, upload_root: Path, use_cache: bool = True,
                session: dict | None = None):
    """세션 → analysis_key → parquet 원본 디코드 → HoneyformTable 리스트.

    manifest.selected_items 필터는 적용하지 않는다 (build_report_payload 가 이후 그 필터를
    in-place 로 적용하므로, 이 헬퍼는 raw data 조회처럼 전체 item 컬럼이 필요한 호출자에도
    안전하게 재사용된다).

    use_cache=True 면 (analysis_key, content_hash) 키의 LRU 캐시를 사용하고, 반환 tables 는
    캐시 원본의 클론이다 (df/data 공유 — 수정 금지). df 를 수정하는 편집 경로는
    use_cache=False 로 호출할 것. 콜드 미스는 single-flight 락으로 같은 키의 다운로드+디코드를
    한 스레드만 수행한다. manifest 는 content_hash 없이 바뀔 수 있어(etc/comments) 별도
    MANIFEST_CACHE 에 두고 편집 시 write-through 로 갱신한다.

    session: 호출자(라우트)가 이미 조회한 세션 dict 를 주면 재조회를 생략한다.
    """
    if session is None:
        session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    cache_key = cache_policy.tables_key(session)   # 키 구성 규약: cache_policy
    if use_cache:
        cached = cache.cache_get(cache.TABLES_CACHE, cache_key)
        if cached is None:
            with cache.keyed_lock(("tables",) + cache_key):
                cached = cache.cache_get(cache.TABLES_CACHE, cache_key)
                if cached is None:
                    # 읽기 경로는 슬림 디코드(df=None) — 캐시 메모리 절반 이하 (Phase 5)
                    tables, manifest = download_decode_tables(
                        analysis_key, upload_root, keep_df=False)
                    cache.tables_cache_put(cache_key, tables)
                    return session, [clone_table(t) for t in tables], manifest
        manifest = cache.load_manifest_cached(analysis_key, upload_root)
        return session, [clone_table(t) for t in cached], manifest

    tables, manifest = download_decode_tables(analysis_key, upload_root)
    return session, tables, manifest
