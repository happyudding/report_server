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
  (l) **발화 signature 커버리지 재료**(2026-09-01) — 헤더 건수 == `_sig_lines` 줄 수 ==
      `ai_comment._case_sig_ids` 길이. 셋이 갈리면 화면 Signature 컬럼과 프롬프트가
      어긋난다("N건" 이라 써놓고 목록은 N-1줄).
  (m) **커버리지·문체 지시 배포 확인**(2026-09-01, 2026-09-02 확장) — 배포 yaml 의
      cover_all_signatures / signature_budget_first / no_metric_names / terse_lines 가
      켜져 있고 프롬프트로 나가는지 + `_INSTRUCTION_EXTRA` 의 축소 지시가 무조건형으로
      되돌아가지 않았는지(그러면 커버리지 요구를 눌러 버린다).
  (t) **출력 문체 규칙**(2026-09-02) — 12줄 + signature 당 5줄 상한, 내부 지표명·수치
      출력 금지(CPK 예외), 사례 위주. ⚠ 핵심은 **비대칭**이다: 금지는 출력 문장에만
      걸고 프롬프트 **재료**의 수치([근거]·[현재 통계]·선례 당시 통계)는 그대로 실린다.
      재료까지 빼면 "그때 값 vs 지금 값" 대조가 원리적으로 불가능해진다.

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
    # ⚠ 이 픽스처는 **옛 토큰**([과거사례])을 일부러 유지한다 — 캐시에 굳은 세션이 계속
    # 이 모양으로 오므로, 새 코드가 옛 코멘트도 파싱하는지가 여기서 함께 검증된다.
    # 새 토큰([사례]) 파싱은 test_split_comment / _CASE_NEW 가 본다.
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
    assert "[현상] 웨이퍼 edge 집중 fail 입니다." in p1   # 옛 토큰 코멘트도 파싱된다
    assert "- P1: edge ring 오염 조치" in p1
    # [기본 조치 목록] 은 발화 signature **전부** — LLM 이 통합 제안의 재료로 쓴다.
    assert "[기본 조치 목록(action_ko)]" in p1
    assert "- EDGE_FAIL: edge 영역 공정 이력을 확인하세요." in p1
    assert "- LOW_CPK: 산포 개선 여부를 확인하세요." in p1
    # 사례 목록 헤더도 새 토큰이다(옛 "[과거사례 목록]" 아님).
    assert "[사례 목록]" in p1 and "[과거사례 목록]" not in p1
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
    # 조치 목록은 발화 **전부**이고 primary 가 맨 앞이다(2026-09-02).
    case = dict(_CASE)
    case["signatures"] = [
        {"id": "LOW_CPK", "role": "secondary", "action_ko": "SECOND 조치"},
        {"id": "EDGE_FAIL", "role": "primary", "action_ko": "PRIMARY 조치"}]
    p = P.build_prompt(case)
    assert p.index("- EDGE_FAIL: PRIMARY 조치") < p.index("- LOW_CPK: SECOND 조치")
    # 같은 문장이 여러 룰에 걸리면 **조치 목록에는** 한 번만 (중복은 사용자에게 잡음).
    # [발화 signature 전체] 는 발화 사실 자체가 재료라 그대로 둘 다 남는다.
    case["signatures"] = [{"id": "A", "role": "primary", "action_ko": "같은 조치"},
                          {"id": "B", "role": "secondary", "action_ko": "같은 조치"}]
    p = P.build_prompt(case)
    action_block = p.split("[기본 조치 목록(action_ko)]")[1]
    assert action_block.count("같은 조치") == 1, action_block
    # action_ko 가 하나도 없으면 코멘트의 [제안] 섹션 파싱값으로 폴백한다(목록 형태 아님).
    case["signatures"] = [{"id": "EDGE_FAIL", "role": "primary", "action_ko": None}]
    p = P.build_prompt(case)
    assert p.split("[기본 조치 목록(action_ko)]")[1].strip() \
        == "edge 영역 공정 이력을 확인하세요."
    print("  (c) 조치 목록 전량·primary 우선·중복 제거 OK")


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


