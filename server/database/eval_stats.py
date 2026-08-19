"""eval 룰 엔진 일별 지표 집계 (report_db facade 구현).

**왜 필요한가**: 판정 근거(evaluation/features/label)는 eval.db 에 계속 쌓이는데, 정확도와
커버리지는 관리자가 탭을 열 때 **전체 누적 한 숫자**로만 나왔다. 그래서 "룰을 고쳤더니
나아졌나"를 볼 방법이 없었다. 여기서 날짜별로 접어 두면 추이가 남는다.

읽기 원본은 eval.db(`REPORT_EVAL_DB_PATH`) **읽기 전용**, 쓰기 대상은 report.db 다.
집계는 **재계산 UPSERT** — 원본이 남아 있으므로 같은 날을 몇 번 다시 접어도 같은 값이
나와야 한다(원문을 지우고 더하는 `rollup_chat_daily` 와 규약이 반대다).

집계하는 지표 4종과 그 연료:
  - **커버리지**: 스냅샷 case 중 fail 이 있는데 원인을 설명 못 한 비율(UNKNOWN).
  - **signature 일치율**: Issue Table `[확정]`(labeler='web-signature')으로 사람이 지목한
    원인 집합 vs 엔진 발화 집합. 확정 버튼이 그대로 채점 연료가 된다.
  - **코멘트 정합**: Status=Close 코멘트 라벨 중 엔진 판정과 case 가 이어진 비율.
    2026-08-19 이전에는 wafer 축이 어긋나 구조적으로 0 이었다(docs/17).
  - **status 채점**: 관리자 정답 라벨(labeler='eval-panel')의 일치 쌍 수.
"""
import logging
import time

from .core import get_conn, _now

_log = logging.getLogger(__name__)

# eval.db 의 run 표식 — 사람 라벨용 run(web_report/eval-panel/web-signature)이 섞이면
# 분모가 오염된다(web-signature run 의 evaluation 은 status 가 NULL 인 placeholder 다).
_SNAPSHOT_INGESTED_BY = "eval-snapshot"
_COMMENT_LABELER = "web_report"
_SIGNATURE_LABELER = "web-signature"
_PANEL_LABELER = "eval-panel"

_COUNT_COLS = ("runs", "cases", "fail_cases", "unknown_cases",
               "sig_labeled", "sig_exact", "sig_overlap",
               "comment_labels", "comment_matched", "score_pairs", "score_agree")


def _day_expr(col):
    """epoch 컬럼 → 'YYYY-MM-DD'(서버 localtime). report.db 쪽 day 컬럼과 같은 규칙."""
    return f"date({col}, 'unixepoch', 'localtime')"


def _bucket(acc, day, engine_version):
    key = (str(day), str(engine_version or ""))
    return acc.setdefault(key, dict.fromkeys(_COUNT_COLS, 0))


def collect_eval_daily(since_day=None, conn=None):
    """eval.db 를 읽어 {(day, engine_version): {지표…}} 로 집계. eval DB 가 없으면 {}.

    `since_day`('YYYY-MM-DD')를 주면 그 날짜 이후만 — 매일 도는 스케줄러가 전 기간을
    다시 훑지 않게 한다. day 컬럼이 문자열이라 사전순 비교가 곧 날짜 비교다.
    `conn` 은 테스트용 주입구(열린 eval.db 커넥션).
    """
    close_after = False
    if conn is None:
        from web_report import eval_export
        conn = eval_export.open_conn(create=False)
        if conn is None:
            return {}
        close_after = True
    try:
        return _collect(conn, since_day)
    finally:
        if close_after:
            conn.close()


