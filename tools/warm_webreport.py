"""재기동/배포 직후 web_report 세션의 report payload 를 미리 계산해 디스크 캐시를 채운다.

왜 필요한가: report payload 캐시 키에는 `cache_policy.REPORT_SCHEMA_VERSION` 이 들어간다.
이 값을 올린 배포는 **전 세션의 디스크 캐시를 한 번에 무효화**하므로, 배포 직후 조회가
몰리면 온디맨드 워커(기본 2, 운영 4)에 콜드 빌드가 줄을 서고 사용자는 그만큼 오래
"세션 불러오는 중" 을 본다. 한산한 시간에 이 스크립트로 미리 데워두면 그 줄이 사라진다.

새 빌드 로직을 만들지 않는다. 이미 검증된 진입점을 세션마다 부르기만 한다:
    web_report/service.py:load_webreport  (콜드 경로가 disk_cache 를 채운다)

서버와 **같은 머신에서 별도 프로세스로** 돌아도 안전하다 — disk_cache 쓰기가
pid/tid 유니크 임시파일 + os.replace 라 동시 쓰기가 서로를 깨지 않는다
(web_report/disk_cache.py). 서버 기동 전에 돌려도, 기동 후에 돌려도 된다.

기본은 **dry-run**(대상만 보여주고 계산하지 않는다). 실제 웜업은 --apply.

실행:
    python tools/warm_webreport.py                          # dry-run (최근 30일·200건)
    python tools/warm_webreport.py --apply
    python tools/warm_webreport.py --apply --days 7 --limit 50
    python tools/warm_webreport.py --apply --session <session_id>

주의:
- 계산은 CPU 를 쓴다. 서비스 중 서버에서 돌린다면 한가한 시간대에 실행할 것.
- 세션 1건씩 **순차** 실행한다(서버와의 CPU 경합을 최소화). 병렬 옵션은 두지 않았다.
- 이미 캐시가 있는 세션은 건너뛴다 — 여러 번 돌려도 낭비가 없다.
- 세션 목록에 viewer 를 넘기지 않는다(관리 작업이라 비공개 세션도 대상).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# 워커 프로세스 풀을 쓰지 않고 이 프로세스에서 인라인 계산한다 — 단독 스크립트에서
# ProcessPool 을 띄우면 자식이 서버 모듈을 다시 임포트할 뿐 이득이 없다.
# web_report.compute 가 import 시점에 이 값을 읽으므로 **import 전에** 설정한다.
os.environ.setdefault("WEB_REPORT_COMPUTE_WORKERS", "0")

import config  # noqa: E402
from database import report_db  # noqa: E402
from web_report import cache_policy, disk_cache, service  # noqa: E402


def _is_warm(session: dict, upload_root: Path) -> bool:
    """이 세션의 report payload 가 이미 디스크 캐시에 있는가 (stat 1회)."""
    edits_rev = report_db.get_webreport_edit_rev(session["session_id"])
    key = cache_policy.report_key(session, session["session_id"], edits_rev)
    return disk_cache.report_exists(upload_root, key)


def _targets(days: int, limit: int, session_id: str | None) -> list:
    if session_id:
        session = report_db.get_session(session_id)
        if not session:
            print(f"세션을 찾을 수 없습니다: {session_id}")
            return []
        return [dict(session)]
    date_from = None
    if days > 0:
        # get_history 의 date_from 은 report_session.created_at 과 같은 **epoch 초**다
        # (날짜 문자열을 넘기면 _history_where 의 int() 에서 깨진다).
        date_from = int(time.time() - days * 86400)
    rows = report_db.get_history(source="web_report", limit=limit, date_from=date_from,
                                 see_all_private=True)
    # get_history 는 목록용 컬럼만 준다 — 캐시 키에 필요한 analysis_key/content_hash/
    # webreport_options 를 얻으려면 세션 원본을 다시 읽어야 한다.
    out = []
    for row in rows:
        session = report_db.get_session(row["session_id"])
        if session:
            out.append(dict(session))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="web_report 세션 report 캐시 선계산")
    ap.add_argument("--apply", action="store_true", help="실제로 계산한다 (기본은 dry-run)")
    ap.add_argument("--days", type=int, default=30, help="최근 N일 세션만 (0=전체, 기본 30)")
    ap.add_argument("--limit", type=int, default=200, help="최대 세션 수 (기본 200)")
    ap.add_argument("--session", default=None, help="이 세션 1건만")
    args = ap.parse_args()

    upload_root = Path(config.REPORT_UPLOAD_DIR)
    sessions = _targets(args.days, args.limit, args.session)
    if not sessions:
        print("대상 세션이 없습니다.")
        return 0

    cold = []
    for session in sessions:
        try:
            if not _is_warm(session, upload_root):
                cold.append(session)
        except Exception as exc:
            print(f"[skip] {session.get('session_id')} 캐시 판정 실패: {exc}")

    print(f"대상 {len(sessions)}건 중 콜드 {len(cold)}건 "
          f"(웜 {len(sessions) - len(cold)}건은 건너뜁니다)")
    if not args.apply:
        for session in cold:
            print(f"  - {session['session_id']}  {session.get('file_name') or ''}")
        print("\n실제 웜업은 --apply 를 붙여 실행하세요.")
        return 0

    ok = fail = 0
    t_all = time.time()
    for i, session in enumerate(cold, 1):
        sid = session["session_id"]
        t0 = time.time()
        try:
            service.load_webreport(sid, report_db=report_db, upload_root=upload_root,
                                   session=session)
            ok += 1
            print(f"[{i}/{len(cold)}] {sid} 완료 ({time.time() - t0:.1f}s)")
        except Exception as exc:
            fail += 1
            print(f"[{i}/{len(cold)}] {sid} 실패: {type(exc).__name__}: {exc}")
    print(f"\n웜업 완료 — 성공 {ok}건 / 실패 {fail}건 / 총 {time.time() - t_all:.1f}s")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
