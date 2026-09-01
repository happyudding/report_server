# -*- coding: utf-8 -*-
"""web_report/ai_prompt.py — 프롬프트 재구성 결정성·vendor copy 드리프트 감지.

실행:
    server\\.venv\\Scripts\\python.exe tests/test_ai_prompt_determinism.py

프롬프트는 엔진이 아니라 서버가 case dict 로 조립한다 — 규칙 #8(eval_engine import 는
ai_comment/eval_export/eval_debug 3곳 고정)이라 ai_prompt.py 는 엔진을 부를 수 없다.
이 테스트가 고정하는 것:
  (a) 같은 case → build_prompt/sha 결정성 (sha 게이트의 전제)
  (b) split_comment — 신([제안])/구([점검제안]) 토큰 둘 다 수용, 형식 불일치 None
  (c) action_ko 출처 우선순위 — primary signature 행 > [제안] 섹션 파싱값
  (d) 재료 부족(comment 없음/형식 파괴) → build_prompt None (폴백 유지)
  (e) **지시문 vendor copy 드리프트** — recommend.py `_build_prompt` 의 지시문 리터럴을
      ast 로 추출(임포트 없이)해 ai_prompt._INSTRUCTION 과 문자 단위 일치 확인.
      엔진 쪽 지시문이 바뀌면 여기가 깨진다 → ai_prompt 를 같은 커밋에서 갱신할 것.
  (f) sanitize_suggestion — 제어문자/섹션 토큰/코드펜스 제거, 개행 보존, 상한
  (g) patch_suggestion_text — 접두·앞 2섹션 바이트 보존, 구 토큰 유지, 무매치 원문 반환
  (i) **상세 보강**(2026-08-28, docs/23) — 현재 통계·unit·limit(enrich)과 엔진이 준 선례
      상세가 프롬프트에 실리는지, 통계 없는 선례·옛 계약 선례는 한 줄로 떨어지는지,
      enrich 유/무 둘 다 결정적인지. ai_prompt 는 순수 함수라 DB 없이 검증된다.
  (j) **엔진 선례 계약** — `store.search_precedents`(최신 run 의 raw_metrics/features
      JOIN) + `present._precedent_result` 가 상세를 실어 주는지. 임시 sqlite eval DB.
      이게 깨지면 프롬프트 선례가 조용히 한 줄로 되돌아간다.
  (k) **운영자 지시문·금지 문구**(2026-09-02, `/pe/eval` AI 지시문 탭) — rules 인자가
      프롬프트에 붙는 위치·결정성, `precedents` 건수, `strip_denied_lines` 가 사례를
      버리는 줄만 지우고 비교문("사례와 달리")은 남기는지. rules 없으면 종전 바이트 동일.

pytest 미사용 (tests/ 관례 — 자체 실행 + assert). 서버 불필요. (j)만 임시 sqlite 를 만든다.
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from web_report import ai_prompt as P                                  # noqa: E402

_CASE = {
    "item_canonical": "VDD_LEAK",
    "item_raw": "VDD Leak(A)",
    "item_class": "ANALOG|CURR",
    "status": "MAJOR",
    "primary_signature": "EDGE_FAIL",
    "secondary_signatures": ["LOW_CPK"],
    "comment": "[현상] 웨이퍼 edge 집중 fail 입니다.\n[과거사례] P1 에서  유사 사례가 "
               "확인 되었습니다 - edge ring 오염 조치. \n [제안] edge 영역 공정 이력을 확인하세요.",
    "signatures": [
        {"id": "EDGE_FAIL", "role": "primary", "action_ko": "edge 영역 공정 이력을 확인하세요."},
        {"id": "LOW_CPK", "role": "secondary", "action_ko": "산포 개선 여부를 확인하세요."},
    ],
    "precedents": [
        {"comment": "edge ring 오염 조치", "product_name": "P1", "action": "", "result": ""},
        {"comment": None, "product_name": "P2"},
    ],
}


def test_determinism():
    p1 = P.build_prompt(_CASE)
    p2 = P.build_prompt(dict(_CASE))
    assert p1 is not None and p1 == p2
    assert P.prompt_sha(p1) == P.prompt_sha(p2)
    assert len(P.prompt_sha(p1)) == 12
    # 재료가 프롬프트에 실제로 들어갔는지
    assert "VDD_LEAK" in p1 and "EDGE_FAIL" in p1 and "LOW_CPK" in p1
    assert "[현상] 웨이퍼 edge 집중 fail 입니다." in p1
    assert "- P1: edge ring 오염 조치" in p1
    assert "참고용 기본 조치(action_ko):edge 영역 공정 이력을 확인하세요." in p1
    # 이 프로젝트 확장 지시문도 함께 나간다 (base 지시문 뒤)
    assert P._INSTRUCTION in p1 and P._INSTRUCTION_EXTRA in p1
    assert p1.index(P._INSTRUCTION) < p1.index(P._INSTRUCTION_EXTRA)
    print("  (a) 결정성·재료 포함 OK")


def test_split_comment():
    new = P.split_comment(_CASE["comment"])
    assert new == ("웨이퍼 edge 집중 fail 입니다.",
                   "P1 에서  유사 사례가 확인 되었습니다 - edge ring 오염 조치.",
                   "edge 영역 공정 이력을 확인하세요.")
    old = P.split_comment("[현상] A\n[과거사례] B \n [점검제안] C")
    assert old == ("A", "B", "C")
    assert P.split_comment("섹션 없는 문자열") is None
    assert P.split_comment(None) is None
    print("  (b) 신/구 토큰·형식 불일치 OK")


def test_action_ko_priority():
    # primary 행 action_ko 가 [제안] 섹션과 다르면 primary 행이 이긴다
    case = dict(_CASE)
    case["signatures"] = [{"id": "EDGE_FAIL", "role": "primary",
                           "action_ko": "PRIMARY 조치"}]
    p = P.build_prompt(case)
    assert "참고용 기본 조치(action_ko):PRIMARY 조치" in p
    # primary 행에 action_ko 가 없으면 [제안] 파싱값 폴백
    case["signatures"] = [{"id": "EDGE_FAIL", "role": "primary", "action_ko": None}]
    p = P.build_prompt(case)
    assert "참고용 기본 조치(action_ko):edge 영역 공정 이력을 확인하세요." in p
    print("  (c) action_ko 우선순위 OK")


def test_missing_materials():
    case = dict(_CASE)
    case["comment"] = None
    assert P.build_prompt(case) is None
    case["comment"] = "형식이 깨진 코멘트"
    assert P.build_prompt(case) is None
    # comment 형식은 맞는데 signatures 도 없고 [제안] 도 빈 경우
    case2 = dict(_CASE)
    case2["signatures"] = []
    case2["comment"] = "[현상] A\n[과거사례] B \n [제안] "
    assert P.build_prompt(case2) is None
    # build_prompts 는 실패 item 을 조용히 뺀다
    out = P.build_prompts({"ok": _CASE, "bad": case})
    assert set(out) == {"ok"} and out["ok"]["sha"]
    print("  (d) 재료 부족 → None·건너뜀 OK")


def _engine_instruction():
    """recommend.py `_build_prompt` 의 lines 첫 원소(암시적 연결 → Constant)를 ast 로 추출."""
    src = (_ROOT / "eval_analyzer" / "eval_engine" / "pipeline" / "recommend.py"
           ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_prompt":
            for stmt in ast.walk(node):
                if (isinstance(stmt, ast.Assign)
                        and any(getattr(t, "id", "") == "lines" for t in stmt.targets)
                        and isinstance(stmt.value, ast.List)):
                    first = stmt.value.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        return first.value
    return None


def test_instruction_vendor_copy():
    engine = _engine_instruction()
    assert engine is not None, "recommend.py 에서 지시문을 찾지 못함 — 구조가 바뀌었나?"
    assert engine == P._INSTRUCTION, (
        "지시문 vendor copy 드리프트 — recommend.py 의 지시문이 바뀌었다. "
        "web_report/ai_prompt.py _INSTRUCTION 을 같은 값으로 갱신할 것.\n"
        f"engine={engine!r}\ncopy={P._INSTRUCTION!r}")
    print("  (e) 지시문 vendor copy 일치 OK")


def test_sanitize():
    s = P.sanitize_suggestion("```\n- 줄1\r\n- 줄2 [제안] 토큰\x07제거\n```")
    assert s == "- 줄1\n- 줄2  토큰제거", repr(s)
    assert P.sanitize_suggestion("x" * 5000) == "x" * P.MAX_SUGGESTION_CHARS
    assert P.sanitize_suggestion(None) == ""
    assert P.sanitize_suggestion("[점검제안][현상][과거사례]") == ""
    print("  (f) sanitize OK")


def test_patch():
    cell = "[MAJOR][이봉] [현상] A\n[과거사례] B \n [제안] 기존 조치"
    out = P.patch_suggestion_text(cell, "- 새 제안\n- 두 번째")
    assert out == "[MAJOR][이봉] [현상] A\n[과거사례] B \n [제안] - 새 제안\n- 두 번째"
    # 앞부분 바이트 보존
    assert out[:out.index("[제안]")] == cell[:cell.index("[제안]")]
    # 구 토큰 캐시 — 토큰은 원문 것을 유지
    old = "[MINOR] [현상] A\n[과거사례] B \n [점검제안] 기존"
    out2 = P.patch_suggestion_text(old, "새것")
    assert out2.endswith("[점검제안] 새것") and "[제안]" not in out2
    # 무매치 → 원문 그대로
    assert P.patch_suggestion_text("섹션 없음", "새것") == "섹션 없음"
    assert P.patch_suggestion_text(None, "새것") is None
    print("  (g) patch 접두·토큰 보존 OK")


def test_apply_suggestions():
    result = {
        "comments": {"Yield|5|VDD Leak(A)": "[MAJOR] [현상] A\n[과거사례] B \n [제안] 기존",
                     "CPK|VDD Leak(A)": "[MAJOR] [현상] A\n[과거사례] B \n [제안] 기존",
                     "ETC|Other": "[MINOR] [현상] C\n[과거사례] D \n [제안] 그대로"},
        "prompts": {"VDD Leak(A)": {"prompt": "p", "sha": "abcdef012345"}},
        "etc_auto_items": [],
    }
    stored = {"VDD Leak(A)": {"sha": "abcdef012345", "suggestion": "새 제안"},
              "Other": {"sha": "ffffffffffff", "suggestion": "sha 불일치 — 무시"}}
    out, patched = P.apply_suggestions(result, stored)
    assert patched == 2
    assert out["comments"]["Yield|5|VDD Leak(A)"].endswith("[제안] 새 제안")
    assert out["comments"]["CPK|VDD Leak(A)"].endswith("[제안] 새 제안")
    assert out["comments"]["ETC|Other"].endswith("그대로")       # sha 게이트 차단
    assert result["comments"]["CPK|VDD Leak(A)"].endswith("기존")  # 원본 불변(copy 계약)
    assert out is not result and out["comments"] is not result["comments"]
    # sha 전부 불일치 → 원본 그대로(0건)
    out2, patched2 = P.apply_suggestions(result, {"VDD Leak(A)": {"sha": "000000000000",
                                                                  "suggestion": "x"}})
    assert patched2 == 0 and out2 is result
    print("  (h) apply_suggestions sha 게이트·copy 계약 OK")


_ENRICH = {
    "unit": "V", "lsl": 0.9, "usl": 1.1,
    "stats": {"cpk": 0.812345, "mean": 1.0000123, "stdev": 0.04,
              "yield": 0.9721, "fail_count": 12, "total_count": 3000},
}

# 엔진이 주는 선례 1건 (present._precedent_result 계약)
_PRECEDENT = {
    "comment": "edge ring 오염 조치", "product_name": "P1",
    "action": None, "result": None, "family_product": "PMIC_ETC",
    "case_id": "C1", "lot_id": "L123", "item_canonical": "VDD_LEAK", "bin": 5,
    "unit": "V", "value_type": "V", "status": "MAJOR", "signature": "EDGE_FAIL",
    "similarity": 0.93,
    "metrics": {"cpk": 0.62, "mean": 1.05, "yield": 0.981, "fail_count": 7},
    "features": {"edge_fail_ratio": 0.81, "tail_mass_3s": 0.0234},
}


def _case_with_precedent(precedents):
    case = dict(_CASE)
    case["signatures"] = [
        {"id": "EDGE_FAIL", "role": "primary", "action_ko": "edge 영역 공정 이력을 확인하세요.",
         "evidence": [{"signal_code": "edge_fail_share", "value": 0.7512},
                      {"signal_code": "n_dut", "value": None}]},
        {"id": "LOW_CPK", "role": "secondary", "action_ko": "산포 개선 여부를 확인하세요."},
    ]
    case["precedents"] = precedents
    return case


def test_enrich_prompt():
    """(i) 보강 — 현재 통계·unit·evidence 근거 + 엔진이 준 선례 상세가 실린다."""
    case = _case_with_precedent([_PRECEDENT])
    p = P.build_prompt(case, _ENRICH)
    # 결정성 — 같은 입력이면 같은 프롬프트/sha (dict 순서에 안 기댄다)
    assert p == P.build_prompt(dict(case), dict(_ENRICH))
    # 현재 쪽: unit/limit + 통계 + evidence 근거
    assert "unit: V" in p and "LSL=0.9" in p and "USL=1.1" in p
    # 유효숫자 6자리 — 미세 차이가 뭉개지면 과거/현재 대비가 "같은 값"으로 보인다
    assert "[현재 통계] cpk=0.812345, mean=1.00001, stdev=0.04, yield=0.9721" in p
    assert "fail_count=12" in p and "total_count=3000" in p
    assert "[근거: edge_fail_share=0.7512]" in p        # value=None 인 n_dut 는 빠진다
    # 과거 쪽: 식별 + 당시 판정 + 당시 수치 + 정답지 원문
    assert "- 사례1 / 제품 P1 / lot L123 / item VDD_LEAK / unit V / bin 5" in p
    assert "당시 status MAJOR / 당시 signature EDGE_FAIL" in p
    assert "  당시 통계: cpk=0.62, mean=1.05, yield=0.981, fail_count=7" in p
    assert "  당시 분포/공간: edge_fail_ratio=0.81, tail_mass_3s=0.0234" in p
    assert "  당시 판단·조치(원문): edge ring 오염 조치" in p
    # value_type 은 unit 과 같으면 생략, 다르면 실린다
    assert "unit V / bin 5" in p and "type V" not in p
    p_code = P.build_prompt(_case_with_precedent([dict(_PRECEDENT, value_type="CODE")]),
                            _ENRICH)
    assert "unit V / type CODE / bin 5" in p_code
    # 통계가 없는 선례(CSV 적재분)도 식별·코멘트는 나온다
    lean = {k: v for k, v in _PRECEDENT.items() if k not in ("metrics", "features")}
    p_lean = P.build_prompt(_case_with_precedent([lean]), _ENRICH)
    assert "- 사례1 / 제품 P1 / lot L123" in p_lean
    assert "당시 통계:" not in p_lean and "당시 판단·조치(원문):" in p_lean
    # 옛 계약(comment/product_name 만) → 종전 한 줄 형태
    p_old = P.build_prompt(_case_with_precedent(
        [{"comment": "다른 조치", "product_name": "P9"}]), _ENRICH)
    assert "- P9: 다른 조치" in p_old and "사례1" not in p_old
    # enrich 없이도 그대로 동작한다(현재 통계 줄만 없음)
    p3 = P.build_prompt(case)
    assert p3 is not None and "[현재 통계]" not in p3 and "당시 통계: cpk=0.62" in p3
    # build_prompts 는 item 키로 enrich 를 찾는다
    out = P.build_prompts({"VDD Leak(A)": case}, {"VDD Leak(A)": _ENRICH})
    assert out["VDD Leak(A)"]["prompt"] == p
    assert out["VDD Leak(A)"]["sha"] == P.prompt_sha(p)
    print("  (i) 보강(현재 통계·선례 상세·evidence) OK")


def test_engine_precedent_contract():
    """(j) 엔진 계약 — to_result 가 선례 상세를 실어 주는지 (우회 제거의 전제).

    `store.search_precedents` 가 최신 run 의 raw_metrics/features 를 JOIN 하고
    `present._precedent_result` 가 그것을 계약 dict 에 담는다. 이게 깨지면 프롬프트의
    선례가 조용히 한 줄로 되돌아간다(에러가 아니라 "상세가 사라짐"으로 나타난다).
    """
    sys.path.insert(0, str(_ROOT / "eval_analyzer"))
    from eval_engine import store                                     # noqa: E402
    from eval_engine.pipeline import present                          # noqa: E402

    tmp = Path(tempfile.mkdtemp(prefix="evalprec_"))
    db = tmp / "eval.db"
    conn = None
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        conn.executescript(store.SCHEMA)
        now = 1
        conn.execute("INSERT INTO product_master (product_name, product_type,"
                     " family_product, updated_at) VALUES ('P1','PMIC','PMIC_ETC',?)", (now,))
        conn.execute("INSERT INTO item_master (item_id, item_name_raw, item_canonical,"
                     " category_major, value_type, unit) VALUES"
                     " (1,'VDD Leak(A)','vdd_leak','ANALOG','V','V')")
        conn.execute("INSERT INTO fail_case (case_id, product_name, lot_id, wafer_number,"
                     " item_id, bin, revision, item_class, created_at)"
                     " VALUES ('C1','P1','L123',1,1,5,1.0,'ANALOG|V',?)", (now,))
        conn.execute("INSERT INTO label (label_id, case_id, human_comment, created_at)"
                     " VALUES (1,'C1','edge ring 오염 조치',?)", (now,))
        conn.execute("INSERT INTO evaluation (eval_id, case_id, run_id, engine_version,"
                     " status, created_at) VALUES (10,'C1',1,'v1','MAJOR',?)", (now,))
        conn.execute("INSERT INTO case_signature (eval_id, signature, role)"
                     " VALUES (10,'EDGE_FAIL','primary')")
        # run 2건 — **최신(run_id 큰 쪽)** 이 실려야 한다
        for run_id, cpk in ((1, 9.99), (2, 0.62)):
            conn.execute("INSERT INTO raw_metrics (case_id, run_id, cpk, mean, yield,"
                         " fail_count, total_count, created_at)"
                         " VALUES ('C1',?,?,1.05,0.981,7,3000,?)", (run_id, cpk, now))
        conn.execute("INSERT INTO features (case_id, run_id, engine_version, computed_at,"
                     " edge_fail_ratio, tail_mass_3s) VALUES ('C1',2,'v1',?,0.81,0.0234)",
                     (now,))
        conn.commit()

        rows = store.search_precedents("V", "vdd_leak", conn=conn)
        assert len(rows) == 1, rows
        got = present._precedent_result(rows[0])
        # 종전 5키는 이름·의미 불변 (기존 소비자 하위호환)
        assert got["comment"] == "edge ring 오염 조치" and got["product_name"] == "P1"
        assert got["family_product"] == "PMIC_ETC"
        assert "action" in got and "result" in got
        # 추가된 식별·판정
        assert got["lot_id"] == "L123" and got["item_canonical"] == "vdd_leak"
        assert got["unit"] == "V" and got["bin"] == 5
        assert got["status"] == "MAJOR" and got["signature"] == "EDGE_FAIL"
        # 당시 수치 — 최신 run(run_id=2)의 cpk 여야 한다
        assert got["metrics"]["cpk"] == 0.62, got["metrics"]
        assert got["metrics"]["fail_count"] == 7
        assert got["features"] == {"edge_fail_ratio": 0.81, "tail_mass_3s": 0.0234}
        # 프롬프트까지 물린다
        p = P.build_prompt(_case_with_precedent([got]), _ENRICH)
        assert "당시 통계: cpk=0.62" in p and "lot L123" in p
    finally:
        if conn is not None:
            conn.close()
        try:
            os.remove(db)
        except OSError:
            pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    print("  (j) 엔진 선례 계약(to_result 상세 동반) OK")


_RULES = {
    "instructions": [
        {"id": "keep_precedents", "enabled": True, "text": "사례를 버리지 마라."},
        {"id": "off_rule", "enabled": False, "text": "꺼진 지시문 — 나가면 안 된다."},
        {"id": "empty_rule", "enabled": True, "text": "   "},
        {"id": "no_distortion", "enabled": True, "text": "사실을 왜곡하지 마라."},
    ],
    "deny_patterns": [
        # ⚠ **배포 yaml 의 실제 패턴**을 읽어 쓴다 — 손으로 베낀 사본을 쓰면 배포 패턴이
        # 잘못돼도 테스트가 통과한다(실제로 "사례는 **직접** 적용할 수 없습니다" 를 놓치던
        # 초안이 이 방식으로 잡혔다).
        {"id": "precedent_denial", "enabled": True, "only_with_precedents": True,
         "regex": None, "note": ""},        # main() 에서 배포값으로 채운다
        {"id": "off_pattern", "enabled": False, "only_with_precedents": False,
         "regex": "절대", "note": ""},
        {"id": "broken", "enabled": True, "only_with_precedents": False,
         "regex": "([", "note": "컴파일 실패 — 건너뛴다"},
    ],
}


def _load_shipped_deny_regex() -> str:
    """rules/ai_prompt.yaml 의 precedent_denial 정규식(배포값)."""
    import yaml
    path = _ROOT / "eval_analyzer" / "eval_engine" / "rules" / "ai_prompt.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for row in doc.get("deny_patterns") or []:
        if row.get("id") == "precedent_denial":
            return str(row.get("regex") or "")
    raise AssertionError("배포 yaml 에 precedent_denial 패턴이 없습니다")


def test_operator_rules():
    """(k) 운영자 지시문 — 프롬프트 위치·결정성 + rules 없으면 종전 바이트 동일."""
    base = P.build_prompt(_CASE)
    with_rules = P.build_prompt(_CASE, None, _RULES)
    assert with_rules != base
    assert with_rules == P.build_prompt(dict(_CASE), None, dict(_RULES))   # 결정성
    # enabled 만, 선언 순서 유지, 빈 text 제외
    assert "사례를 버리지 마라." in with_rules and "사실을 왜곡하지 마라." in with_rules
    assert "꺼진 지시문" not in with_rules
    assert P.instruction_lines(_RULES) == ["사례를 버리지 마라.", "사실을 왜곡하지 마라."]
    # 위치: 고정 지시문 **뒤**, 재료(item:) **앞** — 재료 뒤에 오면 지시로 안 읽힌다
    assert (with_rules.index(P._INSTRUCTION_EXTRA)
            < with_rules.index("사례를 버리지 마라.")
            < with_rules.index("item: VDD_LEAK"))
    # rules 가 없거나 비면 종전 프롬프트와 **바이트 동일**(기존 캐시 sha 불변)
    assert P.build_prompt(_CASE, None, None) == base
    assert P.build_prompt(_CASE, None, {}) == base
    assert P.build_prompt(_CASE, None, {"instructions": []}) == base
    print("  (k1) 운영자 지시문 위치·결정성·무설정 바이트 동일 OK")


def test_precedent_count():
    """(k) prompts 의 `precedents` — 프롬프트에 실제로 실린 선례 수와 같아야 한다."""
    # _CASE 는 comment 있는 선례 1건 + comment 없는 1건 → 1
    out = P.build_prompts({"VDD Leak(A)": _CASE})
    assert out["VDD Leak(A)"]["precedents"] == 1, out
    assert P._precedent_count(_CASE) == 1
    none_case = dict(_CASE, precedents=[{"comment": None, "product_name": "P2"}])
    assert P.build_prompts({"X": none_case})["X"]["precedents"] == 0
    two = dict(_CASE, precedents=[{"comment": "a"}, {"comment": "b"}])
    assert P.build_prompts({"X": two})["X"]["precedents"] == 2
    # sha 는 프롬프트만의 함수다 — precedents 키가 붙어도 값이 흔들리면 안 된다
    assert out["VDD Leak(A)"]["sha"] == P.prompt_sha(P.build_prompt(_CASE))
    print("  (k2) prompts.precedents 건수 OK")


def test_strip_denied_lines():
    """(k) 금지 문구 — 사례를 버리는 줄만 지우고 비교문은 남긴다."""
    pat = P.compile_deny_patterns(_RULES)
    assert [p[0] for p in pat] == ["precedent_denial"], pat   # 꺼짐·컴파일실패 제외
    # 양성 — 사용자 신고 원문 + 흔한 변형(조사·부사 삽입·어순)
    for bad in ("- 검색된 과거 사례 중 현재 현상에 직접 적용할 수 있는 사례는 확인되지 않았습니다.",
                "- 참고할 사례가 없습니다.",
                "- 유사 사례 없음",
                "- 주어진 사례는 직접 적용할 수 없습니다.",
                "- 사례는 적용하기 어렵습니다.",
                "- 사례가 그대로 적용되지 않습니다.",
                "- 관련 사례를 찾을 수 없습니다."):
        assert P.strip_denied_lines(bad, pat, True) == "", bad
    # 음성 — 사례를 **활용하는** 문장은 남아야 한다(지우면 안 되는 쪽)
    for ok in ("- 과거 사례와 달리 이번에는 edge 편중이 확인되지 않으므로 중심부를 보라.",
               "- 사례에서 확인되지 않은 항목은 별도로 점검하라.",
               "- 사례의 조치를 참고해 edge 링 오염 이력을 확인하라.",
               "- 사례는 P1 lot 의 edge 오염 건이며 지금과 cpk 수준이 비슷하다.",
               "- 사례처럼 재측정으로 회복되는지 확인하라."):
        assert P.strip_denied_lines(ok, pat, True) == ok, ok
    # 줄 단위 — 나쁜 줄만 빠지고 나머지는 그대로
    mixed = ("- 적용할 수 있는 사례는 확인되지 않았습니다.\n"
             "- edge 링 오염 이력을 먼저 확인하라.")
    assert P.strip_denied_lines(mixed, pat, True) == "- edge 링 오염 이력을 먼저 확인하라."
    # only_with_precedents — 사례가 0건이면 "사례가 없다"는 사실이므로 지우지 않는다
    assert P.strip_denied_lines("- 참고할 사례가 없습니다.", pat, False) \
        == "- 참고할 사례가 없습니다."
    # 패턴이 없으면 원문 그대로 (필터 미설정 = 종전 동작)
    assert P.strip_denied_lines(mixed, [], True) == mixed
    assert P.strip_denied_lines(None, pat, True) == ""
    print("  (k3) strip_denied_lines 양성/음성/줄단위/선례게이트 OK")


def main():
    _RULES["deny_patterns"][0]["regex"] = _load_shipped_deny_regex()
    test_determinism()
    test_split_comment()
    test_action_ko_priority()
    test_missing_materials()
    test_instruction_vendor_copy()
    test_sanitize()
    test_patch()
    test_apply_suggestions()
    test_enrich_prompt()
    test_engine_precedent_contract()
    test_operator_rules()
    test_precedent_count()
    test_strip_denied_lines()
    print("test_ai_prompt_determinism: 전부 통과")


if __name__ == "__main__":
    main()
