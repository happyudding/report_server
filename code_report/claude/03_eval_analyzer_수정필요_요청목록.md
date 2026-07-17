# 03. eval_analyzer 내부 수정 요청 목록 — 외부 담당자 전달용

> eval_analyzer 는 외부 담당자 소유(동결)라 우리가 직접 고칠 수 없습니다.
> 이 문서는 **그대로 담당자에게 전달할 수 있는 요청서** 형식으로 작성했습니다.
> 각 항목에 배경(현재 코드 상태) → 요청 내용 → 기대 효과 → 우선순위를 담았습니다.
>
> 참고: 이 중 상당수는 eval_analyzer 문서에 이미 "미구현/후속"으로 예고된 항목입니다
> ([eval_analyzer/CLAUDE.md](../../eval_analyzer/CLAUDE.md) 미구현 목록,
> [eval_engine/CLAUDE.md](../../eval_analyzer/eval_engine/CLAUDE.md) "미구현(후속 작업)").
> 즉 새 요구가 아니라 **예정된 작업의 우선순위 확정 + 계약 확정 요청**에 가깝습니다.

---

## 요청 총괄표

| # | 요청 | 종류 | 우선순위 | 왜 |
|---|---|---|---|---|
| R-1 | 선례 인용 상한(top-k) 도입 | **신규 (소규모)** | ★★★ 높음 | DB 가 쌓일수록 코멘트가 폭발하는 유일한 역효과 지점 |
| R-2 | `llm_client.complete()` HTTP 구현 | 예고된 미구현 | ★★★ 높음 | 코멘트 품질 도약의 스위치. 선례가 쌓일수록 효과 배가 |
| R-3 | `ai_extract.extract_rows_from_text()` — 구현 또는 "외부 구현 허용" 계약 확정 | 예고된 미구현 | ★★★ 높음 | 초기 DB 적재(콜드스타트)의 병목 |
| R-4 | calibrate 후속: comment 채굴 + 룰 precision/recall 검증 | 예고된 미구현 | ★★ 중간 | label 이 쌓인 뒤에야 의미 — 중기 |
| R-5 | calibrate 실행 편의(권장 절차/리포트) | 개선 | ★★ 중간 | features 축적(04 문서 E-3)과 세트 |
| R-6 | (장기) 판정 레이어의 선례/label 활용 설계 논의 | 설계 논의 | ★ 낮음 | "쌓일수록 판정도 좋아지는" 최종 단계 |

> 별도 항목이 아닌 것: **RAG 선례 백엔드(`_rag_search`)는 요청 대상이 아닙니다.**
> 핸드오프 문서([docs/PRECEDENT_RAG_HANDOFF.md](../../eval_analyzer/docs/PRECEDENT_RAG_HANDOFF.md))가
> 이미 "report_server 담당자가 그 한 함수만 구현"하도록 계약해 두었습니다. → [04 문서 E-6](04_외부_확장_포인트.md)

---

## R-1. 선례 인용 상한(top-k) — 소규모 신규 요청

