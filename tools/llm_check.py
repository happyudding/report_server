"""LLM 배선 점검 — 이 프로젝트에서 LLM 을 쓰는 소비자 전부의 상태를 한 번에 본다.

    python tools/llm_check.py           설정만 확인 (호출 없음)
    python tools/llm_check.py --ping    실제로 한 번씩 호출해 왕복 확인

왜 필요한가: LLM 설정은 env 5개(EVAL_LLM_*)로 공유하지만 **읽는 코드가 둘**이다.
- 엔진(AI Comment)   : eval_engine/config.py  — os.environ 만, import 시점 1회
- 웹 챗봇(질문 해석) : server/chatbot/planner.py — os.environ → server.env 파일 폴백
기동 경로나 endpoint 표기(base URL vs 완성 경로)가 어긋나면 한쪽만 켜진 채로 돌기 때문에,
"둘 다 같은 URL 로 켜졌는가" 를 한 화면에서 확인할 수 있어야 한다.

종료코드: 0 = 두 소비자 모두 켜짐(--ping 이면 호출까지 성공) / 1 = 하나라도 꺼짐·실패.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def _mark(ok):
    return "OK  " if ok else "OFF "


def main():
    ap = argparse.ArgumentParser(description="LLM 배선 점검")
    ap.add_argument("--ping", action="store_true", help="실제로 1회 호출해 왕복 확인")
    args = ap.parse_args()

    import config                       # server/config.py — server.env → os.environ 브리지
    print(f"설정 파일 : {ROOT / 'server' / 'env' / 'server.env'}")
    print(f"eval.db   : {config.REPORT_EVAL_DB_PATH}")
    print()

    failed = False

    # ── 1. 엔진 (AI Comment 의 [점검제안] 문장) ──────────────────────────────
    from web_report import ai_comment   # eval_engine 접근은 이 파일 경유 (불변규칙 #8)
    eng = ai_comment.llm_status(ping=args.ping)
    print(f"[{_mark(eng['enabled'])}] 엔진 AI Comment   "
          f"(eval_engine/llm_client.complete → pipeline/recommend.make_comment)")
    print(f"        endpoint : {eng['endpoint_raw'] or '(미설정)'}")
    if eng["endpoint_resolved"] and eng["endpoint_resolved"] != eng["endpoint_raw"]:
        print(f"        → 실제 POST: {eng['endpoint_resolved']}")
    print(f"        model    : {eng['model'] or '(미설정)'}"
          f"   timeout {eng['timeout']}s   api_key {'있음' if eng['api_key_set'] else '없음'}")
    if eng["reply"] is not None:
        print(f"        응답     : {eng['reply']!r}")
    if eng["error"]:
        print(f"        오류     : {eng['error']}")
    if not eng["enabled"] or eng["error"]:
        failed = True
    print("        꺼져 있으면 → 룰 기반 [점검제안] 문구로 폴백(코멘트는 항상 나온다)")
    print()

    # ── 2. 웹 챗봇 (질문 → 인텐트 분류) ─────────────────────────────────────
    from chatbot import planner
    chat_on = planner.llm_enabled()
    print(f"[{_mark(chat_on)}] 웹 챗봇 질문 해석  (chatbot/planner._call_llm → answer_web)")
    print(f"        endpoint : {planner._env('EVAL_LLM_ENDPOINT') or '(미설정)'}")
    resolved = planner.chat_url()
    if resolved and resolved != planner._env("EVAL_LLM_ENDPOINT"):
        print(f"        → 실제 POST: {resolved}")
    print(f"        model    : {planner._env('EVAL_LLM_MODEL') or '(미설정)'}")
    if args.ping and chat_on:
        plan = planner.plan("S3222 보고서 찾아줘", use_llm=True)
        if plan.planner == "llm":
            print(f"        호출     : 성공 (intent={plan.intent}, {plan.llm_ms}ms)")
        else:
            print("        호출     : 실패 → 규칙 폴백 (서버 로그의 warning 확인)")
            failed = True
    if not chat_on:
        failed = True
    print("        꺼져 있으면 → 정규식·키워드 규칙으로 분류(골든셋 22/22 기준)")
    print()

    # ── 3. 아직 배선 안 된 자리 (참고) ──────────────────────────────────────
    print("[미구현] 선례 RAG 검색   eval_engine/precedent_client._rag_search")
    print(f"         backend={os.getenv('EVAL_PRECEDENT_BACKEND', 'sql')} "
          f"(rag 로 바꾸려면 이 함수부터 구현)")
    print("[미구현] 텍스트→선례행   db_input/ai_extract.extract_rows_from_text")
    print()

    if failed:
        print("→ 하나 이상이 꺼져 있거나 실패했습니다. server/env/server.env 의 "
              "EVAL_LLM_* 5줄을 확인하고 서버를 재기동하세요.")
    else:
        print("→ 전부 정상.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
