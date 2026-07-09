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
# (manifest 는 아래 _MANIFEST_CACHE 에 별도 캐시, 편집 시 write-through 갱신).
_TABLES_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_TABLES_CACHE", "4") or 4))
_TABLES_CACHE: OrderedDict = OrderedDict()   # (analysis_key, content_hash) -> list[HoneyformTable]
_TABLES_CACHE_LOCK = threading.Lock()        # 아래 파생 캐시 2개도 이 락을 공유 (조작 시간 짧음)

# 파생 결과 캐시 — 동시 사용자 대비 핵심. CPU-bound 재계산(distribution compact 수 초,
# /full payload ~2s)이 GIL 을 잡고 다른 요청까지 밀리게 하므로, 세션당 첫 1회만 계산한다.
_DIST_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_DIST_CACHE", "4") or 4))
_DIST_CACHE: OrderedDict = OrderedDict()     # (analysis_key, content_hash) -> gzip bytes
_REPORT_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_REPORT_CACHE", "8") or 8))
_REPORT_CACHE: OrderedDict = OrderedDict()   # (akey, chash, manifest_digest, incl_dist) -> report dict

# Commonality 인덱스 캐시 — chip 검색(키스트로크)·백분위(chip 클릭)가 매번 전 item 컬럼을
# 재변환하던 유일한 무캐시 heavy 경로였다. 메타 리스트 + item별 정렬 배열을 세션 단위로 보관.
_COMMONALITY_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_COMMONALITY_CACHE", "2") or 2))
_COMMONALITY_CACHE: OrderedDict = OrderedDict()  # (analysis_key, content_hash) -> build_index 결과

# manifest 인메모리 캐시 — warm 조회(/full·raw_data 등)마다 발생하던 S3 manifest GET 왕복 제거.
# 단일 프로세스(waitress 1 process) 전제: manifest 를 바꾸는 코드가 전부 이 모듈이라
# 저장 성공 직후 write-through(_manifest_cache_put) 로 일관성이 유지된다. 값은 canonical
# JSON bytes 로 보관하고 조회마다 json.loads 로 새 dict 를 만들어 호출자의 in-place 수정이
# 캐시를 오염시키지 않는다.
_MANIFEST_CACHE_MAX = max(1, int(os.getenv("WEB_REPORT_MANIFEST_CACHE", "16") or 16))
_MANIFEST_CACHE: OrderedDict = OrderedDict()  # analysis_key -> (canonical bytes, sha256 digest)

# analysis_key 를 키 첫 요소로 쓰는 캐시 레지스트리 — 무효화(invalidate_caches, edit_raw_data)가
# 이 리스트를 순회한다. 파생 캐시를 새로 만들면 여기 append 만 하면 무효화에 자동 편입된다
# (response_cache.py 가 import 시 자기 캐시를 등록).
_AKEY_CACHES: list = [_TABLES_CACHE, _DIST_CACHE, _REPORT_CACHE, _COMMONALITY_CACHE]

# 콜드 캐시 동시 진입(stampede) 방지 single-flight 락 — 캐시에 없는 같은 세션을 여러
# 사용자가 동시에 열면 수 초짜리 CPU-bound 계산이 중복 실행되며 GIL 로 서로 밀어내므로,
# 같은 (종류, akey, chash) 계산은 한 스레드만 수행하고 나머지는 대기 후 캐시를 재확인한다.
_KEYED_LOCKS: OrderedDict = OrderedDict()
_KEYED_LOCKS_MAX = 32


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


def _keyed_lock(key) -> threading.Lock:
    """(종류, ...캐시키) 단위 락을 돌려준다. 레지스트리는 LRU 로 상한 유지."""
    with _TABLES_CACHE_LOCK:
        lock = _KEYED_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEYED_LOCKS[key] = lock
        _KEYED_LOCKS.move_to_end(key)
        while len(_KEYED_LOCKS) > _KEYED_LOCKS_MAX:
            _KEYED_LOCKS.popitem(last=False)
    return lock


def _manifest_cache_put(analysis_key, manifest: dict):
    if analysis_key:
        blob = _canon(manifest)
        _cache_put(_MANIFEST_CACHE, analysis_key,
                   (blob, hashlib.sha256(blob).hexdigest()), _MANIFEST_CACHE_MAX)


