"""web_report 공개 조회 API(/pe/api/v1/web-report) 검증.

실행:
    python tests/test_public_api_web_report.py

시나리오:
  (a) /capabilities — 규약(contracts.FUNCTION_SPECS)과 실제 등록 라우트가 1:1
  (b) 권한 — 비공개 세션이 익명에게 404(존재 은닉), API 키 제시하면 조회됨
      ※ 회귀 방지 핵심: viewer=None 을 만들면 비공개가 전부 노출된다
  (c) 콜드 세션 — 예외/500 이 아니라 202 + status_url + Retry-After
  (d) xlsx 세션 — 400 not_web_report
  (e) 없는 세션 — 404, 비공개와 **같은 응답**
  (f) 파라미터 — limit clamp, 잘못된 table/section 은 400
  (g) payload 슬라이스 — overview/yield/cpk/issue-table/items 가 payload 값을 그대로 옮긴다
      (comment 서식 토큰 strip, Issue Table row_key 재구성 포함)
  (h) 대용량 — 세마포어 소진 시 429
  (i) 캐시 오염 방지 — facade 가 돌려준 행을 고쳐도 원본 payload 가 변하지 않는다

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). DB 는 임시 디렉터리에 새로
만든다(개발 report.db 를 건드리지 않는다).
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

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

_TMP = Path(tempfile.mkdtemp(prefix="public_api_wr_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""
os.environ["WEB_REPORT_API_KEY"] = "test-key-123"

from flask import Flask  # noqa: E402

from database import report_db  # noqa: E402
from public_api import URL_PREFIX, register_public_api  # noqa: E402
from public_api.web_report import contracts, facade, routes  # noqa: E402

API = f"{URL_PREFIX}/web-report"
UPLOADER = "kim"
KEY_HEADER = {"X-Report-Api-Key": "test-key-123"}

app = Flask(__name__)
register_public_api(app)
client = app.test_client()

# 테스트가 만드는 가짜 payload — 실제 콜드 빌드(수 초·parquet 필요)를 돌리지 않고
# facade 의 슬라이스 규약만 검증한다. 키 이름은 web_report/metrics.py 의 것 그대로다.
FAKE_REPORT = {
    "mode": "Normal",
    "sources": [{"name": "SRC_A", "file_name": "a.csv"}],
    "yield_summary": {"total": {"yield_pct": 92.5},
                      "by_step": [{"step": "P2", "step_yield_pct": 92.5}]},
    "yield_basis": {"basis": "gross"},
    "issue_bin_summary": {"5": 40},
    "yield_bin_groups": [{"bin": "5"}],
    "yield_step_groups": [{"step": "P2"}],
    "summary_engr": {"Yield": "수율 이슈 정리", "CPK": "", "ETC": ""},
    "selected_items": ["LDO_OUT", "SGM_TRIM"],
    "distribution_index": [{"subject": "LDO_OUT", "n": 1000, "cpk": 0.9},
                           {"subject": "SGM_TRIM", "n": 1000, "cpk": 2.1}],
    "sheets": {
        "Yield": [{"bin": "1", "Item": "Pass"}, {"bin": "5", "Item": "SGM_TRIM"}],
        "Fail Bin": [{"bin": "5", "count": 40}],
        "CPK": [{"subject": "LDO_OUT", "source": "SRC_A", "cpk": 0.9},
                {"subject": "SGM_TRIM", "source": "SRC_A", "cpk": 2.1},
                {"subject": "NOCPK", "source": "SRC_A", "cpk": None}],
        "Issue Table": [
            {"Category": "Yield", "Bin": "5", "Item": "SGM_TRIM",
             "PTE comment": "*[trim 순서 오류]", "개발 comment": "수정 완료",
             "Status": "Close"},
            # 섹션 순서는 실제 시트와 같다: CPK subhead → cpk 행 → ETC 헤더 → etc 행
            {"Category": "CPK", "Bin": "", "Item": "item name", "avg": "cpk"},
            {"Category": "", "Bin": "", "Item": "LDO_OUT",
             "PTE comment": "ripple 증가", "Status": "Open"},
            {"Category": "ETC", "Bin": "", "Item": "", "avg": ""},
            {"Category": "", "Bin": "", "Item": "USER_ETC_ITEM", "Status": "Open"},
        ],
        "Issue Table Temp": [{"Item": "LDO_OUT", "Category": "TEMP"}],
        "Map Analysis": [{"source": "SRC_A", "bin_counts": {"1": 960, "5": 40}}],
    },
}


# ── 시드 ─────────────────────────────────────────────────────────────────────
def _mk_session(session_id, *, source="web_report", is_private=0, product="S3222"):
    report_db.create_session(session_id, f"{session_id}.parquet", None,
                             product_type="PMIC", product=product, lot_id="LOT1",
                             source=source, uploaded_by=UPLOADER, family_product="SOC")
    fields = {"status": "done", "analysis_key": f"akey_{session_id}",
              "content_hash": f"hash_{session_id}"}
    if is_private:
        fields["is_private"] = 1
    report_db.update_session(session_id, **fields)


class _FakeService:
    """service.load_webreport 대역 — 콜드/정상/미존재를 상황별로 흉내 낸다."""

    ColdBuildRequired = type("ColdBuildRequired", (Exception,), {})

    def __init__(self):
        self.cold = set()
        self.calls = 0

    def load_webreport(self, session_id, **kw):
        self.calls += 1
        if session_id in self.cold:
            raise self.ColdBuildRequired(session_id)
        return {}, FAKE_REPORT

    def report_is_cold(self, session_id, **kw):
        return session_id in self.cold

    def scatter_item(self, session_id, subject, **kw):
        if subject == "MISSING":
            raise KeyError(subject)
        return {"subject": subject, "units": "V", "lower_limit": 1.0, "upper_limit": 2.0,
                "cpk": 0.9, "status": "FAIL", "fail_total": 3, "is_fail": True,
                "stats": [{"source": "SRC_A", "mean": 1.5}],
                "sources": [{"name": "SRC_A", "values": [1.1, 1.9, 1.5]}]}


_FAKE = _FakeService()


def _patch_service():
    """facade 가 함수 안에서 지연 import 하는 web_report 모듈을 대역으로 바꾼다."""
    import web_report
    import web_report.build_status as bs
    import web_report.compute as compute
    import web_report.service as real_service

    real_service.load_webreport = _FAKE.load_webreport
    real_service.report_is_cold = _FAKE.report_is_cold
    real_service.scatter_item = _FAKE.scatter_item
    real_service.ColdBuildRequired = _FAKE.ColdBuildRequired
    bs.failure_blocked = lambda sid, stage="report": None
    bs.snapshot = lambda sid: {"state": "idle"}
    compute.request_build = lambda *a, **kw: None
    _ = web_report


def setup():
    report_db.init_report_db()
    _mk_session("PUB1")
    _mk_session("PRIV1", is_private=1, product="S3110")
    _mk_session("XLSX1", source="xlsx_upload")
    _mk_session("COLD1")
    _FAKE.cold.add("COLD1")
    _patch_service()


# ── (a) 규약 ↔ 라우트 1:1 ────────────────────────────────────────────────────
def test_capabilities_matches_routes():
    res = client.get(f"{API}/capabilities")
    assert res.status_code == 200, res.status_code
    body = res.get_json()
    assert body["count"] == len(contracts.FUNCTION_SPECS)
    assert body["base_path"] == API

    # SPEC 의 path 를 Flask rule 표기로 바꿔 실제 등록 라우트와 대조한다.
    registered = {str(r) for r in app.url_map.iter_rules() if str(r).startswith(API)}
    for spec in contracts.FUNCTION_SPECS:
        rule = (API + spec["path"]).replace("{session_id}", "<session_id>") \
            .replace("{subject}", "<path:subject>").replace("{index}", "<int:index>")
        assert rule in registered, f"규약에 있는데 라우트가 없다: {spec['name']} → {rule}"
    # 반대 방향: capabilities 를 뺀 모든 라우트가 규약에 있어야 한다.
    spec_rules = {(API + s["path"]).replace("{session_id}", "<session_id>")
                  .replace("{subject}", "<path:subject>")
                  .replace("{index}", "<int:index>") for s in contracts.FUNCTION_SPECS}
    for rule in registered - {f"{API}/capabilities"}:
        assert rule in spec_rules, f"라우트가 규약에 없다(문서 누락): {rule}"

    # 비-heavy 함수는 전부 facade 에 실체가 있어야 한다(MCP 가 그 목록을 쓴다).
    for spec in contracts.FUNCTION_SPECS:
        if spec["cost"] != "heavy":
            assert hasattr(facade, spec["name"]), f"facade 누락: {spec['name']}"
    print("  (a) capabilities ↔ 라우트 1:1 OK "
          f"({len(contracts.FUNCTION_SPECS)}개)")


# ── (b) 권한 ─────────────────────────────────────────────────────────────────
def test_private_hidden_without_key():
    res = client.get(f"{API}/PRIV1/overview")
    assert res.status_code == 404, res.status_code
    assert res.get_json() == {"error": "session_not_found"}

    # 없는 세션과 **같은 응답**이어야 한다 — 존재 여부가 새면 안 된다.
    missing = client.get(f"{API}/NOSUCH/overview")
    assert missing.status_code == 404
    assert missing.get_json() == res.get_json()

    ok = client.get(f"{API}/PRIV1/overview", headers=KEY_HEADER)
    assert ok.status_code == 200, ok.status_code
    assert ok.get_json()["data"]["session"]["session_id"] == "PRIV1"

    # 목록에서도 숨는다 / 키가 있으면 보인다
    anon = client.get(f"{API}/sessions").get_json()["data"]["sessions"]
    assert not any(s["session_id"] == "PRIV1" for s in anon)
    keyed = client.get(f"{API}/sessions", headers=KEY_HEADER).get_json()["data"]["sessions"]
    assert any(s["session_id"] == "PRIV1" for s in keyed)

    # 틀린 키는 차단이 아니라 '공개 범위' 다(public_api 는 무인증이 기본).
    wrong = client.get(f"{API}/PRIV1/overview", headers={"X-Report-Api-Key": "nope"})
    assert wrong.status_code == 404
    print("  (b) 비공개 은닉 + API 키 승격 OK")


def test_viewer_never_none():
    """회귀 방지: _access() 는 어떤 경우에도 viewer=None 을 만들지 않는다."""
    with app.test_request_context(f"{API}/sessions"):
        viewer, see_all = routes._access()
        assert viewer == "" and see_all is False
    with app.test_request_context(f"{API}/sessions", headers=KEY_HEADER):
        viewer, see_all = routes._access()
        assert viewer == "" and see_all is True
    print("  (b2) viewer=None 미발생 OK")


# ── (c) 콜드 202 ─────────────────────────────────────────────────────────────
def test_cold_returns_202():
    res = client.get(f"{API}/COLD1/overview")
    assert res.status_code == 202, res.status_code
    body = res.get_json()
    assert body["building"] is True and body["blocked"] is False
    assert body["status_url"].endswith("/COLD1/build-status")
    assert res.headers.get("Retry-After") == str(routes._RETRY_AFTER)

    # build-status 는 콜드여도 200 이어야 폴링이 가능하다.
    st = client.get(f"{API}/COLD1/build-status")
    assert st.status_code == 200, st.status_code
    assert st.get_json()["data"]["cold"] is True
    print("  (c) 콜드 202 + status_url + 폴링 OK")


# ── (d)(e) 세션 종류 ─────────────────────────────────────────────────────────
def test_xlsx_session_is_400():
    res = client.get(f"{API}/XLSX1/overview")
    assert res.status_code == 400, res.status_code
    assert res.get_json()["error"] == "not_web_report"
    print("  (d) xlsx 세션 400 OK")


# ── (f) 파라미터 ─────────────────────────────────────────────────────────────
def test_param_validation():
    bad = client.get(f"{API}/PUB1/issue-table?table=nope")
    assert bad.status_code == 400 and bad.get_json()["error"] == "bad_request"

    bad2 = client.get(f"{API}/PUB1/compare?section=nope")
    assert bad2.status_code == 400

    # limit 은 에러가 아니라 clamp — 외부 호출자가 큰 값을 넣어도 서버가 지킨다.
    res = client.get(f"{API}/PUB1/cpk?worst_n=99999")
    assert res.status_code == 200
    assert res.get_json()["meta"]["returned"] <= 200

    # 잘못된 타입도 400 이 아니라 기본값이다(폴러가 죽지 않게).
    res2 = client.get(f"{API}/PUB1/cpk?worst_n=abc")
    assert res2.status_code == 200

    missing = client.get(f"{API}/compare-sessions")
    assert missing.status_code == 400
    print("  (f) 파라미터 clamp/400 OK")


# ── (g) payload 슬라이스 ─────────────────────────────────────────────────────
def test_overview_and_yield():
    body = client.get(f"{API}/PUB1/overview").get_json()
    data = body["data"]
    assert data["mode"] == "Normal"
    assert data["yield_summary"]["total"]["yield_pct"] == 92.5
    # by_step(=L1/L2 STEP 분해)이 실려야 한다 — 챗봇이 못 보던 값
    assert data["yield_summary"]["by_step"][0]["step"] == "P2"
    assert data["summary_engr"]["Yield"] == "수율 이슈 정리"
    assert body["meta"]["content_hash"] == "hash_PUB1"
    assert body["schema_version"] == contracts.SCHEMA_VERSION

    y = client.get(f"{API}/PUB1/yield").get_json()["data"]
    assert len(y["rows"]) == 2 and y["step_groups"][0]["step"] == "P2"

    fb = client.get(f"{API}/PUB1/fail-bins").get_json()["data"]
    assert fb["fail_bins"][0]["bin"] == "5" and fb["bin_summary"]["5"] == 40
    print("  (g1) overview/yield/fail-bins OK")


def test_cpk_sorted_worst_first():
    data = client.get(f"{API}/PUB1/cpk").get_json()["data"]
    assert [r["subject"] for r in data["cpk_rows"]] == ["LDO_OUT", "SGM_TRIM"]  # cpk 없는 행 제외
    assert data["cpk_worst"]["subject"] == "LDO_OUT"

    filtered = client.get(f"{API}/PUB1/cpk?item=sgm").get_json()["data"]
    assert [r["subject"] for r in filtered["cpk_rows"]] == ["SGM_TRIM"]
    print("  (g2) cpk 나쁜 순 + 필터 OK")


def test_issue_table_rowkey_and_strip():
    data = client.get(f"{API}/PUB1/issue-table").get_json()["data"]
    rows = data["rows"]
    # 섹션 머리행(Item="item name")은 데이터가 아니라 빠져야 한다.
    assert all(r["Item"] != "item name" for r in rows), rows
    keys = {r["row_key"] for r in rows}
    assert "Yield|5|SGM_TRIM" in keys, keys
    assert "CPK|LDO_OUT" in keys, keys           # Category 셀이 빈 행도 직전 섹션을 잇는다
    assert "ETC|USER_ETC_ITEM" in keys, keys     # ETC 헤더 뒤로 섹션이 바뀐다
    # 화면 전용 서식 토큰이 벗겨져야 외부 소비자가 그대로 쓴다.
    yield_row = next(r for r in rows if r["row_key"] == "Yield|5|SGM_TRIM")
    assert yield_row["PTE comment"] == "trim 순서 오류", yield_row["PTE comment"]
    assert yield_row["Status"] == "Close"

    temp = client.get(f"{API}/PUB1/issue-table?table=temp").get_json()["data"]
    assert temp["table"] == "temp" and len(temp["rows"]) == 1
    print("  (g3) Issue Table row_key 재구성 + 서식 strip OK")


def test_items_and_stats():
    data = client.get(f"{API}/PUB1/items?keyword=ldo").get_json()["data"]
    assert [i["subject"] for i in data["items"]] == ["LDO_OUT"]

    stats = client.get(f"{API}/PUB1/items/LDO_OUT/stats").get_json()["data"]
    assert stats["cpk"] == 0.9 and stats["units"] == "V"
    # 통계만 — 측정값 배열은 실리지 않는다(대용량은 별도 경로)
    assert stats["sources"][0]["count"] == 3
    assert "values" not in stats["sources"][0]

    miss = client.get(f"{API}/PUB1/items/MISSING/stats")
    assert miss.status_code == 404 and miss.get_json()["error"] == "item_not_found"
    print("  (g4) items/stats OK")


def test_map_and_temperature():
    m = client.get(f"{API}/PUB1/map").get_json()["data"]
    assert m["maps"][0]["bin_counts"]["5"] == 40
    t = client.get(f"{API}/PUB1/temperature").get_json()["data"]
    assert len(t["rows"]) == 1
    print("  (g5) map 요약 / temperature OK")


def test_compare_sessions():
    data = client.get(f"{API}/compare-sessions?sids=PUB1,COLD1,NOSUCH").get_json()["data"]
    assert [s["session"]["session_id"] for s in data["sessions"]] == ["PUB1"]
    assert data["building"] == ["COLD1"]      # 콜드는 기다리지 않고 알려만 준다
    assert data["missing"] == ["NOSUCH"]

    too_many = client.get(f"{API}/compare-sessions?sids=a,b,c,d,e,f")
    assert too_many.status_code == 400
    print("  (g6) 세션 간 비교 OK")


# ── (h) 대용량 429 ───────────────────────────────────────────────────────────
def test_heavy_semaphore_429():
    held = []
    while routes._HEAVY_SEM.acquire(blocking=False):
        held.append(1)
    try:
        started = time.time()
        res = client.get(f"{API}/PUB1/distribution")
        assert res.status_code == 429, res.status_code
        assert res.get_json()["error"] == "busy"
        # 대기열 없이 즉시 실패해야 한다(타임아웃만큼은 기다린다).
        assert time.time() - started < routes._HEAVY_TIMEOUT + 2
    finally:
        for _ in held:
            routes._HEAVY_SEM.release()
    print("  (h) 대용량 429 OK")


# ── (i) 캐시 오염 방지 ───────────────────────────────────────────────────────
def test_no_cache_mutation():
    """facade 가 돌려준 행을 호출자가 고쳐도 공유 payload 는 그대로여야 한다."""
    before = FAKE_REPORT["sheets"]["CPK"][0]["cpk"]
    res = facade.get_cpk("PUB1", viewer="", see_all_private=False)
    res["data"]["cpk_rows"][0]["cpk"] = -999
    assert FAKE_REPORT["sheets"]["CPK"][0]["cpk"] == before, "캐시 공유 객체가 오염됐다"

    y = facade.get_yield("PUB1", viewer="", see_all_private=False)
    y["data"]["rows"][0]["Item"] = "TAMPERED"
    assert FAKE_REPORT["sheets"]["Yield"][0]["Item"] == "Pass"

    it = facade.get_issue_table("PUB1", viewer="", see_all_private=False)
    it["data"]["rows"][0]["Status"] = "TAMPERED"
    assert FAKE_REPORT["sheets"]["Issue Table"][0]["Status"] == "Close"
    print("  (i) 캐시 오염 없음 OK")


# ── (j) 관리자 규약 탭 데이터원 ──────────────────────────────────────────────
def test_admin_contract_view():
    """관리자 'public API' 탭이 쓰는 규약 데이터 — 규약↔라우트 대조가 여기서 끝난다."""
    from admin_panel.routes import api_public_api_contract
    with app.test_request_context("/api/public_api/contract"):
        body = api_public_api_contract().get_json()

    group = next(g for g in body["groups"] if g["name"] == "web-report")
    assert group["base_path"] == API
    assert len(group["functions"]) == len(contracts.FUNCTION_SPECS)
    # 이 앱에는 web_report 라우트가 전부 등록돼 있으니 모두 '구현됨' 이어야 한다.
    missing = [f["name"] for f in group["functions"] if not f["implemented"]]
    assert not missing, f"규약에만 있고 라우트가 없다: {missing}"
    # 반대로 문서에 없는 라우트도 없어야 한다(product-info/help 는 규약 파일이 없어
    # undocumented 에 남는 것이 정상 — web-report 것만 없으면 된다).
    stray = [r["rule"] for r in body["undocumented"] if r["rule"].startswith(API)]
    assert not stray, f"규약에 없는 web-report 라우트: {stray}"
    print("  (j) 관리자 규약 탭 데이터 OK")


def main():
    setup()
    test_capabilities_matches_routes()
    test_private_hidden_without_key()
    test_viewer_never_none()
    test_cold_returns_202()
    test_xlsx_session_is_400()
    test_param_validation()
    test_overview_and_yield()
    test_cpk_sorted_worst_first()
    test_issue_table_rowkey_and_strip()
    test_items_and_stats()
    test_map_and_temperature()
    test_compare_sessions()
    test_heavy_semaphore_429()
    test_no_cache_mutation()
    test_admin_contract_view()
    print("\nOK - web_report 공개 API 전 시나리오 통과")


if __name__ == "__main__":
    main()