def _collect(conn, since_day):
    acc: dict = {}
    since = str(since_day) if since_day else "0000-00-00"

    # ① 스냅샷 축적 + 커버리지(UNKNOWN) — run 단위 수집 시점 기준.
    #    primary signature 가 없거나 UNKNOWN 인 fail case 가 "설명 못 한 fail" 이다.
    rows = conn.execute(f"""
        SELECT {_day_expr('ir.created_at')} AS day, ev.engine_version AS ver,
               COUNT(*) AS cases,
               SUM(CASE WHEN COALESCE(rm.fail_count, 0) > 0 THEN 1 ELSE 0 END) AS fail_cases,
               SUM(CASE WHEN COALESCE(rm.fail_count, 0) > 0
                         AND COALESCE(cs.signature, 'UNKNOWN') = 'UNKNOWN'
                        THEN 1 ELSE 0 END) AS unknown_cases
          FROM ingest_run ir
          JOIN evaluation ev ON ev.run_id = ir.run_id
          LEFT JOIN raw_metrics rm ON rm.case_id = ev.case_id AND rm.run_id = ev.run_id
          LEFT JOIN case_signature cs ON cs.eval_id = ev.eval_id AND cs.role = 'primary'
         WHERE ir.ingested_by = ? AND {_day_expr('ir.created_at')} >= ?
         GROUP BY day, ver""", (_SNAPSHOT_INGESTED_BY, since)).fetchall()
    for r in rows:
        b = _bucket(acc, r["day"], r["ver"])
        b["cases"] += int(r["cases"] or 0)
        b["fail_cases"] += int(r["fail_cases"] or 0)
        b["unknown_cases"] += int(r["unknown_cases"] or 0)

    rows = conn.execute(f"""
        SELECT {_day_expr('created_at')} AS day, COUNT(*) AS n
          FROM ingest_run
         WHERE ingested_by = ? AND {_day_expr('created_at')} >= ?
         GROUP BY day""", (_SNAPSHOT_INGESTED_BY, since)).fetchall()
    for r in rows:
        # run 은 engine_version 축이 없다(판정이 아니라 수집 단위) — '' 버킷에 넣는다.
        _bucket(acc, r["day"], "")["runs"] += int(r["n"] or 0)

    # ② signature 확정(✓) vs 엔진 발화 — 사람이 지목한 원인 집합과 대조.
    #    확정 라벨은 status=NULL placeholder evaluation 에 달리므로(eval_export
    #    sync_session_signatures), 엔진 발화는 **같은 case 의 최신 스냅샷 판정**에서 찾는다.
    _collect_signature_match(conn, acc, since)

    # ③ 사람 코멘트(Close) 라벨과 그 정합 — case 가 엔진 판정과 이어졌는지.
    rows = conn.execute(f"""
        SELECT {_day_expr('l.created_at')} AS day, COUNT(*) AS n,
               SUM(CASE WHEN EXISTS (
                     SELECT 1 FROM evaluation ev
                       JOIN ingest_run ir ON ir.run_id = ev.run_id
                      WHERE ev.case_id = l.case_id AND ir.ingested_by = ?
                   ) THEN 1 ELSE 0 END) AS matched
          FROM label l
         WHERE l.labeler = ? AND {_day_expr('l.created_at')} >= ?
         GROUP BY day""",
        (_SNAPSHOT_INGESTED_BY, _COMMENT_LABELER, since)).fetchall()
    for r in rows:
        b = _bucket(acc, r["day"], "")
        b["comment_labels"] += int(r["n"] or 0)
        b["comment_matched"] += int(r["matched"] or 0)

    # ④ 관리자 정답 라벨 채점 — eval_admin.scoring() 과 같은 조건(=같은 모집단).
    rows = conn.execute(f"""
        SELECT {_day_expr('l.created_at')} AS day, ev.engine_version AS ver,
               COUNT(*) AS n,
               SUM(CASE WHEN ev.status = l.human_status THEN 1 ELSE 0 END) AS agree
          FROM label l
          JOIN evaluation ev ON ev.eval_id = l.eval_id
         WHERE l.labeler = ? AND l.human_status IS NOT NULL AND ev.status IS NOT NULL
           AND {_day_expr('l.created_at')} >= ?
         GROUP BY day, ver""", (_PANEL_LABELER, since)).fetchall()
    for r in rows:
        b = _bucket(acc, r["day"], r["ver"])
        b["score_pairs"] += int(r["n"] or 0)
        b["score_agree"] += int(r["agree"] or 0)
    return acc


