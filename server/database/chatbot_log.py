"""웹 챗봇 질문/답변 + 부하 계측 기록 (report_db facade 구현).

관리자 대시보드 Chatbot 탭이 읽는 유일한 저장소다. 기록은 **best-effort** — 로그를 못
남겼다고 사용자 답변이 실패하면 안 되므로 예외를 삼킨다(감사 로그와 같은 태도).
"""
import json
import logging

from .core import get_conn, _now

_log = logging.getLogger(__name__)

_COLUMNS = ("created_at", "user", "client_ip", "context_session_id", "question",
            "answer", "intent", "planner", "plan_json", "steps_json",
            "total_ms", "wait_ms", "llm_ms", "result")

# 질문은 라우트가 500자로 막지만 답변은 상한이 없다 — 표 하나가 DB 를 키우지 않게 자른다.
_ANSWER_CAP = 20000


def log_chat(*, question, user=None, client_ip=None, context_session_id=None,
             answer=None, intent=None, planner=None, plan=None, steps=None,
             total_ms=None, wait_ms=None, llm_ms=None, result="ok"):
    """챗 1건 기록. 실패해도 조용히 넘어간다."""
    try:
        text = str(answer or "")
        if len(text) > _ANSWER_CAP:
            text = text[:_ANSWER_CAP] + "…(생략)"
        values = (_now(), user, client_ip, context_session_id, str(question or ""),
                  text or None, intent, planner, _dump(plan), _dump(steps),
                  _int(total_ms), _int(wait_ms), _int(llm_ms), result)
        cols = ", ".join(f'"{c}"' for c in _COLUMNS)
        marks = ", ".join("?" for _ in _COLUMNS)
        with get_conn(busy_timeout_ms=2000) as conn:
            conn.execute(f"INSERT INTO report_chatbot_log ({cols}) VALUES ({marks})",
                         values)
    except Exception:
        _log.debug("chatbot 로그 기록 실패 — 무시", exc_info=True)


def _dump(value):
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def list_chats(q=None, limit=50, offset=0):
    """최신순 목록. q 는 질문·답변·사용자·intent 부분일치."""
    where, params = "", []
    if q:
        where = (' WHERE (question LIKE ? OR answer LIKE ? OR "user" LIKE ?'
                 " OR intent LIKE ?)")
        params = [f"%{q}%"] * 4
    limit = max(1, min(_int(limit) or 50, 500))
    offset = max(0, _int(offset) or 0)
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM report_chatbot_log{where}", params).fetchone()["n"]
        rows = conn.execute(
            f'SELECT id, created_at, "user", client_ip, context_session_id, question,'
            f" answer, intent, planner, total_ms, wait_ms, llm_ms, result"
            f" FROM report_chatbot_log{where}"
            f" ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset]).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total,
            "limit": limit, "offset": offset}


def chat_stats(hours=24):
    """최근 N시간 요약 — 건수·응답시간·LLM 사용률·사용자별 건수."""
    since = _now() - max(1, _int(hours) or 24) * 3600
    with get_conn() as conn:
        agg = conn.execute(
            "SELECT COUNT(*) AS total,"
            "       SUM(CASE WHEN result='ok' THEN 1 ELSE 0 END) AS ok,"
            "       SUM(CASE WHEN result='busy' THEN 1 ELSE 0 END) AS busy,"
            "       SUM(CASE WHEN result LIKE 'error%' THEN 1 ELSE 0 END) AS errors,"
            "       AVG(total_ms) AS avg_ms, MAX(total_ms) AS max_ms,"
            "       AVG(wait_ms) AS avg_wait_ms, MAX(wait_ms) AS max_wait_ms,"
            "       AVG(llm_ms) AS avg_llm_ms, MAX(llm_ms) AS max_llm_ms,"
            "       SUM(CASE WHEN planner='llm' THEN 1 ELSE 0 END) AS llm_planned"
            " FROM report_chatbot_log WHERE created_at >= ?", (since,)).fetchone()
        users = conn.execute(
            'SELECT COALESCE(NULLIF(TRIM("user"), \'\'), \'(무신원)\') AS user,'
            " COUNT(*) AS n FROM report_chatbot_log WHERE created_at >= ?"
            " GROUP BY 1 ORDER BY n DESC LIMIT 10", (since,)).fetchall()
        intents = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(intent), ''), '(없음)') AS intent,"
            " COUNT(*) AS n FROM report_chatbot_log WHERE created_at >= ?"
            " GROUP BY 1 ORDER BY n DESC LIMIT 12", (since,)).fetchall()
        grand = conn.execute("SELECT COUNT(*) AS n FROM report_chatbot_log").fetchone()
    out = {k: agg[k] for k in agg.keys()}
    for key in ("avg_ms", "avg_wait_ms", "avg_llm_ms"):
        out[key] = round(out[key]) if out.get(key) is not None else None
    out["hours"] = max(1, _int(hours) or 24)
    out["all_time"] = grand["n"]
    out["by_user"] = [dict(r) for r in users]
    out["by_intent"] = [dict(r) for r in intents]
    return out


def purge_chat_logs(cutoff_epoch):
    """created_at 이 cutoff 이전인 챗 로그 삭제. 삭제 행 수 반환."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM report_chatbot_log WHERE created_at < ?",
                           (int(cutoff_epoch),))
        return cur.rowcount
