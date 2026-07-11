"""manifest 편집 필드 → 세션 편집 DB(report_webreport_edit) 일괄 이전 (1회 운영 도구).

Phase 2(2026-07-11)에서 comment/override 편집의 진실이 manifest(S3 JSON)에서
세션 단위 DB 로 바뀌었다. 이 도구는 기존 web_report 세션들의 manifest 편집값
(issue_comments / etc_items / trim_overrides / summary_engr)을 **그 analysis_key 를
참조하는 모든 세션에 각각 복사**한다 — 기존에 공유로 보이던 값을 각 세션이 그대로
보게 되어 화면 표시가 보존된다.

- 이미 편집행이 있는 세션(rev>0)은 건너뛴다 (재실행 안전).
- 이 도구를 돌리지 않아도 조회는 rev==0 폴백으로 manifest 값을 계속 보여주고,
  첫 편집 시점에 자동 시드된다 (web_report/edits.py ensure_seeded). 일괄 이전을
  원할 때만 서버 중지 상태에서 실행할 것.

사용:
    cd server
    .venv/Scripts/python.exe tools/migrate_manifest_edits.py [--dry-run]
"""
import sys
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
_ROOT = _SERVER.parent
for p in (str(_SERVER), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def main(dry_run: bool) -> int:
    from config import REPORT_UPLOAD_DIR
    from database import report_db
    import storage_gateway
    from web_report import edits

    report_db.init_report_db()
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id, analysis_key FROM report_session "
            "WHERE source='web_report' AND analysis_key IS NOT NULL "
            "ORDER BY created_at").fetchall()

    manifests = {}   # analysis_key -> manifest | None(로드 실패)
    total = copied = skipped = missing = empty = 0
    for row in rows:
        total += 1
        sid, akey = row["session_id"], row["analysis_key"]
        if report_db.get_webreport_edit_rev(sid) > 0:
            skipped += 1
            continue
        if akey not in manifests:
            try:
                manifests[akey] = storage_gateway.load_webreport_manifest(
                    akey, upload_root=Path(REPORT_UPLOAD_DIR))
            except FileNotFoundError:
                manifests[akey] = None
        manifest = manifests[akey]
        if manifest is None:
            missing += 1
            print(f"[warn] manifest not found: session={sid} akey={akey[:12]}...")
            continue
        state = edits.state_from_manifest(manifest)
        n_fields = (len(state["issue_comments"]) + len(state["etc_items"])
                    + len(state["trim_overrides"]) + len(state["summary_engr"]))
        if n_fields == 0:
            empty += 1
            continue
        if dry_run:
            print(f"[dry-run] session={sid} akey={akey[:12]}... would copy {n_fields} field groups")
            copied += 1
            continue
        n = edits.seed_from_manifest(report_db, sid, manifest)
        print(f"[ok] session={sid} akey={akey[:12]}... copied {n} rows")
        copied += 1

    print(f"\ntotal={total} copied={copied} already-migrated={skipped} "
          f"no-edits={empty} manifest-missing={missing}"
          + (" (dry-run: 실제 기록 없음)" if dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv[1:]))
