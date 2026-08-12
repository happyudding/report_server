"""로컬 hot 캐시 → S3 자동 티어링.

바이너리 산출물(web_report parquet·manifest, issue/dist PNG)을 로컬에 hot 캐시로 두다가,
① 6개월(REPORT_TIER_AGE_DAYS) 이상 됐거나 ② 로컬 티어링대상 총량이 REPORT_TIER_LOCAL_MAX_GB
를 넘으면 오래된 순으로 S3 로 이동하고 로컬 원본을 삭제한다(진짜 티어링 — 로컬 용량 확보).

동결 영역(storage_gateway)을 수정하지 않고 **공개 API 조합**으로 구현한다: 로컬에 남은
산출물을 다시 읽어 S3 로 저장하면 facade 가 object_info.options_json 을 {"storage":"s3"}
로 자동 갱신(부활 방지 규약과 일관)하고, 그 뒤 로컬 원본만 지운다. 조회는 그 기록을 따라
자동으로 S3 에서 읽는다.

S3 미설정(REPORT_S3_BUCKET 공란)이면 no-op. cleanup 스케줄러 주기에 얹혀 실행된다
(report_cleanup._loop). 6개월 만료 세션을 삭제하던 종전 retention 을 이 티어링이 대체하므로
세션/DB 는 유지되고 산출물만 S3 로 아카이브된다(데이터 영구 삭제 없음).

DB·텍스트(report_sheet_data)·편집상태(report_webreport_edit)·계산캐시(web_report/<akey>/cache/)
·DB 백업은 티어링 대상이 아니다(항상 로컬). Note 탭 이미지는 후순위(1단계 제외).
"""
import json
import logging
import shutil
import time
from collections import Counter
from pathlib import Path

import config
from database import report_db

_log = logging.getLogger(__name__)


def _upload_root() -> Path:
    return Path(config.REPORT_UPLOAD_DIR)


# ── 로컬 티어링대상 스캔 (cache/ 제외) ───────────────────────────────────────

def _scan_local_akeys() -> dict:
    """로컬에 티어링대상이 남은 akey별 [총bytes, oldest_mtime].

    대상 경로: web_report/<akey>/ 의 파일(source_*.parquet·manifest.json — cache/ 디렉토리는
    is_file() 로 자연 제외) + issue_img/<akey>/ + dist_combined/<akey>.png.
    disk_cache._enforce_cap 의 총량 합산 + mtime 패턴과 동일 취지.
    """
    root = _upload_root()
    result: dict = {}

    def _add(akey: str, path: Path):
        try:
            st = path.stat()
        except OSError:
            return
        e = result.setdefault(akey, [0, st.st_mtime])
        e[0] += st.st_size
        e[1] = min(e[1], st.st_mtime)

    wr = root / "web_report"
    if wr.is_dir():
        for d in wr.iterdir():
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_file():   # cache/ 디렉토리 제외
                    _add(d.name, p)

    issue = root / "issue_img"
    if issue.is_dir():
        for d in issue.iterdir():
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if p.is_file():
                    _add(d.name, p)

    dist = root / "dist_combined"
    if dist.is_dir():
        for p in dist.iterdir():
            if p.is_file() and p.suffix == ".png":
                _add(p.stem, p)

    return result


# ── 산출물 종류별 로컬 → S3 이동 ──────────────────────────────────────────────

def _tier_parquet(akey, content_hash, upload_root, warnings) -> bool:
    """web_report parquet+manifest 를 S3 로 이동. 이동했으면 True."""
    import storage_gateway
    wr_dir = upload_root / "web_report" / akey
    if not wr_dir.is_dir() or not (wr_dir / "source_0.parquet").exists():
        return False
    try:
        sources, manifest = storage_gateway.load_webreport_sources(akey, upload_root)
        res = storage_gateway.save_webreport_sources(
            akey, content_hash, sources, manifest, upload_root)
    except Exception as exc:
        warnings.append(f"parquet tier failed ({akey}): {exc}")
        return False
    if res.get("storage") != "s3":
        warnings.append(f"parquet tier did not reach s3 ({akey}): {res.get('storage')}")
        return False
    # S3 저장 성공 — 로컬 parquet·manifest 삭제 (cache/ 는 남긴다).
    for p in wr_dir.iterdir():
        if p.is_file():
            try:
                p.unlink()
            except OSError as exc:
                warnings.append(f"local parquet unlink failed ({p}): {exc}")
    return True


