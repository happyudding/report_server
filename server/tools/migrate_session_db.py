"""세션 DB 개선 backfill (2026-08-14) — 재개 가능한 운영 도구.

서버 **기동 중에 돌려도 되도록** 설계했다(부팅 시 대량 작업은 세션이 안 열리는 구간을
만든다). 각 단계는 report_schema_migration 에 상태·cursor 를 남기고, 중간에 끊겨도 다시
실행하면 이어서 진행한다. 모든 단계가 멱등이다.

단계 (--step 으로 하나만 돌릴 수 있다):
  analysis      전 analysis_key → report_analysis (authoritative content_hash)
  hashfix       dedup 형제 세션의 content_hash 불일치를 parquet 실체 기준으로 통일
  objectmeta    저장 위치 메타(report_object_info)가 없는 web_report 세션 채우기
  noteblob      Note 시트 본문 → 객체 저장 + 포인터 확정 (저장 전후 해시 일치할 때만)
  sheetdata     참조 없는 report_sheet_data 고아 행 삭제 (48h 유예)
  pin           평문 세션 PIN 비우기

사용:
    cd server
    .venv/Scripts/python.exe tools/migrate_session_db.py --dry-run     # 대상만 출력
    .venv/Scripts/python.exe tools/migrate_session_db.py               # 전 단계 실행
    .venv/Scripts/python.exe tools/migrate_session_db.py --step noteblob
"""
import argparse
import gzip
import hashlib
import sys
import time
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
_ROOT = _SERVER.parent
for p in (str(_SERVER), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 순서가 의미를 갖는다: objectmeta 가 저장 위치를 채워야 hashfix 가 parquet 실체를 읽을 수
# 있다(그 전에는 다수결로 떨어진다).
STEPS = ("analysis", "objectmeta", "hashfix", "noteblob", "sheetdata", "pin")


def _mark(report_db, step, status, cursor=None, detail=None, dry_run=False):
    if dry_run:
        return
    try:
        report_db.set_migration_step(step, status, cursor=cursor, detail=detail)
    except Exception as exc:            # 기록 실패가 작업을 막지는 않는다
        print(f"  ! 진행 기록 실패 ({step}): {exc}")


# ── analysis ─────────────────────────────────────────────────────────────────

def step_analysis(report_db, dry_run):
    """세션 행에서 analysis_key 단위 원본 상태를 조립한다.

    content_hash 는 형제 중 **가장 많이 쓰인 값**을 취한다 — 실체 검증은 hashfix 단계가
    한다(파일을 읽어야 해서 훨씬 무겁다). 여기서는 표를 채우는 것이 목적."""
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT analysis_key, COALESCE(content_hash,'') AS ch, source, COUNT(*) AS n "
            "FROM report_session WHERE analysis_key IS NOT NULL AND analysis_key != '' "
            "GROUP BY analysis_key, ch, source ORDER BY analysis_key, n DESC").fetchall()
    best = {}
    for r in rows:
        akey = r["analysis_key"]
        if akey not in best:
            best[akey] = (r["ch"], r["source"])
    done = 0
    for akey, (ch, source) in best.items():
        if report_db.get_analysis(akey):
            continue
        if dry_run:
            print(f"  [dry-run] analysis {akey[:12]}… hash={ch[:12] or '(없음)'}")
            done += 1
            continue
        count = len(report_db.get_all_object_infos(akey) or [])
        report_db.upsert_analysis(akey, content_hash=ch, source=source,
                                  source_count=count,
                                  artifact_status="ok" if ch else "missing")
        done += 1
    return {"total": len(best), "written": done}


# ── hashfix ──────────────────────────────────────────────────────────────────

def _parquet_hash(akey):
    """저장된 source parquet 전체의 sha256 (idx 순서 고정). 못 읽으면 None."""
    from config import REPORT_UPLOAD_DIR
    import storage_gateway
    try:
        sources, _manifest = storage_gateway.load_webreport_sources(
            akey, upload_root=Path(REPORT_UPLOAD_DIR))
    except Exception:
        return None
    h = hashlib.sha256()
    for part in sources or []:
        h.update(bytes(part))
    return h.hexdigest()


