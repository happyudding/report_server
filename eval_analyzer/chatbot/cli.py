"""챗봇 CLI — 단발 질문 또는 REPL.

  python -m chatbot.cli "MAJOR 케이스 통계"   # 단발
  python -m chatbot.cli                        # REPL (빈 줄/exit 종료)
"""
import sys

from .agent import ask


def main(argv=None):
    # Windows 콘솔 기본(cp949)에서 한글/기호 깨짐 방지 — UTF-8 출력 강제
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        print(ask(" ".join(argv)))
        return
    print("eval.db 챗봇 (빈 줄 또는 'exit' 로 종료)")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() in ("exit", "quit"):
            break
        print(ask(q))


if __name__ == "__main__":
    main()
