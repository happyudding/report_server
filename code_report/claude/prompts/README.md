# Opus 구현 프롬프트 모음 — web_report ↔ eval_analyzer 선순환 완성

> 상위 분석 보고서([../README.md](../README.md), 01~06 문서)의 실행 계획을
> **Claude Code(Opus) 세션에 그대로 투입할 수 있는 구현 명령 프롬프트**로 만든 것.
> 각 프롬프트는 자기완결(self-contained)이며, 실제 코드베이스의 함수 시그니처·상수·
> 규약과 대조 검증된 핵심 코드를 포함한다 (2026-07-17 작업트리 기준).

## 투입 방법

새 Claude Code 세션(작업 디렉토리 `f:\COINAPI\report_server`)에서:

```
code_report/claude/prompts/P1_db_input_v0_붙여넣기_적재도구.md 파일을 읽고
그 안의 지시를 그대로 수행해줘.
```

또는 파일 내용 전체를 복사해 붙여넣어도 된다. **한 세션에 프롬프트 1개**가 원칙
(스코프 섞임 방지). 각 프롬프트 끝의 "완료 기준"을 만족해야 종료.

## 실행 순서와 의존성

| 순서 | 프롬프트 | 내용 | 선행 조건 |
|---|---|---|---|
| 1 | [P1_db_input_v0_붙여넣기_적재도구.md](P1_db_input_v0_붙여넣기_적재도구.md) | 엑셀 복붙 → 매핑 → 검증 → 선례 DB 적재 로컬 도구 (LLM 불필요) | 없음 (즉시 가능). 단 **대량 적재 실행 전에** R-1(선례 인용 상한) 외부 요청 전달 권장 — [../03 문서](../03_eval_analyzer_수정필요_요청목록.md) |
| 2 | [P2_라벨입력_UI.md](P2_라벨입력_UI.md) | IssueTable 라벨(판정/원인/조치/결과) 입력 → label 완전체 export (E-1) | 없음 |
| 3 | [P3_AI코멘트_피드백.md](P3_AI코멘트_피드백.md) | AI Comment 👍/👎 채택 신호 기록 (E-2) | P2 권장(export 확장 코드가 겹침 — P2 먼저 하면 충돌 적음) |
| 4 | [P4_features_축적.md](P4_features_축적.md) | evaluate(persist=True)로 features/evaluation 을 서버 소유 eval DB 에 축적 (E-3) | **docs/13 §4 규약 개정을 외부 담당자와 합의 후** 착수 |
| 5 | [P5_admin_검수대시보드.md](P5_admin_검수대시보드.md) | admin Eval DB 탭에 커버리지/검수 위젯 (E-5) | P2 이후 권장 (라벨 데이터가 있어야 의미) |
| 6 | [P6_db_input_v1_LLM추출.md](P6_db_input_v1_LLM추출.md) | 자유서식 문서 → rows JSON LLM 추출 트랙 (v1) | P1 완료 + 사내 LLM endpoint 확보 |

## 모든 프롬프트 공통 불변 규칙 (각 프롬프트에도 재기재됨)

1. **`eval_analyzer/` 하위 파일은 한 글자도 수정 금지** (외부 담당자 소유·동결).
2. **eval_engine import 는 `web_report/ai_comment.py` + `web_report/eval_export.py` 2곳만**
   (docs/13 §2). 새 코드는 이 두 파일 내부 확장이거나, 이 파일들을 경유하거나,
   **subprocess 로 db_input CLI 를 호출**하는 형태만 허용.
3. `web_report/` 편집 저장의 진실은 세션 편집 DB(`report_webreport_edit`) — manifest 재저장 금지.
4. **`build_report_payload` 반환 구조를 바꾸면 `web_report/cache_policy.py` 의
   `REPORT_SCHEMA_VERSION` 을 반드시 올릴 것** (안 올리면 disk cache 가 옛 payload 를 재사용해 조용히 회귀).
5. 프런트 JS(static/webreport/)는 classic script 순서 로드 — ES module 전환/로드 순서 변경 금지.
   **새 편집 채널은 autoSave Promise.all(keepalive) 패턴에 합류**시킬 것.
6. 규약 문서를 바꾸는 변경(P4)은 [docs/13_eval_analyzer_integration.md](../../../docs/13_eval_analyzer_integration.md) 를 함께 갱신.
7. 완료 보고는 한국어로: 변경 파일 목록 / 검증 결과(실행 로그 포함) / 미해결·후속 항목.

## 코드 밖에서 별도로 진행할 것 (프롬프트 대상 아님)

- **R-1~R-6 외부 담당자 요청** — [../03 문서](../03_eval_analyzer_수정필요_요청목록.md)를 그대로 전달.
- **운영 배선(E-7)** — 서버 기동 환경에 `EVAL_DB_PATH=<REPORT_EVAL_DB_PATH 실제 경로>` 설정
  (이걸 해야 AI Comment 의 L5 선례검색이 서버가 쌓은 선례 DB 를 읽는다 — P4 프롬프트에 검증 포함),
  eval DB 백업 편입.