def step_hashfix(report_db, dry_run):
    """같은 analysis_key 인데 세션마다 content_hash 가 다른 경우를 통일한다.

    산출물은 akey 단위로 하나뿐인데 형제 세션이 서로 다른 해시를 들고 있으면 캐시 키가
    갈라져 같은 데이터가 두 벌 계산된다. parquet 실체 해시를 authoritative 로 삼되,
    실체를 못 읽으면(로컬 없음·S3 미설정) **다수결 값**으로 통일한다 — 값 자체보다
    '형제가 같은 값을 본다'가 중요하다."""
    with report_db.get_conn() as conn:
        bad = conn.execute(
            "SELECT analysis_key FROM report_session "
            "WHERE analysis_key IS NOT NULL AND analysis_key != '' "
            "GROUP BY analysis_key HAVING COUNT(DISTINCT COALESCE(content_hash,'')) > 1"
        ).fetchall()
    fixed = []
    for row in bad:
        akey = row["analysis_key"]
        with report_db.get_conn() as conn:
            counts = conn.execute(
                "SELECT COALESCE(content_hash,'') AS ch, COUNT(*) AS n FROM report_session "
                "WHERE analysis_key=? GROUP BY ch ORDER BY n DESC", (akey,)).fetchall()
        majority = counts[0]["ch"] if counts else ""
        real = _parquet_hash(akey)
        chosen = real or majority
        source = "parquet" if real else "다수결"
        if not chosen:
            print(f"  ! {akey[:12]}… 통일할 해시를 정할 수 없음 — 건너뜀")
            continue
        if dry_run:
            print(f"  [dry-run] hashfix {akey[:12]}… → {chosen[:12]}… ({source})")
            fixed.append(akey)
            continue
        report_db.update_content_hash_for_analysis_key(akey, chosen)
        report_db.upsert_analysis(akey, content_hash=chosen)
        fixed.append(akey)
        print(f"  hashfix {akey[:12]}… → {chosen[:12]}… ({source})")
    return {"groups": len(bad), "fixed": len(fixed)}


# ── objectmeta ───────────────────────────────────────────────────────────────

def _session_hash(report_db, akey):
    """그 akey 세션들이 들고 있는 content_hash (없으면 빈 문자열)."""
    with report_db.get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(content_hash,'') AS ch FROM report_session "
            "WHERE analysis_key=? AND content_hash IS NOT NULL AND content_hash != '' "
            "LIMIT 1", (akey,)).fetchone()
    return row["ch"] if row else ""


def step_objectmeta(report_db, dry_run):
    """저장 위치 메타가 없는 web_report akey 에 로컬 상대키를 기록한다.

    옛 세션은 report_object_info 행 없이도 legacy 폴백으로 열리지만, 그 폴백은 '어디에
    있는지 모르니 일단 로컬을 찾아본다'는 뜻이라 S3 전환 후에는 판단 근거가 없다.
    실제 파일이 있는 것만 채운다(없으면 그대로 두고 폴백에 맡긴다)."""
    from config import REPORT_UPLOAD_DIR
    root = Path(REPORT_UPLOAD_DIR) / "web_report"
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT analysis_key FROM report_session "
            "WHERE source='web_report' AND analysis_key IS NOT NULL "
            "  AND analysis_key NOT IN (SELECT analysis_key FROM report_object_info "
            "                           WHERE object_type LIKE 'web_report_%')").fetchall()
    filled = skipped = 0
    for row in rows:
        akey = row["analysis_key"]
        d = root / akey
        sources = sorted(d.glob("source_*.parquet")) if d.is_dir() else []
        manifest = d / "manifest.json"
        if not sources or not manifest.exists():
            skipped += 1
            continue
        if dry_run:
            print(f"  [dry-run] objectmeta {akey[:12]}… sources={len(sources)}")
            filled += 1
            continue
        opts = '{"storage": "local"}'
        chash = _session_hash(report_db, akey)
        for path in sources:
            idx = path.stem.rsplit("_", 1)[-1]
            report_db.upsert_object_info(
                akey, chash, opts, f"web_report_source_{idx}", "",
                f"web_report/{akey}/{path.name}", "")
        report_db.upsert_object_info(
            akey, chash, opts, "web_report_manifest", "",
            f"web_report/{akey}/manifest.json", "")
        filled += 1
        print(f"  objectmeta {akey[:12]}… sources={len(sources)}")
    return {"scanned": len(rows), "filled": filled, "skipped": skipped}


# ── noteblob ─────────────────────────────────────────────────────────────────