def _load_shipped_rules() -> dict:
    """rules/ai_prompt.yaml 배포 원문 — 손으로 베낀 사본을 쓰지 않기 위한 단일 창구."""
    import yaml
    path = _ROOT / "eval_analyzer" / "eval_engine" / "rules" / "ai_prompt.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_shipped_deny_regex() -> str:
    """rules/ai_prompt.yaml 의 precedent_denial 정규식(배포값)."""
    for row in _load_shipped_rules().get("deny_patterns") or []:
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
    # 사례 0건 item 은 **프롬프트를 만들지 않는다**(2026-09-02) — LLM 호출 자체를 건너뛴다.
    none_case = dict(_CASE, precedents=[{"comment": None, "product_name": "P2"}])
    assert P.build_prompt(none_case) is None
    assert P.build_prompts({"X": none_case}) == {}
    assert P.build_prompt(dict(_CASE, precedents=[])) is None
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


def test_shipped_deny_patterns_no_info_and_meta():
    """(k4) 배포 deny 패턴 2종 (2026-09-02 사용자 요청) — 변명·메타 판단 문장 제거.

      · `no_info_excuse` — "제공된 과거 사례는 제품명과 LOT 정보만 존재할 뿐 구체적인 판단
        근거 조치내용이 없어서 활용할 수 없다" 류. 사용자 결정: "차라리 빈칸이 낫다".
      · `meta_judgment` — "대표 사례로 선정하기 어렵다" 류. 사례를 고르는 고민 자체는
        읽는 사람에게 쓸모가 없다.

    지시문(omit_when_no_info / no_meta_judgment)의 **안전망**이라 배포 yaml 원문으로 잰다.
    ⚠ 음성 샘플이 이 테스트의 요점이다 — 두 패턴 다 "사례 + 부정어" 라 조금만 넓게 쓰면
    사례를 **활용하는** 문장("사례와 달리 …", "사례에서 확인되지 않은 항목 …", "유사 사례
    2건 있었음")까지 지운다. 그건 필터가 없는 것보다 나쁘다.
    """
    pat = P.compile_deny_patterns(_load_shipped_rules())
    ids = [p[0] for p in pat]
    assert "no_info_excuse" in ids and "meta_judgment" in ids, ids
    for bad in ("- 제공된 과거 사례는 제품명과 LOT 정보만 존재할 뿐 구체적인 판단 근거 조치내용이 없어서 활용할 수 없다.",
                "- 제공된 사례는 제품명과 lot 정보만 있어 참고하기 어렵습니다.",
                "- 구체적인 판단 근거가 없습니다.",
                "- 구체적인 조치 내용이 확인되지 않습니다.",
                "- 대표 사례로 선정하기 어렵습니다.",
                "- 대표적인 사례를 꼽기 어렵다.",
                "- 어느 사례가 더 적합한지 판단하기 어렵다."):
        assert P.strip_denied_lines(bad, pat, True) == "", bad
    for ok in ("- Retest 로 재현 여부 확인",
               "- P1/L1 사례에서 contact open 으로 판정되어 socket 교체 후 회복",
               "- wait 안정화 후 재측정, 개발팀 협의 필요",
               "- 사례와 달리 이번은 edge 편중",
               "- 사례에서 확인되지 않은 항목은 별도 확인",
               "- 유사 사례 2건 있었음",
               "- 진성 여부 판단을 위해 bin map 확인",
               # 사례 결론이 한마디뿐이어도 이력으로 남긴다 (cite_verdict_history 목표 출력)
               "- 가성으로 판단된 이력이 있음",
               "- 유의차 없음으로 판단된 이력이 있음",
               "- Defective Fail 로 판단된 이력이 있음",
               "- 기존 Bin5 에서 Bin15 로 전이된 이력이 있어 PGM 확인 및 time 최적화 검토 필요",
               "- 강제 0xFF 써져서 Fail 된 이력이 있음, 해당 방향 검토 고려"):
        assert P.strip_denied_lines(ok, pat, True) == ok, ok
    # 선례 게이트를 걸지 않는다 — 선례가 실렸는데 내용이 부실한 경우가 바로 이 문장이
    # 나오는 상황이라, 선례 유무로 게이트하면 정작 잡아야 할 때 안 걸린다.
    assert P.strip_denied_lines("- 대표 사례로 선정하기 어렵습니다.", pat, False) == ""
    print("  (k4) 배포 deny 패턴(변명·메타 판단) 양성/음성 OK")