**배경 (현재 코드)**
- SQL 선례검색 호출부가 limit 없이 부른다: [precedent_client.py:21-26](../../eval_analyzer/eval_engine/precedent_client.py#L21-L26)
  → `search_precedents(..., limit=None)` = **매칭 선례 전부 반환** ([store.py:478](../../eval_analyzer/eval_engine/store.py#L478) docstring 에 명시).
- 템플릿 코멘트가 반환된 **모든** human_comment 를 `" / "` 로 이어붙인다:
  [recommend.py:24-26](../../eval_analyzer/eval_engine/pipeline/recommend.py#L24-L26).
- RAG 백엔드에는 `EVAL_PRECEDENT_RAG_TOPK`(기본 5) 상한이 이미 설계되어 있으나
  ([config.py:35](../../eval_analyzer/eval_engine/config.py#L35)), 기본 SQL 백엔드에는 대응 상한이 없다.

**문제**
우리 쪽 목표가 "선례 DB 를 수백~수천 건으로 키우는 것"이다. 같은 item 계열 선례가 수십 건을
넘는 순간 IssueTable 의 AI Comment 셀 하나가 수천 자가 되어 **쌓을수록 UI 가 나빠지는 역전**이 일어난다.

**요청**
- 예: `EVAL_PRECEDENT_SQL_TOPK`(기본 5, env override) 신설 →
  `_sql_search` 에서 `limit=` 로 전달, 또는 `_template_comment` 인용 개수 상한.
- 정렬 기준(사람 코멘트 우선 → 유사도순)은 현행 유지 — 상한만 자르면 됨.
- 하위호환: env 미설정 시 기본 5 로 제안(현행 "전체"를 기본으로 유지하고 싶다면 0=무제한 컨벤션도 무방).

**기대 효과**: DB 축적량과 무관하게 코멘트 길이가 일정 — "쌓을수록 좋아지는 구조"의 전제 조건.

---

## R-2. `llm_client.complete()` HTTP 구현 — 예고된 미구현

**배경 (현재 코드)**
- [llm_client.py](../../eval_analyzer/eval_engine/llm_client.py) 의 `complete()` 가 `NotImplementedError` 스텁 →
  `EVAL_LLM_ENABLED=true` 로 켜도 예외 → 항상 템플릿 코멘트 fallback
  ([recommend.py:48-54](../../eval_analyzer/eval_engine/pipeline/recommend.py#L48-L54)).
- 설정 항목(EVAL_LLM_ENDPOINT/MODEL/API_KEY/TIMEOUT)은 이미 준비됨 ([config.py:23-27](../../eval_analyzer/eval_engine/config.py#L23-L27)).
- eval_engine/CLAUDE.md 미구현 목록에 "사용자 지정 endpoint 로 HTTP POST(OpenAI 호환 shape)" 로 예고됨.

**요청**
- 예고된 사양 그대로: OpenAI 호환 chat endpoint 에 POST, 타임아웃/실패 시 예외 → 상위에서 템플릿 fallback(현행 로직 그대로 동작).
- 모델/주소 하드코딩 금지(기존 원칙 유지) — env 만으로 on/off.

**기대 효과**
- 현재 코멘트는 "룰 골격 + 선례 원문 나열"이다. LLM 이 켜지면 **선례들이 자연어 한 문장으로 요약·합성**되어
  선례가 많아질수록 코멘트 품질이 실제로 올라가는 두 번째 통로가 열린다.
- R-1(상한)과 함께 적용하면 프롬프트 길이도 통제된다.

**참고**: 사내 LLM endpoint 확보는 우리(운영) 쪽 준비물이다. 구현만 있으면 env 주입은 서버 기동 스크립트에서 가능.

---

## R-3. `ai_extract.extract_rows_from_text()` — 구현 또는 "외부 구현" 계약 확정

**배경 (현재 코드)**
- 비정형 텍스트 → 스키마 rows 자동 추출 훅이 스텁: [ai_extract.py:137-142](../../eval_analyzer/db_input/ai_extract.py#L137-L142)
  ("LLM connection/prompting is intentionally out of scope").
- 검증(`validate_rows`)·CSV 변환(`rows_to_csv`)·적재(`import_csv.import_rows`)는 완성되어 있어
  **rows JSON 만 만들어지면 그 뒤는 전부 돌아간다.**

**요청 — 둘 중 하나 (우리는 B 안을 선호)**
- **A 안**: eval_analyzer 쪽에서 LLM 추출기 구현 (R-2 와 같은 EVAL_LLM_* 설정 재사용).
- **B 안 (선호)**: "추출기는 report_server 쪽이 외부에서 구현하고, `validate_rows` 의 검증 통과 rows JSON 을
  `import_text --json` / `import_rows` 로 넣는다" 는 **역할 분담을 공식 계약으로 확정**.
  이 경우 eval_analyzer 코드 수정은 0 — 지금 공개 표면만으로 가능함을 확인했다.
  (RAG 핸드오프와 같은 방식의 1쪽짜리 계약 문서만 있으면 됨)

**기대 효과**: 초기 선례 DB 콜드스타트 해소의 마지막 조각. → 전체 설계는 [05 문서](05_db_input_발전방향.md)

---

## R-4. calibrate 후속 — comment 채굴 + 룰 precision/recall 검증

**배경 (현재 코드/문서)**
- 분위수 보정(recalibrate)은 구현 완료 ([calibrate.py:96](../../eval_analyzer/eval_engine/calibrate.py#L96)).
- "comment 채굴(label/outcome 군집) + 룰 precision/recall 검증"은 문서상 예고된 후속 미구현
  ([eval_engine/CLAUDE.md](../../eval_analyzer/eval_engine/CLAUDE.md) 미구현 목록).

**요청**
- label(human_status) 이 쌓이면 **엔진 status vs 사람 status 의 혼동행렬/precision·recall 리포트**를
  내는 오프라인 도구. (판정 로직 변경이 아니라 "성적표" 도구 — 위험 없음)
- 그 성적표를 근거로 임계값·signature 우선순위를 개정하는 절차 문서화.

**우선순위가 "중간"인 이유**: 재료(human_status label)가 아직 0건이다. 우리 쪽이 04 문서 E-1(라벨 입력 UI)로
재료를 만들기 시작한 뒤에 착수해야 의미가 있다. 다만 **어떤 형태로 쌓아야 소비 가능한지 지금 합의**해 두면
스키마 재작업을 피할 수 있다 (현행 label 스키마로 충분한지 확인 요청).

---

## R-5. calibrate 운영 절차 정리 (개선 요청)

**배경**: calibrate 는 수동 CLI 이고 결과가 새 engine_version 으로 분리된다. 운영에서 "언제, 누가,
어떤 조건에서 돌리고, 어떻게 검증 후 반영하는지" 절차가 없다.

**요청**
- 권장 실행 주기/조건(예: item_class 당 features n≥30 도달 시), 실행 전후 diff 리포트,
  롤백 방법(engine_version 되돌리기)을 담은 **운영 절차 1쪽**.
- (선택) `cli calibrate --dry-run` 처럼 반영 없이 제안값만 보는 모드.

**기대 효과**: 우리(운영)가 features 축적(04 문서 E-3)을 시작하면 바로 소비할 수 있는 운영 루프 완성.

---

## R-6. (장기·설계 논의) 판정 레이어의 선례/label 활용

**배경**: 02 문서 §3 에서 확인했듯 판정(L0~L4)은 DB 를 참조하지 않는다. 이는 의도된 설계로 보이며
(재현성·감사 가능성 관점의 장점), 우리도 단기적으로 바꾸자는 제안이 아니다.

**논의 요청 (아이디어 수준)**
- label 통계 기반 **bin_taxonomy severity_bias 자동 제안**: 특정 (product_type, bin) 에서
  사람이 반복적으로 엔진보다 높은/낮은 등급을 줬다면 그 편차를 bias 후보로 리포트.
- **false_fail 선례 감지**: 같은 case 계열에서 outcome.result=false_fail 이 반복되면
  판정 결과에 "과거 false_fail 이력 N건" 배지 정도의 **참고 정보**로만 노출(판정 등급은 불변).
- 어느 쪽이든 "판정은 룰이 정하고, DB 는 참고 정보와 보정 제안만 낸다"는 현 철학을 지키는 범위로.

**우선순위가 "낮음"인 이유**: R-4 성적표가 나와서 "어디가 얼마나 틀리는지" 데이터가 생긴 뒤에
논의해야 실효가 있다. 지금은 방향 공유만.

---

## 전달 시 참고 — 우리 쪽이 이미/곧 하는 일 (담당자 컨텍스트용)

| 우리 쪽 작업 | 내용 | 문서 |
|---|---|---|
| 코멘트 export | IssueTable 사람 코멘트 → REPORT_EVAL_DB_PATH label 적재 (가동 중) | 01 문서 §5.2 |
| 라벨 입력 UI (예정) | human_status/root_cause/outcome 을 IssueTable 에서 입력받아 label 완전체로 | 04 문서 E-1 |
| features 축적 (예정) | evaluate 결과의 features 를 서버 소유 DB 에 기록 → calibrate 재료 | 04 문서 E-3 |
| RAG 백엔드 (계약됨) | `_rag_search` 한 함수 구현 — 핸드오프 문서 계약대로 | 04 문서 E-6 |
| db_input 외부 파서 (예정) | 복붙 문서 → rows JSON 추출기 (R-3 B 안 전제) | 05 문서 |
