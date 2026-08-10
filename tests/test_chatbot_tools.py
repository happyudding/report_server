"""챗봇 조회 툴 검증 (server/chatbot).

실행:
    python tests/test_chatbot_tools.py

시나리오:
  (a) rowkey — row_key/status_key 파싱과 Yield 의 키 비대칭
  (b) 권한 — 비공개 세션이 익명(viewer="")에 안 보이고, 업로더/master 에는 보인다
      ※ 회귀 방지 핵심: sessions._history_where 는 viewer=None 이면 비공개를 전부 노출한다
  (c) get_session_issues — web_report 세션(코멘트 + Yield Status bin 조인 + item 필터)
  (d) get_session_issues — xlsx 세션(sheet_data 스냅샷 경로, Status 개념 없음)
  (e) search_item_in_sessions — 세션 횡단 item 검색 + 권한 + Yield Close 채움
  (f) search_products — 제품 집계
  (g) tools_eval — eval.db 가 없을 때 예외 없이 빈 결과 + db_available=False
  (h) planner.rule_plan — LLM 없이도 골든 두 질문의 intent 를 맞춘다
  (j) tools_metrics — 미존재/권한없음/xlsx 세션에서 예외 대신 분기 키를 돌려준다
  (k) answer_web — 세션 바로가기 링크·선택 버튼·컨텍스트 세션 주입 + answer() 계약 유지

pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례). 전 과정 read-only 툴만 쓰지만
DB 는 임시 디렉터리에 새로 만든다(개발 report.db 를 건드리지 않는다).
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

# 한국어 콘솔(cp949)에서 출력이 UnicodeEncodeError 로 죽지 않게 한다.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

_TMP = Path(tempfile.mkdtemp(prefix="chatbot_tools_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_EVAL_DB_PATH"] = str(_TMP / "eval" / "eval.db")  # 일부러 만들지 않는다
os.environ["REPORT_S3_BUCKET"] = ""

from database import report_db  # noqa: E402
from database.core import get_conn  # noqa: E402

from chatbot import (agent, eval_store, planner, rowkey, tools_eval,  # noqa: E402
                     tools_metrics, tools_report)

NOW = int(time.time())
UPLOADER = "kim"
OTHER = "lee"


def _mk_session(session_id, *, source="web_report", product="S3222", akey=None,
                uploaded_by=UPLOADER, is_private=0, lot_id="LOT1",
                product_type="PMIC", family_product="SOC"):
    report_db.create_session(session_id, f"{session_id}.xlsx", None,
                             product_type=product_type, product=product, lot_id=lot_id,
                             source=source, uploaded_by=uploaded_by,
                             family_product=family_product)
    fields = {"status": "done", "analysis_key": akey or f"akey_{session_id}"}
    if is_private:
        fields["is_private"] = 1
    report_db.update_session(session_id, **fields)


def _edit(session_id, kind, item_key, value, by=UPLOADER):
    report_db.apply_webreport_edits(session_id, [(kind, item_key, value)], updated_by=by)


def setup():
    report_db.init_report_db()

    # web_report 세션 1 — Yield 이슈(코멘트는 item 단위, Status 는 bin 단위) + CPK 이슈
    _mk_session("S1")
    _edit("S1", "issue_comment", f"Yield|5|SGM_TRIM_CHECK{rowkey.SEP}PTE comment",
          "trim 순서 오류로 fail")
    _edit("S1", "issue_comment", f"Yield|5|SGM_TRIM_CHECK{rowkey.SEP}개발 comment",
          "sequence 수정 후 재평가 OK")
    _edit("S1", "issue_status", "Yield|5", "Close")
    _edit("S1", "issue_comment", f"CPK|LDO_OUTPUT{rowkey.SEP}PTE comment",
          "고온에서 ripple 증가")
    # Close 안 된 CPK 이슈(=Open) 확인용으로 status 는 넣지 않는다

    # web_report 세션 2 — 비공개, 같은 item
    _mk_session("S2", product="S3110", lot_id="LOT2", is_private=1)
    _edit("S2", "issue_comment", f"CPK|SGM_VOLTAGE_MON{rowkey.SEP}PTE comment",
          "비공개 세션의 코멘트")

    # xlsx 업로드 세션
    _mk_session("S3", source="xlsx_upload", product="S9999", akey="akey_S3")
    with get_conn() as conn:
        import json
        conn.execute(
            "INSERT INTO report_sheet_data (analysis_key, sheet_name, data_json, updated_at)"
            " VALUES (?, ?, ?, ?)",
            ("akey_S3", "issue_table",
             json.dumps([{"Category": "Yield", "Bin": 7, "Item": "LDO_VOUT",
                          "PTE 2차 comment": "xlsx 스냅샷 코멘트"}], ensure_ascii=False),
             NOW))


# ── (a) rowkey ───────────────────────────────────────────────────────────────
def test_rowkey():
    rk = rowkey.parse("Yield|5|SGM_TRIM_CHECK")
    assert rk == ("Yield", 5, "SGM_TRIM_CHECK"), rk
    assert rowkey.status_key(rk) == "Yield|5"
    assert rowkey.parse("CPK|LDO_OUTPUT") == ("CPK", None, "LDO_OUTPUT")
    assert rowkey.status_key(rowkey.parse("CPK|LDO_OUTPUT")) == "CPK|LDO_OUTPUT"
    assert rowkey.parse("ETC|자유항목") == ("ETC", None, "자유항목")
    # Yield 상태 키는 2조각이라 parse() 로는 못 읽고 parse_status_key() 가 필요하다
    assert rowkey.parse("Yield|5") is None
    assert rowkey.parse_status_key("Yield|5") == ("Yield", 5, "")
    assert rowkey.parse("Yield|abc|X") is None
    assert rowkey.parse("") is None
    assert rowkey.split_comment_key(f"CPK|X{rowkey.SEP}PTE comment") == ("CPK|X", "PTE comment")
    print("[OK] (a) rowkey")


# ── (b) 권한 ─────────────────────────────────────────────────────────────────
def test_permission():
    anon = tools_report.search_sessions(viewer="")
    ids = {s["session_id"] for s in anon["sessions"]}
    assert "S2" not in ids, f"익명에게 비공개 세션 노출: {ids}"
    assert {"S1", "S3"} <= ids, ids

    mine = tools_report.search_sessions(viewer=UPLOADER)
    assert "S2" in {s["session_id"] for s in mine["sessions"]}

    other = tools_report.search_sessions(viewer=OTHER)
    assert "S2" not in {s["session_id"] for s in other["sessions"]}

    master = tools_report.search_sessions(viewer="", see_all_private=True)
    assert "S2" in {s["session_id"] for s in master["sessions"]}

    # 세션 상세도 같은 판정 — 존재 자체를 숨긴다
    blocked = tools_report.get_session_issues("S2", viewer=OTHER)
    assert blocked.get("error") == "session_not_found", blocked
    allowed = tools_report.get_session_issues("S2", viewer=UPLOADER)
    assert "session" in allowed, allowed
    print("[OK] (b) 권한 — 비공개 필터/상세 차단")


# ── (c) web_report 이슈 ─────────────────────────────────────────────────────
def test_session_issues_webreport():
    res = tools_report.get_session_issues("S1", viewer=UPLOADER)
    assert res["source"] == "web_report"
    by_item = {i["item"]: i for i in res["issues"]}
    assert set(by_item) == {"SGM_TRIM_CHECK", "LDO_OUTPUT"}, by_item.keys()

    sgm = by_item["SGM_TRIM_CHECK"]
    # 코멘트는 item 단위 키, Status 는 bin 단위 키 — 이 조인이 되어야 Close 가 보인다
    assert sgm["status"] == "Close", sgm
    assert sgm["bin"] == 5
    assert sgm["comments"]["PTE comment"] == "trim 순서 오류로 fail"
    assert sgm["comments"]["개발 comment"] == "sequence 수정 후 재평가 OK"

    ldo = by_item["LDO_OUTPUT"]
    assert ldo["status"] == "Open", ldo  # status 행 부재 = Open

    filtered = tools_report.get_session_issues("S1", viewer=UPLOADER, item_keyword="ldo")
    assert [i["item"] for i in filtered["issues"]] == ["LDO_OUTPUT"], filtered
    print("[OK] (c) web_report 이슈 — Yield 키 비대칭 조인 + Open/Close + item 필터")


# ── (d) xlsx 이슈 ────────────────────────────────────────────────────────────
def test_session_issues_xlsx():
    res = tools_report.get_session_issues("S3", viewer=UPLOADER)
    assert res["source"] == "xlsx_upload"
    assert len(res["issues"]) == 1, res
    issue = res["issues"][0]
    assert issue["item"] == "LDO_VOUT"
    assert issue["status"] == "", issue          # xlsx 에는 Open/Close 가 없다
    assert "xlsx 스냅샷 코멘트" in " ".join(issue["comments"].values())
    assert "Status" in res["note"]
    print("[OK] (d) xlsx 이슈 — 스냅샷 경로 + Status 없음 안내")


# ── (e) 세션 횡단 item 검색 ─────────────────────────────────────────────────
def test_search_item_in_sessions():
    anon = tools_report.search_item_in_sessions("SGM", viewer="")
    items = {(h["session_id"], h["item"]) for h in anon["hits"]}
    assert ("S1", "SGM_TRIM_CHECK") in items, items
    assert not any(sid == "S2" for sid, _ in items), f"비공개 세션 유출: {items}"

    # Yield 이슈의 Close 는 별도 조회로 채워져야 한다(LIKE 로는 안 걸린다)
    sgm = next(h for h in anon["hits"] if h["item"] == "SGM_TRIM_CHECK")
    assert sgm["status"] == "Close", sgm
    assert sgm["product"] == "S3222" and sgm["lot_id"] == "LOT1"

    owner = tools_report.search_item_in_sessions("SGM", viewer=UPLOADER)
    assert any(h["session_id"] == "S2" for h in owner["hits"]), owner

    scoped = tools_report.search_item_in_sessions("SGM", viewer="", product_type="MDDI")
    assert scoped["hits"] == [], scoped
    print("[OK] (e) 세션 횡단 item 검색 — 권한/Yield Close/스코프 필터")


# ── (f) 제품 집계 ────────────────────────────────────────────────────────────
def test_search_products():
    res = tools_report.search_products("S3222", viewer=UPLOADER)
    names = [p["product"] for p in res["products"]]
    assert names == ["S3222"], res
    assert res["products"][0]["sessions"] == 1
    assert res["products"][0]["family_product"] == "SOC"
    assert res["products"][0]["lot_ids"] == ["LOT1"]
    print("[OK] (f) 제품 집계")


# ── (g) eval.db 부재 ────────────────────────────────────────────────────────
def test_eval_db_absent():
    assert not eval_store.available(), eval_store.db_path()
    cand = tools_eval.search_item_candidates("SGM")
    assert cand["items"] == [] and cand["db_available"] is False, cand
    assert tools_eval.get_item_history("SGM_TRIM_CHECK")["history"] == []
    assert tools_eval.search_similar_cases("SGM_TRIM_CHECK")["similar"] == []
    assert tools_eval.search_comments("ripple")["comments"] == []
    # 답변 경로도 죽지 않고 report.db 근거로 답해야 한다
    out = agent.answer("SGM 들어가는 항목 이력 알려줘", viewer=UPLOADER, use_llm=False)
    assert "SGM_TRIM_CHECK" in out["text"], out["text"]
    assert "eval DB" in out["text"]
    print("[OK] (g) eval.db 부재 — 예외 없이 빈 결과 + report.db 폴백 답변")


# ── (h) 규칙 계획 ───────────────────────────────────────────────────────────
def test_rule_plan():
    p1 = planner.rule_plan(
        "PMIC 에 SOC Family 제품에 무슨 항목인지는 기억 안 나는데 SGM 들어가는 항목 "
        "예전에 어떻게 됬었지? 히스토리 알려줘")
    assert p1.intent == "item_history", p1
    assert p1.product_type == "PMIC" and p1.family_product == "SOC", p1
    assert "SGM" in p1.item_keywords, p1

    p2 = planner.rule_plan(
        "내가 예전에 S3222 라는 제품 평가 한 보고서가 있었던거같은데 거기에 LDO 라는 "
        "item 어떻게 Issue close 됫었지?")
    assert p2.intent == "session_issue", p2
    assert p2.product == "S3222", p2
    assert "LDO" in p2.item_keywords, p2

    assert planner.rule_plan("어제 뭐 했더라").intent == "unknown"
    print("[OK] (h) 규칙 계획 — LLM 없이 두 대표 질문 분류")


# ── (i) eval.db 가 있을 때 ──────────────────────────────────────────────────
def _build_eval_db(path):
    """정본 스키마(store.SCHEMA)로 eval.db 를 만들고 최소 데이터를 넣는다.

    스키마를 손으로 적지 않고 `eval_export.open_conn` 을 쓴다 — 운영이 실제로 만드는
    파일과 같은 DDL/마이그레이션을 거쳐야 컬럼 이름이 어긋나도 여기서 잡힌다.
    """
    # config 는 import 시점에 경로를 확정하므로 env 를 지금 바꿔도 안 먹는다 — 직접 대입.
    import config
    from web_report import eval_export
    config.REPORT_EVAL_DB_PATH = str(path)
    conn = eval_export.open_conn(create=True)
    c = conn.cursor()
    c.execute("INSERT INTO product_master(product_name,product_type,family_product,updated_at)"
              " VALUES('S3222','PMIC','SOC',?)", (NOW,))
    c.execute("INSERT INTO item_master(item_name_raw,item_canonical,category_major,"
              "value_type,unit) VALUES('SGM_TRIM_CHECK','SGM_TRIM_CHECK','TRIM','V','V')")
    sgm = c.lastrowid
    c.execute("INSERT INTO item_master(item_name_raw,item_canonical,category_major,"
              "value_type,unit) VALUES('LDO_OUTPUT','LDO_OUTPUT','LDO','V','V')")
    ldo = c.lastrowid
    c.execute("INSERT INTO item_alias(raw_name,item_id) VALUES('SGM TRIM',?)", (sgm,))
    c.execute("INSERT INTO ingest_run(product_name,lot_id,session_id,analysis_key,created_at)"
              " VALUES('S3222','LOT1','S1','akey_S1',?)", (NOW,))
    run = c.lastrowid
    for case_id, item_id, bin_ in (("case_sgm", sgm, 5), ("case_ldo", ldo, 1)):
        c.execute("INSERT INTO fail_case(case_id,product_name,lot_id,item_id,bin,revision,"
                  "item_class,created_at) VALUES(?,'S3222','LOT1',?,?,1.0,'TRIM|V|5',?)",
                  (case_id, item_id, bin_, NOW))
        c.execute("INSERT INTO run_case(run_id,case_id,seen_at) VALUES(?,?,?)",
                  (run, case_id, NOW))
        c.execute('INSERT INTO raw_metrics(case_id,run_id,cpk,mean,stdev,"yield",fail_count,'
                  'total_count,created_at) VALUES(?,?,0.92,1.2,0.05,97.3,27,1000,?)',
                  (case_id, run, NOW))
        c.execute("INSERT INTO evaluation(case_id,run_id,engine_version,model_version,status,"
                  "confidence,comment,created_at) VALUES(?,?,'v1','','MAJOR',0.8,'산포 과다',?)",
                  (case_id, run, NOW))
        c.execute("INSERT INTO label(case_id,human_comment,labeler,created_at) "
                  "VALUES(?,'[PTE] trim 순서 오류\n[개발] sequence 수정 후 OK','web_report',?)",
                  (case_id, NOW))
    conn.commit()
    conn.close()


def test_eval_db_present():
    path = _TMP / "eval_present" / "eval.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    _build_eval_db(path)
    eval_store.set_db_path(path)
    try:
        assert eval_store.available()

        cand = tools_eval.search_item_candidates("SGM")
        items = cand["items"]
        assert [i["item_canonical"] for i in items] == ["SGM_TRIM_CHECK"], items
        assert items[0]["cases"] == 1 and items[0]["products_sample"] == ["S3222"], items[0]

        # alias 로도 찾혀야 한다 (item_alias 조인)
        via_alias = tools_eval.search_item_candidates("SGM TRIM")["items"]
        assert [i["item_canonical"] for i in via_alias] == ["SGM_TRIM_CHECK"], via_alias

        # 스코프 필터 — 맞으면 나오고 틀리면 안 나온다
        assert tools_eval.search_item_candidates(
            "SGM", product_type="PMIC", family_product="SOC")["items"], "스코프 일치인데 공집합"
        assert tools_eval.search_item_candidates(
            "SGM", product_type="MDDI")["items"] == [], "스코프 불일치인데 결과 있음"

        hist = tools_eval.get_item_history("SGM_TRIM_CHECK")["history"]
        assert len(hist) == 1, hist
        row = hist[0]
        assert row["product_name"] == "S3222" and row["bin"] == 5
        assert row["cpk"] == 0.92 and row["yield"] == 97.3
        assert row["engine_status"] == "MAJOR"
        assert row["session_id"] == "S1", "report.db 조인 키(session_id)가 비었다"
        assert "trim 순서 오류" in row["human_comment"]

        similar = tools_eval.search_similar_cases("SGM_TRIM_CHECK")["similar"]
        assert [s["item_canonical"] for s in similar] == ["LDO_OUTPUT"], similar

        found = tools_eval.search_comments("sequence")["comments"]
        assert len(found) == 2, found

        # 집계 (eval_analyzer/chatbot_prototype 의 stats_summary 이식분)
        by_status = tools_eval.stats_summary("status")
        assert by_status["total"] == 2, by_status
        assert by_status["groups"] == [{"key": "MAJOR", "count": 2,
                                        "last_at": NOW}], by_status["groups"]
        by_item = tools_eval.stats_summary("item")
        assert {g["key"] for g in by_item["groups"]} == {"SGM_TRIM_CHECK", "LDO_OUTPUT"}, by_item
        assert all(g["count"] == 1 for g in by_item["groups"]), by_item
        # 스코프·판정 필터
        assert tools_eval.stats_summary("product", product_type="PMIC")["total"] == 2
        assert tools_eval.stats_summary("product", product_type="MDDI")["total"] == 0
        assert tools_eval.stats_summary("product", status="MAJOR")["total"] == 2
        assert tools_eval.stats_summary("product", status="CRITICAL")["total"] == 0
        # 축 이름은 화이트리스트 — 사용자 입력이 SQL 로 새지 않는다
        try:
            tools_eval.stats_summary("product_name; DROP TABLE fail_case")
            raise AssertionError("잘못된 축이 통과했다")
        except ValueError:
            pass
        # 답변 경로
        out = agent.answer("PMIC 에 MAJOR 몇 건이야?", viewer=UPLOADER, use_llm=False)
        assert out["plan"]["intent"] == "stats", out["plan"]
        assert out["plan"]["status"] == "MAJOR", out["plan"]
        assert "총 2건" in out["text"], out["text"]

        # 답변에 eval.db 이력과 report.db 근거가 함께 나와야 한다
        out = agent.answer("SGM 들어가는 항목 예전에 어떻게 됐었지?",
                           viewer=UPLOADER, use_llm=False)
        text = out["text"]
        assert "SGM_TRIM_CHECK" in text and "cpk=0.92" in text, text
        assert "S1" in text, text
        # 여러 줄 코멘트가 한 줄로 접혀야 들여쓰기가 안 깨진다
        assert "[PTE] trim 순서 오류 / [개발] sequence 수정 후 OK" in text, text
        tools = [s["tool"] for s in out["steps"]]
        assert tools[:2] == ["search_item_candidates", "get_item_history"], tools
    finally:
        eval_store.set_db_path(None)
    print("[OK] (i) eval.db 조회 — 후보/alias/스코프/이력/유사/코멘트/집계 + 병합 답변")


# ── (j) 세션 수치 툴 ────────────────────────────────────────────────────────
def test_metrics_branches():
    """수치 툴은 예외를 던지지 않는다 — 챗 답변이 500 으로 끊기면 안 된다."""
    missing = tools_metrics.get_session_metrics("NOPE", viewer=UPLOADER)
    assert missing.get("error") == "session_not_found", missing

    # 권한 없음도 '없음' 과 같은 응답이어야 세션 존재가 새지 않는다
    hidden = tools_metrics.get_session_metrics("S2", viewer=OTHER)
    assert hidden.get("error") == "session_not_found", hidden

    # xlsx 세션에는 yield/CPK payload 자체가 없다
    xlsx = tools_metrics.get_session_metrics("S3", viewer=UPLOADER)
    assert xlsx.get("error") == "not_web_report", xlsx

    values = tools_metrics.get_item_values("NOPE", "VDD", viewer=UPLOADER)
    assert values.get("error") == "session_not_found", values

    # 산출물이 없는 web_report 세션 — 콜드(building) 또는 not-found 로 끝나야 한다.
    # 실제 백그라운드 빌드는 막는다: 테스트 DB 는 임시 경로라 자식 프로세스가 못 열고,
    # 그 실패 로그가 테스트 출력에 섞인다(검증 대상은 "예외 없이 분기하는가" 뿐).
    from web_report import compute
    real_request_build = compute.request_build
    compute.request_build = lambda *a, **k: False
    try:
        cold = tools_metrics.get_session_metrics("S1", viewer=UPLOADER)
    finally:
        compute.request_build = real_request_build
    assert cold.get("building") or cold.get("error"), cold
    print("[OK] (j) 수치 툴 — 미존재/권한/xlsx/콜드 분기 (예외 없음)")


# ── (k) 웹 응답 ─────────────────────────────────────────────────────────────
def test_answer_web():
    # answer() 계약(키 4개)은 CLI 가 의존한다 — 웹 확장이 이걸 깨면 안 된다
    plain = agent.answer("S3222 보고서 찾아줘", viewer=UPLOADER, use_llm=False)
    assert set(plain) == {"plan", "steps", "text", "data"}, sorted(plain)

    web = agent.answer_web("S3222 보고서 찾아줘", viewer=UPLOADER, use_llm=False)
    assert web["plan"]["intent"] == "session_find", web["plan"]
    urls = [l["url"] for l in web["web"]["links"] if l.get("url")]
    assert urls and all(u.startswith("/pe/report/view/") for u in urls), urls
    assert any("S1" in u for u in urls), urls
    # 세션이 1건이면 후속 질의문이 완성돼 내려온다(서버에 대화 상태를 두지 않는다)
    assert any("세션 S1" in c["question"] for c in web["web"]["choices"]), web["web"]

    # 컨텍스트 세션이 있으면 "이 세션" 이 그 세션으로 해석된다
    ctx = agent.answer_web("이 세션 수율 알려줘", viewer=UPLOADER, use_llm=False,
                           context_session_id="S1")
    assert ctx["plan"]["session_id"] == "S1", ctx["plan"]
    assert ctx["plan"]["intent"] == "session_metrics", ctx["plan"]

    # 컨텍스트가 없으면 지어내지 않고 되묻는다
    ask = agent.answer_web("이 세션 수율 알려줘", viewer=UPLOADER, use_llm=False)
    assert ask["plan"]["session_id"] is None, ask["plan"]
    assert "보고서" in ask["text"], ask["text"]

    # 세션을 열어 둔 채 제품명 없이 물어도 그 세션 질문으로 읽는다
    # (컨텍스트가 없으면 unknown 이 맞다 — 어느 보고서인지 알 수 없으므로)
    opened = agent.answer_web("이슈 알려줘", viewer=UPLOADER, use_llm=False,
                              context_session_id="S1")
    assert opened["plan"]["intent"] == "session_issue", opened["plan"]
    assert "SGM_TRIM_CHECK" in opened["text"], opened["text"]
    assert planner.rule_plan("이슈 알려줘").intent == "unknown"

    # 같은 페이지 안에서의 이동은 url 이 아니라 action 으로 나간다
    jump = agent.answer_web("맵 열어줘", viewer=UPLOADER, use_llm=False,
                            context_session_id="S1")
    actions = [l.get("action") for l in jump["web"]["links"]]
    assert actions == ["open_map"], jump["web"]["links"]
    print("[OK] (k) answer_web — 링크/선택지/컨텍스트 주입 + answer() 계약 유지")


def main():
    setup()
    test_rowkey()
    test_permission()
    test_session_issues_webreport()
    test_session_issues_xlsx()
    test_search_item_in_sessions()
    test_search_products()
    test_eval_db_absent()
    test_eval_db_present()
    test_rule_plan()
    test_metrics_branches()
    test_answer_web()
    print("\n전부 통과")


if __name__ == "__main__":
    main()
