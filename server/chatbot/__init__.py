"""ENGR 이력 검색 챗봇 — 조회 툴 계층 (1단계).

목적: "PMIC SOC family 에 SGM 들어가는 항목 예전에 어떻게 됐었지?" / "S3222 보고서에서
LDO 이슈 어떻게 close 됐지?" 같은 자연어 질문을, **미리 정의한 조회 함수**로만 답한다.

설계 원칙 (docs/13 · CLAUDE.md 규칙과의 정합):
- LLM 에게 SQL 을 생성시키지 않는다. 노출하는 것은 파라미터 바인딩 SELECT 뿐이다.
- **eval_engine 을 import 하지 않는다** (불변 규칙 #8 — eval_engine import 는
  web_report/ 의 ai_comment·eval_export·eval_debug 3곳만). eval.db 는 sqlite mode=ro 로
  직접 열어 SELECT 만 한다(스키마 계약은 eval_engine/store.py SCHEMA).
- 라우트를 등록하지 않는다. 1단계는 CLI 전용이라 운영 서버 동작에 영향이 없다.

데이터 정본 2개:
- report.db  : 세션·제품·lot·Issue Table 편집(코멘트/Status)  → tools_report.py
- eval.db    : item_master/alias·fail_case·raw_metrics·label   → tools_eval.py
  두 DB 의 조인 키는 eval.db ``ingest_run.session_id`` / ``analysis_key`` 다.
"""