def step_noteblob(report_db, dry_run):
    """Note 시트 본문을 객체 저장으로 옮기고 포인터를 확정한다.

    **저장 직전·직후 본문 해시가 같을 때만** 포인터를 쓴다. 옮기는 도중 사용자가 편집하면
    해시가 달라지므로 그 세션은 건너뛰고 다음 실행에서 다시 시도한다. legacy 행은 이
    단계에서 지우지 않는다(cutover 는 별도 배포) — 어느 쪽으로 롤백해도 본문이 살아 있다."""
    import storage_gateway
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT session_id FROM report_webreport_edit "
            "WHERE kind='note_sheet' AND item_key='sheet' ORDER BY session_id").fetchall()
    moved = skipped = raced = 0
    for row in rows:
        sid = row["session_id"]
        if report_db.get_session_blob(sid, "note_sheet"):
            skipped += 1
            continue
        with report_db.get_conn() as conn:
            cur = conn.execute(
                "SELECT value, updated_by FROM report_webreport_edit "
                "WHERE session_id=? AND kind='note_sheet' AND item_key='sheet'",
                (sid,)).fetchone()
        if not cur:
            continue
        body = str(cur["value"])
        raw = body.encode("utf-8")
        content_hash = hashlib.sha256(raw).hexdigest()
        if dry_run:
            print(f"  [dry-run] noteblob {sid} bytes={len(raw)}")
            moved += 1
            continue
        stored = storage_gateway.save_session_blob(sid, "note_sheet", content_hash,
                                                   gzip.compress(raw, 6))
        # 저장 직후 다시 읽어 확인 — 여기서 통과해야만 포인터를 쓴다.
        back = gzip.decompress(storage_gateway.load_session_blob(
            stored["backend"], stored["object_key"]))
        if hashlib.sha256(back).hexdigest() != content_hash:
            print(f"  ! noteblob {sid} 검증 실패 — 포인터 미기록(legacy 유지)")
            continue
        with report_db.get_conn() as conn:
            again = conn.execute(
                "SELECT value FROM report_webreport_edit "
                "WHERE session_id=? AND kind='note_sheet' AND item_key='sheet'",
                (sid,)).fetchone()
        if not again or hashlib.sha256(str(again["value"]).encode("utf-8")).hexdigest() \
                != content_hash:
            raced += 1
            print(f"  · noteblob {sid} 이전 중 편집됨 — 다음 실행에서 재시도")
            continue
        report_db.upsert_session_blob(
            sid, "note_sheet", backend=stored["backend"],
            object_key=stored["object_key"], content_hash=content_hash,
            base_token=report_db.note_base_token(body),
            size_bytes=stored["size_bytes"], content_encoding="gzip",
            updated_by=cur["updated_by"])
        moved += 1
        print(f"  noteblob {sid} → {stored['backend']} ({len(raw)}B)")
    return {"total": len(rows), "moved": moved, "skipped": skipped, "raced": raced}


# ── sheetdata ────────────────────────────────────────────────────────────────

def step_sheetdata(report_db, dry_run):
    """세션이 하나도 참조하지 않는 report_sheet_data 고아 행 삭제 (48h 유예).

    xlsx 세션이 지워졌는데 delete_analysis_rows 가 돌지 않은 구간에서 남는다. 48h 은
    업로드 진행 중 행을 건드리지 않기 위한 유예."""
    cutoff = int(time.time()) - 48 * 3600
    with report_db.get_conn() as conn:
        rows = conn.execute(
            "SELECT analysis_key, sheet_name, updated_at FROM report_sheet_data "
            "WHERE analysis_key NOT IN (SELECT analysis_key FROM report_session "
            "                           WHERE analysis_key IS NOT NULL)").fetchall()
    targets = [r for r in rows if int(r["updated_at"] or 0) < cutoff]
    for r in targets:
        print(f"  {'[dry-run] ' if dry_run else ''}sheetdata 고아 "
              f"{r['analysis_key'][:12]}…/{r['sheet_name']}")
    if targets and not dry_run:
        with report_db.get_conn() as conn:
            for r in targets:
                conn.execute("DELETE FROM report_sheet_data "
                             "WHERE analysis_key=? AND sheet_name=?",
                             (r["analysis_key"], r["sheet_name"]))
    return {"orphans": len(rows), "deleted": 0 if dry_run else len(targets)}


# ── pin ──────────────────────────────────────────────────────────────────────

def step_pin(report_db, dry_run):
    """평문 세션 PIN 비우기. 접근제어에 쓰이지 않은 지 오래인데 평문으로 남아 있었다."""
    with report_db.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM report_session "
                         "WHERE password IS NOT NULL AND password != ''").fetchone()[0]
        if n and not dry_run:
            conn.execute("UPDATE report_session SET password=NULL "
                         "WHERE password IS NOT NULL AND password != ''")
    print(f"  {'[dry-run] ' if dry_run else ''}평문 PIN {n}건")
    return {"cleared": 0 if dry_run else n, "found": n}


_RUNNERS = {"analysis": step_analysis, "hashfix": step_hashfix,
            "objectmeta": step_objectmeta, "noteblob": step_noteblob,
            "sheetdata": step_sheetdata, "pin": step_pin}


def main(argv=None):
    ap = argparse.ArgumentParser(description="세션 DB 개선 backfill (재개 가능)")
    ap.add_argument("--dry-run", action="store_true", help="대상만 출력하고 쓰지 않는다")
    ap.add_argument("--step", choices=STEPS, help="한 단계만 실행")
    args = ap.parse_args(argv)

    from database import report_db
    report_db.init_report_db()

    steps = (args.step,) if args.step else STEPS
    summary = {}
    for step in steps:
        print(f"[{step}]")
        _mark(report_db, step, "running", dry_run=args.dry_run)
        try:
            result = _RUNNERS[step](report_db, args.dry_run)
        except Exception as exc:
            _mark(report_db, step, "failed", detail=str(exc)[:300], dry_run=args.dry_run)
            print(f"  ! 실패: {exc}")
            summary[step] = {"error": str(exc)[:200]}
            continue
        summary[step] = result
        _mark(report_db, step, "done" if not args.dry_run else "pending",
              detail=str(result)[:300], dry_run=args.dry_run)
        print(f"  → {result}")
    print("\n요약:", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