def invalidate_caches(analysis_key) -> None:
    """akey 산출물이 삭제됐을 때(세션 삭제 등) 인메모리 캐시 전부 정리 — 메모리 회수 +
    stale manifest 재사용 방지."""
    if not analysis_key:
        return
    with _TABLES_CACHE_LOCK:
        for cache in _AKEY_CACHES:
            for key in [k for k in cache if k[0] == analysis_key]:
                cache.pop(key, None)
        _MANIFEST_CACHE.pop(analysis_key, None)


def _load_manifest_with_digest(analysis_key, upload_root: Path) -> tuple[dict, str]:
    """(manifest dict, canonical digest) 를 단일 캐시 읽기로 반환.

    digest 는 캐시 엔트리에 동봉돼 있어 warm 요청마다 _canon+sha256 을 재계산하지 않고,
    manifest 와 digest 가 항상 같은 엔트리에서 나와 편집 경합 시에도 짝이 어긋나지 않는다.
    """
    entry = _cache_get(_MANIFEST_CACHE, analysis_key)
    if entry is None:
        import storage_gateway
        manifest = storage_gateway.load_webreport_manifest(analysis_key, upload_root=upload_root)
        blob = _canon(manifest)
        entry = (blob, hashlib.sha256(blob).hexdigest())
        _cache_put(_MANIFEST_CACHE, analysis_key, entry, _MANIFEST_CACHE_MAX)
        return manifest, entry[1]
    return json.loads(entry[0].decode("utf-8")), entry[1]


def _load_manifest_cached(analysis_key, upload_root: Path) -> dict:
    return _load_manifest_with_digest(analysis_key, upload_root)[0]


def _clone_table(t: HoneyformTable) -> HoneyformTable:
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


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def _webreport_colors(opts_raw: str):
    """세션의 webreport_options JSON → Distribution source 색 팔레트.

    반환 None → 색 미지정(legacy) → 프런트가 기본 팔레트(DIST_PALETTE) 사용.
    반환 list → 색 hex 리스트. distribution source i 가 리스트[i] 색을 쓴다.
    """
    if not opts_raw:
        return None
    try:
        opts = json.loads(opts_raw)
    except Exception:
        return None
    if not isinstance(opts, dict):
        return None
    colors = opts.get("colors")
    return [str(c) for c in colors] if isinstance(colors, list) and colors else None


WEB_REPORT_MODES = ("Normal", "Compare", "DUT", "Commonality")


def _validate_mode(value) -> str:
    """manifest.mode 를 허용 모드 중 하나로 정규화. 미지정/불명은 'Normal'."""
    mode = str(value or "").strip()
    return mode if mode in WEB_REPORT_MODES else "Normal"


def _mode_tables(tables, mode):
    """세션 모드에 따라 분석용 tables 를 변형한다.

    DUT 모드는 업로드된 단일 source 를 honeyform 의 DUT 컬럼으로 분할해 DUT별 pseudo-source
    리스트로 만든다 (클라가 아니라 서버에서 분할 — df_honey→honeyform 포맷 변환 회피).
    Normal/Compare/Commonality 는 tables 를 그대로 쓴다. 반환 tables 는 새 객체(또는 원본
    클론)이므로 이후 in-place item 필터가 캐시 원본을 오염시키지 않는다.
    """
    if mode == "DUT" and len(tables) == 1:
        from .honeyform import split_table_by_dut
        return split_table_by_dut(tables[0])
    return tables


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


def _client_identity(manifest: dict) -> tuple[str, str]:
    """manifest["client"] 에서 클라이언트 신고 신원 추출 → (uploaded_by, client_host).

    구버전 클라이언트(키 없음)는 ("", "") — 하위호환. 클라 신고값이라 위조 가능
    (사내망 감사 용도). analysis_key 산출에는 포함되지 않는다.
    """
    info = manifest.get("client") or {}
    if not isinstance(info, dict):
        return "", ""
    user = str(info.get("user") or "").strip()[:80]
    host = str(info.get("host") or "").strip()[:80]
    domain = str(info.get("domain") or "").strip()[:80]
    # 도메인 미가입 PC 는 USERDOMAIN == 호스트명 — 중복 표기 생략
    if domain and user and domain.lower() != host.lower():
        uploaded_by = f"{domain}\\{user}"
    else:
        uploaded_by = user
    return uploaded_by, host


def ingest_webreport(manifest: dict, files: list[dict], *, report_db, upload_root: Path,
                     client_ip: str = "", user_agent: str = "") -> dict:
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
    _manifest_cache_put(analysis_key, manifest)

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