def test_signature_coverage_materials():
    """(l) 발화 signature 가 전부·건수와 함께 실린다 (2026-09-01).

    증상은 "signature 는 여러 개 걸렸는데 [제안]은 하나만 다룬다" 였다. 재료는 원래
    전량 실렸으므로 이 테스트가 고정하는 것은 **화면 Signature 컬럼과 프롬프트가
    갈리지 않는다**는 쪽이다 — 헤더 건수 == _sig_lines 줄 수 == _case_sig_ids 길이.
    셋이 어긋나면 "3건이라 써놓고 2줄만 있는" 프롬프트가 나가 모델을 혼란시킨다.
    """
    from web_report import ai_comment as AC       # Signature 컬럼의 정본(같은 case 를 읽는다)

    case = _case_with_precedent([_PRECEDENT])     # signatures 2건
    p = P.build_prompt(case, _ENRICH)
    n = P._sig_count(case)
    assert n == 2
    assert len(P._sig_lines(case).split("\n")) == n
    assert len(AC._case_sig_ids(case)) == n, "화면 Signature 컬럼과 프롬프트 기준이 갈렸다"
    assert f"[발화 signature 전체] {n}건 - 아래 {n}개 항목을 모두 다뤄라" in p
    assert "- EDGE_FAIL(primary)" in p and "- LOW_CPK(secondary)" in p

    # id 가 빈 행이 섞여도 건수와 줄 수가 **함께** 줄어야 한다(_sig_lines 와 같은 기준)
    case3 = dict(case)
    case3["signatures"] = list(case["signatures"]) + [{"id": "", "action_ko": "무시"}]
    assert P._sig_count(case3) == 2
    assert len(P._sig_lines(case3).split("\n")) == 2
    assert "[발화 signature 전체] 2건" in P.build_prompt(case3, _ENRICH)

    # 3건이면 헤더도 3건 — 커버리지 요구가 실제 발화 수를 따라간다
    case_n = dict(case)
    case_n["signatures"] = list(case["signatures"]) + [
        {"id": "OUTLIER", "role": "secondary", "action_ko": "튄 값을 확인하세요."}]
    p_n = P.build_prompt(case_n, _ENRICH)
    assert "[발화 signature 전체] 3건 - 아래 3개 항목을 모두 다뤄라" in p_n
    assert "- OUTLIER(secondary)" in p_n

    # 0건이면 종전 헤더 그대로 — 셀 수가 없으니 요구도 하지 않는다
    case0 = dict(_CASE)
    case0["signatures"] = []
    p0 = P.build_prompt(case0)          # action_ko 는 [제안] 섹션 폴백으로 살아 있다
    assert p0 is not None
    assert "[발화 signature 전체]\n- (없음)" in p0 and "모두 다뤄라" not in p0
    print("  (l) 발화 signature 커버리지 재료(건수·목록·화면 일치) OK")


