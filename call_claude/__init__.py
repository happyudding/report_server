# -*- coding: utf-8 -*-
"""call_claude — 로컬 Claude Code CLI(`claude -p`) subprocess 호출 단일 진입점.

report_server 의 AI Comment 클라 대행(docs/23)이 첫 사용처이지만, **다른 프로젝트에서
그대로 가져다 쓰는 것**이 설계 목표다. 그래서:

- 표준 라이브러리만 사용한다. 이 저장소의 다른 패키지를 import 하지 않는다.
- 공개 함수는 예외를 던지지 않는다 — 실패는 None(단건) / None 원소(배치)로 돌려주고
  상세는 `log` 콜백과 logging("call_claude")으로만 남긴다.
  (첫 사용처의 계약: LLM 실패 시 룰 폴백 문장이 이미 있으므로 실패가 무해하다.)
- 프롬프트는 절대 argv 에 넣지 않는다(stdin only).

사용 예::

    import call_claude
    info = call_claude.probe()                      # {"ok","bin","version","flags","error"}
    text = call_claude.run_prompt("한 문장으로 답하라: ...")
    texts = call_claude.run_batch(["요청1", "요청2"])  # 길이 보존, 실패 원소는 None

상세 계약·현장 검증 체크리스트는 README.md 참조.
"""

from .runner import (  # noqa: F401
    DEFAULT_TIMEOUT,
    ENV_BIN,
    ClaudeCliError,
    find_cli,
    probe,
    run_batch,
    run_prompt,
    supports_json_schema,
)
from .batch import (  # noqa: F401
    BATCH_JSON_SCHEMA,
    batch_schema_json,
    build_meta_prompt,
    parse_batch_reply,
)
