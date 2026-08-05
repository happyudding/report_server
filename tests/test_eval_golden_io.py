"""골든셋 추가(golden_io) + 기대값 대조(golden_check._check_cases) 검증.

실행:
    python tests/test_eval_golden_io.py

검증:
  (a) add_case — 트레이스 케이스의 현재 발화 상태가 expect 항목으로 기록된다
  (b) 같은 (item, bin, source) 재추가는 교체(replaced) — 중복이 쌓이지 않는다
  (c) 선두 주석 블록 보존 + 백업 생성 (백업은 rules/_backup 이 아니라 골든셋 옆)
  (d) _check_cases — 누락/오탐/status/케이스없음 4종 finding, 전부 맞으면 빈 목록

운영 golden.yaml 은 건드리지 않는다 — tmp 파일로 GOLDEN_FILE 을 갈아 끼운다.
pytest 미사용 — 자체 실행 + assert 스타일(tests/ 관례).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "server"))

_TMP = Path(tempfile.mkdtemp(prefix="golden_io_test_"))

from eval_panel import golden_io                    # noqa: E402
from tools.eval_golden import golden_check          # noqa: E402

_HEAD = "# 골든셋 — 기대 발화 기록\n# 두 번째 주석 줄\n"
golden_io.GOLDEN_FILE = _TMP / "golden.yaml"
golden_io.GOLDEN_FILE.write_text(_HEAD + "sessions: []\n", encoding="utf-8")


def case(item, bin_, status, fired, source_index=0):
    return {"item_raw": item, "bin": bin_, "status": status, "source_index": source_index,
            "signature_matrix": [{"id": s, "fired": True} for s in fired]
                                + [{"id": "OFF_RULE", "fired": False}]}


def main():
    # (a) 추가 ──────────────────────────────────────────────────────────────
    r1 = golden_io.add_case("S1", "MDDI/MX · PRODX · LOT1",
                            case("VOUT", 18, "MAJOR", ["SEVERE_OUTLIER", "OUTLIER_WARN"]))
    assert r1["replaced"] is False, r1
    assert r1["entry"] == {"item": "VOUT", "bin": 18, "source": 0,
                           "fire": ["OUTLIER_WARN", "SEVERE_OUTLIER"],
                           "status": "MAJOR"}, r1["entry"]

    doc = golden_io.read_golden()
    assert len(doc["sessions"]) == 1 and doc["sessions"][0]["note"].startswith("MDDI"), doc
    assert doc["sessions"][0]["session_id"] == "S1"

    # 발화 0건이면 fire 키를 넣지 않는다 (의미 없는 빈 기대값 방지)
    r_none = golden_io.add_case("S1", "", case("IDD", 31, "OK", []))
    assert "fire" not in r_none["entry"] and r_none["entry"]["status"] == "OK", r_none

    # 다른 세션은 별도 항목으로
    golden_io.add_case("S2", "PMIC/SOC", case("VREF", 5, "MINOR", ["SPEC_TOO_TIGHT"]))
    doc = golden_io.read_golden()
    assert len(doc["sessions"]) == 2, doc
    assert r_none["total_expect"] == 2, r_none

    # (b) 같은 대상 재추가 → 교체 ──────────────────────────────────────────
    r2 = golden_io.add_case("S1", "", case("VOUT", 18, "MINOR", ["OUTLIER_WARN"]))
    assert r2["replaced"] is True, r2
    s1 = next(s for s in golden_io.read_golden()["sessions"] if s["session_id"] == "S1")
    assert len(s1["expect"]) == 2, s1["expect"]
    vout = next(e for e in s1["expect"] if e["item"] == "VOUT")
    assert vout["fire"] == ["OUTLIER_WARN"] and vout["status"] == "MINOR", vout

    # source 가 다르면 별개 항목
    golden_io.add_case("S1", "", case("VOUT", 18, "MAJOR", ["LOW_CPK"], source_index=1))
    s1 = next(s for s in golden_io.read_golden()["sessions"] if s["session_id"] == "S1")
    assert len(s1["expect"]) == 3, s1["expect"]

    # (c) 주석 보존 + 백업 ─────────────────────────────────────────────────
    text = golden_io.GOLDEN_FILE.read_text(encoding="utf-8")
    assert text.startswith("# 골든셋 — 기대 발화 기록"), text[:80]
    assert "# 두 번째 주석 줄" in text
    assert yaml.safe_load(text)["sessions"], "yaml 파싱 실패"
    backups = list((_TMP / "_backup").glob("golden.yaml.*.bak"))
    assert backups, "백업이 생기지 않았다"
    assert not (_TMP / "rules").exists(), "룰 백업 폴더로 샜다"

    # (d) _check_cases ─────────────────────────────────────────────────────
    cases = [case("VOUT", 18, "MAJOR", ["SEVERE_OUTLIER"]),
             case("IDD", 31, "OK", [])]
    entry_ok = {"session_id": "S1", "expect": [
        {"item": "VOUT", "bin": 18, "fire": ["SEVERE_OUTLIER"], "status": "MAJOR"}]}
    findings, checked = golden_check._check_cases(entry_ok, cases)
    assert findings == [] and checked == 1, (findings, checked)

    entry_bad = {"session_id": "S1", "expect": [
        {"item": "VOUT", "bin": 18, "fire": ["LOW_CPK"]},          # 누락
        {"item": "IDD", "bin": 31, "not_fire": []},                # 통과
        {"item": "VOUT", "bin": 18, "not_fire": ["SEVERE_OUTLIER"]},   # 오탐
        {"item": "VOUT", "bin": 18, "status": "OK"},               # status 불일치
        {"item": "GHOST", "bin": 1},                               # 케이스없음
    ]}
    findings, checked = golden_check._check_cases(entry_bad, cases)
    kinds = [f["kind"] for f in findings]
    assert kinds == ["누락", "오탐", "status", "케이스없음"], kinds
    assert checked == 4, checked
    assert findings[0]["signature"] == "LOW_CPK" and findings[0]["item"] == "VOUT"
    assert findings[0]["text"].strip().startswith("[누락]"), findings[0]["text"]
    assert findings[3]["signature"] is None, findings[3]

    print("PASS: test_eval_golden_io (a/b/c/d)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
