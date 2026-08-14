"""중복 신원 병합 — 같은 사람의 갈라진 계정 키를 정규화 키 하나로 합친다 (1회 운영 도구).

한 사람이 표기별로 여러 사용자로 쌓여 있던 문제를 정리한다:

    SECDS\\chumji.kim · Chumji.Kim · chumji.kim   →   chumji.kim

정규화 규칙은 서버와 **같은 함수**(identity_norm.normalize_uid — 마지막 백슬래시 뒤 →
trim → 소문자)를 쓴다. 2026-08-14 자로 신원 진입점이 전부 이 규칙을 통과하므로 새로
갈라지지는 않지만, 그 이전에 쌓인 행은 이 도구로만 합쳐진다.

**합치는 것** (사람 키로 쓰이는 컬럼):
    report_usage_daily / report_usage_hourly   접속 카운터 (count 합산, last_at 최대)
    report_web_visitor                          편집자 후보 풀 (first_seen 최소·last_seen 최대)
    report_user_profile                         실명 (updated_at 이 최신인 이름 채택)
    report_user_favorite / report_user_important 즐겨찾기·개인 중요표시 (created_at 최소)
    report_session_editor                       편집 권한 위임 (editor_user·granted_by)
    report_user                                 웹 로그인 계정 (충돌 시 먼저 만든 계정 유지)
    report_chatbot_log                          챗봇 질문 기록의 신원 칸

**건드리지 않는 것** (의도적):
    report_audit_log.client_user    감사 기록은 **당시 원문**이 증거다. 화면 표기는
                                    users_admin.attach_names 가 정규화해 보여준다.
    report_session.uploaded_by      세션 소유 근거. 접근제어 SQL(_UPLOADER_MATCH)이 이미
                                    도메인 꼬리·소문자로 비교하므로 원문이어도 정확하다.

재실행해도 안전하다 — 이미 정규화된 키는 대상이 아니라 두 번째 실행은 0건이 된다.

사용:
    cd server
    .venv/Scripts/python.exe tools/merge_duplicate_users.py            # 미리보기(기본)
    .venv/Scripts/python.exe tools/merge_duplicate_users.py --apply    # 실제 병합

--apply 는 먼저 DB 사본을 만든다 (report.db 옆 `report.db.premerge-<시각>`).
운영에서는 **서버를 내린 상태**로 실행할 것 — 병합 도중 들어온 쓰기는 합쳐지지 않는다.
"""
import sys
import time
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent
_ROOT = _SERVER.parent
for p in (str(_SERVER), str(_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# 테이블별 병합 SQL. 각 항목은 (설명, 원행을 세는 SQL, 병합 INSERT, 원행 삭제 SQL) 이며
# 파라미터는 모두 (정규화키, 원래키) 또는 (원래키,) 순서다.
# 병합은 "정규화 키로 다시 INSERT → 충돌하면 두 행을 합침 → 원행 삭제" 3단이다.
def _merge_specs():
    return [
        ("report_usage_daily", "접속 카운터(일별)", "user_id", """
            INSERT INTO report_usage_daily (day, kind, user_id, count, last_at)
            SELECT day, kind, ?, count, last_at FROM report_usage_daily WHERE user_id = ?
            ON CONFLICT(day, kind, user_id) DO UPDATE SET
                count   = count + excluded.count,
                last_at = MAX(last_at, excluded.last_at)
        """),
        ("report_usage_hourly", "접속 카운터(시간별)", "user_id", """
            INSERT INTO report_usage_hourly (day, hour, kind, user_id, count, last_at)
            SELECT day, hour, kind, ?, count, last_at FROM report_usage_hourly WHERE user_id = ?
            ON CONFLICT(day, hour, kind, user_id) DO UPDATE SET
                count   = count + excluded.count,
                last_at = MAX(last_at, excluded.last_at)
        """),
        ("report_web_visitor", "편집자 후보 풀", "user_id", """
            INSERT INTO report_web_visitor (user_id, first_seen, last_seen)
            SELECT ?, first_seen, last_seen FROM report_web_visitor WHERE user_id = ?
            ON CONFLICT(user_id) DO UPDATE SET
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen  = MAX(last_seen,  excluded.last_seen)
        """),
        ("report_user_profile", "실명", "user_id", """
            INSERT INTO report_user_profile (user_id, display_name, updated_at, updated_by)
            SELECT ?, display_name, updated_at, updated_by
              FROM report_user_profile WHERE user_id = ?
            ON CONFLICT(user_id) DO UPDATE SET
                display_name = CASE WHEN excluded.updated_at > updated_at
                                    THEN excluded.display_name ELSE display_name END,
                updated_by   = CASE WHEN excluded.updated_at > updated_at
                                    THEN excluded.updated_by   ELSE updated_by   END,
                updated_at   = MAX(updated_at, excluded.updated_at)
        """),
        ("report_user_favorite", "즐겨찾기", "user_id", """
            INSERT INTO report_user_favorite (user_id, session_id, created_at)
            SELECT ?, session_id, created_at FROM report_user_favorite WHERE user_id = ?
            ON CONFLICT(user_id, session_id) DO UPDATE SET
                created_at = MIN(created_at, excluded.created_at)
        """),
        ("report_user_important", "개인 중요표시", "user_id", """
            INSERT INTO report_user_important (user_id, session_id, created_at)
            SELECT ?, session_id, created_at FROM report_user_important WHERE user_id = ?
            ON CONFLICT(user_id, session_id) DO UPDATE SET
                created_at = MIN(created_at, excluded.created_at)
        """),
        ("report_session_editor", "편집 권한 위임", "editor_user", """
            INSERT INTO report_session_editor (session_id, editor_user, granted_by, granted_at)
            SELECT session_id, ?, granted_by, granted_at
              FROM report_session_editor WHERE editor_user = ?
            ON CONFLICT(session_id, editor_user) DO UPDATE SET
                granted_at = MIN(granted_at, excluded.granted_at)
        """),
    ]

# 사람 키가 **PK 가 아닌** 컬럼 — 충돌이 없어 그냥 UPDATE 한다.
_PLAIN_UPDATES = [
    ("report_session_editor", "granted_by", "권한 부여자"),
    ("report_chatbot_log", "user", "챗봇 질문자"),
]


def _distinct_keys(conn, table, col):
    """{원래키: 정규화키} — 정규화하면 값이 달라지는(= 합쳐야 하는) 것만."""
    from identity_norm import normalize_uid
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {col} AS v FROM {table} "
            f"WHERE {col} IS NOT NULL AND TRIM({col}) <> ''").fetchall()
    except Exception as e:          # 구 DB 에 없는 테이블 — 건너뛴다
        print(f"[skip] {table}: {e}")
        return {}
    out = {}
    for r in rows:
        raw = r["v"]
        norm = normalize_uid(raw)
        if norm and norm != raw:
            out[raw] = norm
    return out


def _merge_users_table(conn, apply):
    """report_user(웹 로그인 계정) — 정규화 키 계정이 이미 있으면 중복 계정을 지우고,
    없으면 이름만 바꾼다. 비밀번호는 **먼저 만든 계정** 것을 남긴다(둘 다 본인 것이고,
    지워지는 쪽은 어차피 같은 사람의 여벌 계정이다)."""
    pairs = _distinct_keys(conn, "report_user", "user_id")
    renamed = dropped = 0
    for raw, norm in sorted(pairs.items()):
        exists = conn.execute(
            "SELECT created_at FROM report_user WHERE user_id = ?", (norm,)).fetchone()
        if exists:
            print(f"    - 계정 중복: '{raw}' 삭제 (먼저 만든 '{norm}' 계정과 비밀번호 유지)")
            if apply:
                conn.execute("DELETE FROM report_user WHERE user_id = ?", (raw,))
            dropped += 1
        else:
            print(f"    - 계정 이름 변경: '{raw}' → '{norm}'")
            if apply:
                conn.execute("UPDATE report_user SET user_id = ? WHERE user_id = ?",
                             (norm, raw))
            renamed += 1
    return renamed, dropped


def main(apply):
    from config import REPORT_DB_PATH
    from database import report_db

    report_db.init_report_db()
    print(f"DB: {REPORT_DB_PATH}")
    print("모드: " + ("실제 병합(--apply)" if apply else "미리보기 (변경 없음)"))
    print()

    if apply:
        # VACUUM INTO 는 대상 파일이 이미 있으면 실패한다 — 같은 초에 두 번 돌려도
        # 백업 단계에서 멈추지 않도록 접미사를 붙인다 (eval 룰 백업과 같은 관례).
        stamp = str(REPORT_DB_PATH) + ".premerge-" + time.strftime("%Y%m%d_%H%M%S")
        backup = Path(stamp)
        n = 2
        while backup.exists():
            backup = Path(f"{stamp}-{n}")
            n += 1
        with report_db.get_conn() as conn:
            conn.execute("VACUUM INTO ?", (str(backup),))
        print(f"[backup] {backup}\n")

    total_rows = 0
    with report_db.get_conn(busy_timeout_ms=30000) as conn:
        for table, label, col, merge_sql in _merge_specs():
            pairs = _distinct_keys(conn, table, col)
            if not pairs:
                continue
            print(f"[{table}] {label}")
            for raw, norm in sorted(pairs.items()):
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?",
                                 (raw,)).fetchone()[0]
                print(f"    - '{raw}' → '{norm}' ({n}행)")
                total_rows += n
                if apply:
                    conn.execute(merge_sql, (norm, raw))
                    conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (raw,))
            print()

        for table, col, label in _PLAIN_UPDATES:
            pairs = _distinct_keys(conn, table, col)
            if not pairs:
                continue
            print(f"[{table}.{col}] {label}")
            for raw, norm in sorted(pairs.items()):
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?",
                                 (raw,)).fetchone()[0]
                print(f"    - '{raw}' → '{norm}' ({n}행)")
                total_rows += n
                if apply:
                    conn.execute(f"UPDATE {table} SET {col} = ? WHERE {col} = ?", (norm, raw))
            print()

        print("[report_user] 웹 로그인 계정")
        renamed, dropped = _merge_users_table(conn, apply)
        if not (renamed or dropped):
            print("    - 합칠 계정 없음")
        print()

    if not total_rows and not (renamed or dropped):
        print("합칠 중복 신원이 없습니다 (이미 정리된 상태).")
    elif apply:
        print(f"완료 — {total_rows}행 병합. 관리자 화면에서 사용자 현황을 확인하세요.")
    else:
        print(f"미리보기 끝 — 대상 {total_rows}행. 실제로 합치려면 --apply 를 붙여 다시 실행하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