def test_coverage_instruction_shipped():
    """(m) 커버리지·문체 지시가 **배포 yaml** 에 실제로 있고 프롬프트로 나간다.

    2026-09-01 커버리지 2종으로 시작해 2026-09-02 에 문체 2종이 늘었다
    (`no_metric_names` 지표명 금지 · `terse_lines` 간결). 넷 다 화면 문장의 모양을
    직접 정하므로, 꺼지거나 사라지면 사용자가 바로 알아채는 회귀가 된다.
    """
    rules = _load_shipped_rules()
    by_id = {str(r.get("id") or ""): r for r in rules.get("instructions") or []}
    shipped = ("cover_all_signatures", "signature_budget_first",
               "no_metric_names", "terse_lines")
    for rid in shipped:
        assert rid in by_id, f"배포 yaml 에 {rid} 지시문이 없습니다"
        assert by_id[rid].get("enabled") is True, f"{rid} 가 꺼져 있습니다"

    p = P.build_prompt(_CASE, None, rules)
    for rid in shipped:
        text = str(by_id[rid].get("text") or "").strip()
        assert text and text in p, f"{rid} 문장이 프롬프트에 없습니다"
        # 위치: 고정 지시문 **뒤**, 재료(item:) **앞** — 재료 뒤면 지시로 안 읽힌다
        assert (p.index(P._INSTRUCTION_EXTRA) < p.index(text) < p.index("item: VDD_LEAK"))

    # 축소 지시가 **무조건형**으로 되돌아가는 회귀를 잡는다. 대상이 "발화 목록 밖" 으로
    # 한정돼 있지 않으면, 뒤에 있고 더 구체적인 이 문장이 커버리지 요구를 눌러 버린다.
    assert "발화 목록에 없는 항목을 지어내" in P._INSTRUCTION_EXTRA, (
        "_INSTRUCTION_EXTRA 의 축소 지시가 커버리지와 충돌하는 무조건형으로 되돌아갔다 — "
        "대상을 '발화 목록 밖' 으로 한정할 것 (cache_policy v8 사유 참조)")
    assert "발화 signature 를 전부 덮고 나서도" in P._INSTRUCTION_EXTRA
    print("  (m) 배포 yaml 커버리지·문체 지시 + 축소 지시 한정 OK")


