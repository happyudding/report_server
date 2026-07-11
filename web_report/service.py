"""Service layer for /pe/report/upload_webreport and web report rendering.

계층 구성 (2026-07-10 분리):
- cache.py      — LRU 캐시 레지스트리·락·무효화 (evict_akey_caches/invalidate_caches)
- validation.py — canon/모드·meta 정규화 등 순수 헬퍼
- loader.py     — 세션 → parquet 다운로드·디코드 → HoneyformTable (tables 캐시 결합)
- service.py    — 업로드 ingest + 조회/편집 오케스트레이션 (외부 진입점, 시그니처 불변)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

from . import cache
from . import disk_cache
from . import edits
from .honeyform import encode_honeyform_parquet
from .loader import load_tables as _load_tables
from .metrics import build_report_payload
from .tabs import raw_data as raw_data_tab
from .validation import (
    canon as _canon,
    client_identity as _client_identity,
    mode_tables as _mode_tables,
    validate_meta as _validate_meta,
    validate_mode as _validate_mode,
    webreport_colors as _webreport_colors,
)


def invalidate_caches(analysis_key) -> None:
    """akey 산출물이 삭제됐을 때(세션 삭제 등) 인메모리 캐시 전부 정리.

    외부(report_routes/report_cleanup/sessions_admin) 진입점 — 구현은 cache.py."""
    cache.invalidate_caches(analysis_key)


def ingest_webreport(manifest: dict, files: list[dict], *, report_db, upload_root: Path,
                     client_ip: str = "", user_agent: str = "") -> dict:
    from .honeyform import decode_split_honeyform_parquet

    meta = _validate_meta(manifest.get("meta") or {})
    mode = _validate_mode(manifest.get("mode"))
    uploaded_by, client_host = _client_identity(manifest)
    sources_manifest = manifest.get("sources") or []
    selected_items = manifest.get("selected_items") or []
    sheets = manifest.get("sheets") or []

    # Compare 모드는 정확히 2개 파일만 허용 (Honey Compare Mode 관례: after/before 2개).
    if mode == "Compare" and len(files) != 2:
        raise ValueError(
            f"Compare 모드는 입력 파일이 2개일 때만 가능합니다 (현재 {len(files)}개)")

    file_hashes = []
    decoded = []
    for idx, item in enumerate(files):
        data = item["data"]
        file_hashes.append(hashlib.sha256(data).hexdigest())
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or item.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or item.get("filename") or source_name)
        # 검증 겸 decode+split — 이 tables 를 아래에서 TABLES_CACHE 에 시딩해
        # prewarm 의 재디코드(파일당 ~1s)를 없앤다.
        table = decode_split_honeyform_parquet(data, source=source_name, file_name=file_name)
        decoded.append({
            "source": source_name,
            "file_name": file_name,
            "table": table,
            "bytes": data,
            "hash": file_hashes[-1],
        })
    if not decoded:
        raise ValueError("no webreport parquet files received")

    key_meta = {k: meta[k] for k in ("product_type", "product", "lot_id")}
    h = hashlib.sha256()
    h.update(_canon({"files": file_hashes, "meta": key_meta, "selected_items": selected_items}))
    analysis_key = h.hexdigest()
    content_hash = hashlib.sha256(_canon({"files": file_hashes})).hexdigest()
    session_id = f"{int(time.time())}_{secrets.token_hex(3)}"

    import storage_gateway
    storage_result = storage_gateway.save_webreport_sources(
        analysis_key, content_hash, [item["bytes"] for item in decoded], manifest,
        upload_root=upload_root)
    cache.manifest_cache_put(analysis_key, manifest)
    # ingest 가 이미 디코드한 tables 를 loader 와 같은 키로 시딩 — prewarm/첫 조회의
    # storage 재다운로드+재디코드 생략. (캐시엔 원본 저장, 소비자는 loader 가 클론 반환.)
    cache.cache_put(cache.TABLES_CACHE, (analysis_key, content_hash),
                    [item["table"] for item in decoded], cache.TABLES_CACHE_MAX)

    session_dir = Path(upload_root) / "web_report" / analysis_key
    report_db.create_session(
        session_id=session_id,
        file_name=meta["file_name"],
        file_path=str(session_dir),
        product_type=meta["product_type"],
        process=meta["process"],
        product=meta["product"],
        revision=meta["revision"],
        edm_link=meta["edm_link"],
        lot_id=meta["lot_id"],
        password=meta["password"],
        source="web_report",
        uploaded_by=uploaded_by or None,
        client_host=client_host or None,
        mode=mode,
    )
    report_db.update_session(
        session_id, analysis_key=analysis_key, content_hash=content_hash, status="done")

    # F10 웹리포트 옵션(Distribution source 색)을 세션에 영속화 — 조회 시 동일 재현용.
    # analysis_key 는 여러 세션이 공유(dedup)할 수 있으나 옵션은 세션 단위이므로 DB 세션행에
    # 저장한다. {"colors":[...]} 형태이며 조회 시 distribution source 색으로 적용된다.
    options = manifest.get("options")
    if isinstance(options, dict) and options:
        try:
            report_db.update_session(
                session_id, webreport_options=json.dumps(options, sort_keys=True))
        except Exception:
            pass

    # manifest 에 편집값(comment/override)이 실려 오면 세션 편집 DB 로 시드 —
    # 이후 manifest 는 불변 스냅샷이고 편집 진실은 DB(세션 단위)다.
    try:
        edits.seed_from_manifest(report_db, session_id, manifest,
                                 updated_by=uploaded_by or None)
    except Exception:
        pass

    try:
        report_db.log_audit(
            "upload", session_id=session_id, analysis_key=analysis_key,
            product_type=meta["product_type"], product=meta["product"],
            lot_id=meta["lot_id"], file_name=meta["file_name"],
            client_ip=client_ip, user_agent=user_agent,
            client_user=uploaded_by or None, client_host=client_host or None)
    except Exception:
        pass

    # 캐시 프리웜: 업로더가 곧바로 여는 첫 조회(cold: parquet decode + payload + dist compact
    # ~10s)를 없애기 위해 백그라운드 데몬 스레드로 미리 계산해 둔다. 실패해도 무해 —
    # 조회 시 다시 계산될 뿐이다.
    def _prewarm():
        try:
            load_webreport(session_id, report_db=report_db, upload_root=upload_root)
            get_distribution_gzip(session_id, report_db=report_db, upload_root=upload_root)
        except Exception:
            pass

    threading.Thread(target=_prewarm, name=f"webreport-prewarm-{session_id}", daemon=True).start()

    return {
        "session_id": session_id,
        "analysis_key": analysis_key,
        "status": "done",
        "mode": mode,
        "web_report_url": f"/pe/report/view/{session_id}",
        "sources": [item["source"] for item in decoded],
        "item_count": len({str(v) for v in selected_items if str(v)}),
        "storage": storage_result["storage"],
    }


def load_webreport(session_id: str, *, report_db, upload_root: Path,
                   session: dict | None = None) -> tuple[dict, dict]:
    """세션 조회: build_report_payload 결과를 (analysis_key, content_hash, session_id,
    edits_rev) 키로 캐시한다 — comment/override 편집은 세션 편집 rev 증가로 자연 무효화되고,
    raw_data 편집은 content_hash 변경으로 무효화된다. 편집 상태는 세션 단위 DB
    (report_webreport_edit)가 진실이며 manifest 는 업로드 시점 불변 스냅샷이다
    (rev==0 legacy 세션만 manifest 필드로 폴백 — edits.effective_state). 반환 report 는
    캐시 공유 객체 — 호출자는 읽기 전용(jsonify 직렬화)으로만 쓸 것. 콜드 미스 계산은
    single-flight 락으로 중복 실행을 막는다. session 은 라우트가 이미 조회한 세션 dict
    전달용(재조회 생략).

    Distribution ECDF(대용량)는 항상 payload 에서 제외되고 프런트가 get_distribution 으로
    지연 로드한다.
    """
    if session is None:
        session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    # 편집 rev 는 작은 인덱스 SELECT 1회 — warm 요청은 manifest/tables 로드 없이
    # 캐시 키만으로 끝난다. 콜드 미스에서만 manifest(캐시)와 tables 를 로드한다.
    edits_rev = report_db.get_webreport_edit_rev(session_id)

    # F10 웹리포트 옵션(세션 DB, authoritative): Distribution source 색.
    # 옵션은 analysis_key 공유(dedup) 세션마다 다를 수 있으므로 report 캐시 키에 포함한다.
    opts_raw = session.get("webreport_options") or ""
    dist_colors = _webreport_colors(opts_raw)
    # 모드는 세션 DB(authoritative). analysis_key 는 여러 세션이 공유(dedup)할 수 있으나
    # 모드는 세션 단위이므로 report 캐시 키에 포함한다.
    mode = _validate_mode(session.get("mode"))

    cache_key = (analysis_key, str(session.get("content_hash") or ""),
                 session_id, edits_rev, opts_raw, mode)
    report = cache.cache_get(cache.REPORT_CACHE, cache_key)
    if report is None:
        with cache.keyed_lock(("report",) + cache_key):
            report = cache.cache_get(cache.REPORT_CACHE, cache_key)
            if report is None:
                # RAM 미스여도 디스크 캐시(재시작·LRU 퇴출 생존)가 있으면 재계산 생략
                report = disk_cache.load_report(upload_root, cache_key)
                if report is None:
                    session, tables, manifest = _load_tables(
                        session_id, report_db=report_db, upload_root=upload_root,
                        session=session)
                    edit_state, _ = edits.effective_state(report_db, session_id, manifest)
                    tables = _mode_tables(tables, mode)
                    report = build_report_payload(
                        tables,
                        selected_items=manifest.get("selected_items") or [],
                        sheets=manifest.get("sheets") or [],
                        etc_items=edit_state["etc_items"],
                        issue_comments=edit_state["issue_comments"],
                        summary_engr=edit_state["summary_engr"],
                        product_type=session.get("product_type", ""),
                        product=session.get("product", ""),
                        mode=mode,
                        dist_colors=dist_colors,
                    )
                    disk_cache.save_report(upload_root, cache_key, report)
                cache.cache_put(cache.REPORT_CACHE, cache_key, report, cache.REPORT_CACHE_MAX)
    public = dict(session)
    public["has_password"] = bool(public.get("password"))
    public.pop("password", None)
    return public, report


def get_distribution(session_id: str, *, report_db, upload_root: Path) -> dict:
    """Distribution lazy 엔드포인트용 컴팩트 ECDF (전 포인트, 다운샘플 없음).

    /full 의 payload 와 동일하게 manifest.selected_items 필터를 적용한다 — 빠뜨리면
    distribution_index 와 항목 집합이 어긋난다. tables 는 캐시 클론이라 필터가 안전하다.
    """
    from .tabs.distribution import build_distribution_compact

    session, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    tables = _mode_tables(tables, _validate_mode(session.get("mode")))
    selected = {str(v) for v in (manifest.get("selected_items") or []) if str(v)}
    if selected:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected]
    all_items = sorted({c for t in tables for c in t.item_columns})
    return build_distribution_compact(tables, all_items)


def get_distribution_gzip(session_id: str, *, report_db, upload_root: Path) -> bytes:
    """get_distribution 결과를 JSON→gzip bytes 로 캐시해 반환 (라우트가 그대로 응답).

    계산(수 초 CPU)+직렬화+압축을 세션당 1회만 수행 — 동시 사용자·재방문 모두 캐시 히트.
    키는 tables 캐시와 동일한 (analysis_key, content_hash) — manifest.selected_items 는
    업로드 시 확정되어 content_hash 와 함께만 바뀌므로 키에 포함하지 않아도 안전하다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    # DUT 모드는 같은 analysis_key 라도 분할된 ECDF 를 내므로 mode 를 키에 포함한다.
    cache_key = (analysis_key, str(session.get("content_hash") or ""),
                 _validate_mode(session.get("mode")))
    blob = cache.cache_get(cache.DIST_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock(("dist",) + cache_key):
        blob = cache.cache_get(cache.DIST_CACHE, cache_key)
        if blob is not None:
            return blob
        # RAM 미스여도 디스크 캐시(재시작·LRU 퇴출 생존)가 있으면 재계산 생략
        blob = disk_cache.load_dist(upload_root, cache_key)
        if blob is None:
            compact = get_distribution(session_id, report_db=report_db, upload_root=upload_root)
            blob = gzip.compress(
                json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                compresslevel=1)
            disk_cache.save_dist(upload_root, cache_key, blob)
        cache.cache_put(cache.DIST_CACHE, cache_key, blob, cache.DIST_CACHE_MAX)
    return blob


def get_raw_data_columns(session_id: str, *, report_db, upload_root: Path) -> dict:
    """Raw Data 탭 컬럼 선택 UI용: item 메타 + source 목록 + 전체 die 수."""
    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return raw_data_tab.build_raw_data_columns(tables)


def query_raw_data(session_id: str, *, report_db, upload_root: Path, columns,
                   search="", bin_filter="", source_filter="") -> dict:
    """Raw Data 탭 조회: 선택된 columns + 필터로 행을 반환 (60개 컬럼 상한, ValueError 로 초과 통지)."""
    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return raw_data_tab.query_raw_data(
        tables, columns=columns, search=search, bin_filter=bin_filter, source_filter=source_filter)


def scatter_item(session_id: str, subject: str, *, report_db, upload_root: Path) -> dict:
    """Distribution 상세용: 항목의 소스별 전체 측정값(다운샘플 없음) + cpk/status 지연 로드.

    항목이 어떤 소스에도 없으면 KeyError (라우트가 404 처리).
    """
    from .tabs.distribution import scatter_item as _scatter_item

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    tables = _mode_tables(tables, _validate_mode(session.get("mode")))
    return _scatter_item(tables, subject)


def _commonality_index(session: dict, tables):
    """Commonality 인덱스(메타 리스트 + item별 정렬 배열)를 세션 단위로 캐시해 반환.

    키는 tables 캐시와 동일한 (analysis_key, content_hash) — raw_data 편집 시 content_hash
    변경으로 자연 무효화되고, AKEY_CACHES 등록으로 세션 삭제 시에도 정리된다.
    콜드 미스(전 item 정렬, 수 초 CPU)는 single-flight 락으로 중복 계산을 막는다.
    """
    from .tabs.commonality import build_index

    cache_key = (session.get("analysis_key"), str(session.get("content_hash") or ""))
    idx = cache.cache_get(cache.COMMONALITY_CACHE, cache_key)
    if idx is None:
        with cache.keyed_lock(("commonality",) + cache_key):
            idx = cache.cache_get(cache.COMMONALITY_CACHE, cache_key)
            if idx is None:
                idx = build_index(tables)
                cache.cache_put(cache.COMMONALITY_CACHE, cache_key, idx,
                                cache.COMMONALITY_CACHE_MAX)
    return idx


def commonality_chips(session_id: str, *, report_db, upload_root: Path,
                      q: str = "", limit: int = 300,
                      serial: str = "", xpos: str = "", ypos: str = "") -> dict:
    """Commonality chip 검색: serial/xpos/ypos 개별 칸(AND) 또는 q(OR, dut 포함) 부분일치 후보 목록."""
    from .tabs.commonality import search_chips

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return search_chips(tables, q=q, limit=limit,
                        index=_commonality_index(session, tables),
                        serial=serial, xpos=xpos, ypos=ypos)


def commonality_chip(session_id: str, *, report_db, upload_root: Path,
                     serial: str = "", xpos: str = "", ypos: str = "", source: str = "") -> dict:
    """선택 chip 의 항목별 값 + 누적%(ECDF 위치) + wafer 좌표. 못 찾으면 KeyError."""
    from .tabs.commonality import chip_percentiles

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return chip_percentiles(tables, serial=serial, xpos=xpos, ypos=ypos, source=source,
                            index=_commonality_index(session, tables))


def edit_raw_data(session_id: str, *, report_db, upload_root: Path, edits: list,
                  client_ip: str = "", user_agent: str = "") -> dict:
    """Raw Data 셀 편집을 저장된 parquet 원본에 그대로 반영한다.

    버전관리·undo 없음 — 편집된 source 는 df 기준으로 재인코딩해 기존 analysis_key 의
    web_report_source_<idx> 를 덮어쓴다 (Honey 재업로드 전까지 이전 값은 복구 불가).
    """
    # apply_raw_data_edits 가 df 를 in-place 수정하므로 캐시 원본 오염 방지 위해 캐시 우회
    session, tables, manifest = _load_tables(
        session_id, report_db=report_db, upload_root=upload_root, use_cache=False)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    updated_tables = raw_data_tab.apply_raw_data_edits(tables, edits)
    sources_bytes = [encode_honeyform_parquet(t.df) for t in updated_tables]

    content_hash = hashlib.sha256(
        _canon({"files": [hashlib.sha256(b).hexdigest() for b in sources_bytes]})
    ).hexdigest()

    import storage_gateway
    storage_result = storage_gateway.save_webreport_sources(
        analysis_key, content_hash, sources_bytes, manifest, upload_root=upload_root)

    report_db.update_session(session_id, content_hash=content_hash)
    # 구 content_hash 키 엔트리는 더 이상 조회되지 않으므로 메모리 회수용으로만 정리
    cache.evict_akey_caches(analysis_key)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"raw_data({len(edits)} cells)",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {"ok": True, "edited_cells": len(edits), "storage": storage_result["storage"]}


def update_issue_etc_items(session_id: str, *, report_db, upload_root: Path,
                           add: str = "", remove: str = "",
                           client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table ETC 섹션에 ENGR 가 임의로 추가/삭제한 item 이름을 세션 편집 DB
    (report_webreport_edit, kind=etc_item)에 반영한다. manifest 는 불변 스냅샷.

    Bin/TNO/Distribution 값 자체는 저장하지 않는다 — item 이름만 기억해두고, 조회할 때마다
    build_issue_table_rows 가 tables/yield_rows 에서 그때그때 다시 채운다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    add = str(add or "").strip()
    remove = str(remove or "").strip()
    if add and len(add) > 120:
        raise ValueError("item name too long (max 120 chars)")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    etc_items = edits.load_edit_state(report_db, session_id)["etc_items"]
    changes = []
    if add and add not in etc_items:
        # 측정항목이 아닌 자유입력 Engr item(Item명 직접 타이핑)도 허용한다 — 이 경우
        # Bin/TNO/Distribution 은 매칭 데이터가 없어 조회 시 빈 칸으로 채워진다.
        changes.append((edits.KIND_ETC_ITEM, add, ""))
        etc_items.append(add)
    if remove and remove in etc_items:
        changes.append((edits.KIND_ETC_ITEM, remove, None))
        etc_items = [it for it in etc_items if it != remove]
    if changes:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"issue_table_etc_items(add={add!r},remove={remove!r})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {"ok": True, "etc_items": etc_items,
            "storage": "db" if changes else "unchanged"}


_COMMENT_MAX_ITEMS = 200
_COMMENT_MAX_LEN = 2000


def update_issue_comments(session_id: str, comments: list, *, report_db, upload_root: Path,
                          client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 의 PTE/개발 comment 를 세션 편집 DB(kind=issue_comment)에 저장한다.
    manifest 는 불변 스냅샷.

    comments: [{"key": row_key, "col": comment 컬럼명, "value": str}, ...].
    row_key 는 tabs/issue_table.py 규칙("Yield|<bin>|<item>", "CPK|<item>", "ETC|<item>")을
    따르고, 빈 value 는 해당 항목 삭제로 처리한다.
    """
    from .tabs.issue_table import COMMENT_COLS

    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    if not isinstance(comments, list):
        raise ValueError("comments must be a list")
    if len(comments) > _COMMENT_MAX_ITEMS:
        raise ValueError(f"too many comment entries ({len(comments)} > {_COMMENT_MAX_ITEMS})")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    saved = edits.load_edit_state(report_db, session_id)["issue_comments"]
    changes = []
    changed = 0
    for entry in comments:
        entry = entry or {}
        key = str(entry.get("key") or "").strip()
        col = str(entry.get("col") or "")
        value = str(entry.get("value") or "").strip()
        if not key or len(key) > 300:
            raise ValueError(f"invalid comment key: {key!r}")
        if col not in COMMENT_COLS:
            raise ValueError(f"unknown comment column: {col!r}")
        if len(value) > _COMMENT_MAX_LEN:
            raise ValueError(f"comment too long ({len(value)} > {_COMMENT_MAX_LEN} chars)")
        row = saved.get(key) or {}
        if str(row.get(col) or "") == value:
            continue
        changes.append((edits.KIND_ISSUE_COMMENT, edits.comment_key(key, col),
                        value if value else None))
        changed += 1
    if changed:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
        try:
            report_db.log_audit(
                "edit", session_id=session_id, analysis_key=analysis_key,
                product_type=session.get("product_type", ""), product=session.get("product", ""),
                lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
                changed_fields=f"issue_comments({changed} cells)",
                client_ip=client_ip, user_agent=user_agent)
        except Exception:
            pass
        storage = "db"
    else:
        storage = "unchanged"

    return {"ok": True, "updated": changed, "storage": storage}


_ENGR_KEYS = ("yield", "cpk", "etc")


def update_summary_engr(session_id: str, values: dict, *, report_db, upload_root: Path,
                        client_ip: str = "", user_agent: str = "") -> dict:
    """Summary 탭의 Engr Comment(Yield/CPK/ETC 3칸)를 세션 편집 DB(kind=summary_engr)에
    저장한다. manifest 는 불변 스냅샷.

    values: {"yield": str, "cpk": str, "etc": str} 중 온 키만 갱신하고, 빈 값은 삭제로
    처리한다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    if not isinstance(values, dict):
        raise ValueError("values must be an object")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    saved = edits.load_edit_state(report_db, session_id)["summary_engr"]
    changes = []
    changed = 0
    for key in _ENGR_KEYS:
        if key not in values:
            continue
        val = str(values.get(key) or "").strip()
        if len(val) > _COMMENT_MAX_LEN:
            raise ValueError(f"comment too long ({len(val)} > {_COMMENT_MAX_LEN} chars)")
        if str(saved.get(key) or "") == val:
            continue
        changes.append((edits.KIND_SUMMARY_ENGR, key, val if val else None))
        if val:
            saved[key] = val
        else:
            saved.pop(key, None)
        changed += 1
    if changed:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
        try:
            report_db.log_audit(
                "edit", session_id=session_id, analysis_key=analysis_key,
                product_type=session.get("product_type", ""), product=session.get("product", ""),
                lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
                changed_fields=f"summary_engr({changed} fields)",
                client_ip=client_ip, user_agent=user_agent)
        except Exception:
            pass
        storage = "db"
    else:
        storage = "unchanged"

    return {"ok": True, "updated": changed, "summary_engr": saved, "storage": storage}


# ── Trim Analysis (lazy — 탭 진입 시에만 계산, 세션 open 비용 없음) ────────────

_TRIM_SLOTS = ("INIT", "CODE", "TRIM", "VERIFY", "MEMBER")
_TRIM_OVERRIDE_MAX = 500
_TRIM_NAME_MAX = 200


def get_trim_analysis_gzip(session_id: str, *, report_db, upload_root: Path,
                           source: str = "") -> tuple[bytes, str]:
    """Trim Analysis 탭 payload(항목 매칭 + 그룹 통계/shift)를 JSON→gzip bytes 로 캐시해 반환.

    반환은 (gzip bytes, etag token) — token 은 라우트 ETag 용이다 (trim_overrides
    편집 직후 stale 304 방지). overrides 는 세션 편집 DB 가 진실이라 캐시 키·token 에
    (session_id, edits_rev)가 들어가 저장 시 자연 무효화된다. product_type 은
    analysis_key 산출 meta 에 이미 포함되어 키에 안 넣는다.
    """
    from .tabs.trim_analysis import build_trim_payload

    session, tables, manifest = _load_tables(
        session_id, report_db=report_db, upload_root=upload_root)
    analysis_key = session.get("analysis_key")
    edit_state, edits_rev = edits.effective_state(report_db, session_id, manifest)
    etag_token = hashlib.sha256(f"{session_id}:{edits_rev}".encode("utf-8")).hexdigest()
    mode = _validate_mode(session.get("mode"))

    cache_key = (analysis_key, str(session.get("content_hash") or ""),
                 session_id, edits_rev, mode, str(source or ""))
    blob = cache.cache_get(cache.TRIM_CACHE, cache_key)
    if blob is not None:
        return blob, etag_token
    with cache.keyed_lock(("trim",) + cache_key):
        blob = cache.cache_get(cache.TRIM_CACHE, cache_key)
        if blob is None:
            tables = _mode_tables(tables, mode)
            selected = {str(v) for v in (manifest.get("selected_items") or []) if str(v)}
            if selected:
                for table in tables:
                    table.item_columns = [c for c in table.item_columns if c in selected]
            payload = build_trim_payload(
                tables, str(source or ""), edit_state["trim_overrides"],
                session.get("product_type", ""))
            blob = gzip.compress(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                compresslevel=1)
            cache.cache_put(cache.TRIM_CACHE, cache_key, blob, cache.TRIM_CACHE_MAX)
    return blob, etag_token


def get_trim_chart_gzip(session_id: str, *, report_db, upload_root: Path,
                        source: str = "", group_id: str = "") -> bytes:
    """Trim 그룹 1개의 chip-to-chip 차트 payload 를 gzip bytes 로 캐시해 반환.

    그룹 재도출(build_groups)은 문자열 연산(ms 단위)이라 요청마다 수행하고, 캐시 키는
    슬롯 구성 digest — overrides 편집이 구성을 바꾸지 않은 그룹의 차트는 캐시가 살아있다.
    그룹/소스가 없으면 KeyError (라우트 404).
    """
    from .tabs.trim_analysis import _select_table, build_trim_chart
    from .trim_match import build_groups, rule_set_for

    session, tables, manifest = _load_tables(
        session_id, report_db=report_db, upload_root=upload_root)
    analysis_key = session.get("analysis_key")
    mode = _validate_mode(session.get("mode"))
    tables = _mode_tables(tables, mode)
    selected = {str(v) for v in (manifest.get("selected_items") or []) if str(v)}
    if selected:
        for table in tables:
            table.item_columns = [c for c in table.item_columns if c in selected]

    table = _select_table(tables, str(source or ""))
    product_type = session.get("product_type", "")
    rule_set = rule_set_for(product_type)
    edit_state, _ = edits.effective_state(report_db, session_id, manifest)
    match = build_groups(table.item_columns,
                         overrides=edit_state["trim_overrides"],
                         rule_set=rule_set, product_type=product_type)
    group = next((g for g in match["groups"] if g["id"] == str(group_id)), None)
    if group is None:
        raise KeyError(str(group_id))

    items_digest = hashlib.sha256(_canon({"slots": group["slots"]})).hexdigest()[:16]
    cache_key = (analysis_key, str(session.get("content_hash") or ""), mode,
                 table.source, items_digest)
    blob = cache.cache_get(cache.TRIM_CHART_CACHE, cache_key)
    if blob is not None:
        return blob
    with cache.keyed_lock(("trim_chart",) + cache_key):
        blob = cache.cache_get(cache.TRIM_CHART_CACHE, cache_key)
        if blob is None:
            chart = build_trim_chart(table, group, rule_set)
            blob = gzip.compress(
                json.dumps(chart, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                compresslevel=1)
            cache.cache_put(cache.TRIM_CHART_CACHE, cache_key, blob,
                            cache.TRIM_CHART_CACHE_MAX)
    return blob


def update_trim_overrides(session_id: str, ops: list, *, report_db, upload_root: Path,
                          client_ip: str = "", user_agent: str = "") -> dict:
    """Trim Analysis 수동 재배치(드래그앤드랍)를 manifest.trim_overrides 에 병합 저장한다.

    ops: [{"item": 항목명, "group": 그룹 id, "slot": INIT|CODE|TRIM|VERIFY|MEMBER} |
          {"item": 항목명, "reset": true}]. reset 은 해당 override 삭제(자동 매칭 복귀).
    수정본은 자동 매칭 결과보다 우선 적용된다(적용 자체는 trim_match._apply_overrides).
    세션 편집 DB(kind=trim_override)에 저장하며 manifest 는 불변 스냅샷이다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)
    if not isinstance(ops, list):
        raise ValueError("ops must be a list")
    if len(ops) > _TRIM_OVERRIDE_MAX:
        raise ValueError(f"too many override entries ({len(ops)} > {_TRIM_OVERRIDE_MAX})")

    # legacy 미이전 세션이면 manifest 편집값을 먼저 세션 편집행으로 복사 (연속성 보존)
    edits.ensure_seeded(report_db, session_id,
                        lambda: cache.load_manifest_cached(analysis_key, upload_root))
    saved = edits.load_edit_state(report_db, session_id)["trim_overrides"]
    changes = []
    changed = 0
    for entry in ops:
        entry = entry or {}
        item = str(entry.get("item") or "").strip()
        if not item or len(item) > _TRIM_NAME_MAX:
            raise ValueError(f"invalid item name: {item!r}")
        if entry.get("reset"):
            if saved.pop(item, None) is not None:
                changes.append((edits.KIND_TRIM_OVERRIDE, item, None))
                changed += 1
            continue
        slot = str(entry.get("slot") or "").strip().upper()
        group = str(entry.get("group") or "").strip().upper()
        if slot not in _TRIM_SLOTS:
            raise ValueError(f"unknown slot: {slot!r}")
        if not group or len(group) > _TRIM_NAME_MAX:
            raise ValueError(f"invalid group name: {group!r}")
        spec = {"group": group, "slot": slot}
        if saved.get(item) == spec:
            continue
        saved[item] = spec
        changes.append((edits.KIND_TRIM_OVERRIDE, item,
                        json.dumps(spec, sort_keys=True, ensure_ascii=False)))
        changed += 1
    if len(saved) > _TRIM_OVERRIDE_MAX:
        raise ValueError(f"too many overrides stored ({len(saved)} > {_TRIM_OVERRIDE_MAX})")

    if changed:
        report_db.apply_webreport_edits(session_id, changes,
                                        updated_by=edits.user_from_ua(user_agent) or None)
        try:
            report_db.log_audit(
                "edit", session_id=session_id, analysis_key=analysis_key,
                product_type=session.get("product_type", ""), product=session.get("product", ""),
                lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
                changed_fields=f"trim_overrides({changed} items)",
                client_ip=client_ip, user_agent=user_agent)
        except Exception:
            pass
        storage = "db"
    else:
        storage = "unchanged"

    return {"ok": True, "updated": changed, "overrides": saved, "storage": storage}
