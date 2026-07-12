"""로컬 폴백 산출물 → S3 백필 스크립트 (멱등, dry-run 기본).

S3 미설정 시절(또는 S3 장애 폴백)에 로컬에만 저장된 산출물을 S3 로 이관하고
object_info 를 기록한다. 대상 선정 기준이 "object_info 미기록(issue_img 는 S3 index
부재) = 로컬 유일본"이라 재실행해도 이미 이관된 것은 자동 제외된다 - S3 를 켠 직후
1회 + 이후 S3 장애로 폴백이 발생했을 때마다 실행하면 로컬이 비워진다.

사용법 (server/ 디렉터리에서):
    python tools/backfill_local_to_s3.py                    # dry-run (기본)
    python tools/backfill_local_to_s3.py --apply            # 실제 업로드
    python tools/backfill_local_to_s3.py --apply --delete-local
        # 업로드 후 S3 재다운로드/존재 검증 통과 시에만 로컬 파일 삭제
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

# 한국어 Windows 콘솔(cp949)에서 인코딩 불가 문자로 print 가 죽지 않도록
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

from config import REPORT_UPLOAD_DIR  # noqa: E402
from database import report_db  # noqa: E402
import storage_gateway  # noqa: E402


def _fmt_mb(size: int) -> str:
    return f"{size / 1024 / 1024:.2f} MB"


def _content_hash_for(analysis_key: str):
    """akey 세션 행의 content_hash. 세션 자체가 없으면 None (orphan)."""
    with report_db.get_conn() as conn:
        row = conn.execute(
            "SELECT content_hash FROM report_session WHERE analysis_key=? "
            "ORDER BY created_at DESC LIMIT 1", (analysis_key,)).fetchone()
    if row is None:
        return None
    return str(row[0] or "")


def _object_types(analysis_key: str) -> set:
    return {o["object_type"] for o in report_db.get_all_object_infos(analysis_key)}


def backfill_web_report(upload_root: Path, apply: bool, delete_local: bool) -> int:
    """web_report/<akey>/ (parquet+manifest) 백필. 이관 건수 반환."""
    root = upload_root / "web_report"
    done = 0
    if not root.is_dir():
        return done
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        akey = d.name
        manifest_path = d / "manifest.json"
        sources = sorted(d.glob("source_*.parquet"),
                         key=lambda p: int(p.stem.rsplit("_", 1)[1]))
        if not manifest_path.exists() or not sources:
            print(f"[skip] web_report {akey[:12]}…  파일 불완전 (manifest/parquet 누락)")
            continue
        objs = _object_types(akey)
        if "web_report_source_0" in objs and "web_report_manifest" in objs:
            continue  # 이미 S3 에 있음
        chash = _content_hash_for(akey)
        if chash is None:
            print(f"[skip] web_report {akey[:12]}…  세션 행 없음 (orphan) - 수동 확인 필요")
            continue
        size = sum(p.stat().st_size for p in sources) + manifest_path.stat().st_size
        print(f"[대상] web_report {akey[:12]}…  sources={len(sources)}  {_fmt_mb(size)}")
        if not apply:
            done += 1
            continue

        sources_bytes = [p.read_bytes() for p in sources]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        res = storage_gateway.save_webreport_sources(
            akey, chash, sources_bytes, manifest, upload_root=upload_root)
        if res.get("storage") != "s3":
            print(f"  → [FAIL] S3 업로드 실패, 로컬 유지: {res.get('warnings')}")
            continue
        print("  → S3 업로드 + object_info 기록 완료")
        done += 1

        if delete_local:
            infos = {o["object_type"]: o for o in report_db.get_all_object_infos(akey)}
            verified = True
            for idx, data in enumerate(sources_bytes):
                info = infos.get(f"web_report_source_{idx}")
                remote = storage_gateway.download_bytes_from_s3(info["s3_key"]) if info else b""
                if hashlib.sha256(remote).digest() != hashlib.sha256(data).digest():
                    verified = False
                    break
            if verified:
                shutil.rmtree(d)
                print("  → 재다운로드 해시 일치 - 로컬 삭제 완료")
            else:
                print("  → [WARN] 재다운로드 검증 불일치 - 로컬 보존")
    return done


def backfill_issue_images(upload_root: Path, apply: bool, delete_local: bool) -> int:
    """issue_img/<akey>/ (행별 PNG) 백필. S3 index 가 이미 있으면 스킵."""
    root = upload_root / "issue_img"
    done = 0
    if not root.is_dir():
        return done
    s3_on = storage_gateway.s3_available()
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        akey = d.name
        pngs = sorted(d.glob("*.png"), key=lambda p: int(p.stem))
        if not pngs:
            continue
        if s3_on and storage_gateway.list_issue_image_rows(akey):
            continue  # S3 index 존재 - 이미 이관됨
        size = sum(p.stat().st_size for p in pngs)
        print(f"[대상] issue_img  {akey[:12]}…  rows={len(pngs)}  {_fmt_mb(size)}")
        if not apply:
            done += 1
            continue

        images = [{"row": int(p.stem), "png": p.read_bytes()} for p in pngs]
        res = storage_gateway.save_issue_images(akey, images)
        if res.get("backend") != "s3" or len(res.get("rows", [])) != len(images):
            print(f"  → [FAIL] S3 업로드 실패/부분 성공: {res}")
            continue
        print("  → S3 업로드 (PNG + index.json) 완료")
        done += 1

        if delete_local:
            local_rows = {int(p.stem) for p in pngs}
            if local_rows.issubset(set(storage_gateway.list_issue_image_rows(akey))):
                shutil.rmtree(d)
                print("  → S3 index 확인 - 로컬 삭제 완료")
            else:
                print("  → [WARN] S3 index 검증 불일치 - 로컬 보존")
    return done


def backfill_dist_combined(upload_root: Path, apply: bool, delete_local: bool) -> int:
    """dist_combined/<akey>.png 백필."""
    root = upload_root / "dist_combined"
    done = 0
    if not root.is_dir():
        return done
    for png in sorted(root.glob("*.png")):
        akey = png.stem
        if "distribution_combined" in _object_types(akey):
            continue
        chash = _content_hash_for(akey)
        if chash is None:
            print(f"[skip] dist_png   {akey[:12]}…  세션 행 없음 (orphan) - 수동 확인 필요")
            continue
        print(f"[대상] dist_png   {akey[:12]}…  {_fmt_mb(png.stat().st_size)}")
        if not apply:
            done += 1
            continue

        warnings = []
        storage_gateway.save_distribution_png(
            akey, chash, "{}", png.read_bytes(), s3_ok=True, warnings=warnings)
        # save_distribution_png 는 S3 실패 시 로컬 폴백으로도 True 를 돌려주므로
        # object_info 기록 여부로 실제 S3 성공을 판정한다.
        if "distribution_combined" not in _object_types(akey):
            print(f"  → [FAIL] S3 업로드 실패, 로컬 유지: {warnings}")
            continue
        print("  → S3 업로드 + object_info 기록 완료")
        done += 1

        if delete_local:
            key = storage_gateway.make_distribution_combined_s3_key(akey)
            if storage_gateway.s3_object_exists(key):
                png.unlink()
                print("  → S3 존재 확인 - 로컬 삭제 완료")
            else:
                print("  → [WARN] S3 존재 확인 실패 - 로컬 보존")
    return done


def main():
    ap = argparse.ArgumentParser(
        description="로컬 폴백 산출물 → S3 백필 (멱등, dry-run 기본)")
    ap.add_argument("--apply", action="store_true", help="실제 업로드 수행 (기본 dry-run)")
    ap.add_argument("--delete-local", action="store_true",
                    help="업로드 후 S3 검증 통과 시 로컬 파일 삭제 (--apply 와 함께)")
    args = ap.parse_args()

    if args.delete_local and not args.apply:
        ap.error("--delete-local 은 --apply 와 함께 써야 한다")

    report_db.init_report_db()
    upload_root = Path(REPORT_UPLOAD_DIR)

    s3_configured = storage_gateway.s3_available()

    if not s3_configured:
        print("[!] REPORT_S3_BUCKET 미설정 - 아래는 이관 대상 목록(dry-run)만 출력한다.")
        if args.apply:
            print("[!] --apply 는 S3 설정 후에만 가능. 중단.")
            sys.exit(1)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== backfill_local_to_s3 [{mode}]  upload_root={upload_root} ===")
    n1 = backfill_web_report(upload_root, args.apply, args.delete_local)
    n2 = backfill_issue_images(upload_root, args.apply, args.delete_local)
    n3 = backfill_dist_combined(upload_root, args.apply, args.delete_local)
    verb = "이관" if args.apply else "이관 예정"
    print(f"=== 완료: web_report {n1}건, issue_img {n2}건, dist_png {n3}건 {verb} ===")


if __name__ == "__main__":
    main()
