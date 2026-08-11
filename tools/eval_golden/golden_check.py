"""eval 룰 골든셋 회귀 검사 — 기대 발화 vs 실제 발화 diff.

임계값을 만졌을 때 "무엇이 좋아지고 무엇이 나빠졌는지"를 수치로 보기 위한 도구다.
golden.yaml 에 사람이 적어 둔 판단(이 항목은 SUBPOP_GAP 이 떠야 한다 / 뜨면 안 된다)을
실제 트레이스 결과와 대조해 누락(miss)·오탐(false-fire)을 세고, 하나라도 있으면 종료코드 1.

엔진에는 직접 닿지 않는다 — web_report.eval_debug.trace_session 만 호출한다
(eval_engine import 3곳 규약 유지, docs/13 §2). 운영 서버와 같은 DB·업로드 루트를 읽으므로
서버가 떠 있는 상태에서 돌려도 되지만, 트레이스는 CPU 를 쓰니 한가할 때 실행할 것.

사용:
    python tools/eval_golden/golden_check.py
    python tools/eval_golden/golden_check.py --session 20260803_120000_abcd
    python tools/eval_golden/golden_check.py --file other_golden.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent.parent
for p in (str(_ROOT), str(_ROOT / "server")):
    if p not in sys.path:
        sys.path.insert(0, p)

import config                                    # noqa: E402  (server/config.py)
from database import report_db                   # noqa: E402
from web_report import eval_debug                # noqa: E402

_DEFAULT_GOLDEN = Path(__file__).resolve().parent / "golden.yaml"


def _fired(case):
    return {r["id"] for r in (case.get("signature_matrix") or []) if r.get("fired")}


def _match(case, exp):
    if str(case.get("item_raw") or "") != str(exp["item"]):
        return False
    if exp.get("bin") is not None and case.get("bin") != exp["bin"]:
        return False
    if exp.get("source") is not None and case.get("source_index") != exp["source"]:
        return False
    return True


def _finding(kind, exp, text, signature=None):
    """finding 1건 — `text` 는 CLI 출력 원문 그대로, 나머지는 패널 렌더용 분해."""
    return {"kind": kind, "item": str(exp.get("item") or ""), "bin": exp.get("bin"),
            "signature": signature, "text": text}


def _check_cases(entry, cases):
    """기대값 vs 트레이스 케이스 대조 (순수 함수 — 트레이스 없이 테스트 가능).

    반환 (findings, checked). findings 원소는 dict 이고 `text` 에 사람이 읽는 문장을
    담는다 — CLI 는 그 문장만 출력하므로 stdout 형식은 종전과 같다.
    """
    session_id = str(entry.get("session_id") or "")
    findings, checked = [], 0
    for exp in entry.get("expect") or []:
        matched = [c for c in cases if _match(c, exp)]
        label = f"{session_id} · {exp['item']}"
        if exp.get("bin") is not None:
            label += f" bin{exp['bin']}"
        if not matched:
            findings.append(_finding("케이스없음", exp,
                                     f"  [케이스없음] {label} — 트레이스에 이 항목이 없다"))
            continue
        checked += 1
        fired = set().union(*(_fired(c) for c in matched))
        for sig in exp.get("fire") or []:
            if sig not in fired:
                findings.append(_finding("누락", exp,
                                         f"  [누락]   {label} — {sig} 가 안 떴다 "
                                         f"(실제 발화: {', '.join(sorted(fired)) or '없음'})",
                                         signature=sig))
        for sig in exp.get("not_fire") or []:
            if sig in fired:
                findings.append(_finding("오탐", exp,
                                         f"  [오탐]   {label} — {sig} 가 떴다", signature=sig))
        want_status = exp.get("status")
        if want_status:
            got = {str(c.get("status")) for c in matched}
            if want_status not in got:
                findings.append(_finding("status", exp,
                                         f"  [status] {label} — 기대 {want_status}, "
                                         f"실제 {'/'.join(sorted(got))}"))
    return findings, checked


def check_session(entry):
    """세션 1건 검사 → (findings, checked). findings 는 _check_cases 의 dict 목록.

    max_cases=None (전체) 로 트레이스한다 — 기본 상한 400 이면 뒤쪽 항목이 통째로
    [케이스없음] 으로 잡혀 오탐이 된다. 같은 이유로 `fail_only=False`(전체 item) 도
    고정한다 — 서버가 fail-only 로 돌고 있으면 fail 이 없는 골든 항목(특히 "발화하면
    안 된다"는 not_fire 가드)이 통째로 [케이스없음] 이 되어 오탐이 된다.
    """
    session_id = str(entry["session_id"])
    trace = eval_debug.trace_session(session_id, report_db=report_db,
                                     upload_root=Path(config.REPORT_UPLOAD_DIR),
                                     max_cases=None, fail_only=False)
    return _check_cases(entry, trace.get("cases") or [])


def main():
    ap = argparse.ArgumentParser(description="eval 룰 골든셋 회귀 검사")
    ap.add_argument("--file", default=str(_DEFAULT_GOLDEN), help="골든셋 yaml 경로")
    ap.add_argument("--session", help="이 세션 id 만 검사")
    args = ap.parse_args()

    doc = yaml.safe_load(Path(args.file).read_text(encoding="utf-8")) or {}
    entries = doc.get("sessions") or []
    if args.session:
        entries = [e for e in entries if str(e.get("session_id")) == args.session]
    if not entries:
        print(f"검사할 항목이 없다 — {args.file} 의 sessions 를 채워라 "
              f"(/pe/eval 트레이스 화면을 보며 기록).")
        return 0

    print(f"rules_rev={eval_debug.rules_rev() or '(없음)'} · 세션 {len(entries)}건\n")
    total_find, total_checked = [], 0
    for entry in entries:
        note = entry.get("note")
        print(f"● {entry.get('session_id')}" + (f" — {note}" if note else ""))
        try:
            findings, checked = check_session(entry)
        except Exception as exc:                          # 세션 삭제·파일 유실 등
            print(f"  [실패]   트레이스 불가: {exc}")
            total_find.append("trace_failed")
            continue
        total_checked += checked
        if findings:
            print("\n".join(f["text"] for f in findings))
            total_find.extend(findings)
        else:
            print(f"  일치 ({checked}건)")
    print(f"\n검사 {total_checked}건 · 불일치 {len(total_find)}건")
    return 1 if total_find else 0


if __name__ == "__main__":
    sys.exit(main())