def test_output_style_rules():
    """(t) 출력 문체 규칙 (2026-09-02 사용자 결정) — 줄 수·수치 금지·사례 위주.

    ⚠ 이 테스트의 요점은 **금지가 출력에만 걸리고 재료에는 안 걸린다**는 비대칭이다.
    지표 수치를 프롬프트 재료에서까지 빼면 "그때 값 vs 지금 값" 대조가 원리적으로
    불가능해져 사례가 무용지물이 된다 — 되돌림을 여기서 막는다.
    """
    # ① 줄 수: 5줄 → 10줄 → **12줄**(2026-09-02) + signature 당 5줄. 양쪽 사본 모두.
    # 숫자가 세 곳(_INSTRUCTION 사본 2벌 + _INSTRUCTION_EXTRA + yaml)에 흩어져 있어
    # 하나만 고치면 프롬프트가 스스로 모순된 예산을 말한다 — 여기서 함께 고정한다.
    assert "최대 12줄" in P._INSTRUCTION and "5줄을 넘기지 마라" in P._INSTRUCTION
    assert "최대 5줄로 쓰고" not in P._INSTRUCTION, "옛 5줄 상한이 남아 있다"
    assert "최대 10줄" not in P._INSTRUCTION, "옛 10줄 상한이 남아 있다"
    assert "12줄은 상한이지" in P._INSTRUCTION_EXTRA, "EXTRA 의 줄 수 안내가 옛 값이다"
    # 상한 문자 수도 12줄에 맞게 올라가 있어야 한다(잘라내기라 모자라면 문장이 끊긴다)
    assert P.MAX_SUGGESTION_CHARS >= 2160

    # ② 수치·지표명은 **출력**에서 금지 — 지시문이 실제 지표명을 예로 들어야 모델이 안다.
    assert "지표 이름과 그 수치는 쓰지 마라" in P._INSTRUCTION
    assert "FAIL_MAD_MIN" in P._INSTRUCTION and "TAIL_MASS_3S_HIGH" in P._INSTRUCTION
    # CPK 예외(사용자 지정) — 이게 빠지면 유일하게 읽히는 수치까지 사라진다
    assert "CPK" in P._INSTRUCTION and "써도 된다" in P._INSTRUCTION

    # ③ 재료 쪽 수치는 **그대로 실린다**(위 비대칭). 하나라도 빠지면 대조가 죽는다.
    case = _case_with_precedent([_PRECEDENT])
    p = P.build_prompt(case, _ENRICH, _load_shipped_rules())
    assert "[근거: edge_fail_share=0.7512]" in p, "발화 근거 수치가 재료에서 사라졌다"
    assert "[현재 통계] cpk=0.812345" in p, "현재 통계가 재료에서 사라졌다"
    assert "당시 통계: cpk=0.62" in p, "선례 당시 통계가 재료에서 사라졌다"
    assert "당시 분포/공간: edge_fail_ratio=0.81" in p

    # ④ 사례 위주 — action_ko 나열이 아니라 사례가 [제안] 의 중심이라고 말해야 한다.
    assert "사례에서 무엇을 어떻게 확인해 해결했는지를 중심으로" in P._INSTRUCTION
    assert "그대로 옮겨 적지 마라" in P._INSTRUCTION
    # 기본 조치 목록은 재료로는 계속 실린다(사례가 안 덮는 signature 를 메운다)
    assert "[기본 조치 목록(action_ko)]" in p

    # ⑤ 배포 yaml 지시문(2026-09-02) — 프롬프트에 실제로 합류하는지까지 본다.
    #   · 사례가 내린 **결론**을 지금 할 일로 바꿔 쓸 것(기본 조치 문구 복붙·요약 금지)
    #   · 예산이 모자라면 **사례가 없는** signature 줄부터 줄일 것
    #   yaml 은 문장을 접어 저장하므로(줄바꿈+들여쓰기) 프롬프트 문자열에서 공백을
    #   접어 비교한다 — 원문 그대로 찾으면 줄바꿈 위치에 따라 헛되이 깨진다.
    flat = " ".join(p.split())
    assert "사례가 실제로 내린 결론" in flat and "지금 확인할 일로 바꿔 써라" in flat, \
        "integrate_precedents 개정본이 프롬프트에 안 실렸다"
    assert "진성/낙도성 여부" in flat and "wait 안정화" in flat, \
        "사례 결론의 예시 어휘가 빠졌다 — 모델이 무엇을 옮겨 쓸지 모른다"
    assert "사례가 없는 signature 줄부터 줄이고" in flat, \
        "예산 부족 시 줄이는 순서가 프롬프트에 없다"
    assert "전체 12줄" in flat, "yaml 예산이 코드(_INSTRUCTION)와 갈렸다"

    # ⑥ 지시문 3건(2026-09-02 사용자 요청) — 현장 영어 보존 / 메타 판단 금지 / 변명 금지.
    #   셋 다 "무엇을 쓰지 마라" 라 문장이 빠지면 조용히 옛 문체로 돌아간다.
    assert "retest" in flat and "contact" in flat, \
        "keep_english_terms 의 예시 용어가 빠졌다 — 모델이 무엇을 영어로 둘지 모른다"
    assert "한글로 옮기지" in flat, "현장 영어 보존 지시가 프롬프트에 없다"
    assert "대표 사례로 선정하기 어렵다" in flat, \
        "no_meta_judgment(메타 판단 금지)가 프롬프트에 없다"
    assert "변명 문장은 절대 쓰지 마라" in flat, \
        "omit_when_no_info(변명 금지)가 프롬프트에 없다"

    # ⑦ 금지는 **문장의 모양**에만 걸리고 사례 자체는 반드시 인용한다 (2026-09-02 개정).
    #   금지를 넓게 쓴 첫 판(⑥의 옛 문구 "그런 사례가 있었다는 사실만" · "생략하거나 크게
    #   줄여라 … 아예 비워 두는 편이 낫다")은 모델이 **사례 인용 자체를 포기**하게 만들어,
    #   화면에 action_ko 요약만 남는 회귀가 났다. 금지 범위 한정과 인용 의무를 고정한다.
    assert "사례를 빼라는 뜻이 아니다" in flat, \
        "no_meta_judgment 가 다시 사례 자체를 막는 문장으로 돌아갔다"
    assert "반드시 문장에 녹여 써라" in flat, "사례 인용 의무가 프롬프트에서 사라졌다"
    assert "비워 두는 편이 낫다" not in flat, \
        "omit_when_no_info 에 '빈칸이 낫다'(사례 포기 유도)가 되살아났다"
    assert "가성으로 판단된 이력이 있음" in flat, \
        "cite_verdict_history 의 한마디 사례 예시가 빠졌다 — 모델이 형태를 모른다"
    assert "PGM 확인 및 time 최적화 검토 필요" in flat, \
        "cite_verdict_history 의 구체 조치 사례 예시가 빠졌다"
    print("  (t) 출력 문체(12줄·지표명 금지·사례 위주) + 재료 비대칭 OK")


