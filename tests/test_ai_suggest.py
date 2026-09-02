# -*- coding: utf-8 -*-
"""AI Comment [제안] 클라 LLM 대행 — 서버 저장·병합·라우트 + 클라 워커 시뮬레이션 (docs/23).

실행:
    server\\.venv\\Scripts\\python.exe tests/test_ai_suggest.py

엔진 평가는 스텁(safe_build_ex 몽키패치 — 결정적 comments+prompts)으로 대체한다.
eval_analyzer 는 이 흐름에서 무수정·무관여가 설계이므로 스텁이 곧 계약 검증이다.

검증 항목 (docs/23 §검증):
  (a) ai_suggest_store — round-trip / merge upsert / 상한 / delete_stale
  (b) webreport_ai_model — 결측/파싱실패/미지값 → "default"
  (c) 라우트 가드 — X-Honey-Agent 부재 403 / 비편집자 거부 / ai_model!=claude 404
  (d) GET prompts — 콜드 202 → (백그라운드 ai 잡) → 200 items(sha 포함)
  (e) POST suggestions — accepted + payload_rev bump (**sha 불일치도 수용** — 게이트 폐기)
  (f) /full payload 에 병합된 [제안] 반영 (rev 채널 재사용 확인)
  (g) 재빌드 생존 — 캐시 전부 비워도 store 재병합으로 suggestion 유지
  (h) 룰 변경 모사 — 프롬프트 sha 가 갈려도 **저장된 LLM 문장이 계속 붙는다**
      (2026-09-02 사용자 결정으로 sha 게이트 폐기 — 종전 "자동 폴백"의 반대)
  (i) 클라 워커(transport/ai_suggest._worker) 동기 시뮬레이션 —
      가짜 requests(Flask test_client 위임) + 가짜 call_claude 로 폴링→생성→push 전체
  (m) **금지 문구**(2026-09-02, /pe/eval AI 지시문 탭) — 사례를 줬는데 "적용할 사례 없음"
      이라 답한 줄을 서버가 지운다. 전부/부분/선례0건 3갈래.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "server"))
# ⚠ client/ 경로는 여기서 넣지 않는다 — client/config.py 가 server/config.py 를 가려
# storage_gateway import 가 깨진다. 워커 시뮬레이션 직전(test_worker_simulation)에 append.

_TMP = Path(tempfile.mkdtemp(prefix="ai_suggest_test_"))
os.environ["REPORT_DB_PATH"] = str(_TMP / "report.db")
os.environ["REPORT_UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["REPORT_S3_BUCKET"] = ""              # S3 비활성 → 로컬 폴백
os.environ["WEB_REPORT_COMPUTE_WORKERS"] = "0"   # 인라인 계산(스텁 몽키패치가 통해야 함)

import pandas as pd  # noqa: E402
from flask import Flask  # noqa: E402

import storage_gateway  # noqa: E402
from database import report_db  # noqa: E402
from report.report_extension import report_bp  # noqa: E402
from web_report import ai_comment as wr_ai_comment  # noqa: E402
from web_report import ai_prompt as wr_ai_prompt  # noqa: E402
from web_report import ai_suggest_store as store  # noqa: E402
from web_report import cache as wr_cache  # noqa: E402
from web_report import edits as wr_edits  # noqa: E402
from web_report import service as wr_service  # noqa: E402
from web_report.honeyform import META_COLUMNS, encode_honeyform_parquet  # noqa: E402
from web_report.validation import canon, webreport_ai_model  # noqa: E402

app = Flask(__name__)
app.register_blueprint(report_bp)
report_db.init_report_db()
client = app.test_client()

USER = "tester"
UPLOAD_ROOT = Path(os.environ["REPORT_UPLOAD_DIR"])
ITEM = "ItemA"
AI_OPTS = json.dumps({"ai_comment": True, "ai_comment_optin": True,
                      "ai_model": "claude"})
AI_OPTS_DEFAULT = json.dumps({"ai_comment": True, "ai_comment_optin": True})

# ── 엔진 스텁 — 결정적 comments + prompts ────────────────────────────────────
_PROMPT_SALT = {"v": "p1"}   # 룰 변경 모사: 값을 바꾸면 프롬프트·sha 가 갈린다
# 새 토큰 3섹션(2026-09-02). [사례]/[제안] 은 코드가 만든 뼈대이고, LLM 이 오면 각각 교체된다.
_CELL = "[MAJOR] [현상] - EDGE: 스텁 현상\n[사례] ①(P1/L1) 스텁 사례 \n [제안] - EDGE: 기본조치"


def _stub_prompt():
    return f"스텁 프롬프트 {_PROMPT_SALT['v']} — {ITEM}"


_PRECEDENTS = {"n": 1}   # 이 item 의 프롬프트에 선례가 실렸나 (금지 문구 게이트 재료)
_PREC_ROWS = [{"product_name": "P1", "lot_id": "L1", "item_canonical": "itema",
               "status": "MAJOR", "signature": "EDGE", "comment": "스텁 사례",
               "metrics": {"cpk": 0.62}}]


def _stub_safe_build_ex(tables, session, selected_items=None, fail_only=None,
                        generate_comment=True):
    prompt = _stub_prompt()
    keys = [f"Yield|5|{ITEM}", f"CPK|{ITEM}", f"ETC|{ITEM}"]
    n = _PRECEDENTS["n"]
    result = {
        "comments": {k: _CELL for k in keys},
        "etc_auto_items": [], "row_signatures": {}, "signature_options": [],
        # 선례 0건이면 프롬프트를 만들지 않는다 — build_prompt 의 실제 동작과 같게 흉내낸다.
        "prompts": ({ITEM: {"prompt": prompt, "sha": wr_ai_prompt.prompt_sha(prompt),
                            "precedents": n}} if n else {}),
        "precedents": ({ITEM: _PREC_ROWS} if n else {}),
        "precedent_counts": ({k: n for k in keys} if n else {}),
    }
    if not generate_comment:
        result = dict(result, comments={}, prompts={},
                      precedents={}, precedent_counts={})
    return result, True


wr_ai_comment.safe_build_ex = _stub_safe_build_ex


def _make_parquet():
    cols = META_COLUMNS + [ITEM, "ItemB"]
    rows = [["TSEQ", "", "", "", "", "", "", 1, 2],
            ["TNO", "", "", "", "", "", "", 100, 200],
            ["STEP", "", "", "", "", "", "", "P1", "P2"],
            ["UNIT", "", "", "", "", "", "", "V", "V"],
            ["HILIM", "", "", "", "", "", "", 12, 12],
            ["LOLIM", "", "", "", "", "", "", 8, 8]]
    for i in range(20):
        a, b, bin_code, failtno = 10 + (i % 5) * 0.1, 10 + (i % 7) * 0.1, 1, ""
        if i == 18:
            a, bin_code, failtno = 11.9, 5, 100
        if i == 19:
            b, bin_code, failtno = 11.9, 6, 200
        rows.append([f"s{i}", 1, 1, i % 5, i // 5, bin_code, failtno, a, b])
    return encode_honeyform_parquet(pd.DataFrame(rows, columns=cols))


def _setup(sid, akey, opts):
    blob = _make_parquet()
    chash = hashlib.sha256(canon({"files": [hashlib.sha256(blob).hexdigest()]})).hexdigest()
    report_db.create_session(sid, "x.parquet", None, product_type="MDDI", lot_id="LOT1",
                             product="P1", source="web_report", uploaded_by=USER)
    report_db.update_session(sid, analysis_key=akey, content_hash=chash, status="done",
                             webreport_options=opts)
    storage_gateway.save_webreport_sources(
        akey, chash, [blob],
        {"sources": [{"name": "Lot1", "file_name": "lot1.csv"}],
         "selected_items": [], "mode": "Normal"},
        upload_root=UPLOAD_ROOT)
    return chash


def _headers(user=USER):
    return {"User-Agent": f"Mozilla/5.0 HoneyUser/{user}",
            "X-Honey-Agent": "1", "Content-Type": "application/json"}


def _get_prompts(sid, headers=None, wait_200=False, timeout=30):
    url = f"/pe/report/session/{sid}/web_report/ai_comment/prompts"
    deadline = time.time() + timeout
    while True:
        r = client.get(url, headers=headers or _headers())
        if not wait_200 or r.status_code != 202 or time.time() > deadline:
            return r
        time.sleep(0.3)


def _full_text(sid, must_contain=None, timeout=30):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        r = client.get(f"/pe/report/session/{sid}/full",
                       headers={"User-Agent": f"Mozilla/5.0 HoneyUser/{USER}"})
        if r.status_code == 200:
            body = r.data if r.headers.get("Content-Encoding") != "gzip" \
                else gzip.decompress(r.data)
            last = body.decode("utf-8")
            if not must_contain or must_contain in last:
                return last
        time.sleep(0.4)
    return last


# ── (a) store 단위 ───────────────────────────────────────────────────────────

def test_store_roundtrip():
    root = _TMP / "store_unit"
    n = store.save_merge(root, "AKEY", "c" * 64, "Normal",
                         {"X": {"sha": "a" * 12, "suggestion": "s1"}}, by="u1")
    assert n == 1
    loaded = store.load(root, "AKEY", "c" * 64, "Normal")
    assert loaded["X"]["sha"] == "a" * 12 and loaded["X"]["by"] == "u1"
    # upsert(멱등) + 추가
    n = store.save_merge(root, "AKEY", "c" * 64, "Normal",
                         {"X": {"sha": "b" * 12, "suggestion": "s2"},
                          "Y": {"sha": "d" * 12, "suggestion": "s3"}})
    assert n == 2
    loaded = store.load(root, "AKEY", "c" * 64, "Normal")
    assert loaded["X"]["sha"] == "b" * 12 and "Y" in loaded
    # 전처리 variant 는 별도 파일
    assert store.load(root, "AKEY", "c" * 64, "Normal", prep_digest="12345678") == {}
    # 손상 파일 → {}
    p = store.store_path(root, "AKEY", "c" * 64, "Normal")
    p.write_text("{broken", encoding="utf-8")
    assert store.load(root, "AKEY", "c" * 64, "Normal") == {}
    # delete_stale — 다른 chash 세대만 지운다
    store.save_merge(root, "AKEY", "c" * 64, "Normal", {"X": {"sha": "a" * 12, "suggestion": "s"}})
    store.save_merge(root, "AKEY", "e" * 64, "Normal", {"X": {"sha": "a" * 12, "suggestion": "s"}})
    removed = store.delete_stale(root, "AKEY", "c" * 64)
    assert removed == 1
    assert store.load(root, "AKEY", "c" * 64, "Normal")
    assert store.load(root, "AKEY", "e" * 64, "Normal") == {}

    # raw(LLM 원문) 보존 (2026-09-01) — 관리자 검수에서 "모델이 이상하게 답한 것"과
    # "서버 sanitize 가 잘라낸 것"을 가르는 유일한 근거다.
    root2 = _TMP / "store_raw"
    store.save_merge(root2, "AK2", "f" * 64, "Normal", {
        # 같으면 저장하지 않는다 — 정보가 0인데 파일만 커진다
        "SAME": {"sha": "a" * 12, "suggestion": "본문", "raw": "본문"},
        "DIFF": {"sha": "b" * 12, "suggestion": "본문", "raw": "```json\n본문\n```"},
        "LONG": {"sha": "c" * 12, "suggestion": "x", "raw": "y" * (store.MAX_RAW_CHARS + 500)},
        "NONE": {"sha": "d" * 12, "suggestion": "본문"},        # 옛 클라 — raw 키 없음
    })
    loaded = store.load(root2, "AK2", "f" * 64, "Normal")
    assert "raw" not in loaded["SAME"], loaded["SAME"]
    assert loaded["DIFF"]["raw"] == "```json\n본문\n```", loaded["DIFF"]
    assert len(loaded["LONG"]["raw"]) == store.MAX_RAW_CHARS      # 상한으로 자른다
    assert "raw" not in loaded["NONE"], loaded["NONE"]
    print("  (a) store round-trip/upsert/delete_stale/raw 보존 OK")


# ── (b) 옵션 파싱 ────────────────────────────────────────────────────────────

def test_ai_model_validation():
    assert webreport_ai_model("") == "default"
    assert webreport_ai_model(None) == "default"
    assert webreport_ai_model("{broken") == "default"
    assert webreport_ai_model('"str"') == "default"
    assert webreport_ai_model('{"ai_model": "gpt"}') == "default"
    assert webreport_ai_model('{"ai_model": "claude"}') == "claude"
    assert webreport_ai_model(AI_OPTS_DEFAULT) == "default"
    print("  (b) webreport_ai_model 폴백 OK")


# ── (c)~(h) 라우트·병합 e2e ─────────────────────────────────────────────────

SID = "AISUG01"
AKEY = "b" * 64
SID_DEF = "AISUG02"
AKEY_DEF = "d" * 64


def test_route_guards():
    # X-Honey-Agent 부재 → 403
    r = client.get(f"/pe/report/session/{SID}/web_report/ai_comment/prompts",
                   headers={"User-Agent": f"Mozilla/5.0 HoneyUser/{USER}"})
    assert r.status_code == 403, r.status_code
    # 비편집자 → 거부 (편집자 가드)
    r = _get_prompts(SID, headers=_headers("someone.else"))
    assert r.status_code in (401, 403), r.status_code
    # ai_model 미지정 세션 → 404
    r = _get_prompts(SID_DEF)
    assert r.status_code == 404, r.status_code
    r = client.post(f"/pe/report/session/{SID_DEF}/web_report/ai_comment/suggestions",
                    headers=_headers(), json={"items": []})
    assert r.status_code == 404, r.status_code
    print("  (c) 가드 403/비편집자/404 OK")


def test_prompts_flow():
    r = _get_prompts(SID, wait_200=True)
    assert r.status_code == 200, (r.status_code, r.data[:200])
    items = r.get_json()["items"]
    assert len(items) == 1 and items[0]["key"] == ITEM
    assert items[0]["sha"] == wr_ai_prompt.prompt_sha(_stub_prompt())
    assert "스텁 프롬프트" in items[0]["prompt"]
    print("  (d) prompts 202→200 OK")
    return items[0]


def test_push_and_merge(prompt_item):
    url = f"/pe/report/session/{SID}/web_report/ai_comment/suggestions"
    rev0 = report_db.get_webreport_edit_rev(SID)
    # ⚠ **sha 가 달라도 수용한다**(2026-09-02 사용자 결정 — 게이트 폐기).
    # 종전에는 여기서 skips["sha_mismatch"]==1 이었다. 그 게이트 때문에 지시문을 고칠
    # 때마다 프롬프트 sha 가 갈려 클라가 방금 만든 문장까지 버려졌고, 화면은 재대행
    # 전까지 action_ko 나열로 후퇴했다(사용자 신고의 실제 원인). **되살리지 말 것.**
    # `sha_mismatch` 키는 응답 형식 호환으로 남지만 이제 항상 0 이다.
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": "0" * 12,
                                     "suggestion": "- 옛 sha 문장"}]})
    body = r.get_json()
    assert r.status_code == 200 and body["accepted"] == 1, body
    assert body["skips"]["sha_mismatch"] == 0, body
    rev0 = report_db.get_webreport_edit_rev(SID)   # 위 push 가 수용됐으므로 기준 재설정
    # 일치 → 수용 + rev bump
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": prompt_item["sha"],
                                     "suggestion": "- 클로드 제안 1\n- 클로드 제안 2"}]})
    assert r.status_code == 200, (r.status_code, r.data[:300])
    body = r.get_json()
    assert body["accepted"] == 1 and body["skipped"] == 0, body
    assert not any(body["skips"].values()), body
    assert report_db.get_webreport_edit_rev(SID) == rev0 + 1
    # 형식 불량(sha 12hex 아님) 과 sanitize 후 빈 문자열은 **다른 사유**로 세어야 한다
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": "XYZ", "suggestion": "x"},
                                    {"key": ITEM, "sha": prompt_item["sha"],
                                     "suggestion": "[제안][현상]"}]})
    body = r.get_json()
    assert body["accepted"] == 0 and body["skipped"] == 2, body
    assert body["skips"]["badsha"] == 1 and body["skips"]["empty"] == 1, body
    # 서버가 프롬프트를 만들지 않은 item → unknown_item (임의 row_key 제출 차단)
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": "ETC|없는항목", "sha": prompt_item["sha"],
                                     "suggestion": "- 임의 제출"}]})
    body = r.get_json()
    assert body["accepted"] == 0 and body["skips"]["unknown_item"] == 1, body
    # 감사 action 은 'edit' 과 분리돼 있어야 한다(2026-08-28) — 관리자 모니터링 탭이
    # action='ai_suggest' 인덱스로 집계하므로, 되돌리면 그 화면이 조용히 빈다.
    logs = report_db.get_audit_logs(action="ai_suggest", session_id=SID)
    assert logs, "action='ai_suggest' 감사 기록이 없다"
    assert "ai_suggest(accepted=1,skipped=0)" in {r["changed_fields"] for r in logs}
    print("  (e) push 수용(sha 무관)·skip 사유·rev bump·감사 action 분리 OK")


def test_denied_lines(prompt_item):
    """금지 문구(/pe/eval AI 지시문) — 사례를 줬는데 "사례 없음" 이라 답한 줄을 서버가 지운다.

    지시문만으로는 LLM 이 계속 어겨서 넣은 마지막 안전장치다(사용자 신고). 세 갈래를 본다:
      ① 전부 금지 문구 → 저장 안 함 + skips.denied (룰 문장 폴백)
      ② 섞여 있으면 → 나쁜 줄만 빼고 accepted
      ③ **선례가 0건인 item** → "사례가 없다" 는 사실이므로 지우지 않는다
    """
    url = f"/pe/report/session/{SID}/web_report/ai_comment/suggestions"
    deny_only = ("- 검색된 과거 사례 중 현재 현상에 직접 적용할 수 있는 사례는 "
                 "확인되지 않았습니다.")
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": prompt_item["sha"],
                                     "suggestion": deny_only}]})
    body = r.get_json()
    assert body["accepted"] == 0 and body["skips"]["denied"] == 1, body
    # 세션 저장분에는 직전(정상) 문장이 그대로 남아야 한다 — 폐기가 덮어쓰기가 되면 안 된다
    stored = wr_edits.load_ai_suggestions(report_db, SID)
    assert stored[ITEM]["suggestion"].startswith("- 클로드 제안 1"), stored[ITEM]

    # ② 혼합 — 나쁜 줄만 빠지고 나머지는 저장된다
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": prompt_item["sha"],
                                     "suggestion": deny_only + "\n- edge 링 오염 이력을 확인하라."}]})
    body = r.get_json()
    assert body["accepted"] == 1 and not any(body["skips"].values()), body
    stored = wr_edits.load_ai_suggestions(report_db, SID)
    assert stored[ITEM]["suggestion"] == "- edge 링 오염 이력을 확인하라.", stored[ITEM]
    assert "확인되지 않았습니다" in stored[ITEM]["raw"], "원문(raw) 이 안 남았다"

    # ③ 선례 0건 item — **프롬프트 자체가 안 만들어진다**(2026-09-02). LLM 을 거치지 않으니
    #    금지 문구가 적용될 문장도 없다. 클라 워커는 빈 목록을 받고 조용히 끝낸다.
    _PRECEDENTS["n"] = 0
    _wipe_caches(AKEY)
    assert _get_prompts(SID, wait_200=True).get_json()["items"] == [], \
        "선례 0건인데 프롬프트를 만들었다 — LLM 토큰·시간 낭비"
    _PRECEDENTS["n"] = 1
    _wipe_caches(AKEY)
    # 뒤 테스트(재빌드 생존)가 기대하는 상태로 되돌린다
    item3 = _get_prompts(SID, wait_200=True).get_json()["items"][0]
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": item3["sha"],
                                     "suggestion": "- 클로드 제안 1\n- 클로드 제안 2"}]})
    assert r.get_json()["accepted"] == 1
    print("  (m) 금지 문구 폐기/부분제거/선례0건 프롬프트 생략 OK")


def test_two_block_push(prompt_item):
    """(n) 두 블록 계약 — [사례] 요약과 [제안] 이 **각각의 섹션**으로 들어간다.

    사용자 결정(2026-09-02): 사례가 있으면 [사례]는 LLM 요약으로, [제안]은 통합 문장으로
    바뀐다. 한 덩어리로 [제안]에만 들어가면 사례 요약이 화면에서 사라진다.
    """
    url = f"/pe/report/session/{SID}/web_report/ai_comment/suggestions"
    reply = ("[사례]\n- P1/L1: 재측정으로 회복된 건\n"
             "[제안]\n- edge 이력 먼저 확인\n- 이어서 산포 재측정")
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": prompt_item["sha"],
                                     "suggestion": reply}]})
    body = r.get_json()
    assert body["accepted"] == 1, body

    stored = wr_edits.load_ai_suggestions(report_db, SID)
    assert stored[ITEM]["cases"] == "- P1/L1: 재측정으로 회복된 건", stored[ITEM]
    assert stored[ITEM]["suggestion"].startswith("- edge 이력 먼저"), stored[ITEM]

    text = _full_text(SID, must_contain="재측정으로 회복된 건")
    assert "[사례] - P1/L1: 재측정으로 회복된 건" in text, "사례 요약이 [사례] 섹션에 없다"
    assert "[제안] - edge 이력 먼저 확인" in text, "통합 제안이 [제안] 섹션에 없다"
    assert "스텁 사례" not in text, "코드가 만든 사례 나열이 안 교체됐다"
    print("  (n) 두 블록 push → 섹션별 반영 OK")


def test_precedents_payload_and_route():
    """(o) 사례 건수(payload)와 상세 목록(라우트) — 「📋 사례 N건 상세」의 재료."""
    text = _full_text(SID, must_contain="ai_precedents")
    payload = json.loads(text)["web_report"]
    assert payload["ai_precedents"][f"CPK|{ITEM}"] == 1, payload["ai_precedents"]

    url = f"/pe/report/session/{SID}/web_report/ai_comment/precedents"
    # 조회 전용이라 Honey 헤더 없이도 열린다(뷰어 권한이면 충분).
    r = client.get(url + f"?key=CPK|{ITEM}",
                   headers={"User-Agent": f"Mozilla/5.0 HoneyUser/{USER}"})
    assert r.status_code == 200, (r.status_code, r.data[:200])
    items = r.get_json()["items"]
    assert len(items) == 1 and items[0]["comment"] == "스텁 사례", items
    assert items[0]["metrics"]["cpk"] == 0.62, items[0]
    # 매칭 안 되는 키는 빈 목록 (에러가 아니다 — 화면은 링크를 안 그린다)
    r = client.get(url + "?key=CPK|없는항목",
                   headers={"User-Agent": f"Mozilla/5.0 HoneyUser/{USER}"})
    assert r.status_code == 200 and r.get_json()["items"] == []

    # 출처 세션 링크 재료 — 팝오버의 "세션 열기 ↗" 는 선례 행의 session_id 로 그린다.
    # 엔진은 계약 dict 에 담아 주는데 서버 필터(_PREC_VIEW_KEYS)가 버리면 링크가 안 뜬다
    # (2026-09-02 실제로 그랬다). 그 필터를 직접 태워 통과를 고정한다.
    view = wr_ai_comment._precedent_views({ITEM: {"precedents": [
        {"product_name": "P9", "comment": "다른 세션 사례",
         "session_id": "SRC_SESSION_1", "metrics": {"cpk": 0.5}}]}})
    assert view[ITEM][0].get("session_id") == "SRC_SESSION_1", \
        f"선례에 session_id 가 안 실렸다 — 팝오버 '세션 열기' 링크가 안 뜬다: {view}"
    print("  (o) 사례 건수 payload + 상세 라우트 + 출처 세션 링크 재료 OK")


def test_full_payload_merged():
    text = _full_text(SID, must_contain="클로드 제안 1")
    assert "클로드 제안 1" in text, "payload 에 병합된 [제안] 이 없다"
    assert "[현상] - EDGE: 스텁 현상" in text          # 앞 섹션 보존
    print("  (f) /full payload 반영 OK")


def _wipe_caches(akey):
    wr_cache.invalidate_caches(akey)
    shutil.rmtree(UPLOAD_ROOT / "web_report" / akey / "cache", ignore_errors=True)


def _overlay_cell(sid, akey, *, wipe=True):
    """콜드 재빌드 뒤 payload 에 실릴 AI Comment 셀 — 엔진 결과 + 세션 문장 덧칠.

    2026-09-02 개편으로 문장 병합이 **공유 캐시가 아니라 payload 조립 시점**에 일어난다
    (service._session_ai_overlay). 그래서 검증도 엔진 캐시(_ai_comment_cached)만이 아니라
    그 오버레이를 거친 결과를 본다 — 화면이 실제로 받는 값이다.
    """
    if wipe:
        _wipe_caches(akey)
    session = report_db.get_session(sid)
    tables_sess, tables, manifest = wr_service._load_tables(
        sid, report_db=report_db, upload_root=UPLOAD_ROOT, session=session)
    result, how = wr_service._ai_comment_cached(
        tables_sess, sid, tables, manifest, report_db=report_db, upload_root=UPLOAD_ROOT)
    merged, pending, sources = wr_service._session_ai_overlay(
        tables_sess, sid, result, report_db=report_db)
    return merged, how, pending, sources


def test_rebuild_survival():
    merged, how, _pending, _src = _overlay_cell(SID, AKEY)
    assert how == "build", how
    # 직전 push(두 블록)가 **두 섹션 모두** 재빌드 뒤에도 살아 있어야 한다.
    cell = merged["comments"][f"CPK|{ITEM}"]
    assert cell.endswith("- edge 이력 먼저 확인\n- 이어서 산포 재측정"), \
        f"재빌드에서 세션 문장 재병합이 안 됐다: {cell!r}"
    assert "[사례] - P1/L1: 재측정으로 회복된 건" in cell, \
        f"사례 요약이 재빌드에서 사라졌다: {cell!r}"
    print("  (g) 재빌드 생존(재병합 — 두 섹션) OK")


def test_sources_no_precedent_is_rule():
    """(q) **사례 0건 행은 서버 LLM 을 안 거치므로 아이콘도 rule** (2026-09-02 사용자 지적).

    엔진은 선례가 1건도 없으면 LLM 을 아예 호출하지 않는다
    (recommend.make_comment 의 has_precedent_comments 게이트) — 그 행의 문장은 룰 조립
    (action_ko)이다. 종전에는 서버 LLM 배선이 켜져 있으면 **전 행**에 "llm" 을 줘서,
    LLM 을 거치지도 않은 칸이 🤖 로 표시됐다. 프롬프트 유무(= 선례 유무)로 갈라야 한다.
    """
    # 선례 0건 → 서버가 프롬프트를 만들지 않는다(= LLM 미경유 행).
    _PRECEDENTS["n"] = 0
    try:
        merged, _how, pending, sources = _overlay_cell(SID, AKEY)
        assert not merged.get("prompts"), "선례 0건인데 프롬프트가 만들어졌다(전제 붕괴)"
        assert not pending, f"선례 0건 행이 Claude 대기로 잡혔다: {pending}"
        vals = set(sources.values())
        assert vals and vals == {"rule"}, \
            f"선례 0건 행의 출처가 rule 이 아니다(LLM 미경유인데 🤖 로 보인다): {vals}"
    finally:
        _PRECEDENTS["n"] = 1
        _wipe_caches(AKEY)
    print("  (q) 선례 0건 행 = rule 아이콘(서버 LLM 미경유) OK")


def test_sibling_isolation():
    """(b2) **형제 세션 무간섭** — 같은 analysis_key 를 쓰는 다른 세션에 문장이 안 샌다.

    2026-09-02 개편의 핵심 회귀 가드다. 종전에는 문장이 analysis_key 단위 공유 파일에
    저장되고 그 병합 결과가 session_id 없는 aicmt 캐시에 구워져, 같은 rawdata 를 다시
    올린 형제 세션이 남의 문장을 그대로 봤다(사용자 신고: "새 세션인데 옛 문장이 먼저
    보인다", "이미 만든 세션이 바뀐다"). 이제 저장이 세션 편집 DB 라 구조적으로 불가능하다.
    """
    # 같은 analysis_key·content_hash 를 쓰는 dedup 형제 = 같은 rawdata 재업로드 상황.
    sib = SID + "SIB"
    src = report_db.get_session(SID)
    report_db.create_session(sib, "sibling.parquet", None, product_type="MDDI",
                             lot_id="LOT1", product="P1", source="web_report",
                             uploaded_by=USER)
    report_db.update_session(sib, analysis_key=src["analysis_key"],
                             content_hash=src.get("content_hash"), status="done",
                             webreport_options=src.get("webreport_options"))
    try:
        stored = wr_edits.load_ai_suggestions(report_db, sib)
        assert stored == {}, f"형제 세션이 남의 저장분을 읽었다: {stored}"
        merged, _how, _pending, _src2 = _overlay_cell(sib, AKEY, wipe=False)
        cell = merged["comments"][f"CPK|{ITEM}"]
        assert "edge 이력 먼저 확인" not in cell, \
            f"형제 세션에 남의 LLM 문장이 샜다: {cell!r}"
        assert "스텁 조치" in cell or "[제안]" in cell, \
            f"형제 세션이 코드 문장(action_ko)을 못 받았다: {cell!r}"
    finally:
        report_db.delete_session(sib)
    print("  (b2) 형제 세션 무간섭(문장·캐시 분리) OK")


def test_sha_drift_keeps_suggestion():
    """룰을 고쳐 프롬프트 sha 가 갈려도 **저장된 LLM 문장이 계속 붙는다**.

    2026-09-02 사용자 결정으로 sha 게이트를 폐기했다. 종전에는 여기서 action_ko
    기본조치로 폴백하는 것이 정상이었는데, 실제 운영에서는 지시문을 한 번 고칠 때마다
    전 세션의 LLM 문장이 통째로 사라지고 화면이 룰 문장 나열로 후퇴했다 — store 에는
    멀쩡한 문장이 있는데 관리자 화면(게이트 없음)과 Issue Table(게이트 있음)이 서로
    다르게 보이던 신고의 원인. 옛 프롬프트 기준 문장이라도 룰 문장보다 낫고, 다음
    재대행 때 자연히 교체된다(클라 워커는 sha 로 건너뛰지 않는다). **되살리지 말 것.**
    """
    _PROMPT_SALT["v"] = "p2-rules-changed"   # 룰 변경 모사 — 프롬프트가 달라진다
    merged, how, _pending, _src = _overlay_cell(SID, AKEY)
    assert how == "build"
    cell = merged["comments"][f"CPK|{ITEM}"]
    assert cell.endswith("- edge 이력 먼저 확인\n- 이어서 산포 재측정"), \
        f"sha 가 갈렸다고 LLM 문장을 버렸다(게이트 부활): {cell!r}"
    _PROMPT_SALT["v"] = "p1"                 # 원복
    print("  (h) 룰 변경(sha drift) 후에도 LLM 문장 유지 OK")


# ── (i) 클라 워커 동기 시뮬레이션 ────────────────────────────────────────────

SID_W = "AISUG03"
AKEY_W = "f" * 64


class _FakeResp:
    def __init__(self, r):
        self.status_code = r.status_code
        self._data = r.data

    def json(self):
        return json.loads(self._data)


class _FakeRequests:
    """transport.ai_suggest 의 requests 호출을 Flask test_client 로 위임."""

    def get(self, url, headers=None, timeout=None):
        return _FakeResp(client.get(urlsplit(url).path, headers=headers))

    def post(self, url, data=None, headers=None, timeout=None):
        return _FakeResp(client.post(urlsplit(url).path, data=data, headers=headers))


class _FakeCallClaude:
    ENV_BIN = "CALL_CLAUDE_BIN"
    calls = []
    models = []

    @staticmethod
    def find_cli(env=None):
        return "C:/fake/claude.exe"

    @staticmethod
    def run_batch(prompts, *, bin_path=None, model=None, timeout=None, log=None):
        _FakeCallClaude.calls.append(list(prompts))
        _FakeCallClaude.models.append(model)
        return [f"- 워커 생성 제안 ({i + 1})" for i in range(len(prompts))]

    @staticmethod
    def probe(*, bin_path=None, timeout=None, log=None):
        return {"ok": True, "bin": bin_path, "version": "9.9.9-fake", "flags": [],
                "error": None}

    @staticmethod
    def run_prompt(prompt, *, bin_path=None, model=None, timeout=None, log=None):
        _FakeCallClaude.models.append(model)
        return "ok"


def test_worker_simulation():
    _setup(SID_W, AKEY_W, AI_OPTS)
    # client 경로는 server 모듈 로드가 다 끝난 지금 **append**(뒤 순위) — config 이름 충돌 회피
    sys.path.append(os.path.join(_ROOT, "client"))
    from transport import ai_suggest as t_ai

    t_ai.requests = _FakeRequests()
    t_ai._import_call_claude = lambda: _FakeCallClaude
    t_ai._headers = lambda: _headers()
    t_ai._POLL_INTERVAL_SEC = 0.2
    t_ai._PUSH_RETRY_WAIT_SEC = 0.2

    # 게이트: 옵트인 아니면 기동 안 함
    assert t_ai.start_background(SID_W, {"ai_comment_optin": True}) is False
    assert t_ai.start_background("?", {"ai_comment_optin": True, "ai_model": "claude"}) is False

    # 진행 알림(2026-09-01) — 성공 경로에서도 사용자에게 한 줄이 가야 한다. 종전에는
    # 워커가 전부 조용해서, 화면엔 룰 문장이 정상처럼 나오는 탓에 사용자가 실패를
    # 알아챌 방법이 아예 없었다.
    notes = []
    t_ai._worker(SID_W, "http://fake-server", notes.append)   # 동기 실행 (스레드 없이)
    assert _FakeCallClaude.calls, "run_batch 가 불리지 않았다"
    assert "스텁 프롬프트" in _FakeCallClaude.calls[0][0]
    assert any("대행 완료" in n for n in notes), notes
    assert any("새로고침" in n for n in notes), notes   # 다음에 뭘 할지까지 알려 준다
    # 기본 모델은 정식명 고정 — 별칭('sonnet')은 새 버전이 나오면 말없이 바뀐다.
    assert t_ai.DEFAULT_MODEL == "claude-sonnet-5"
    assert _FakeCallClaude.models[-1] == "claude-sonnet-5", _FakeCallClaude.models
    # push 결과가 **그 세션의** 저장분과 payload 에 반영됐는지
    stored = wr_edits.load_ai_suggestions(report_db, SID_W)
    assert ITEM in stored and stored[ITEM]["suggestion"].startswith("- 워커 생성 제안")
    assert stored[ITEM].get("provider") == "claude", stored[ITEM]
    text = _full_text(SID_W, must_contain="워커 생성 제안")
    assert "워커 생성 제안" in text
    print("  (i) 클라 워커 시뮬레이션(폴링→생성→push→반영) OK")
    return t_ai


def test_worker_parallel_and_incremental_push(t_ai):
    """(p) 배치 **병렬 실행** + **배치별 즉시 push** (2026-09-02 사용자 요구 4).

    종전엔 배치를 완전 순차로 돌리고 전부 모아 마지막에 한 번만 push 했다 — 100건 세션이
    7~10분이고 그동안 화면에 아무 변화가 없었다. 여기서 보는 것:
      ① 동시에 실행 중인 배치가 2개 이상 있었다(순차면 최대 1이다)
      ② push 가 배치 수만큼 나뉘어 갔다(한 번에 몰아 보내지 않는다) — 화면 점진 갱신의 근거
      ③ 진행 알림이 배치마다 나온다(종전엔 최대 10분간 무소식)
    """
    import threading as _th

    lock = _th.Lock()
    live = {"now": 0, "peak": 0}
    orig_batch = _FakeCallClaude.run_batch
    orig_post = t_ai._post_suggestions
    pushes = []

    def slow_batch(prompts, *, bin_path=None, model=None, timeout=None, log=None):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            time.sleep(0.25)      # 동시성이 실제로 겹치도록 — 순차면 peak 가 1 에 머문다
            return [f"- 병렬 제안 ({i + 1})" for i in range(len(prompts))]
        finally:
            with lock:
                live["now"] -= 1

    def spy_post(base, session_id, headers, rows):
        pushes.append(len(rows))
        return orig_post(base, session_id, headers, rows)

    # 프롬프트 6건 → 배치 크기 2 → 3배치. 병렬 3 이면 peak 가 2 이상 나와야 한다.
    orig_prompts = t_ai._fetch_prompts
    t_ai._fetch_prompts = lambda *a, **k: (
        [{"key": ITEM, "sha": "b" * 12, "prompt": f"스텁 프롬프트 {i}"} for i in range(6)],
        "", 200)
    _FakeCallClaude.run_batch = staticmethod(slow_batch)
    t_ai._post_suggestions = spy_post
    os.environ["HONEY_CLAUDE_BATCH"] = "2"
    os.environ["HONEY_CLAUDE_PARALLEL"] = "3"
    notes = []
    try:
        t_ai._worker(SID_W, "http://fake-server", notes.append)
    finally:
        _FakeCallClaude.run_batch = orig_batch
        t_ai._post_suggestions = orig_post
        t_ai._fetch_prompts = orig_prompts
        os.environ.pop("HONEY_CLAUDE_BATCH", None)
        os.environ.pop("HONEY_CLAUDE_PARALLEL", None)

    assert live["peak"] >= 2, f"배치가 순차로 돌았다(동시 최대 {live['peak']}) — 병렬화 회귀"
    assert len(pushes) >= 2, f"push 가 한 번에 몰렸다({pushes}) — 화면 점진 갱신이 안 된다"
    assert any("배치" in n and "/" in n for n in notes), \
        f"배치별 진행 알림이 없다(무소식 구간 재발): {notes}"
    print(f"  (p) 배치 병렬(동시 {live['peak']})·배치별 push({len(pushes)}회)·진행 알림 OK")


def test_worker_failure_reports(t_ai):
    """실패 사유가 서버 진단 사건으로 나가는지 (관리자 모니터링의 유일한 신호원).

    실패해도 화면에는 룰 폴백 문장이 나오므로, 이 보고가 빠지면 관리자는 기능이
    죽은 것을 영영 모른다 — 그래서 '조용히 끝난다'와 '보고는 한다'가 함께 성립해야 한다.
    """
    sent = []
    notes = []      # 사용자에게 가는 알림 — 관리자 보고와 **별개 경로**다
    orig_report = t_ai._report_failure
    t_ai._report_failure = lambda kind, msg, sid, ctx: sent.append((kind, sid, ctx))
    orig_find = _FakeCallClaude.find_cli
    orig_batch = _FakeCallClaude.run_batch
    try:
        # ① CLI 없음
        _FakeCallClaude.find_cli = staticmethod(lambda env=None: None)
        t_ai._worker(SID_W, "http://fake-server", notes.append)
        assert sent and sent[-1][0] == "ai_suggest_no_cli", sent
        # 사용자도 알아야 한다 — 무엇을 확인할지(HONEY_CLAUDE_BIN)까지 문장에 있어야
        # 신고를 받은 담당자가 되묻지 않는다.
        assert "HONEY_CLAUDE_BIN" in notes[-1], notes[-1]
        # ② CLI 는 있는데 생성 0건 — 현장 인증 실패의 1순위 신호
        _FakeCallClaude.find_cli = orig_find
        _FakeCallClaude.run_batch = staticmethod(
            lambda prompts, **kw: [None] * len(prompts))
        t_ai._worker(SID_W, "http://fake-server", notes.append)
        kind, sid, ctx = sent[-1]
        assert kind == "ai_suggest_empty" and sid == SID_W, sent[-1]
        assert ctx["items"] >= 1 and "batches" in ctx and "cli_log" in ctx
        assert "신호등" in notes[-1], notes[-1]      # 다음 행동을 지목
        # ③ 대상 아닌 세션(404) → 사유가 denied 로 구분돼 보고된다
        t_ai._worker(SID_DEF, "http://fake-server", notes.append)
        kind, _sid, ctx = sent[-1]
        assert kind == "ai_suggest_no_prompts" and ctx["reason"] == "denied", sent[-1]
        assert ctx["status"] == 404, ctx     # 상태 코드가 보고에 실린다
        assert "HTTP 404" in notes[-1], notes[-1]
    finally:
        t_ai._report_failure = orig_report
        _FakeCallClaude.find_cli = orig_find
        _FakeCallClaude.run_batch = orig_batch
    print("  (j) 클라 실패 보고 3종(no_cli/empty/no_prompts) + 사용자 알림 OK")


def test_http_hint(t_ai):
    """HTTP 상태 → 사용자용 안내 (2026-09-01).

    숫자만 보여 주면 사용자는 여전히 다음에 뭘 할지 모른다 — 조치가 함께 나와야 한다.
    모르는 코드에서도 문장이 나와야 하고(빈 문자열 금지), 예외를 던지면 안 된다.
    """
    assert "권한" in t_ai.http_hint(403)
    assert "서버 버전" in t_ai.http_hint(404)
    assert "잠겨" in t_ai.http_hint(423)          # 사용자가 예로 든 코드
    for code in (500, 502, 503):
        assert t_ai.http_hint(code).startswith(f"HTTP {code}"), code
    assert t_ai.http_hint(418).startswith("HTTP 418")   # 모르는 4xx 도 문장은 나온다
    assert t_ai.http_hint(599).startswith("HTTP 599")
    # 요청 자체가 안 나간 경우(status 0/None) — 서버가 아니라 네트워크를 보라고 한다
    assert "네트워크" in t_ai.http_hint(0)
    assert "네트워크" in t_ai.http_hint(None)
    assert t_ai.http_hint("bad") == ""            # 숫자가 아니면 조용히 빈 문자열
    print("  (l) http_hint 사용자 안내(403/404/423/5xx/미상) OK")


def test_check_status(t_ai):
    """Honey UI 신호등 판정 — 실호출 1회로 초록/빨강을 가른다.

    바이너리 존재만 보면 인증·정책 실패를 못 잡아 '초록인데 안 되는' 거짓 신호가 된다.
    그래서 run_prompt 가 None 이면 반드시 빨강이어야 한다.
    """
    orig_find, orig_probe, orig_run = (_FakeCallClaude.find_cli, _FakeCallClaude.probe,
                                       _FakeCallClaude.run_prompt)
    try:
        # ① 정상 — 실호출 성공 → 초록 + 모델·버전 표기
        res = t_ai.check_status()
        assert res["ok"] is True, res
        assert res["model"] == "claude-sonnet-5" and "9.9.9-fake" in res["detail"], res
        # ② CLI 없음 → 빨강, 사유가 경로를 지목
        _FakeCallClaude.find_cli = staticmethod(lambda env=None: None)
        res = t_ai.check_status()
        assert res["ok"] is False and "찾지 못" in res["detail"], res
        # ③ **바이너리는 있는데 호출 실패**(인증·정책) → 빨강 (거짓 초록 방지의 핵심)
        _FakeCallClaude.find_cli = orig_find
        _FakeCallClaude.run_prompt = staticmethod(
            lambda prompt, **kw: (kw.get("log") or (lambda m: None))("call_claude exit: rc=1")
            or None)
        res = t_ai.check_status()
        assert res["ok"] is False and "인증" in res["detail"], res
        assert "rc=1" in res["detail"], "실패 사유(cli 로그)가 안 실렸다"
        # ④ probe 실패 → 빨강
        _FakeCallClaude.run_prompt = orig_run
        _FakeCallClaude.probe = staticmethod(
            lambda **kw: {"ok": False, "error": "version_check_failed", "version": ""})
        res = t_ai.check_status()
        assert res["ok"] is False and "실행 확인 실패" in res["detail"], res
    finally:
        _FakeCallClaude.find_cli, _FakeCallClaude.probe, _FakeCallClaude.run_prompt = (
            orig_find, orig_probe, orig_run)
    print("  (k) check_status 신호등 4분기(정상/CLI없음/호출실패/probe실패) OK")


def main():
    test_store_roundtrip()
    test_ai_model_validation()
    _setup(SID, AKEY, AI_OPTS)
    _setup(SID_DEF, AKEY_DEF, AI_OPTS_DEFAULT)
    test_route_guards()
    prompt_item = test_prompts_flow()
    test_push_and_merge(prompt_item)
    test_denied_lines(prompt_item)
    test_precedents_payload_and_route()
    test_full_payload_merged()
    # 두 블록 push 는 store 의 suggestion 을 갈아치우므로 **마지막**에 둔다 — 앞 테스트가
    # "클로드 제안 1" 상태를 기대한다. 이후 (g)(h) 는 각자 원하는 상태를 다시 만든다.
    test_two_block_push(prompt_item)
    test_rebuild_survival()
    test_sources_no_precedent_is_rule()
    test_sibling_isolation()
    test_sha_drift_keeps_suggestion()
    t_ai = test_worker_simulation()
    test_worker_parallel_and_incremental_push(t_ai)
    test_worker_failure_reports(t_ai)
    test_check_status(t_ai)
    test_http_hint(t_ai)
    print("test_ai_suggest: 전부 통과")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
