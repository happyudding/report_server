"""기존 web_report 세션의 Issue Table 코멘트를 eval.db 로 일괄 재적재(백필).

왜 필요한가: 챗봇의 **item 축**(항목 이름으로 과거를 뒤지는 검색)은 eval.db 의
`item_master`/`item_alias`/`fail_case` 위에서만 성립한다. report.db 쪽
`report_analysis_summary.item_name` 은 실제로 bin 번호 문자열이라 item 축이 아니다.
평소에는 업로드/코멘트 편집 훅이 세션 단위로 export 하지만, 훅이 생기기 전에 올라온
세션은 eval.db 에 없다 — 이 스크립트가 그 공백을 메운다.

새 export 로직을 만들지 않는다. 이미 검증된 진입점을 세션마다 부르기만 한다:
    web_report/eval_export.py:export_session_comments  (멱등 — 여러 번 돌려도 안전)

기본은 **dry-run**(대상만 보여주고 아무것도 쓰지 않는다). 실제 적재는 --apply.

실행:
    python tools/eval_backfill/backfill_eval_db.py                 # dry-run
    python tools/eval_backfill/backfill_eval_db.py --apply
    python tools/eval_backfill/backfill_eval_db.py --apply --session <session_id>

주의:
- 대상 eval.db 는 config.REPORT_EVAL_DB_PATH (없으면 새로 만들어진다). eval.db **스키마를
  바꾸지 않는다** — store.SCHEMA 를 그대로 적용할 뿐이다.
- export 는 parquet 로드를 동반할 수 있어 CPU 를 쓴다. 서비스 중 서버에서 돌린다면
  한가한 시간대에 실행할 것.
- 세션 목록 조회에 viewer 를 넘기지 않는다(관리 작업이라 비공개 세션도 대상). 이건
  의도된 예외이고, 챗봇 조회 경로는 반대로 viewer 를 반드시 넘긴다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT), str(_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

import config  # noqa: E402
from database import report_db  # noqa: E402
from web_report import eval_export  # noqa: E402


def _targets(session_id=None):
    """백필 대상 web_report 세션 목록."""
    if session_id:
        session = report_db.get_session(session_id)
        return [dict(session)] if session else []
    return report_db.get_history(source="web_report", limit=5000, viewer=None)


def _comment_count(session_id):
    """이 세션에 남아 있는 issue_comment 행 수 (dry-run 표시용)."""
    try:
        rows = report_db.get_webreport_edits(session_id, kinds=("issue_comment",))
        return len(rows)
    except Exception:
        return -1


def main(argv=None):
    parser = argparse.ArgumentParser(description="web_report 세션 코멘트 → eval.db 백필")
    parser.add_argument("--apply", action="store_true",
                        help="실제로 적재한다 (없으면 dry-run)")
    parser.add_argument("--session", help="이 세션 하나만 처리")
    parser.add_argument("--limit", type=int, default=0, help="처리할 최대 세션 수(0=전부)")
    args = parser.parse_args(argv)

    db_path = Path(config.REPORT_EVAL_DB_PATH)
    sessions = _targets(args.session)
    if args.limit:
        sessions = sessions[:args.limit]

    print(f"eval.db      : {db_path} ({'있음' if db_path.exists() else '없음 — 새로 생성됨'})")
    print(f"대상 세션    : {len(sessions)}건 (source=web_report)")
    print(f"모드         : {'APPLY (실제 적재)' if args.apply else 'DRY-RUN (쓰기 없음)'}")
    print()

    if not sessions:
        print("대상 세션이 없습니다.")
        return 0

    if not args.apply:
        total_comments = 0
        for s in sessions:
            n = _comment_count(s["session_id"])
            total_comments += max(n, 0)
            print(f"  - {s['session_id']} / {s.get('product') or '?'} / "
                  f"lot {s.get('lot_id') or '-'} / issue_comment {n}행")
        print(f"\n합계 issue_comment {total_comments}행. 실제 적재하려면 --apply 를 붙이세요.")
        return 0

    ok = skipped = failed = 0
    cases = labels = 0
    for s in sessions:
        sid = s["session_id"]
        try:
            result = eval_export.export_session_comments(
                sid, report_db=report_db, upload_root=Path(config.REPORT_UPLOAD_DIR))
        except Exception as exc:  # 한 세션 실패가 전체를 멈추지 않게
            failed += 1
            print(f"  [FAIL] {sid}: {exc}")
            continue
        if result.get("skipped"):
            skipped += 1
            print(f"  [SKIP] {sid}: {result['skipped']}")
            continue
        ok += 1
        cases += int(result.get("cases") or 0)
        labels += int(result.get("labels") or 0)
        print(f"  [OK  ] {sid}: cases={result.get('cases')} labels={result.get('labels')} "
              f"removed={result.get('removed')}")

    print(f"\n적재 완료: 성공 {ok} / 건너뜀 {skipped} / 실패 {failed}")
    print(f"          case {cases}건, label {labels}건")
    print(f"\n확인: python -m chatbot --eval-db \"{db_path}\" --no-llm \"SGM 항목 이력\"")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
