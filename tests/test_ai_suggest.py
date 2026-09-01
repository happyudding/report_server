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
  (e) POST suggestions — sha 불일치 skip / 일치 accepted + payload_rev bump
  (f) /full payload 에 병합된 [제안] 반영 (rev 채널 재사용 확인)
  (g) 재빌드 생존 — 캐시 전부 비워도 store 재병합으로 suggestion 유지
  (h) 룰 변경 모사 — 프롬프트 sha 가 갈리면 자동 폴백(병합 안 됨)
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
_CELL = "[MAJOR] [현상] 스텁 현상\n[과거사례] 스텁 사례 \n [제안] 기본조치"


def _stub_prompt():
    return f"스텁 프롬프트 {_PROMPT_SALT['v']} — {ITEM}"


_PRECEDENTS = {"n": 1}   # 이 item 의 프롬프트에 선례가 실렸나 (금지 문구 게이트 재료)


def _stub_safe_build_ex(tables, session, selected_items=None, fail_only=None,
                        generate_comment=True):
    prompt = _stub_prompt()
    result = {
        "comments": {f"Yield|5|{ITEM}": _CELL, f"CPK|{ITEM}": _CELL, f"ETC|{ITEM}": _CELL},
        "etc_auto_items": [], "row_signatures": {}, "signature_options": [],
        "prompts": {ITEM: {"prompt": prompt, "sha": wr_ai_prompt.prompt_sha(prompt),
                           "precedents": _PRECEDENTS["n"]}},
    }
    if not generate_comment:
        result = dict(result, comments={}, prompts={})
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
    # sha 불일치 → skip
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": "0" * 12, "suggestion": "무시"}]})
    body = r.get_json()
    assert r.status_code == 200 and body["accepted"] == 0 and body["skipped"] == 1, body
    # 사유별 내역(2026-09-01) — 합계만으로는 "룰이 바뀐 것"과 "모델이 이상한 것"을
    # 구분할 수 없어 관리자가 다음에 뭘 할지 정하지 못한다.
    assert body["skips"]["sha_mismatch"] == 1, body
    assert report_db.get_webreport_edit_rev(SID) == rev0   # 수용 0건 = rev 불변
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
    print("  (e) push sha 게이트·rev bump·감사 action 분리 OK")


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
    # store 에는 직전(정상) 문장이 그대로 남아야 한다 — 폐기가 덮어쓰기가 되면 안 된다
    session = report_db.get_session(SID)
    coords = wr_service._ai_suggest_coords(session, SID, report_db=report_db)
    stored = store.load(UPLOAD_ROOT, coords[0], coords[1], coords[2], prep_digest=coords[3])
    assert stored[ITEM]["suggestion"].startswith("- 클로드 제안 1"), stored[ITEM]

    # ② 혼합 — 나쁜 줄만 빠지고 나머지는 저장된다
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": prompt_item["sha"],
                                     "suggestion": deny_only + "\n- edge 링 오염 이력을 확인하라."}]})
    body = r.get_json()
    assert body["accepted"] == 1 and not any(body["skips"].values()), body
    stored = store.load(UPLOAD_ROOT, coords[0], coords[1], coords[2], prep_digest=coords[3])
    assert stored[ITEM]["suggestion"] == "- edge 링 오염 이력을 확인하라.", stored[ITEM]
    assert "확인되지 않았습니다" in stored[ITEM]["raw"], "원문(raw) 이 안 남았다"

    # ③ 선례 0건 item — 같은 문장이 통과해야 한다(사실을 지우면 왜곡)
    _PRECEDENTS["n"] = 0
    _wipe_caches(AKEY)
    item2 = _get_prompts(SID, wait_200=True).get_json()["items"][0]
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": item2["sha"],
                                     "suggestion": deny_only}]})
    body = r.get_json()
    assert body["accepted"] == 1 and body["skips"]["denied"] == 0, body
    _PRECEDENTS["n"] = 1
    _wipe_caches(AKEY)
    # 뒤 테스트(재빌드 생존)가 기대하는 상태로 되돌린다
    item3 = _get_prompts(SID, wait_200=True).get_json()["items"][0]
    r = client.post(url, headers=_headers(),
                    json={"items": [{"key": ITEM, "sha": item3["sha"],
                                     "suggestion": "- 클로드 제안 1\n- 클로드 제안 2"}]})
    assert r.get_json()["accepted"] == 1
    print("  (m) 금지 문구 폐기/부분제거/선례0건 통과 OK")


def test_full_payload_merged():
    text = _full_text(SID, must_contain="클로드 제안 1")
    assert "클로드 제안 1" in text, "payload 에 병합된 [제안] 이 없다"
    assert "[현상] 스텁 현상" in text          # 앞 섹션 보존
    print("  (f) /full payload 반영 OK")


def _wipe_caches(akey):
    wr_cache.invalidate_caches(akey)
    shutil.rmtree(UPLOAD_ROOT / "web_report" / akey / "cache", ignore_errors=True)


def test_rebuild_survival():
    _wipe_caches(AKEY)
    session = report_db.get_session(SID)
    tables_sess, tables, manifest = wr_service._load_tables(
        SID, report_db=report_db, upload_root=UPLOAD_ROOT, session=session)
    result, how = wr_service._ai_comment_cached(
        tables_sess, SID, tables, manifest, report_db=report_db, upload_root=UPLOAD_ROOT)
    assert how == "build", how
    assert result["comments"][f"CPK|{ITEM}"].endswith("클로드 제안 2"), \
        "재빌드에서 store 재병합이 안 됐다"
    print("  (g) 재빌드 생존(재병합) OK")


def test_sha_drift_fallback():
    _PROMPT_SALT["v"] = "p2-rules-changed"   # 룰 변경 모사 — 프롬프트가 달라진다
    _wipe_caches(AKEY)
    session = report_db.get_session(SID)
    _s, tables, manifest = wr_service._load_tables(
        SID, report_db=report_db, upload_root=UPLOAD_ROOT, session=session)
    result, how = wr_service._ai_comment_cached(
        _s, SID, tables, manifest, report_db=report_db, upload_root=UPLOAD_ROOT)
    assert how == "build"
    assert result["comments"][f"CPK|{ITEM}"].endswith("기본조치"), \
        "sha 가 갈렸는데 옛 suggestion 이 붙었다(게이트 실패)"
    _PROMPT_SALT["v"] = "p1"                 # 원복
    print("  (h) 룰 변경 모사 sha 폴백 OK")


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
    # push 결과가 store 와 payload 에 반영됐는지
    session = report_db.get_session(SID_W)
    coords = wr_service._ai_suggest_coords(session, SID_W, report_db=report_db)
    stored = store.load(UPLOAD_ROOT, *coords[:3], prep_digest=coords[3]) \
        if coords[3] else store.load(UPLOAD_ROOT, coords[0], coords[1], coords[2])
    assert ITEM in stored and stored[ITEM]["suggestion"].startswith("- 워커 생성 제안")
    text = _full_text(SID_W, must_contain="워커 생성 제안")
    assert "워커 생성 제안" in text
    print("  (i) 클라 워커 시뮬레이션(폴링→생성→push→반영) OK")
    return t_ai


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
    test_full_payload_merged()
    test_rebuild_survival()
    test_sha_drift_fallback()
    t_ai = test_worker_simulation()
    test_worker_failure_reports(t_ai)
    test_check_status(t_ai)
    test_http_hint(t_ai)
    print("test_ai_suggest: 전부 통과")
    shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
