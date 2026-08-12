"""SUBPOP_GAP → BIMODALITY 룰 id 개명에 따른 누적 데이터 치환 (1회 운영 도구).

2026-08-12 룰셋 재편에서 `SUBPOP_GAP` 을 `BIMODALITY` 로 개명했다. 룰 id 는 코드·yaml
에만 있는 게 아니라 **DB 에 문자열로 쌓여 있다** — 그대로 두면 과거 발화·정답라벨·ENGR
확정값이 신규 데이터와 다른 이름으로 갈라져 선례검색·채점·Signature 컬럼이 어긋난다.

치환 대상 3곳:
  1. 엔진 eval.db      — case_signature.signature / label_signature.signature
  2. export eval DB    — 같은 두 테이블 (REPORT_EVAL_DB_PATH, 없으면 skip)
  3. report.db         — report_webreport_edit 의 kind='issue_signature' JSON 배열 안

각 DB 는 손대기 전에 `<파일명>.bak_bimodality_<timestamp>` 로 복사한다.
멱등하다 — 두 번 돌려도 안전하고, 남은 SUBPOP_GAP 이 없으면 아무 것도 하지 않는다.
signature 는 PK 의 일부라 이미 BIMODALITY 행이 있는 case 는 UPDATE 가 충돌하므로
`UPDATE OR IGNORE` 후 남은 구 행을 지운다(= 중복 병합).

사용:
    cd server
    .venv/Scripts/python.exe tools/migrate_bimodality_rename.py --dry-run
    .venv/Scripts/python.exe tools/migrate_bimodality_rename.py

⚠ 서버를 내린 상태에서 실행할 것 (실행 중 쓰기와 겹치면 백업 시점이 어긋난다).
"""
import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
_ROOT = _SERVER.parent
# eval_analyzer 는 eval_engine 패키지의 부모 — DB 경로 하나만 읽으려고 넣는다
# (import 는 config 뿐이라 단방향 의존 규약의 "엔진 import 3곳" 과 무관하다).
for p in (str(_SERVER), str(_ROOT), str(_ROOT / "eval_analyzer")):
    if p not in sys.path:
        sys.path.insert(0, p)

OLD_ID = "SUBPOP_GAP"
NEW_ID = "BIMODALITY"
_SIG_TABLES = ("case_signature", "label_signature")


def _backup(path: Path) -> Path:
    dst = path.with_suffix(path.suffix + f".bak_bimodality_{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, dst)
    return dst


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _migrate_eval_db(path: Path, label: str, dry_run: bool) -> int:
    """eval.db 계열 — signature 컬럼을 가진 두 테이블을 치환. 반환: 바뀐 행 수."""
    if not path.exists():
        print(f"  [{label}] 파일 없음 → skip ({path})")
        return 0
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        counts = {t: conn.execute(f"SELECT count(*) FROM {t} WHERE signature=?",
                                  (OLD_ID,)).fetchone()[0]
                  for t in _SIG_TABLES if _table_exists(conn, t)}
        total = sum(counts.values())
        detail = ", ".join(f"{t}={n}" for t, n in counts.items()) or "대상 테이블 없음"
        print(f"  [{label}] {detail} → 합계 {total}건")
        if total == 0 or dry_run:
            return total
        _backup_note = _backup(path)
        print(f"  [{label}] 백업 {_backup_note.name}")
        for table in counts:
            conn.execute(f"UPDATE OR IGNORE {table} SET signature=? WHERE signature=?",
                         (NEW_ID, OLD_ID))
            # UPDATE OR IGNORE 로 남은 것 = 같은 case 에 BIMODALITY 행이 이미 있던 중복
            conn.execute(f"DELETE FROM {table} WHERE signature=?", (OLD_ID,))
        conn.commit()
        return total
    finally:
        conn.close()


def _migrate_report_db(path: Path, dry_run: bool) -> int:
    """report.db — issue_signature 편집행의 JSON 배열 안 문자열을 치환. 반환: 바뀐 행 수."""
    if not path.exists():
        print(f"  [report.db] 파일 없음 → skip ({path})")
        return 0
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        if not _table_exists(conn, "report_webreport_edit"):
            print("  [report.db] report_webreport_edit 없음 → skip")
            return 0
        rows = conn.execute(
            "SELECT rowid, value FROM report_webreport_edit "
            "WHERE kind='issue_signature' AND value LIKE ?", (f"%{OLD_ID}%",)).fetchall()
        patched = []
        for rowid, value in rows:
            try:
                ids = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(ids, list) or OLD_ID not in ids:
                continue
            # 순서 보존 + 중복 제거 (구/신 id 가 함께 들어 있는 행 대비)
            seen, new_ids = set(), []
            for sid in ids:
                sid = NEW_ID if sid == OLD_ID else sid
                if sid not in seen:
                    seen.add(sid)
                    new_ids.append(sid)
            patched.append((rowid, json.dumps(new_ids, ensure_ascii=False)))
        print(f"  [report.db] issue_signature 편집행 {len(patched)}건")
        if not patched or dry_run:
            return len(patched)
        print(f"  [report.db] 백업 {_backup(path).name}")
        conn.executemany("UPDATE report_webreport_edit SET value=? WHERE rowid=?",
                         [(v, r) for r, v in patched])
        conn.commit()
        return len(patched)
    finally:
        conn.close()


def main(dry_run: bool) -> int:
    from eval_engine import config as eval_config     # noqa: E402  (sys.path 세팅 후)
    import config as server_config                    # noqa: E402

    print(f"{OLD_ID} → {NEW_ID} 치환" + (" (dry-run — 쓰지 않음)" if dry_run else ""))
    total = 0
    total += _migrate_eval_db(Path(eval_config.DB_PATH), "engine eval.db", dry_run)
    total += _migrate_eval_db(Path(server_config.REPORT_EVAL_DB_PATH), "export eval.db", dry_run)
    total += _migrate_report_db(Path(server_config.REPORT_DB_PATH), dry_run)
    print(f"합계 {total}건" + (" (dry-run)" if dry_run else " 치환 완료"))
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="대상 건수만 세고 쓰지 않는다")
    raise SystemExit(main(ap.parse_args().dry_run))
