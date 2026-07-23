"""감사 로그 (업로드/수정/삭제) 기록·조회·롤오프 (report_db facade 구현)."""
from .core import get_conn, _now

_AUDIT_COLUMNS = (
    "action", "session_id", "analysis_key", "product_type", "product",
    "lot_id", "file_name", "changed_fields", "client_ip", "user_agent",
    "client_user", "client_host", "result", "created_at",
)


def log_audit(action, session_id=None, analysis_key=None, product_type=None,
              product=None, lot_id=None, file_name=None, changed_fields=None,
              client_ip=None, user_agent=None, client_user=None, client_host=None,
              result="ok", busy_timeout_ms=5000):
    """업로드/수정/삭제 감사 기록 1행 추가. user_agent 는 과도하게 길면 잘라 저장."""
    if user_agent and len(user_agent) > 500:
        user_agent = user_agent[:500]
    values = (
        action, session_id, analysis_key, product_type, product,
        lot_id, file_name, changed_fields, client_ip, user_agent,
        client_user, client_host, result, _now(),
    )
    placeholders = ", ".join("?" for _ in _AUDIT_COLUMNS)
    cols = ", ".join(_AUDIT_COLUMNS)
    with get_conn(busy_timeout_ms=busy_timeout_ms) as conn:
        conn.execute(
            f"INSERT INTO report_audit_log ({cols}) VALUES ({placeholders})",
            values,
        )


def get_audit_logs(action=None, session_id=None, q=None, limit=200, offset=0):
    """감사 로그 조회. action/session_id 필터 + q(파일명/product/lot_id/사용자/PC/IP/변경필드 부분일치)."""
    conditions = []
    params = []
    if action:
        conditions.append("action = ?")
        params.append(action)
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if q:
        conditions.append(
            "(file_name LIKE ? OR product LIKE ? OR lot_id LIKE ? "
            " OR client_user LIKE ? OR client_host LIKE ? OR client_ip LIKE ? "
            " OR changed_fields LIKE ?)")
        like = f"%{q}%"
        params.extend([like] * 7)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 200
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    params.extend([limit, offset])
    sql = f"""
        SELECT id, action, session_id, analysis_key, product_type, product,
               lot_id, file_name, changed_fields, client_ip, user_agent,
               client_user, client_host, result, created_at
        FROM report_audit_log
        {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
    """
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def recent_upload_user_by_ip(client_ip, since_epoch):
    """같은 IP 에서 마지막으로 Honey 업로드한 계정 (없으면 None) — 웹 회원가입 창의
    ID 자동완성 힌트용. 신원 판단에는 쓰지 않는다(공유 PC·DHCP 재할당에서 어긋남).

    idx_report_audit_action(action, created_at DESC) 를 타고, since_epoch 로 스캔 범위를
    제한한다."""
    if not client_ip:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT client_user FROM report_audit_log "
            " WHERE action='upload' AND client_ip=? AND client_user IS NOT NULL "
            "   AND client_user<>'' AND created_at>=? "
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (client_ip, int(since_epoch)),
        ).fetchone()
    return row["client_user"] if row else None


def purge_audit_logs(cutoff_epoch):
    """created_at 이 cutoff 이전인 감사 로그 행 삭제 (롤오프). 삭제 행 수 반환.

    report_audit_log 는 세션 삭제 시에도 의도적으로 보존되어 무한 증가하므로,
    cleanup 스케줄러가 REPORT_AUDIT_RETENTION_DAYS 기준으로 주기 호출한다.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM report_audit_log WHERE created_at < ?", (int(cutoff_epoch),))
        return cur.rowcount
