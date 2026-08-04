"""챗봇 CLI — 1단계 검증 수단 (서버 라우트는 아직 없다).

    python -m chatbot "PMIC SOC 에 SGM 들어가는 항목 이력 알려줘"
    python -m chatbot --json "S3222 보고서에서 LDO 이슈 어떻게 close 됐어?"
    python -m chatbot --repl
    python -m chatbot --golden tests/chatbot_golden.yaml

server/ 에서 실행하거나(sys.path 에 server 가 있어야 config/database import 가 된다),
repo 루트에서 `python server/chatbot/cli.py` 로 실행한다 — 아래 _bootstrap 이 둘 다 받는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _bootstrap():
    """server/ 와 repo 루트를 sys.path 에 넣는다 (report_routes.py 와 같은 규약)."""
    server_dir = Path(__file__).resolve().parent.parent
    root_dir = server_dir.parent
    for path in (str(server_dir), str(root_dir)):
        if path not in sys.path:
            sys.path.insert(0, path)
    # 한국어 콘솔(cp949)에서 답변 출력이 UnicodeEncodeError 로 죽지 않게 한다.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass


_bootstrap()

from chatbot import agent, eval_store, planner  # noqa: E402


def _run_one(question, args):
    result = agent.answer(question, viewer=args.viewer,
                          see_all_private=args.master, use_llm=not args.no_llm)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        plan = result["plan"]
        print(f"[plan/{plan['planner']}] intent={plan['intent']} "
              f"product={plan['product']} type={plan['product_type']} "
              f"family={plan['family_product']} items={plan['item_keywords']}")
        print(result["text"])
    return result


def _repl(args):
    print("질문을 입력하세요 (빈 줄 또는 Ctrl+C 로 종료)")
    while True:
        try:
            question = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            return
        _run_one(question, args)
        print()


def _golden(path, args):
    """골든 세트 채점 — 기대 intent / 기대 툴 호출을 실제 결과와 비교한다.

    정답 텍스트를 비교하지 않는다(데이터가 환경마다 다르다). 계획과 툴 선택의 안정성만 본다.
    """
    import yaml

    cases = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    ok_intent = ok_tools = 0
    for i, case in enumerate(cases, 1):
        question = case["question"]
        result = agent.answer(question, viewer=args.viewer,
                              see_all_private=args.master, use_llm=not args.no_llm)
        intent = result["plan"]["intent"]
        tools = [s["tool"] for s in result["steps"]]
        intent_ok = intent == case.get("expect_intent")
        want_tools = case.get("expect_tools") or []
        tools_ok = all(t in tools for t in want_tools)
        ok_intent += intent_ok
        ok_tools += tools_ok
        mark = "OK " if (intent_ok and tools_ok) else "FAIL"
        print(f"[{mark}] {i:2d}. {question}")
        if not intent_ok:
            print(f"         intent: {intent} (기대 {case.get('expect_intent')})")
        if not tools_ok:
            print(f"         tools : {tools} (기대 포함 {want_tools})")
    total = len(cases)
    print(f"\nintent {ok_intent}/{total} · tools {ok_tools}/{total}")
    return 0 if (ok_intent == total and ok_tools == total) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="ENGR 이력 검색 챗봇 (1단계 CLI)")
    parser.add_argument("question", nargs="?", help="질문 1건")
    parser.add_argument("--repl", action="store_true", help="대화형 모드")
    parser.add_argument("--json", action="store_true", help="계획/툴호출/결과를 JSON 으로")
    parser.add_argument("--golden", metavar="YAML", help="골든 질문 세트 채점")
    parser.add_argument("--viewer", default="",
                        help="조회 신원(HoneyUser 계정). 빈 값이면 공개 세션만 보인다")
    parser.add_argument("--master", action="store_true",
                        help="비공개 세션도 조회 (master PC 상당)")
    parser.add_argument("--no-llm", action="store_true", help="규칙 기반 계획만 사용")
    parser.add_argument("--eval-db", metavar="PATH",
                        help="조회할 eval.db 경로 (기본: config.REPORT_EVAL_DB_PATH)")
    args = parser.parse_args(argv)

    if args.eval_db:
        eval_store.set_db_path(args.eval_db)

    if args.golden:
        return _golden(args.golden, args)
    if args.repl:
        _repl(args)
        return 0
    if not args.question:
        parser.print_help()
        print(f"\neval.db: {eval_store.db_path()} "
              f"({'있음' if eval_store.available() else '없음'})")
        print(f"LLM planner: {'사용' if planner.llm_enabled() else '미설정(규칙 폴백)'}")
        return 0
    _run_one(args.question, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
