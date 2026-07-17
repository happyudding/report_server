# 05. db_input 발전 방향 — "기존 평가 문서를 복붙하면 DB 에 쌓이는" 파서 설계

> 목표: 과거에 평가했던 문서(엑셀 정리본, 보고서 등)를 **복사·붙여넣기 수준의 노력**으로
> 선례 DB 에 적재하는 것. 현재 무엇이 있고 무엇이 없는지 → 어떤 순서로 만들면 좋은지를 정리했습니다.
> db_input 은 eval_analyzer 내부(동결)이므로, **추가 코드는 전부 밖에** 두는 설계입니다.

---

## 1. 현재 자산 인벤토리 — 생각보다 많이 준비되어 있다

| 부품 | 상태 | 근거 |
|---|---|---|
| CSV → DB 적재 (`import_csv.py`) | ✅ 완성 | 필수 6컬럼 검증, 엔진 정규화 재사용, cpk 계산, **멱등**(재실행 중복 없음), `--to-eval-db` 통합 적재 ([import_csv.py:214-226](../../eval_analyzer/db_input/import_csv.py#L214-L226)) |
| label 완전체 입력 | ✅ 지원 | CSV 컬럼에 human_comment/human_status/root_cause_category/outcome_* 포함 — **web_report export 보다 완전한 label 을 넣을 수 있는 유일한 경로** |
| rows(JSON) 검증기 (`ai_extract.validate_rows`) | ✅ 완성 | 행별 READY/오류 판정 ([ai_extract.py:86](../../eval_analyzer/db_input/ai_extract.py#L86)) |
| rows → CSV 변환 (`rows_to_csv`) | ✅ 완성 | 검수용 중간 산출물 생성 ([ai_extract.py:124](../../eval_analyzer/db_input/ai_extract.py#L124)) |
| JSON/텍스트 CLI (`import_text.py`) | ✅ 완성 | `--json → --preview → --write-csv → --save` 단계 실행 |
| **비정형 텍스트 → rows 자동 추출** | ❌ **스텁** | `extract_rows_from_text` = NotImplementedError ([ai_extract.py:137-142](../../eval_analyzer/db_input/ai_extract.py#L137-L142)) |
| 검수(사람 확인) UI | ❌ 없음 | CLI 텍스트 출력뿐 |
| 헤더/어휘 매핑 보조 | ❌ 없음 | 컬럼명이 정확히 일치해야 함 |

**핵심 판단**: 파이프라인의 **뒷단(검증→변환→적재)은 전부 완성**되어 있고 멱등해서 안전하다.
없는 것은 **앞단(붙여넣은 것 → rows JSON)** 과 **가운데(사람 검수 화면)** 두 조각뿐이다.
그리고 그 두 조각은 db_input 을 한 줄도 고치지 않고 밖에서 만들 수 있다 (03 문서 R-3 B 안).

---

## 2. 제안 파이프라인 — 5단계

```
 ┌─────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │ ①수집    │──▶│ ②추출         │──▶│ ③검증         │──▶│ ④검수(사람)   │──▶│ ⑤적재         │
 │ 복붙/파일 │   │ 표→규칙 매핑   │   │ validate_rows │   │ 미리보기 표    │   │ import_rows  │
 │          │   │ 자유서식→LLM  │   │ + 어휘 검증    │   │ 수정·보완·태깅 │   │ (멱등 upsert) │
 └─────────┘   └──────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
   신규 개발       신규 개발          ✅ 있음(재사용)      신규 개발(얇게)      ✅ 있음(재사용)
```

### ① 수집 — 입력 창구

- 텍스트 붙여넣기 상자 (엑셀 범위 복사 = 탭 구분 텍스트로 들어옴 → 사실상 표 데이터)
- 파일 업로드: csv / xlsx / txt
- 어느 쪽이든 "원문 그대로"를 보관(출처 추적용 source_file 기록)

### ② 추출 — 2트랙 전략 (LLM 없이 시작 가능)

**트랙 A — 표 형태 (규칙 기반, LLM 불필요)**: 기존 평가 문서가 대부분 엑셀 표라면 이것만으로
목표의 80% 를 달성한다.
- 엑셀 복붙 = 탭 구분 텍스트 → 표로 복원
- **헤더 시노님 사전**으로 컬럼 자동 매핑: 예) "품명/제품명/product" → `product_name`,
  "항목/테스트항목/item" → `item_name`, "코멘트/비고/조치내용" → `human_comment`
- 매핑 안 되는 컬럼은 ④검수 화면에서 드롭다운으로 수동 지정 (그 선택을 사전에 학습시켜 다음부터 자동)

**트랙 B — 자유 서식 (LLM 추출)**: 보고서 문장, 메일 등 표가 아닌 문서용.
- 프롬프트에 스키마(20컬럼)와 **taxonomy 열거값을 제약으로 명시**해 rows JSON 만 출력하게 강제
- 출력을 그대로 ③검증에 넣음 — 검증을 통과 못 하면 애초에 적재 불가이므로 LLM 환각이 DB 를 오염 못 함
- **주의**: 사내 문서를 외부 LLM 에 보내면 안 되므로 사내/승인된 endpoint 전제 (03 문서 R-2 의 LLM 배선과 동일 인프라 재사용 가능)

### ③ 검증 — 있는 것 재사용

- `ai_extract.validate_rows` (필수 6필드: product_name/product_type/family_product/item_name/value_type/bin)
- product_type↔family_product 조합은 엔진 taxonomy 가 강제 검증 (불일치 시 에러 — 조용히 잘못 들어가지 않음)
- outcome action/result 도 어휘 검증됨 (`validate_outcome`)

### ④ 검수 — 사람이 마지막으로 보는 얇은 화면

- 추출된 rows 를 표로 보여주고: 오류 행 빨간 표시 / 셀 직접 수정 / family_product 확인
  (⚠ family_product 는 **eval taxonomy 정본 어휘**를 쓴다 — 현장 표기와 다른 값이 있음:
  IF, Contactless, SECU_ETC, PDDI_IT 등. 매핑 표를 화면에 상시 노출 권장)
- `label_quality` 태깅: 이 경로로 넣는 데이터에 `imported`/`legacy` 같은 표시를 남겨
  web_report 실사용 코멘트(manual)와 구분 — 나중에 품질별 필터링 가능
- "이 배치 전체 미리보기 OK" → ⑤로

### ⑤ 적재 — 있는 것 재사용 + 안전장치 1개

- `import_csv.import_rows`(또는 rows_to_csv 후 import_csv CLI) 로 적재 — case_id 자연키 멱등이라
  같은 문서를 두 번 넣어도 중복이 없다
- **안전장치(중요)**: [import_csv.py:107-108](../../eval_analyzer/db_input/import_csv.py#L107-L108) 이
  실행 중 `config.DB_PATH` 를 덮어쓰는 **단발 스크립트 전제** 코드다. 서버 프로세스 안에서 직접
  import 해 부르면 이후 eval 관련 동작이 엉뚱한 DB 를 볼 수 있다 →
  **적재는 반드시 별도 프로세스(subprocess)로 실행**할 것. (또는 검증까지만 서버에서 하고,
  적재는 기존 CLI/bat 그대로 사용)

---

## 3. 어디에 만들 것인가 — 배치 옵션

| 옵션 | 장점 | 단점 | 판단 |
|---|---|---|---|
| **(A) 독립 로컬 도구** (예: `tools/precedent_import/` — CLI 또는 간단한 로컬 웹페이지) | config 덮어쓰기 문제 원천 회피(단발 프로세스), 서버 무영향, 빨리 만듦 | 담당자 PC 에서만 | **1단계 권장** — 초기 대량 적재는 어차피 소수 담당자의 일 |
| (B) admin 패널 탭 ("선례 적재") | 접근 쉬움, 검수 UI 를 admin 에 통합, E-5(검수 대시보드)와 시너지 | 적재 subprocess 격리 필요, 서버에 LLM 배선 필요 | **2단계** — A 로 검증된 파이프라인을 옮겨 심기 |

두 경우 모두 eval_engine/db_input import 는 최소화하고, 가능하면 **rows JSON → 기존 CLI 호출**
형태로 경계를 유지한다 (import 2곳 규약은 web_report 패키지에 적용되는 규약이지만, 정신은 동일하게 —
새 접점을 만들면 docs/13 에 접점 목록을 갱신하고 합의할 것).

---

## 4. 무엇부터 넣을 것인가 — 콜드스타트 적재 우선순위

선례검색이 실제로 소비하는 필드를 기준으로 하면 우선순위가 명확해진다
(검색 스코프 = value_type + family_product + item 이름 유사도, 인용 대상 = human_comment):

| 우선 | 데이터 | 이유 |
|---|---|---|
| 1 | **human_comment + root_cause + outcome 이 있는 과거 사례** | 선례 인용의 원료 그 자체. outcome(조치→결과)은 향후 "무엇이 실제로 먹혔나" 통계의 재료 |
| 2 | 위 사례의 **item_name 표기 변형들** | difflib 유사도 0.70 을 넘기려면 이름이 비슷해야 함 — 같은 item 의 옛 표기를 함께 넣거나 alias 로 등록해야 검색이 붙는다 |
| 3 | USL/LSL/average/stdev 가 있는 행 | cpk 가 계산되어 raw_metrics 도 채워짐 (통계 맥락 강화) |
| 4 | human_status 만 있는 행 | 선례 인용에는 안 쓰이지만 R-4(룰 성적표)의 정답지가 됨 |
| — | 코멘트 없는 fail 목록 단순 나열 | 가치 낮음 — 선례검색이 human_comment 없는 행을 후순위로 미룸. 굳이 초기 적재에 넣을 필요 없음 |

**item 이름 정규화 주의**: 표기 편차가 크면 `item_alias.yaml`(동결 폴더) 보강이 필요하다.
운영에서 alias 를 자주 추가해야 한다면 — ①외부 담당자에게 추가 요청 프로세스를 정하거나
②`EVAL_RULES_DIR` 분리(04 문서 E-7)로 서버 소유 rules 사본에서 관리하는 방안을 협의.

---

## 5. 단계별 구현 로드맵 (제안)

| 버전 | 내용 | LLM | 기대 효과 |
|---|---|---|---|
| **v0 (빠른 승리)** | 로컬 도구: 엑셀 복붙(탭 구분 표) → 헤더 시노님 매핑 → validate_rows → 콘솔/HTML 미리보기 → 기존 CLI 적재 | 불필요 | 기존 정리본 문서의 대량 적재 즉시 가능 — **콜드스타트 해소의 최단 경로** |
| v1 | 자유 서식 트랙 추가: LLM 추출(rows JSON 강제) + 실패 행만 수동 보정 | 사내 endpoint | 보고서·메일 등 비정형 문서까지 커버 |
| v2 | admin 패널 통합: "선례 적재" 탭(업로드→검수→적재 subprocess), E-5 검수 대시보드와 연결 | — | 담당자 아닌 사용자도 적재 가능, 품질 관리 일원화 |

**v0 를 강조하는 이유**: "기존에 평가했던 전체 문서"가 대부분 표 형태라면, LLM 없이
헤더 매핑 + 검증 + 멱등 적재만으로 목표의 대부분이 달성된다. R-3(LLM 추출기) 합의를
기다릴 필요 없이 지금 시작할 수 있는 부분이다.

---

## 6. 이 경로가 만드는 가치 (02 문서와의 연결)

- 초기 선례가 채워지면 → **AI Comment 의 "(과거 사례 N건: …)" 인용이 첫날부터 동작**
  (빈 DB 로는 룰 골격 문장만 나온다)
- human_status/root_cause 를 함께 넣으면 → R-4 성적표·calibrate 검증의 정답지가 즉시 확보
  (web_report 라벨 UI(E-1)가 쌓아줄 데이터를 과거분으로 선납하는 셈)
- 단, R-1(선례 인용 상한)이 없는 상태에서 같은 item 계열 선례를 수백 건 넣으면
  코멘트 폭발(02 문서 리스크 ⑤)이 먼저 온다 — **대량 적재 전에 R-1 을 요청해 두는 것이 순서상 안전**