def _collect_signature_match(conn, acc, since):
    """확정 라벨 1건마다 사람 지목 집합 vs 엔진 발화 집합을 비교해 버킷에 더한다.

    집합 비교라 SQL 한 방으로 접을 수 없어 라벨 단위로 모은 뒤 파이썬에서 맞춘다.
    확정 라벨은 사람이 누른 것이라 하루 수십 건 규모다(전량 스캔해도 가볍다).
    엔진 쪽 기준은 **그 case 의 최신 스냅샷 evaluation** — 확정 시점에 화면이 보여 준
    판정과 같은 것을 보려면 최신 하나여야 한다(review/signature_reason 과 같은 규약).
    """
    labels = conn.execute(f"""
        SELECT l.label_id, l.case_id, {_day_expr('l.created_at')} AS day
          FROM label l
         WHERE l.labeler = ? AND {_day_expr('l.created_at')} >= ?""",
        (_SIGNATURE_LABELER, since)).fetchall()
    if not labels:
        return
    for lb in labels:
        human = {r[0] for r in conn.execute(
            "SELECT signature FROM label_signature WHERE label_id=?", (lb["label_id"],))}
        row = conn.execute("""
            SELECT ev.eval_id, ev.engine_version
              FROM evaluation ev
              JOIN ingest_run ir ON ir.run_id = ev.run_id
             WHERE ev.case_id = ? AND ir.ingested_by = ?
             ORDER BY ev.eval_id DESC LIMIT 1""",
            (lb["case_id"], _SNAPSHOT_INGESTED_BY)).fetchone()
        if row is None:
            continue          # 스냅샷 없는 세션 — 대조할 엔진 판정이 없다
        fired = {r[0] for r in conn.execute(
            "SELECT signature FROM case_signature WHERE eval_id=?", (row["eval_id"],))}
        b = _bucket(acc, lb["day"], row["engine_version"])
        b["sig_labeled"] += 1
        if human and human == fired:
            b["sig_exact"] += 1
        if human & fired:
            b["sig_overlap"] += 1


def save_eval_daily(buckets, now=None):
    """집계 결과를 report_eval_daily 에 **재계산 UPSERT**. 갱신한 행 수 반환.

    누적 더하기가 아니라 덮어쓰기다 — 원본(eval.db)이 그대로 있으므로 같은 날을 다시
    집계하면 같은 값이 나와야 한다. 더하면 스케줄러가 도는 만큼 값이 부풀어 오른다.
    """
    if not buckets:
        return 0
    now = _now() if now is None else int(now)
    sets = ", ".join(f"{c}=excluded.{c}" for c in _COUNT_COLS)
    sql = (f"INSERT INTO report_eval_daily (day, engine_version, {', '.join(_COUNT_COLS)},"
           f" updated_at) VALUES (?, ?, {', '.join('?' * len(_COUNT_COLS))}, ?)"
           f" ON CONFLICT(day, engine_version) DO UPDATE SET {sets},"
           f" updated_at=excluded.updated_at")
    with get_conn() as conn:
        for (day, ver), vals in buckets.items():
            conn.execute(sql, (day, ver, *[int(vals.get(c) or 0) for c in _COUNT_COLS], now))
    return len(buckets)


def rollup_eval_daily(days=14, now=None):
    """최근 `days` 일을 다시 집계해 저장 — cleanup 스케줄러가 하루 1회 호출한다.

    최근 구간만 다시 보는 이유: 과거 행은 원본이 안 바뀌면 값도 안 바뀌고, 매번 전 기간을
    훑으면 DB 가 커질수록 느려진다. 다만 세션 재수집(force)·뒤늦은 코멘트 Close 가 과거
    날짜를 바꿀 수 있으므로 하루치가 아니라 2주를 겹쳐 본다.
    실패해도 예외를 밖으로 내지 않는다(집계 실패가 cleanup 을 멈추면 안 된다).
    """
    now = int(time.time()) if now is None else int(now)
    since = time.strftime("%Y-%m-%d", time.localtime(now - max(0, int(days)) * 86400))
    try:
        buckets = collect_eval_daily(since_day=since)
    except Exception:
        _log.exception("[eval_stats] 집계 실패 — 건너뜀")
        return 0
    return save_eval_daily(buckets, now=now)


def eval_daily_series(since_day=None, limit=800):
    """일별 지표 행 목록(최신순) — /pe/eval 채점 탭 추이 그래프용.

    비율은 저장하지 않고 여기서도 만들지 않는다 — 카운터만 돌려주고 화면이 나눈다
    (합계는 더할 수 있어도 비율은 못 더한다, report_chatbot_daily 와 같은 이유).
    수집 시작 이전 날짜는 **행 자체가 없다** — 화면이 '0' 과 '기록 없음' 을 구분해야 한다.
    """
    sql = ("SELECT day, engine_version, " + ", ".join(_COUNT_COLS) + ", updated_at "
           "FROM report_eval_daily")
    params: list = []
    if since_day:
        sql += " WHERE day >= ?"
        params.append(str(since_day))
    sql += " ORDER BY day DESC, engine_version LIMIT ?"
    params.append(int(limit))
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params)]


def purge_eval_daily(cutoff_day):
    """cutoff **이전** 날짜 행 삭제 (사용량 일별과 같은 보존기간을 쓴다). 삭제 행 수 반환."""
    if not cutoff_day:
        return 0
    with get_conn() as conn:
        return conn.execute("DELETE FROM report_eval_daily WHERE day < ?",
                            (str(cutoff_day),)).rowcount
