"""관리자 스토리지 뷰 — 서버가 점유한 로컬 데이터를 범주별/세션별로 집계·정렬.

S3 저장 산출물의 바이트 크기는 집계하지 않는다(로컬 디스크만) — S3 저장 세션은
백엔드 표시만 하고 용량 컬럼엔 로컬 폴백/캐시 실측만 반영한다. 삭제는 라우트가
sessions_admin.bulk_delete(artifact-aware) 를 그대로 재사용하므로 여기엔 없다.

디렉토리 재귀 스캔이 느릴 수 있어 analysis_key 별 크기 맵을 TTL 캐시(기본 60초)로 감싸고
refresh 인자로 우회한다 (sysinfo._dir_size_cached 와 동일 패턴).
"""
import json
import logging
import os
import threading
import time
from pathlib import Path

import config
import storage_gateway
from database import report_db

_log = logging.getLogger(__name__)

# ── 로컬 산출물 크기 스캔 (analysis_key 별 + 범주 총계) ────────────────────────
_scan_lock = threading.Lock()
_scan_cache = None      # (ts, dict)
_SCAN_TTL = 60.0


def _scandir(path):
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _dir_size(path):
    """recursive (bytes, files). 접근 실패 항목은 건너뜀 (sysinfo._dir_size 와 동일)."""
    total = files = 0
    stack = [path]
    while stack:
        for entry in _scandir(stack.pop()):
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
                    files += 1
            except OSError:
                pass
    return total, files


