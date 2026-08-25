# eval_analyzer

반도체 Fail-Item 평가 분석 엔진. 엔지니어가 수동으로 하던 fail 판단(status 판정 + 분석방향
comment)을 코드로 옮긴다. **코드 의존은 report_server → 여기 한 방향**이다.

- 룰(결정론) + RAG(과거 선례) + LLM(자연어 합성) 하이브리드.
- 입력: 측정 raw → 메모리에서 feature 계산 → **계산값만 DB 에 저장**(raw 비저장).
- 출력: fail item 별 status / signature / 분석방향 comment / confidence.

## 독립성 규칙 (중요)
- **report_server 코드를 import 하지 않는다.** 필요한 계산은 직접 구현하거나 vendor(복사)한다.
  포팅 대상 알고리즘은 [docs/CODE_TO_PORT.md](docs/CODE_TO_PORT.md) 참조.
- **eval.db(SQLite) 스키마의 주인은 여기다.** report.db 는 import 하지 않는다 —
  seed/샘플만으로 단독 개발할 수 있다.
- 반대 방향(report_server → 여기)의 결합은 **import 3곳뿐**이다:
  `web_report/ai_comment.py`(`evaluate` 호출) · `web_report/eval_export.py`(코멘트 export) ·
  `web_report/eval_debug.py`(룰 리로드·트레이스). 규약 정본은
  [../docs/13](../docs/13_eval_analyzer_integration.md).

## ⚠️ 현행 사실 3가지 (옛 문서와 다름)
1. **이 폴더가 원본이다** (2026-08-03). 외부 사본 `F:\COINAPI\eval_analyzer` 는 참조·동기화
   대상이 아니다. 하위 파일은 자유 수정이고 옛 "동결" 규칙은 폐지됐다.
2. **서버는 `evaluate(..., persist=False)` 로 부른다 — 운영 eval.db 에 기록하지 않는다.**
   서버가 쓰는 것은 report_server 소유의 **별도 파일**(`REPORT_EVAL_DB_PATH`, 같은 스키마)
   뿐이고, 그것도 코멘트 export 경로에서만 쓴다.
3. 호출자는 `report_generator` 가 **아니다** — 웹 리포트의 콜드 빌드
   (`web_report/ai_comment.py`)가 부른다.

## ⚠️ eval.db 스키마 변경은 사전 승인 대상
`eval_engine/store.py` 의 DDL·컬럼을 바꿔야 하면 **바로 고치지 말고** 어떤 테이블·컬럼을
어떻게 바꾸는지와 기존 데이터 영향을 설명한 뒤 승인을 받는다(운영 eval.db 에 누적 데이터가
있다). 스키마와 무관한 나머지 수정은 자유.

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
L0~L6 파이프라인·store CRUD·선례검색·분위수 보정 **구현 완료**(테스트 통과). 운영 서버의
AI Comment / Signature 컬럼이 이 엔진을 쓴다. 미구현은 RAG 선례 백엔드(상대측) 등 일부.
자세한 현황은 [CLAUDE.md](CLAUDE.md) 하단 "검증" 절.
