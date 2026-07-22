"""get_history_page (한 커넥션 목록+total, CSV 상관 집계) 동등성 검증.

실행:
    python tests/test_history_page.py

시나리오:
  (a) get_history + count_history 와 결과 완전 동일 — 필터(product_type/q/mine/
      visibility/mode/source/date/lot_id) × 정렬 4종 × viewer(None/""/uid) ×
      페이지네이션 조합 전수 비교
  (b) total_file_size — csv 2건 합산 / 0건 / dedup(동일 analysis_key) 형제 동일값
  (c) 즐겨찾기 최상단 고정 + 페이지 분할 안정성 (5건씩 이어 붙이면 전체와 동일)
  (d) 비공개 가시성 — 익명 차단 / 업로더 본인 / 위임 편집자 / legacy(업로더 없음)
  (e) /api/history 응답 — 페이지네이션형에 viewer{user_id,source,has_pin} 동봉,
      파라미터 없는 레거시 배열 응답 무변경(viewer 없음)

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))

_TMP = Path(tempfile.mkdtemp(prefix="hist_page_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""

from flask import Flask  # noqa: E402

from report.report_extension import report_bp  # noqa: E402  (전체 라우트 등록 트리거)
from database import report_db  # noqa: E402
from database.core import get_conn  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

NOW = int(time.time())


# ── 시드 ─────────────────────────────────────────────────────────────────────

def seed():
    # (sid, ak, file, ptype, product, lot, created_delta, status, source, mode,
    #  uploaded_by, private, important, deleted)
    sessions = [
        ("s01", "ak1", "a.xlsx", "MDDI", "P-A", "LOT100", -10, "done", "web_report", "Normal", "SECDS\\Alice", 0, 0, None),
        ("s02", "ak1", "a2.xlsx", "MDDI", "P-A", "LOT101", -20, "done", "web_report", "Normal", "SECDS\\Alice", 0, 0, None),  # ak1 dedup 형제
        ("s03", "ak2", "b.xlsx", "MDDI", "P-B", "LOT200", -30, "done", "web_report", "Compare", "SECDS\\Bob", 0, 1, None),
        ("s04", "ak3", "c.xlsx", "PDDI", "P-C", "LOT300", -40, "done", "xlsx_upload", "Normal", None, 0, 0, None),        # legacy (업로더 없음)
        ("s05", "ak4", "d.xlsx", "PDDI", "P-A", "LOT100", -50, "reused", "web_report", "Normal", "SECDS\\Alice", 1, 0, None),  # alice 비공개
        ("s06", "ak5", "e.xlsx", "MDDI", "P-D", "LOT400", -60, "done", "web_report", "Normal", "SECDS\\Bob", 1, 0, None),      # bob 비공개, alice 위임
        ("s07", "ak6", "f.xlsx", "MDDI", "P-E", "LOT500", -70, "pending", "web_report", "Normal", "SECDS\\Alice", 0, 0, None),  # status 제외
        ("s08", "ak7", "g.xlsx", "MDDI", "P-F", "LOT600", -80, "done", "web_report", "Normal", "SECDS\\Alice", 0, 0, NOW),      # 휴지통 제외
        ("s09", "ak8", "h.xlsx", "PDDI", "P-G", "LOT700", -90, "done", "web_report", "DUT", "SECDS\\Bob", 0, 0, None),
        ("s10", "ak9", "i.xlsx", "MDDI", "P-H", "LOT800", -100, "done", "web_report", "Normal", "", 1, 0, None),                # 업로더 '' + 비공개(legacy 통과)
    ]
    # 페이지네이션 검증용 대량 행 (전부 공개·done)
    for i in range(11, 31):
        sessions.append((f"s{i}", f"akx{i}", f"x{i}.xlsx", "MDDI" if i % 2 else "PDDI",
                         f"P-{i}", f"LOTX{i}", -i * 100, "done", "web_report", "Normal",
                         "SECDS\\Carol", 0, 0, None))
    with get_conn() as conn:
        for (sid, ak, fn, pt, prod, lot, dt, st, src, mode, ub, priv, imp, deleted) in sessions:
            conn.execute(
                "INSERT INTO report_session (session_id, analysis_key, file_name,"
                " product_type, product, process, lot_id, created_at, status, source,"
                " mode, uploaded_by, is_private, is_important, deleted_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, ak, fn, pt, prod, "FAB1", lot, NOW + dt, st, src, mode, ub,
                 priv, imp, deleted))
        # csv: ak1=100+200(dedup 형제 공유), ak2=50, 나머지 없음
        for ak, fn, size in (("ak1", "c1.csv", 100), ("ak1", "c2.csv", 200),
                             ("ak2", "c3.csv", 50)):
            conn.execute(
                "INSERT INTO report_csv_files (analysis_key, filename, s3_key,"
                " file_size, uploaded_at) VALUES (?,?,?,?,?)",
                (ak, fn, f"k/{fn}", size, NOW))
        # 즐겨찾기: alice → s03(중요보다 우선 확인), s20(뒷페이지 → 1페이지로 부상)
        for sid in ("s03", "s20"):
            conn.execute(
                "INSERT INTO report_user_favorite (user_id, session_id, created_at)"
                " VALUES (?,?,?)", ("alice", sid, NOW))
        # 위임: bob 비공개 s06 을 alice 가 편집 가능
        conn.execute(
            "INSERT INTO report_session_editor (session_id, editor_user, granted_by,"
            " granted_at) VALUES (?,?,?,?)", ("s06", "alice", "bob", NOW))
        conn.commit()


seed()


# ── (a) 동등성 전수 비교 ──────────────────────────────────────────────────────

BASE_CASES = []
for viewer in (None, "", "alice", "bob"):
    for sort in ("new", "old", "product", "lot"):
        BASE_CASES.append({"viewer": viewer, "sort": sort})
BASE_CASES += [
    {"product_type": "MDDI", "viewer": "alice"},
    {"q": "LOT1", "viewer": "alice"},
    {"q": "carol", "viewer": ""},          # uploaded_by 부분일치
    {"mine": True, "viewer": "alice"},
    {"mine": True, "viewer": ""},          # 신원 없음 → 공집합
    {"visibility": "private", "viewer": "alice"},
    {"visibility": "public", "viewer": ""},
    {"mode": "Compare", "viewer": "alice"},
    {"source": "xlsx_upload", "viewer": None},
    {"date_from": NOW - 5000, "date_to": NOW - 30, "viewer": "alice"},
    {"lot_id": "LOT1", "viewer": "bob"},
    {"product": "P-A", "process": "FAB1", "viewer": "alice"},
]

checked = 0
for case in BASE_CASES:
    for limit, offset in ((500, 0), (5, 0), (5, 5), (3, 7)):
        c = dict(case)
        viewer = c.pop("viewer", None)
        sort = c.pop("sort", "new")
        legacy_rows = report_db.get_history(**c, limit=limit, offset=offset,
                                            viewer=viewer, sort=sort)
        legacy_total = report_db.count_history(**c, viewer=viewer)
        rows, total = report_db.get_history_page(**c, limit=limit, offset=offset,
                                                 viewer=viewer, sort=sort)
        assert rows == legacy_rows, f"rows 불일치: {case} limit={limit} offset={offset}"
        assert total == legacy_total, f"total 불일치: {case}"
        checked += 1
print(f"(a) 동등성 {checked}조합 통과")

# ── (b) total_file_size ──────────────────────────────────────────────────────

rows, _ = report_db.get_history_page(viewer="alice", limit=500)
by_sid = {r["session_id"]: r for r in rows}
assert by_sid["s01"]["total_file_size"] == 300, "ak1 합산(100+200) 실패"
assert by_sid["s02"]["total_file_size"] == 300, "dedup 형제 동일값 실패"
assert by_sid["s03"]["total_file_size"] == 50
assert by_sid["s04"]["total_file_size"] == 0, "csv 없는 세션은 0"
print("(b) total_file_size 통과")

# ── (c) 즐겨찾기 최상단 + 페이지 분할 안정성 ─────────────────────────────────

assert [r["session_id"] for r in rows[:2]] == ["s03", "s20"], \
    f"즐겨찾기 최상단 고정 실패: {[r['session_id'] for r in rows[:3]]}"
assert all(r["is_favorite"] == 1 for r in rows[:2])
paged = []
off = 0
while True:
    p, total = report_db.get_history_page(viewer="alice", limit=5, offset=off)
    if not p:
        break
    paged.extend(p)
    off += 5
assert paged == rows, "5건 페이지 이어붙임 != 전체 목록"
assert total == len(rows)
print("(c) 즐겨찾기/페이지네이션 통과")

# ── (d) 비공개 가시성 ────────────────────────────────────────────────────────

anon_rows, _ = report_db.get_history_page(viewer="", limit=500)
anon_sids = {r["session_id"] for r in anon_rows}
assert "s05" not in anon_sids and "s06" not in anon_sids, "익명에게 비공개 노출"
assert "s10" not in anon_sids, "익명은 업로더 없는 비공개도 차단(현행 유지)"
alice_sids = set(by_sid)
assert "s10" in alice_sids, "신원 있는 사용자는 legacy(업로더 없음) 비공개 열람(현행 유지)"
assert "s05" in alice_sids, "본인 비공개 미노출"
assert "s06" in alice_sids, "위임 편집자 비공개 미노출"
bob_rows, _ = report_db.get_history_page(viewer="bob", limit=500)
bob_sids = {r["session_id"] for r in bob_rows}
assert "s06" in bob_sids and "s05" not in bob_sids, "bob 가시성 오류"
assert "s07" not in alice_sids and "s08" not in alice_sids, "pending/휴지통 제외 실패"
print("(d) 비공개 가시성 통과")

# ── (e) /api/history 응답 형태 ───────────────────────────────────────────────

UA = "Mozilla/5.0 HoneyUser/alice"
r = client.get("/pe/report/api/history?limit=20&offset=0", headers={"User-Agent": UA})
assert r.status_code == 200
j = r.get_json()
assert set(j) == {"rows", "total", "limit", "offset", "viewer"}, f"응답 키: {set(j)}"
assert j["viewer"]["user_id"] == "alice"
assert j["viewer"]["source"] == "honey"
assert j["viewer"]["has_pin"] is False, "PW 미설정 계정 has_pin 은 False"
assert [row["session_id"] for row in j["rows"][:2]] == ["s03", "s20"]

anon = client.get("/pe/report/api/history?limit=20&offset=0").get_json()
assert anon["viewer"] == {"user_id": "", "source": ""}, f"익명 viewer: {anon['viewer']}"
assert all(row["session_id"] not in ("s05", "s06") for row in anon["rows"])

legacy = client.get("/pe/report/api/history", headers={"User-Agent": UA}).get_json()
assert isinstance(legacy, list), "레거시(파라미터 없음)는 배열 응답 유지"
print("(e) /api/history 응답 형태 통과")

print("\n전체 통과 —", _TMP)