def _download_decode_tables(analysis_key, upload_root: Path):
    """parquet 원본 다운로드 + 디코드. 반환 (tables, manifest).

    sources 와 함께 받은 manifest 를 manifest 캐시에 write-through 해 이어지는 warm 조회의
    S3 manifest GET 을 없앤다.
    """
    import storage_gateway

    sources, manifest = storage_gateway.load_webreport_sources(analysis_key, upload_root=upload_root)
    _manifest_cache_put(analysis_key, manifest)

    sources_manifest = manifest.get("sources") or []
    tables = []
    for idx, data in enumerate(sources):
        df = decode_honeyform_parquet(data)
        source_info = sources_manifest[idx] if idx < len(sources_manifest) else {}
        source_name = str(source_info.get("name") or f"source_{idx + 1}")
        file_name = str(source_info.get("file_name") or source_name)
        tables.append(split_honeyform(df, source=source_name, file_name=file_name))
    return tables, manifest


def _load_tables(session_id: str, *, report_db, upload_root: Path, use_cache: bool = True,
                 session: dict | None = None):
    """세션 → analysis_key → parquet 원본 디코드 → HoneyformTable 리스트.

    manifest.selected_items 필터는 적용하지 않는다 (build_report_payload 가 이후 그 필터를
    in-place 로 적용하므로, 이 헬퍼는 raw data 조회처럼 전체 item 컬럼이 필요한 호출자에도
    안전하게 재사용된다).

    use_cache=True 면 (analysis_key, content_hash) 키의 LRU 캐시를 사용하고, 반환 tables 는
    캐시 원본의 클론이다 (df/data 공유 — 수정 금지). df 를 수정하는 편집 경로는
    use_cache=False 로 호출할 것. 콜드 미스는 single-flight 락으로 같은 키의 다운로드+디코드를
    한 스레드만 수행한다. manifest 는 content_hash 없이 바뀔 수 있어(etc/comments) 별도
    _MANIFEST_CACHE 에 두고 편집 시 write-through 로 갱신한다.

    session: 호출자(라우트)가 이미 조회한 세션 dict 를 주면 재조회를 생략한다.
    """
    if session is None:
        session = report_db.get_session(session_id)
    if not session:
        raise KeyError(session_id)
    analysis_key = session.get("analysis_key")
    if not analysis_key:
        raise FileNotFoundError(session_id)

    cache_key = (analysis_key, str(session.get("content_hash") or ""))
    if use_cache:
        cached = _cache_get(_TABLES_CACHE, cache_key)
        if cached is None:
            with _keyed_lock(("tables",) + cache_key):
                cached = _cache_get(_TABLES_CACHE, cache_key)
                if cached is None:
                    tables, manifest = _download_decode_tables(analysis_key, upload_root)
                    _cache_put(_TABLES_CACHE, cache_key, tables, _TABLES_CACHE_MAX)
                    return session, [_clone_table(t) for t in tables], manifest
        manifest = _load_manifest_cached(analysis_key, upload_root)
        return session, [_clone_table(t) for t in cached], manifest

    tables, manifest = _download_decode_tables(analysis_key, upload_root)
    return session, tables, manifest


