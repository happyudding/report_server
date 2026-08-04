"""value_type 어휘 P_F → PF 일괄 변환 (2026-08-04).

엔진이 쓰는 어휘를 `P_F` 에서 `PF` 로 바꿨으므로, 이미 적재된 DB 의 값도 함께
옮겨야 선례검색(`search_precedents` 가 value_type 을 등호 하드필터로 씀)이 갈리지 않는다.

**스키마(DDL) 변경 없음** — 값만 UPDATE 한다.
  - item_master.value_type : 'P_F' → 'PF'
  - fail_case.item_class   : '<cat>|P_F|<bin>' → '<cat>|PF|<bin>' (가운데 축만 치환)

대상 DB 는 둘일 수 있다 (경로를 인자로 준다):
  1. eval_analyzer 운영 eval.db      (EVAL_DB_PATH / eval_analyzer/data/eval.db)
  2. report_server 코멘트 export DB  (REPORT_EVAL_DB_PATH)

사용:
    python -m tools.migrate_value_type_pf <db경로> [<db경로> ...]     # 미리보기
    python -m tools.migrate_value_type_pf <db경로> --apply            # 실제 변경
`--apply` 없이 실행하면 바꿀 건수만 세고 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

OLD, NEW = "P_F", "PF"


def _counts(conn) -> dict:
    item = conn.execute("SELECT COUNT(*) FROM item_master WHERE value_type = ?",
                        (OLD,)).fetchone()[0]
    case = conn.execute("SELECT COUNT(*) FROM fail_case WHERE item_class LIKE ?",
                        (f"%|{OLD}|%",)).fetchone()[0]
    return {"item_master": item, "fail_case": case}


def migrate(path: Path, apply: bool) -> dict:
    if not path.is_file():
        return {"path": str(path), "error": "파일 없음"}
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        before = _counts(conn)
        if apply and (before["item_master"] or before["fail_case"]):
            with conn:
                conn.execute("UPDATE item_master SET value_type = ? WHERE value_type = ?",
                             (NEW, OLD))
                # item_class = "<category_major>|<value_type>|<bin>" — 가운데 축만 바꾼다.
                conn.execute(
                    "UPDATE fail_case SET item_class = "
                    "  replace(item_class, ?, ?) "
                    "WHERE item_class LIKE ?",
                    (f"|{OLD}|", f"|{NEW}|", f"%|{OLD}|%"))
        after = _counts(conn) if apply else before
        return {"path": str(path), "before": before, "remaining": after}
    finally:
        conn.close()


def main(argv) -> int:
    apply = "--apply" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    rc = 0
    for path in paths:
        result = migrate(path, apply)
        if result.get("error"):
            print(f"[skip] {result['path']}: {result['error']}")
            rc = 1
            continue
        b = result["before"]
        print(f"[{'적용' if apply else '미리보기'}] {result['path']}: "
              f"item_master {b['item_master']}건 · fail_case {b['fail_case']}건")
        if apply:
            r = result["remaining"]
            print(f"    남은 P_F: item_master {r['item_master']} · fail_case {r['fail_case']}")
    if not apply:
        print("\n실제로 바꾸려면 --apply 를 붙여 다시 실행하세요.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
