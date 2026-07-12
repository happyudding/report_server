# eval_analyzer

반도체 Fail-Item 평가 분석 엔진. 엔지니어가 수동으로 하던 fail 판단(status 판정 + 분석방향
comment)을 코드로 옮긴다. **report_server 와 완전 독립**된 프로젝트.

- 룰(결정론) + RAG(과거 선례) + LLM(자연어 합성) 하이브리드.
- 입력: 측정 raw → 메모리에서 feature 계산 → **계산값만 자체 DB(eval.db)에 저장**(raw 비저장).
- 출력: fail item 별 status / signature / 분석방향 comment / confidence.

## 독립성 규칙 (중요)
- **report_server 코드를 import 하지 않는다.** 필요한 계산은 직접 구현하거나 vendor(복사)한다.
  포팅 대상 알고리즘은 [docs/CODE_TO_PORT.md](docs/CODE_TO_PORT.md) 참조.
- **자체 DB(eval.db, SQLite)를 직접 관리**한다. report.db 는 무시. → seed/샘플로 단독 개발 가능.
- 추후 report_server(client)가 매 파일 run 시 `evaluate()` 를 호출해 결합하지만, DB 주인은 eval_analyzer.

## 구조
```
eval_analyzer/
├── eval_engine/          분석 엔진 패키지 (서버·UI 없음)
│   ├── api.py            evaluate(...) 공개 진입점
│   ├── config.py         DB 경로 / LLM endpoint·model·key / rules 경로
│   ├── store.py          eval.db 스키마(DDL) + CRUD
│   ├── pipeline/         L0 ingest → L1 metrics → L2 features → L3 signatures
│   │                     → L4 status → L5 recommend(RAG+LLM) → L6 present
│   ├── llm_client.py     교체형 LLM 어댑터 (모델은 사용자 지정, 기본값 없음)
│   ├── calibrate.py      과거 데이터 분위수 보정 + comment 채굴
│   ├── cli.py            테스트/보정 CLI (서버 아님)
│   └── rules/            thresholds.yaml / signatures.yaml / bin_taxonomy.yaml / item_alias.yaml
├── docs/                 설계·연동·핸드오프 문서
├── data/                 eval.db (런타임 생성)
└── seeds/                background seed 예시
```

## 문서
- [docs/DB_SCHEMA.md](docs/DB_SCHEMA.md) — DB 스키마 확정본 (테이블·grain·키·관계)
- [docs/5STAGE_COLUMNS.md](docs/5STAGE_COLUMNS.md) — 판단 5단계 컬럼 의미 사전
- [docs/REPORT_SERVER_CONTEXT.md](docs/REPORT_SERVER_CONTEXT.md) — report_server 파악(바이브코딩용)
- [docs/INTEGRATION_CONTRACT.md](docs/INTEGRATION_CONTRACT.md) — report_server ↔ eval_analyzer 연동 계약
- [docs/HANDOFF_TO_REPORT_SERVER.md](docs/HANDOFF_TO_REPORT_SERVER.md) — report_server 담당자 전달용
- [docs/CODE_TO_PORT.md](docs/CODE_TO_PORT.md) — import 금지: 가져갈/재구현할 알고리즘

## 상태
스캐폴드 + 문서 단계. 각 pipeline 모듈은 시그니처/TODO 스텁. 실제 로직은 후속(바이브코딩).