def test_parse_llm_blocks():
    """(n) 두 블록 계약 파서 (2026-09-02) — 관대 수용 3분기 + 엔진 사본 동치."""
    two = "[사례]\n- P1 사례 요약\n[제안]\n- 확인 순서 1\n- 확인 순서 2"
    assert P.parse_llm_blocks(two) == ("- P1 사례 요약", "- 확인 순서 1\n- 확인 순서 2")
    # 옛 토큰도 받는다(모델이 프롬프트 예시를 흉내낼 수 있다)
    assert P.parse_llm_blocks("[과거사례] 요약\n[점검제안] 제안") == ("요약", "제안")
    # [제안] 만 오면 사례 요약은 None — 호출부가 코드 나열을 유지한다
    assert P.parse_llm_blocks("[제안]\n- 하나") == (None, "- 하나")
    # 토큰이 없으면 전체가 제안 (종전 단일 출력 하위호환)
    assert P.parse_llm_blocks("- 그냥 한 줄") == (None, "- 그냥 한 줄")
    assert P.parse_llm_blocks("") == (None, None)
    assert P.parse_llm_blocks(None) == (None, None)

    # ⚠ 엔진 사본과 **같은 동작**이어야 한다 (규칙 #8 로 import 불가 → 사본 유지)
    sys.path.insert(0, str(_ROOT / "eval_analyzer"))
    from eval_engine.pipeline import recommend as R                  # noqa: E402
    for sample in (two, "[과거사례] 요약\n[점검제안] 제안", "[제안]\n- 하나",
                   "- 그냥 한 줄", "", None):
        assert R.parse_llm_blocks(sample) == P.parse_llm_blocks(sample), sample
    print("  (n) parse_llm_blocks 3분기 + 엔진 사본 동치 OK")


def test_patch_cell():
    """(o) 섹션별 교체 — 멱등·한쪽만·토큰 보존 (2026-09-02)."""
    cell = ("[MAJOR][이봉] [현상] - EDGE_FAIL: 현상\n[사례] ①(P1) 원문 사례 \n"
            " [제안] - EDGE_FAIL: 룰 조치")
    out = P.patch_cell(cell, past="요약된 사례", suggestion="- 통합 제안")
    assert "[사례] 요약된 사례" in out and "[제안] - 통합 제안" in out
    assert out.startswith("[MAJOR][이봉] [현상] - EDGE_FAIL: 현상")   # 접두·현상 보존
    assert "원문 사례" not in out and "룰 조치" not in out
    # 멱등 — 재빌드마다 재병합되므로 두 번 적용해도 같아야 한다
    assert P.patch_cell(out, past="요약된 사례", suggestion="- 통합 제안") == out
    # 한쪽만 주면 다른 섹션은 그대로
    only_case = P.patch_cell(cell, past="요약만")
    assert "[사례] 요약만" in only_case and "[제안] - EDGE_FAIL: 룰 조치" in only_case
    only_sugg = P.patch_cell(cell, suggestion="제안만")
    assert "[사례] ①(P1) 원문 사례" in only_sugg and "[제안] 제안만" in only_sugg
    # 옛 토큰 셀도 **토큰을 바꾸지 않고** 값만 갈린다(캐시 세션 호환)
    old = "[MINOR] [현상] A\n[과거사례] 옛 사례 \n [점검제안] 옛 제안"
    o2 = P.patch_cell(old, past="새 요약", suggestion="새 제안")
    assert "[과거사례] 새 요약" in o2 and "[점검제안] 새 제안" in o2
    assert "[사례]" not in o2 and "[제안] " not in o2.replace("[점검제안] ", "")
    # 백슬래시가 섞여도 역참조로 해석되지 않는다(정규식 replacement 함수 계약)
    assert "a\\1b" in P.patch_cell(cell, suggestion="a\\1b")
    # 섹션 토큰이 없으면 원문 그대로
    assert P.patch_cell("섹션 없음", past="x", suggestion="y") == "섹션 없음"
    assert P.patch_cell(None, suggestion="y") is None
    print("  (o) patch_cell 멱등·부분 치환·토큰 보존 OK")


