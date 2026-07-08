"""Service layer for /pe/report/upload_webreport and web report rendering."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from werkzeug.utils import secure_filename

from .honeyform import HoneyformTable, decode_honeyform_parquet, encode_honeyform_parquet, split_honeyform
from .metrics import build_report_payload
from .tabs import raw_data as raw_data_tab

# ── decoded tables 인메모리 LRU 캐시 ──────────────────────────────────────────
# parquet decode+split 이 요청당 ~2.4s 로 /full·raw_data·scatter 등 모든 조회의 고정비라
# (analysis_key, content_hash) 키로 캐시한다. raw_data 편집은 content_hash 를 갱신하므로
# 키 자체가 바뀌어 자연 무효화되고, etc/comments 편집은 manifest 만 바꾸므로 캐시가 유효하다
# (manifest 는 여기 캐시하지 않고 매번 재조회한다).
_TABLES_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_TABLES_CACHE", "4") or 4))
_TABLES_CACHE: OrderedDict = OrderedDict()   # (analysis_key, content_hash) -> list[HoneyformTable]
_TABLES_CACHE_LOCK = threading.Lock()        # 아래 파생 캐시 2개도 이 락을 공유 (조작 시간 짧음)

# 파생 결과 캐시 — 동시 사용자 대비 핵심. CPU-bound 재계산(distribution compact 수 초,
# /full payload ~2s)이 GIL 을 잡고 다른 요청까지 밀리게 하므로, 세션당 첫 1회만 계산한다.
_DIST_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_DIST_CACHE", "4") or 4))
_DIST_CACHE: OrderedDict = OrderedDict()     # (analysis_key, content_hash) -> gzip bytes
_REPORT_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_REPORT_CACHE", "8") or 8))
_REPORT_CACHE: OrderedDict = OrderedDict()   # (akey, chash, manifest_digest, incl_dist) -> report dict


def _cache_get(cache: OrderedDict, key):
    with _TABLES_CACHE_LOCK:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
    return value


def _cache_put(cache: OrderedDict, key, value, max_size: int):
    with _TABLES_CACHE_LOCK:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > max_size:
            cache.popitem(last=False)


def _clone_table(t: HoneyformTable) -> HoneyformTable:
    """캐시 원본 보호용 얕은 클론.

    build_report_payload 가 item_columns 를 in-place 필터하므로 리스트/메타 dict 는 복사하고,
    df/data 는 공유한다 — 호출자는 df/data 를 수정하지 않는다는 계약 (편집 경로는
    use_cache=False 로 캐시를 우회한다).
    """
    return HoneyformTable(
        source=t.source, file_name=t.file_name, df=t.df,
        item_columns=list(t.item_columns),
        tno=dict(t.tno), step=dict(t.step), units=dict(t.units),
        hilim=dict(t.hilim), lolim=dict(t.lolim), data=t.data)


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _validate_meta(meta: dict) -> dict:
    return {
        "product_type": str(meta.get("product_type") or "").strip(),
        "product": str(meta.get("product") or "").strip(),
        "lot_id": str(meta.get("lot_id") or "").strip(),
        "revision": str(meta.get("revision") or "").strip()[:80],
        "process": str(meta.get("process") or "").strip()[:80],
        "edm_link": str(meta.get("edm_link") or "").strip()[:500],
        "password": str(meta.get("password") or "").strip(),
        "file_name": secure_filename(str(meta.get("file_name") or "web_report")) or "web_report",
    }


def ingest_webreport(manifest: dict, files: list[dict], *, report_db, upload_root: Path,
                     client_ip: str = "", user_agent: str = "") -> dict:
    meta = _validate_meta(manifest.get("meta") or {})
    sources_manifest = manifest.get("sources") or []
    selected_items = manifest.get("selected_items") or []
    sheets = manifest.get("sheets") or []

    file_hashes = []
    decoded = []
    for idx, item in enumerate(files):
        data = item["data"]
        file_hashes.append(hashlib.sha256(data).hexdigest())
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or item.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or item.get("filename") or source_name)
        df = decode_honeyform_parquet(data)
        decoded.append({
            "source": source_name,
            "file_name": file_name,
            "df": df,
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
    )
    report_db.update_session(
        session_id, analysis_key=analysis_key, content_hash=content_hash, status="done")

    try:
        report_db.log_audit(
            "upload", session_id=session_id, analysis_key=analysis_key,
            product_type=meta["product_type"], product=meta["product"],
            lot_id=meta["lot_id"], file_name=meta["file_name"],
            client_ip=client_ip, user_agent=user_agent)
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
        "web_report_url": f"/pe/report/view/{session_id}",
        "sources": [item["source"] for item in decoded],
        "item_count": len({str(v) for v in selected_items if str(v)}),
        "storage": storage_result["storage"],
    }


def _load_tables(session_id: str, *, report_db, upload_root: Path, use_cache: bool = True):
    """세션 → analysis_key → parquet 원본 디코드 → HoneyformTable 리스트.

    manifest.selected_items 필터는 적용하지 않는다 (build_report_payload 가 이후 그 필터를
    in-place 로 적용하므로, 이 헬퍼는 raw data 조회처럼 전체 item 컬럼이 필요한 호출자에도
    안전하게 재사용된다).

    use_cache=True 면 (analysis_key, content_hash) 키의 LRU 캐시를 사용하고, 반환 tables 는
    캐시 원본의 클론이다 (df/data 공유 — 수정 금지). df 를 수정하는 편집 경로는
    use_cache=False 로 호출할 것. manifest 는 content_hash 없이 바뀔 수 있어(etc/comments)
    캐시하지 않고 매번 재조회한다.
    """
    session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    import storage_gateway

    cache_key = (analysis_key, str(session.get("content_hash") or ""))
    if use_cache:
        cached = _cache_get(_TABLES_CACHE, cache_key)
        if cached is not None:
            manifest = storage_gateway.load_webreport_manifest(analysis_key, upload_root=upload_root)
            return session, [_clone_table(t) for t in cached], manifest

    sources, manifest = storage_gateway.load_webreport_sources(analysis_key, upload_root=upload_root)

    sources_manifest = manifest.get("sources") or []
    tables = []
    for idx, data in enumerate(sources):
        df = decode_honeyform_parquet(data)
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or source_name)
        tables.append(split_honeyform(df, source=source_name, file_name=file_name))

    if use_cache:
        _cache_put(_TABLES_CACHE, cache_key, tables, _TABLES_CACHE_MAX)
        return session, [_clone_table(t) for t in tables], manifest
    return session, tables, manifest


def load_webreport(session_id: str, *, report_db, upload_root: Path) -> tuple[dict, dict]:
    """세션 조회: build_report_payload 결과를 (analysis_key, content_hash, manifest 해시)
    키로 캐시한다 — manifest 해시가 키에 들어가므로 comments/etc 편집은 자연 무효화되고,
    raw_data 편집은 content_hash 변경으로 무효화된다. 반환 report 는 캐시 공유 객체 —
    호출자는 읽기 전용(jsonify 직렬화)으로만 쓸 것.

    Distribution ECDF(대용량)는 항상 payload 에서 제외되고 프런트가 get_distribution 으로
    지연 로드한다.
    """
    session, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)

    cache_key = (session.get("analysis_key"), str(session.get("content_hash") or ""),
                 hashlib.sha256(_canon(manifest)).hexdigest())
    report = _cache_get(_REPORT_CACHE, cache_key)
    if report is None:
        report = build_report_payload(
            tables,
            selected_items=manifest.get("selected_items") or [],
            sheets=manifest.get("sheets") or [],
            etc_items=manifest.get("etc_items") or [],
            issue_comments=manifest.get("issue_comments") or {},
        )
        _cache_put(_REPORT_CACHE, cache_key, report, _REPORT_CACHE_MAX)
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

    _, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
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

    cache_key = (analysis_key, str(session.get("content_hash") or ""))
    blob = _cache_get(_DIST_CACHE, cache_key)
    if blob is not None:
        return blob
    compact = get_distribution(session_id, report_db=report_db, upload_root=upload_root)
    blob = gzip.compress(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        compresslevel=1)
    _cache_put(_DIST_CACHE, cache_key, blob, _DIST_CACHE_MAX)
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

    _, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return _scatter_item(tables, subject)


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
    with _TABLES_CACHE_LOCK:
        for cache in (_TABLES_CACHE, _DIST_CACHE, _REPORT_CACHE):
            for key in [k for k in cache if k[0] == analysis_key]:
                cache.pop(key, None)
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
    """Issue Table ETC 섹션에 ENGR 가 임의로 추가/삭제한 item 이름을 manifest.etc_items 에 반영한다.

    Bin/TNO/Distribution 값 자체는 저장하지 않는다 — item 이름만 기억해두고, 조회할 때마다
    build_issue_table_rows 가 tables/yield_rows 에서 그때그때 다시 채운다. sources 원본은
    불변이므로 manifest 만 재저장한다 (parquet 재업로드 없음, content_hash 도 그대로).
    """
    session, tables, manifest = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    analysis_key = session.get("analysis_key")

    import storage_gateway

    all_items = set()
    for table in tables:
        all_items.update(table.item_columns)

    etc_items = list(manifest.get("etc_items") or [])
    add = str(add or "").strip()
    remove = str(remove or "").strip()
    if add:
        if add not in all_items:
            raise ValueError(f"unknown item: {add}")
        if add not in etc_items:
            etc_items.append(add)
    if remove:
        etc_items = [it for it in etc_items if it != remove]
    manifest["etc_items"] = etc_items

    storage_result = storage_gateway.save_webreport_manifest(
        analysis_key, manifest, upload_root=upload_root)
    try:
        report_db.log_audit(
            "edit", session_id=session_id, analysis_key=analysis_key,
            product_type=session.get("product_type", ""), product=session.get("product", ""),
            lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
            changed_fields=f"issue_table_etc_items(add={add!r},remove={remove!r})",
            client_ip=client_ip, user_agent=user_agent)
    except Exception:
        pass

    return {"ok": True, "etc_items": etc_items, "storage": storage_result["storage"]}


_COMMENT_MAX_ITEMS = 200
_COMMENT_MAX_LEN = 2000


def update_issue_comments(session_id: str, comments: list, *, report_db, upload_root: Path,
                          client_ip: str = "", user_agent: str = "") -> dict:
    """Issue Table 의 PTE/개발 comment 를 manifest.issue_comments 에 저장한다.

    comments: [{"key": row_key, "col": comment 컬럼명, "value": str}, ...].
    row_key 는 tabs/issue_table.py 규칙("Yield|<bin>|<item>", "CPK|<item>", "ETC|<item>")을
    따르고, 빈 value 는 해당 항목 삭제로 처리한다. sources 원본은 불변이므로 manifest 만
    재저장한다 (parquet 재업로드 없음).
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

    import storage_gateway
    manifest = storage_gateway.load_webreport_manifest(analysis_key, upload_root=upload_root)

    saved = dict(manifest.get("issue_comments") or {})
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
        row = dict(saved.get(key) or {})
        if str(row.get(col) or "") == value:
            continue
        if value:
            row[col] = value
        else:
            row.pop(col, None)
        if row:
            saved[key] = row
        else:
            saved.pop(key, None)
        changed += 1
    if changed:
        manifest["issue_comments"] = saved
        storage_result = storage_gateway.save_webreport_manifest(
            analysis_key, manifest, upload_root=upload_root)
        try:
            report_db.log_audit(
                "edit", session_id=session_id, analysis_key=analysis_key,
                product_type=session.get("product_type", ""), product=session.get("product", ""),
                lot_id=session.get("lot_id", ""), file_name=session.get("file_name", ""),
                changed_fields=f"issue_comments({changed} cells)",
                client_ip=client_ip, user_agent=user_agent)
        except Exception:
            pass
        storage = storage_result["storage"]
    else:
        storage = "unchanged"

    return {"ok": True, "updated": changed, "storage": storage}