def _tier_issue_images(akey, upload_root, warnings) -> bool:
    """로컬 issue_img/<akey>/ PNG 를 S3 로 이동. 이동했으면 True.

    S3 연결 상태에선 storage_gateway.list_issue_image_rows/load_issue_image 가 S3 인덱스를
    먼저 보므로 로컬 파일을 못 읽는다 — 로컬 index.json·PNG 를 직접 읽어 재구성한다.
    """
    import storage_gateway
    issue_dir = upload_root / "issue_img" / akey
    if not issue_dir.is_dir():
        return False
    idx = issue_dir / "index.json"
    rows = []
    if idx.exists():
        try:
            rows = [int(r) for r in json.loads(idx.read_text(encoding="utf-8")).get("rows", [])]
        except Exception:
            rows = []
    if not rows:  # index 없으면 파일명에서 행 추출
        rows = sorted(int(p.stem) for p in issue_dir.glob("*.png") if p.stem.isdigit())
    images = []
    for r in rows:
        p = issue_dir / f"{r}.png"
        if p.exists():
            images.append({"row": r, "png": p.read_bytes()})
    if not images:
        return False
    try:
        res = storage_gateway.save_issue_images(akey, images)
    except Exception as exc:
        warnings.append(f"issue image tier failed ({akey}): {exc}")
        return False
    if res.get("backend") != "s3":
        warnings.append(f"issue image tier did not reach s3 ({akey}): {res.get('backend')}")
        return False
    try:
        shutil.rmtree(issue_dir)
    except OSError as exc:
        warnings.append(f"local issue_img rmtree failed ({issue_dir}): {exc}")
    return True


def _tier_dist_png(akey, content_hash, obj_by_type, upload_root, warnings) -> bool:
    """로컬 dist_combined/<akey>.png 를 S3 로 이동. 이동했으면 True.

    save_distribution_png 는 S3 성공/로컬폴백 모두 True 를 반환해 구분이 안 되므로,
    업로드 후 s3_object_exists 로 S3 도달을 확정한 뒤에만 로컬을 지운다.
    """
    import storage_gateway
    dist_local = upload_root / "dist_combined" / f"{akey}.png"
    if not dist_local.exists():
        return False
    data = dist_local.read_bytes()
    meta_str = (obj_by_type.get("distribution_combined") or {}).get("options_json") or ""
    try:
        storage_gateway.save_distribution_png(
            akey, content_hash, meta_str, data, s3_ok=True, warnings=warnings)
        key = storage_gateway.make_distribution_combined_s3_key(akey)
        reached = storage_gateway.s3_object_exists(key)
    except Exception as exc:
        warnings.append(f"dist png tier failed ({akey}): {exc}")
        return False
    if not reached:
        warnings.append(f"dist png tier did not reach s3 ({akey})")
        return False
    try:
        dist_local.unlink()
    except OSError as exc:
        warnings.append(f"local dist png unlink failed ({dist_local}): {exc}")
    return True


def _tier_one(akey, dry_run) -> dict:
    """akey 의 로컬 산출물을 S3 로 이동. {"moved":[...], "warnings":[...]} 반환."""
    upload_root = _upload_root()
    if dry_run:
        size = sum(v[0] for k, v in _scan_local_akeys().items() if k == akey)
        _log.info("[tier:dry-run] would tier akey=%s local_bytes=%d", akey, size)
        return {"moved": ["dry-run"], "warnings": []}

    objs = report_db.get_all_object_infos(akey)
    content_hash = next((o["content_hash"] for o in objs if o.get("content_hash")), "")
    obj_by_type = {o["object_type"]: o for o in objs}

    moved, warnings = [], []
    if _tier_parquet(akey, content_hash, upload_root, warnings):
        moved.append("web_report_sources")
    if _tier_issue_images(akey, upload_root, warnings):
        moved.append("issue_images")
    if _tier_dist_png(akey, content_hash, obj_by_type, upload_root, warnings):
        moved.append("distribution_combined")

    if moved:
        _log_audit(akey, "ok")
        _log.info("[tier] moved akey=%s -> s3: %s", akey, moved)
    for w in warnings:
        _log.warning("[tier] %s", w)
    return {"moved": moved, "warnings": warnings}