def test_denied_ignores_spacing():
    """(p) 금지 문구는 **띄어쓰기 변형**도 잡는다 — 사용자 신고 문장이 그 한 칸으로 샜다."""
    pat = P.compile_deny_patterns(_RULES)
    for bad in ("- 검색된 과거 사례 중 현재 현상에 직접 적용할 수 있는 사례는 확인 되지 않았습니다.",
                "- 참고할 사례가 없 습니다.",
                "- 사례는 적용 할 수 없습니다."):
        assert P.strip_denied_lines(bad, pat, True) == "", bad
    # 공백을 지워도 비교문은 여전히 통과해야 한다(과잉 차단 방지)
    ok = "- 과거 사례와 달리 이번에는 edge 편중이 확인되지 않으므로 중심부를 보라."
    assert P.strip_denied_lines(ok, pat, True) == ok
    print("  (p) 금지 문구 띄어쓰기 무시 매칭 OK")


def test_no_suggest_option():
    """(s) "제안 제외"(Honey 체크) — [제안] 섹션을 통째로 빼고 사례만 남긴다 (2026-09-02).

    LLM 을 아예 호출하지 않는 옵션이라, 화면에 조치 문장을 남기면 "제안이 만들어지다
    말았다" 로 보인다. ⚠ 섹션 **토큰까지** 제거해야 빈 라벨이 안 뜬다.
    """
    import json as _json
    sys.path.insert(0, str(_ROOT))
    from web_report import ai_comment as A
    from web_report.validation import webreport_ai_no_suggest as _no_sugg

    # 옵션 리더 — 키가 없으면 False(기존 세션 캐시 키 바이트 불변 규약)
    assert _no_sugg(_json.dumps({"ai_comment": True, "ai_comment_optin": True})) is False
    assert _no_sugg(_json.dumps({"ai_no_suggest": True})) is True
    assert _no_sugg("") is False and _no_sugg("{broken") is False

    case = {"status": "MAJOR", "signatures": [], "comment":
            "[현상] - LOW_CPK: CPK 부족\n[사례] ①(P1/L1) 재측정 회복 \n"
            " [제안] - LOW_CPK: spec 재검토"}
    normal = A._cell_text(case)
    assert "[제안] - LOW_CPK: spec 재검토" in normal
    dropped = A._cell_text(case, no_suggest=True)
    assert "[제안]" not in dropped and "spec 재검토" not in dropped, dropped
    assert "[사례] ①(P1/L1) 재측정 회복" in dropped, dropped   # 사례는 남는다
    assert dropped.startswith("[MAJOR] [현상]")                 # 접두·현상 보존
    # 옛 토큰 코멘트도 같은 결과 (캐시에 굳은 세션)
    old = dict(case, comment="[현상] A\n[과거사례] B \n [점검제안] 옛 조치")
    o = A._cell_text(old, no_suggest=True)
    assert "[점검제안]" not in o and "옛 조치" not in o and "[과거사례] B" in o, o
    print("  (s) 제안 제외 — [제안] 섹션 제거·사례 보존 OK")


