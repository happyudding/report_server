"""라우트 집결자 (Phase 4 분리 — 2026-07-11).

이 모듈을 import 하면(report_extension 이 트리거) 아래 모듈들의 데코레이터가
평가되어 모든 라우트가 report_bp 에 등록된다. URL·endpoint 이름·응답 형태는
분리 전과 동일하다. 구현 위치:

- security.py          CSRF / 신원 가드 / 입력 검증 / 감사 기록 (공용 헬퍼)
- routes_session.py    세션 조회(/full 포함)·삭제·중요/비공개·권한 위임
- routes_webreport.py  /session/<sid>/web_report/* 프록시 전부
- routes_misc.py       주석·즐겨찾기·페이지·vendor·히스토리·(폐지)인증·디버그
- routes_voc.py        VOC 게시판 (페이지 + 목록/등록/이미지/삭제 API, 별도 voc.db)
- routes_eval_input.py Honey 'DB Input' — 선례 CSV 검증/적재 (별도 eval DB)
- routes_chat.py       웹 챗봇 (관리자 전용 POST /api/chat — server/chatbot 엔진 노출)
"""
import sys
from pathlib import Path

# web_report 패키지(repo 루트)가 sys.path 에 없을 수 있어(작업 디렉토리 server/)
# 라우트 모듈 import 전에 등록한다.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from report import routes_session  # noqa: E402,F401
from report import routes_webreport  # noqa: E402,F401
from report import routes_misc  # noqa: E402,F401
from report import routes_voc  # noqa: E402,F401
from report import routes_eval_input  # noqa: E402,F401
from report import routes_chat  # noqa: E402,F401
