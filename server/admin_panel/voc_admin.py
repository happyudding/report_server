"""VOC 게시판 읽기 전용 조회 — 관리자 패널 VOC 탭.

voc_db.py 는 수정하지 않고 open_conn / list_voc 만 재사용한다. 등록·수정·상태 전환·삭제는
사용자 게시판(/pe/report/voc, report/routes_voc.py) 쪽 권한 흐름이 정본이라 여기는 조회만
제공한다.
"""
import time

from database import voc_db


def overview():
    """카운트 타일 — 전체 / 최근 7일 / 미처리(open). 목록 상한과 무관하게 COUNT 로 센다."""
    cutoff = int(time.time()) - 7 * 86400
    with voc_db.open_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM report_voc").fetchone()[0]
        last7 = conn.execute(
            "SELECT COUNT(*) FROM report_voc WHERE created_at >= ?", (cutoff,)).fetchone()[0]
        open_cnt = conn.execute(
            "SELECT COUNT(*) FROM report_voc WHERE status = 'open'").fetchone()[0]
    return {"total": int(total), "last_7d": int(last7), "open": int(open_cnt)}


def list_voc(q=None, limit=50, offset=0):
    """VOC 목록 (최신순, 본문 제외). limit/offset 클램프는 list_sessions 와 같은 관례."""
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    rows, total = voc_db.list_voc(limit=limit, offset=offset, q=q)
    return {"total": int(total), "limit": limit, "offset": offset, "rows": rows}