def load_webreport(session_id: str, *, report_db, upload_root: Path,
                   session: dict | None = None) -> tuple[dict, dict]:
    """세션 조회: build_report_payload 결과를 (analysis_key, content_hash, manifest 해시)
    키로 캐시한다 — manifest 해시가 키에 들어가므로 comments/etc 편집은 자연 무효화되고,
    raw_data 편집은 content_hash 변경으로 무효화된다. 반환 report 는 캐시 공유 객체 —
    호출자는 읽기 전용(jsonify 직렬화)으로만 쓸 것. 콜드 미스 계산은 single-flight 락으로
    중복 실행을 막는다. session 은 라우트가 이미 조회한 세션 dict 전달용(재조회 생략).

    Distribution ECDF(대용량)는 항상 payload 에서 제외되고 프런트가 get_distribution 으로
    지연 로드한다.
    """
    session, tables, manifest = _load_tables(
        session_id, report_db=report_db, upload_root=upload_root, session=session)
    # manifest digest 는 캐시 엔트리에 동봉된 값을 재사용 (warm 요청마다 재해싱 방지).
    # _load_tables 가 write-through 했으므로 사실상 dict 조회 1회 — manifest 도 같은
    # 엔트리에서 다시 받아 digest 와 짝을 맞춘다.
    manifest, manifest_digest = _load_manifest_with_digest(
        session.get("analysis_key"), upload_root)

    # F10 웹리포트 옵션(세션 DB, authoritative): Distribution source 색.
    # 옵션은 analysis_key 공유(dedup) 세션마다 다를 수 있으므로 report 캐시 키에 포함한다.
    opts_raw = session.get("webreport_options") or ""
    dist_colors = _webreport_colors(opts_raw)
    # 모드는 세션 DB(authoritative). analysis_key 는 여러 세션이 공유(dedup)할 수 있으나
    # 모드는 세션 단위이므로 report 캐시 키에 포함한다.
    mode = _validate_mode(session.get("mode"))
    tables = _mode_tables(tables, mode)

    cache_key = (session.get("analysis_key"), str(session.get("content_hash") or ""),
                 manifest_digest, opts_raw, mode)
    report = _cache_get(_REPORT_CACHE, cache_key)
    if report is None:
        with _keyed_lock(("report",) + cache_key):
            report = _cache_get(_REPORT_CACHE, cache_key)
            if report is None:
                report = build_report_payload(
                    tables,
                    selected_items=manifest.get("selected_items") or [],
                    sheets=manifest.get("sheets") or [],
                    etc_items=manifest.get("etc_items") or [],
                    issue_comments=manifest.get("issue_comments") or {},
                    product_type=session.get("product_type", ""),
                    product=session.get("product", ""),
                    mode=mode,
                    dist_colors=dist_colors,
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
    blob = _cache_get(_DIST_CACHE, cache_key)
    if blob is not None:
        return blob
    with _keyed_lock(("dist",) + cache_key):
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

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    tables = _mode_tables(tables, _validate_mode(session.get("mode")))
    return _scatter_item(tables, subject)


def _commonality_index(session: dict, tables):
    """Commonality 인덱스(메타 리스트 + item별 정렬 배열)를 세션 단위로 캐시해 반환.

    키는 tables 캐시와 동일한 (analysis_key, content_hash) — raw_data 편집 시 content_hash
    변경으로 자연 무효화되고, _AKEY_CACHES 등록으로 세션 삭제 시에도 정리된다.
    콜드 미스(전 item 정렬, 수 초 CPU)는 single-flight 락으로 중복 계산을 막는다.
    """
    from .tabs.commonality import build_index

    cache_key = (session.get("analysis_key"), str(session.get("content_hash") or ""))
    idx = _cache_get(_COMMONALITY_CACHE, cache_key)
    if idx is None:
        with _keyed_lock(("commonality",) + cache_key):
            idx = _cache_get(_COMMONALITY_CACHE, cache_key)
            if idx is None:
                idx = build_index(tables)
                _cache_put(_COMMONALITY_CACHE, cache_key, idx, _COMMONALITY_CACHE_MAX)
    return idx


def commonality_chips(session_id: str, *, report_db, upload_root: Path,
                      q: str = "", limit: int = 300) -> dict:
    """Commonality chip 검색: serial/xpos/ypos/dut 부분일치 후보 목록."""
    from .tabs.commonality import search_chips

    session, tables, _ = _load_tables(session_id, report_db=report_db, upload_root=upload_root)
    return search_chips(tables, q=q, limit=limit,
                        index=_commonality_index(session, tables))


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
    with _TABLES_CACHE_LOCK:
        for cache in _AKEY_CACHES:
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

    etc_items = list(manifest.get("etc_items") or [])
    add = str(add or "").strip()
    remove = str(remove or "").strip()
    if add:
        # 측정항목이 아닌 자유입력 Engr item(Item명 직접 타이핑)도 허용한다 — 이 경우
        # Bin/TNO/Distribution 은 매칭 데이터가 없어 조회 시 빈 칸으로 채워진다.
        if len(add) > 120:
            raise ValueError("item name too long (max 120 chars)")
        if add not in etc_items:
            etc_items.append(add)
    if remove:
        etc_items = [it for it in etc_items if it != remove]
    manifest["etc_items"] = etc_items

    storage_result = storage_gateway.save_webreport_manifest(
        analysis_key, manifest, upload_root=upload_root)
    _manifest_cache_put(analysis_key, manifest)
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
    manifest = _load_manifest_cached(analysis_key, upload_root)

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
        _manifest_cache_put(analysis_key, manifest)
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