def test_real_llm_reply_shape():
    """(r) 관리자 화면에서 실제로 관측된 응답 모양 — 토큰이 **줄 맨 앞**, 본문은 다음 줄.

    사용자 확인(2026-09-02): 모델이 이렇게 답한다.
        [사례]
        사례1 …설명…
        (빈 줄)
        [제안]
        - …

    종전 픽스처는 `[사례] 본문` 처럼 **같은 줄**만 봤다 — 토큰 뒤 개행·빈 줄이 섞여도
    섹션이 갈리는지 여기서 고정한다. 하나라도 틀리면 사례 요약이 [제안] 에 섞여 들어간다.
    """
    reply = ("[사례]\n"
             "사례1 P1/L1 - Retest 후 정상 복귀. contact 저항이 원인으로 확인됨.\n"
             "사례2 P2/L2 - trim 재조정으로 개선.\n"
             "\n"
             "[제안]\n"
             "- 사례1처럼 Retest 로 재현성을 먼저 확인한다.\n"
             "- contact 등 환경성 요인을 점검한다.")
    cases, sugg = P.parse_llm_blocks(reply)
    assert cases.startswith("사례1 P1/L1"), cases
    assert "사례2 P2/L2" in cases
    assert "[제안]" not in cases and "Retest 로 재현성" not in cases, \
        f"제안이 사례 블록에 섞였다: {cases!r}"
    assert sugg.startswith("- 사례1처럼"), sugg
    assert "사례1 P1/L1" not in sugg, f"사례 요약이 제안에 섞였다: {sugg!r}"

    # 셀에 박히는 최종 모습 — 두 섹션이 각자 자리에 들어간다
    cell = ("[MAJOR] [현상] - LOW_CPK: CPK 부족\n[사례] ①(P1/L1) 코드 나열 \n"
            " [제안] - LOW_CPK: spec 재검토")
    out = P.patch_cell(cell, past=P.sanitize_suggestion(cases),
                       suggestion=P.sanitize_suggestion(sugg))
    assert "[사례] 사례1 P1/L1" in out and "[제안] - 사례1처럼" in out, out
    assert "코드 나열" not in out and "spec 재검토" not in out, out
    # 지시문이 이 형태를 **예시로** 못 박고 있어야 한다(모델이 형식을 지키는 근거)
    assert "[사례] <사례 요약 문장들>" in P._INSTRUCTION, \
        "출력 형식 예시가 지시문에서 사라졌다 — 모델이 형식을 지킬 근거가 없어진다"
    assert "JSON" in P._INSTRUCTION, "JSON 금지 지시가 사라졌다"
    print("  (r) 실제 관측 응답 모양(토큰 줄바꿈·빈 줄) 파싱 OK")


def test_unwrap_json_reply():
    """(q) 모델이 JSON 객체를 내면 문장만 꺼낸다 — 2026-09-02 현장 신고 재현.

    신고 원문이 그대로 셀에 박혔다:
      {"precedent":{…}, "suggestion":{"text":"Retest…"}, "evidence_refs":["E1"]}
    이 스키마는 우리 코드에 없다(배치 계약은 [{id,text}]) — 모델이 text 안에 자기 구조를
    또 만든 것이고, 파서가 [사례]/[제안] 토큰을 못 찾아 통째로 [제안] 본문이 됐다.
    """
    raw = ('{"precedent" : {"use:":false, "selected_id": null, "relevance":"low",'
           '"summary":null},"suggestion":{"text": "Retest를 통해 이상치 재현성을 '
           '먼저 확인한다."}, "evidence_refs":["E1"]}')
    # sanitize 를 통과하면 문장만 남는다(= 실제 push 경로)
    assert P.sanitize_suggestion(raw) == "Retest를 통해 이상치 재현성을 먼저 확인한다."
    # ```json 코드펜스로 감싼 변형도 같다(펜스 제거 뒤에 unwrap 이 돈다)
    assert P.sanitize_suggestion("```json\n" + raw + "\n```") \
        == "Retest를 통해 이상치 재현성을 먼저 확인한다."
    # 건질 문장이 없으면 "" → 호출부가 skip 하고 룰 문장으로 폴백한다
    assert P.sanitize_suggestion('{"a": "low", "b": null, "c": ["E1"]}') == ""
    # ⚠ 정상 응답은 손대지 않는다(회귀 방지)
    plain = "- edge 이력을 확인하라\n- 산포 재측정"
    assert P.sanitize_suggestion(plain) == plain
    assert P.unwrap_json_reply('{"broken') == '{"broken'      # 깨진 JSON 은 원문 유지
    print("  (q) JSON 응답 → 문장만 추출(신고 재현) OK")


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
    test_shipped_deny_patterns_no_info_and_meta()
    test_signature_coverage_materials()
    test_coverage_instruction_shipped()
    test_output_style_rules()
    test_parse_llm_blocks()
    test_patch_cell()
    test_denied_ignores_spacing()
    test_real_llm_reply_shape()
    test_unwrap_json_reply()
    test_no_suggest_option()
    print("test_ai_prompt_determinism: 전부 통과")


if __name__ == "__main__":
    main()