def _log_audit(akey, result):
    """티어링 이력을 report_audit_log 에 기록 (best-effort). cleanup 스케줄러와 동일 패턴 —
    Flask request 컨텍스트가 없으므로 client_ip/user_agent 는 고정값.
    busy_timeout 도 cleanup 과 같은 이유로 5초 명시(사용자 대기 없음 + 이동 기록 보존)."""
    try:
        report_db.log_audit(
            "tier", analysis_key=akey, changed_fields="storage:local->s3",
            client_ip="system", user_agent="tier-scheduler", result=result,
            busy_timeout_ms=5000)
    except Exception:
        pass


# ── 스케줄러 진입점 ───────────────────────────────────────────────────────────

def run_tiering(dry_run=None):
    """나이(6개월↑) + 용량(1TB 초과) 트리거로 로컬 산출물을 S3 로 이동.
    {'s3','tiered','dry_run'} 요약 반환. S3 미설정이면 no-op."""
    if not config.REPORT_TIER_ENABLED:
        return {"s3": False, "tiered": 0, "dry_run": None, "enabled": False}

    import storage_gateway
    if not storage_gateway.s3_available():
        _log.info("[tier] S3 not configured — skip (no-op)")
        return {"s3": False, "tiered": 0, "dry_run": None}

    if dry_run is None:
        dry_run = config.REPORT_TIER_DRYRUN

    tiered: set = set()

    # ① 나이 트리거: created_at 이 AGE_DAYS 이전인 세션의 akey. 단, 같은 akey 를 참조하는
    #    최근(6개월 미만) 세션이 남아 있으면 보류 — 재업로드로 아직 hot 하게 보는 데이터.
    cutoff = int(time.time()) - int(config.REPORT_TIER_AGE_DAYS) * 86400
    try:
        aged = report_db.get_expired_sessions(cutoff)
    except Exception:
        _log.exception("[tier] get_expired_sessions failed")
        aged = []
    aged_cnt = Counter(s["analysis_key"] for s in aged if s.get("analysis_key"))
    for akey, cnt in aged_cnt.items():
        try:
            if report_db.count_sessions_for_analysis_key(akey) > cnt:
                continue  # 최근 참조 세션 있음 → 보류
            _tier_one(akey, dry_run)
            tiered.add(akey)
        except Exception:
            _log.exception("[tier] age-trigger failed for %s", akey)

    # ② 용량 트리거: 아직 로컬인 산출물 총량이 상한을 넘으면 오래된(mtime) 순으로 추가 이동.
    max_bytes = int(float(config.REPORT_TIER_LOCAL_MAX_GB) * (1024 ** 3))
    local_map = _scan_local_akeys()
    total = sum(v[0] for v in local_map.values())
    if total > max_bytes:
        for akey, (size, _mtime) in sorted(local_map.items(), key=lambda kv: kv[1][1]):
            if total <= max_bytes:
                break
            if akey in tiered:
                continue
            try:
                _tier_one(akey, dry_run)
                tiered.add(akey)
                total -= size  # dry-run 도 시뮬레이션해 "어디까지 옮기면 되는지" 로그
            except Exception:
                _log.exception("[tier] size-trigger failed for %s", akey)

    _log.info("[tier] done: tiered=%d dry_run=%s age_days=%d local_max_gb=%.0f",
              len(tiered), dry_run, config.REPORT_TIER_AGE_DAYS,
              config.REPORT_TIER_LOCAL_MAX_GB)
    return {"s3": True, "tiered": len(tiered), "dry_run": dry_run}