def _stat(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _do_scan():
    """uploads 하위 3범주를 1회 순회해 analysis_key 별 로컬 바이트 + 범주 총계를 만든다.

    web_report/<akey>/ 는 직속 파일(parquet/manifest)과 cache/ 하위(compute 캐시)를
    분리 집계한다 — 범주 카드에서 parquet 과 재생성 가능한 캐시를 구분해 보여준다."""
    upload_root = Path(config.REPORT_UPLOAD_DIR)
    wr_root = upload_root / "web_report"
    img_root = upload_root / "issue_img"
    dist_root = upload_root / "dist_combined"

    per_key = {}        # akey -> 로컬 바이트 합(parquet+cache+issue+dist)
    per_key_cache = {}   # akey -> 그중 compute 캐시분 (재생성 가능 · 티어링 대상 아님)
    cat = {        # 범주 -> [bytes, files]
        "parquet": [0, 0],
        "cache": [0, 0],
        "issue_img": [0, 0],
        "dist_combined": [0, 0],
    }

    # web_report/<akey>/ : 직속 파일 = parquet/manifest, cache/ = compute 캐시
    for entry in _scandir(wr_root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        akey = entry.name
        p_bytes = p_files = c_bytes = c_files = 0
        for sub in _scandir(entry.path):
            try:
                if sub.is_dir(follow_symlinks=False):
                    b, f = _dir_size(sub.path)
                    if sub.name == "cache":
                        c_bytes += b; c_files += f
                    else:
                        p_bytes += b; p_files += f   # 예상 밖 하위디렉토리는 parquet 범주로
                elif sub.is_file(follow_symlinks=False):
                    p_bytes += sub.stat(follow_symlinks=False).st_size; p_files += 1
            except OSError:
                pass
        cat["parquet"][0] += p_bytes; cat["parquet"][1] += p_files
        cat["cache"][0] += c_bytes; cat["cache"][1] += c_files
        per_key[akey] = per_key.get(akey, 0) + p_bytes + c_bytes
        per_key_cache[akey] = per_key_cache.get(akey, 0) + c_bytes

    # issue_img/<akey>/
    for entry in _scandir(img_root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        b, f = _dir_size(entry.path)
        cat["issue_img"][0] += b; cat["issue_img"][1] += f
        per_key[entry.name] = per_key.get(entry.name, 0) + b

    # dist_combined/<akey>.png
    for entry in _scandir(dist_root):
        try:
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(".png"):
                b = entry.stat(follow_symlinks=False).st_size
                cat["dist_combined"][0] += b; cat["dist_combined"][1] += 1
                akey = entry.name[:-4]
                per_key[akey] = per_key.get(akey, 0) + b
        except OSError:
            pass

    return {"per_key": per_key, "per_key_cache": per_key_cache, "cat": cat}


def _scan(refresh=False):
    global _scan_cache
    now = time.time()
    with _scan_lock:
        if _scan_cache and not refresh and now - _scan_cache[0] < _SCAN_TTL:
            return _scan_cache[1]
    result = _do_scan()
    with _scan_lock:
        _scan_cache = (now, result)
    return result


# ── 현황 오버뷰 (디스크 + 서버 점유 총량 + 범주별) ───────────────────────────

def overview(refresh=False):
    """상단 카드용: 디스크 사용량 · report_server 로컬 점유 총량 · 범주별 크기 · S3 설정여부."""
    import psutil

    scan = _scan(refresh=refresh)
    cat = scan["cat"]

    # report.db 뿐 아니라 같이 운영되는 eval.db·voc.db 도 집계한다 — 빠져 있으면
    # 점유 총량이 실제보다 작게 보인다.
    def _db_size(path):
        p = Path(path)
        parts = [p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")]
        return sum(_stat(x) for x in parts), sum(1 for x in parts if x.exists())

    db_bytes, db_files = _db_size(config.REPORT_DB_PATH)
    eval_bytes, eval_files = _db_size(config.REPORT_EVAL_DB_PATH)
    voc_bytes, voc_files = _db_size(config.REPORT_VOC_DB_PATH)

    backup_dir = Path(config.REPORT_DB_BACKUP_DIR)
    backup_bytes, backup_files = _dir_size(backup_dir) if backup_dir.is_dir() else (0, 0)

    # Raw Data Excel 왕복 편집이 남기는 원본 백업 (report_cleanup 이 세션 정리 때 걷어감).
    rawbak_dir = Path(config.REPORT_UPLOAD_DIR) / "webreport_backup"
    rawbak_bytes, rawbak_files = _dir_size(rawbak_dir) if rawbak_dir.is_dir() else (0, 0)

    categories = [
        {"key": "db", "label": "DB 파일 (report.db + wal/shm)", "bytes": db_bytes, "files": db_files},
        {"key": "eval_db", "label": "eval.db (코멘트 export)", "bytes": eval_bytes, "files": eval_files},
        {"key": "voc_db", "label": "voc.db (VOC 게시판)", "bytes": voc_bytes, "files": voc_files},
        {"key": "backup", "label": "DB 백업", "bytes": backup_bytes, "files": backup_files},
        {"key": "rawedit_backup", "label": "Raw Data 편집 원본 백업", "bytes": rawbak_bytes, "files": rawbak_files},
        {"key": "parquet", "label": "web_report parquet/manifest", "bytes": cat["parquet"][0], "files": cat["parquet"][1]},
        {"key": "cache", "label": "web_report compute 캐시 (재생성 가능)", "bytes": cat["cache"][0], "files": cat["cache"][1]},
        {"key": "issue_img", "label": "issue 이미지", "bytes": cat["issue_img"][0], "files": cat["issue_img"][1]},
        {"key": "dist_combined", "label": "distribution 합성 PNG", "bytes": cat["dist_combined"][0], "files": cat["dist_combined"][1]},
    ]
    occupied = sum(c["bytes"] for c in categories)

    disk = psutil.disk_usage(str(config.ROOT_DIR))
    return {
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "percent": disk.percent},
        "occupied_bytes": occupied,
        "categories": categories,
        "s3_configured": storage_gateway.s3_available(),
    }


# ── 세션별 데이터 목록 (용량/시간/범주 정렬) ─────────────────────────────────

_SORT_KEYS = {"size", "time", "category"}
_MAX_FETCH = 10000   # 크기 정렬은 파이썬에서 하므로 전체를 받되 폭주 방지 상한


def _backend_map():
    """analysis_key -> 'local'|'s3'|'' (web_report_manifest 저장 위치 마커). 1 쿼리."""
    out = {}
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT analysis_key, options_json FROM report_object_info "
            "WHERE object_type='web_report_manifest'").fetchall()
    for r in rows:
        try:
            out[r["analysis_key"]] = str(json.loads(r["options_json"] or "{}").get("storage") or "")
        except (json.JSONDecodeError, TypeError):
            out[r["analysis_key"]] = ""
    return out


def list_sessions_by_storage(sort="size", order="desc", q=None, limit=100, offset=0, refresh=False):
    """세션 1건 = 1행. 로컬 산출물 바이트를 붙여 용량/시간/범주(제품군) 정렬 후 페이지네이션."""
    sort = sort if sort in _SORT_KEYS else "size"
    reverse = order != "asc"
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    scan = _scan(refresh=refresh)
    per_key = scan["per_key"]
    per_key_cache = scan.get("per_key_cache", {})

    conditions = ["1=1"]
    params = []
    if q:
        conditions.append(
            "(s.file_name LIKE ? OR s.product LIKE ? OR s.lot_id LIKE ? "
            " OR s.session_id LIKE ? OR s.product_type LIKE ?)")
        like = f"%{q}%"
        params.extend([like] * 5)
    where = " AND ".join(conditions)

    with report_db.get_conn() as conn:
        rows = conn.execute(f"""
            SELECT s.session_id, s.analysis_key, s.file_name, s.product_type,
                   s.product, s.lot_id, s.created_at, s.status, s.source
            FROM report_session s
            WHERE {where}
            ORDER BY s.created_at DESC, s.session_id
            LIMIT ?
        """, params + [_MAX_FETCH]).fetchall()
        share_rows = conn.execute(
            "SELECT analysis_key, COUNT(*) AS cnt FROM report_session "
            "WHERE analysis_key IS NOT NULL AND analysis_key <> '' "
            "GROUP BY analysis_key").fetchall()

    shared = {r["analysis_key"]: r["cnt"] for r in share_rows}
    backend = _backend_map()

    items = []
    for r in rows:
        akey = r["analysis_key"] or ""
        items.append({
            "session_id": r["session_id"],
            "analysis_key": akey,
            "file_name": r["file_name"],
            "product_type": r["product_type"],
            "product": r["product"],
            "lot_id": r["lot_id"],
            "created_at": r["created_at"],
            "status": r["status"],
            "source": r["source"],
            "local_bytes": int(per_key.get(akey, 0)),
            # 티어링(S3 이동) 대상은 캐시를 뺀 실산출물뿐이다. local_bytes 만 보면
            # 캐시로 부푼 세션을 "옮기면 큰 공간이 빈다"고 오해하게 된다.
            "cache_bytes": int(per_key_cache.get(akey, 0)),
            "tierable_bytes": max(0, int(per_key.get(akey, 0)) - int(per_key_cache.get(akey, 0))),
            "backend": backend.get(akey, ""),
            "shared": int(shared.get(akey, 1)) if akey else 1,
        })

    if sort == "size":
        items.sort(key=lambda x: (x["local_bytes"], x["created_at"] or 0), reverse=reverse)
    elif sort == "time":
        items.sort(key=lambda x: (x["created_at"] or 0, x["session_id"]), reverse=reverse)
    else:  # category = 제품군(product_type) → product → lot
        items.sort(key=lambda x: ((x["product_type"] or "").lower(), (x["product"] or "").lower(),
                                  (x["lot_id"] or "").lower(), x["created_at"] or 0), reverse=reverse)

    total = len(items)
    page = items[offset:offset + limit]
    return {"total": total, "limit": limit, "offset": offset, "sort": sort,
            "order": "asc" if not reverse else "desc", "rows": page}
