# 17. eval 학습 루프 — L1/L2 적재와 case grain 재정의 (설계·로드맵)

> 2026-08-04 작성. [13](13_eval_analyzer_integration.md) 이 "무엇을 어떻게 연결했나"(현행
> 규약)라면, 이 문서는 **"무엇이 비어 있고 어떤 순서로 채우나"**(로드맵)다.
> **이 문서 시점에 구현된 코드는 없다.** 결정과 근거만 못 박아 둔 것이다.

---

## 0. 확정된 결정 (2026-08-04)

| 항목 | 결정 |
|---|---|
| L1/L2 DB 적재 | **한다** |
| 적재 방식 | 업로드 시점 **전용 실행 1회**, 전 web_report 세션 (§4). AI Comment 옵션과 무관 |
| 대상 DB | `REPORT_EVAL_DB_PATH`(report_server 소유). 엔진 소유 `eval.db` 는 계속 무기록 |
| case grain | **item × unit** — `bin` 은 case 키에서 제외 |
| bin 정보 | 대표 bin(최다 fail)만 `fail_case.bin` 에 참고용 보존 |
| 기존 누적 데이터 | Phase 0 에서 규모 실측 후 결정 |
| 사람 피드백 경로 | 관리자 `/pe/eval` 트레이스 정답라벨 **하나만** |
| 임계값 자동보정(`calibrate`) | **보류** — 이번에 하지 않는다 |
| web_report O/X 버튼 | 하지 않는다 |

---

## 1. 진단 — 판단 근거 데이터가 한 건도 안 쌓인다

### 1-1. L1/L2 는 "스키마에는 있고 데이터는 0행"

| 층 | 테이블 | grain | 현재 운영 |
|---|---|---|---|
| L1 metrics | `raw_metrics` | (case_id, run_id) | **0행** |
| L2 features | `features` | (case_id, run_id, engine_version) | **0행** |
| L3 signatures | — (별도 테이블 없음) | — | 발화분만 L4 child 로 |
| L4 status | `evaluation` + `case_signature` + `eval_evidence` | (case,run,engine,model) | 관리자가 라벨 단 것만 |
| L5 recommend | `evaluation.comment` + `eval_precedent` | — | 위와 동일 |

원인은 한 줄이다 — [web_report/ai_comment.py:210](../web_report/ai_comment.py#L210)

```python
result = evaluate({"meta": meta, "raw_df": raw_df}, persist=False)
```

`persist=False` 면 [ingest.py:430](../eval_analyzer/eval_engine/pipeline/ingest.py#L430)
이 **DB 파일 자체를 열지 않는다.** 콜드 빌드마다 L1/L2 를 전부 계산해 놓고
([api.py:52-53](../eval_analyzer/eval_engine/api.py#L52-L53)) 코멘트 문자열 한 줄만
남기고 버리는 중이다.

### 1-2. 지금 eval DB 에 쌓이는 것 = 사람 텍스트뿐

| 경로 | 쓰는 것 | labeler | 채점(`scoring`)에 잡히나 |
|---|---|---|---|
| `eval_export.export_session_comments` | PTE/개발 코멘트 병합 1행 | `web_report` | ❌ `eval_id=NULL` 이라 join 탈락 |
| `eval_export.save_human_label` (`/pe/eval`) | `evaluation` + `label` **쌍** | `eval-panel` | ✅ 유일하게 채점됨 |
| `db_input/import_csv.py` | 과거사례 CSV | `db_input` | ❌ |

**"사람이 뭐라 썼나"는 쌓이는데 "엔진이 그때 뭐라 판단했나"가 안 쌓인다.**
채점 표본은 관리자가 트레이스에서 직접 클릭한 것뿐이다.

### 1-3. AI Comment 원문은 어디에도 없다

AI Comment 컬럼은 콜드 빌드 payload 캐시 안에만 존재한다. 캐시가 무효화되면
사용자가 본 문장은 영구히 사라진다. `evaluation.comment` 에 남는 것은 관리자가
라벨을 단 시점의 **트레이스 스냅샷**이지, 사용자가 본 셀 텍스트가 아니다.

### 1-4. 트레이스는 휘발성이다

`trace_store` 는 프로세스 메모리 LRU **4런 / 30분**. 서버 재시작이나 30분 경과면
어제 본 케이스에 오늘 라벨을 달 수 없고 매번 세션을 다시 트레이스해야 한다.
**관리자 라벨링을 유일한 피드백 경로로 삼은 이상 이게 실질적 병목이다.**

### 1-5. ⚠ AI Comment 옵션은 지금 꺼져 있다 — 적재 설계를 좌우한다

[client/honey_main.py:637](../client/honey_main.py#L637) 에서 AI Comment 체크박스는
`setEnabled(False)` 다(라벨 10회 클릭 숨김 해제, :488-489). 즉 실제 업로드되는 세션
대부분은 `manifest.options.ai_comment` 가 꺼져 있고, **`evaluate()` 가 아예 호출되지
않는다.**

따라서 **"AI Comment 콜드 빌드에 편승해 적재한다"는 설계는 성립하지 않는다** —
수집량이 사실상 0 이 된다. §4 가 이 제약에서 출발한다.

---

## 2. 왜 L1/L2 를 쌓아야 하는가 — 자동보정을 안 하더라도

자동 임계값 보정([calibrate.py](../eval_analyzer/eval_engine/calibrate.py))은 이번에
하지 않기로 했다. 그래도 적재해야 하는 이유는 세 가지다.

1. **소급이 불가능하다 — 지금 안 쌓으면 그 데이터는 영영 없다.**
   불변 규칙(per-DUT raw 미저장) 때문에 feature 는 **forward-only** 다. 나중에
   필요해져도 과거 세션에서 다시 뽑을 방법이 없다 — `dist_digest` 가 바로 그
   "raw 폐기해도 feature 소급 재계산" 용도로
   [DB_SCHEMA §11](../eval_analyzer/docs/DB_SCHEMA.md) 에 보류돼 있는 것이 증거다.
   자동보정을 켜고 싶어지는 시점에 표본이 0이면 다시 1년을 기다려야 한다.

2. **"임계값을 X→Y 로 바꾸면 과거 몇 건이 뒤집히나"를 SQL 로 볼 수 있다.**
   임계값은 앞으로도 사람이 손으로 고칠 것이므로 그 판단 근거가 필요하다. 지금은
   하나 만질 때마다 세션을 다시 트레이스해 눈으로 봐야 하고 그마저 30분이면
   사라진다. `features` 테이블 하나면 과거 전 세션 what-if 가 즉시 나온다.
   [tools/eval_golden](../tools/eval_golden/golden_check.py) 골든셋의 회귀 대상도
   사람이 적은 몇 줄에서 전수로 넓어진다.

3. **사후 라벨링이 가능해진다.** §1-4 의 휘발성이 사라져야 채점 표본이 수십 건 →
   수백 건이 된다. 선택한 피드백 경로가 실효를 가지려면 이게 전제다.

**비용은 사실상 없다.** L1/L2 는 **이미 매 콜드 빌드마다 계산되고 있다.** 추가 계산
0, 늘어나는 것은 비동기 DB write 뿐이다.

**규칙 위반이 아니다.** "raw 저장 금지"는 per-DUT 측정값 이야기이고, L1/L2 는 그
규칙이 명시적으로 "이것만 저장하라"고 지목한 요약값이다.

---

## 3. case grain 재정의 — item × unit

### 3-1. 왜 bin 을 빼는 것이 맞나

L1(cpk/mean/stdev/spread)과 L2(분포·공간 feature)는 **item 축에서 계산되는 값**이라
bin 과 무관하다. 지금처럼 bin 별로 case 를 쪼개면 **같은 통계값이 bin 수만큼 중복
저장된다.** bin 에 의존하는 것은 `fail_count`/`yield` 뿐이다. 선례 검색도 이미
bin 을 매칭 조건에서 뺐다(커밋 4166cb1).

### 3-2. 새 키

```
현재:  case_id = sha256(product_name | lot_id | wafer_number | item_id | bin | revision)
       item_master UNIQUE(item_canonical)
       item_alias  PRIMARY KEY(raw_name)
       item_class  = category_major | value_type | bin

변경:  case_id = sha256(product_name | lot_id | NULL | item_id | NULL | revision)
       item_master UNIQUE(item_canonical, value_type)     ← ★ 스키마 변경
       item_alias  PRIMARY KEY(raw_name, value_type)      ← ★ 스키마 변경 (§3-4)
       item_class  = category_major | value_type          ← bin 자리 제거
       fail_case.bin = 대표 bin(최다 fail) — 참고용, 키 아님
```

`wafer_number` 를 `NULL` 로 두는 것은 신규 규칙이 아니라 **기존 `eval_export` 에
맞추는 것**이다(코멘트·라벨이 이미 lot 수준). 이래야 엔진 판정과 사람 라벨이 같은
`case_id` 로 join 된다.

### 3-3. unit = 원문이 아니라 `value_type` 을 쓴다

`item_master` 에는 `unit`(원문: VOLTS, mV, HERTZ…)과 `value_type`(어휘:
V/A/Hz/CODE/PF/Ohm/Sec)이 둘 다 있다. **키에는 `value_type` 을 쓴다.**

- 룰 스코프(`item_class`)와 선례 검색 하드필터가 이미 `value_type` 을 쓴다.
- 원문 `unit` 은 표기 흔들림(VOLTS/VOLT/V/mV)이 심해 키로 쓰면 **같은 물리량이
  쪼개진다.** mV 와 V 를 별개 item 으로 볼 실무적 이유가 없다.
- [13 §9](13_eval_analyzer_integration.md) 에 이미 unit→value_type 보정 이슈가
  기록돼 있다(엔진 `UNIT_TO_VALUE_TYPE` 은 정확매칭 표라 `VOLTS` 를 놓친다).
- 엔진 자신도 `unit` 은 판정에 쓰지 않는다고 명시한다
  ([ingest.py:191-192](../eval_analyzer/eval_engine/pipeline/ingest.py#L191-L192)) —
  "`unit` 은 value_type 이 왜 그렇게 나왔는지 되짚기 위한 진단용 원문이다."

### 3-4. ⚠ 스키마 변경은 **2개 테이블**이다 — 별도 승인 필요

설계 검토 중 확인된 사항: `item_master` UNIQUE 만 바꾸면 **동작하지 않는다.**

[store.py:298](../eval_analyzer/eval_engine/store.py#L298) 의 `resolve_item_id` 는
`item_canonical` 이 아니라 **`item_alias.raw_name`** 으로 item_id 를 찾는데, 이
테이블의 PK 가 `raw_name` 단일이다. 같은 원본 item 명이 두 unit 으로 들어오면
**두 번째 item 을 만들 방법이 없다.** 따라서 `item_alias` PK 도
`(raw_name, value_type)` 으로 확장해야 item × unit grain 이 실제로 성립한다.

SQLite 는 UNIQUE/PK 제약을 `ALTER` 로 못 바꾼다 → **두 테이블 모두 재생성**
(새 테이블 → 복사 → rename)이 필요하고, `SCHEMA_VERSION` 6 → 7 +
`_MIGRATIONS` 에 `_migrate_v6_to_v7` 추가가 따라온다.

> 이 repo 의 규칙([CLAUDE.md](../CLAUDE.md) §5-8,
> [eval_analyzer/CLAUDE.md](../eval_analyzer/CLAUDE.md) 규칙 2)상
> **eval.db 스키마 변경은 사용자 사전 승인 대상**이다. 방향은 승인됐으나
> 실제 DDL 변경은 착수 직전에 영향 범위를 다시 설명하고 승인받는다.

**영향을 받는 코드 전수** (`upsert_item_master` / `resolve_item_id` 호출부):

| 파일 | 위치 | 성격 |
|---|---|---|
| `eval_engine/store.py` | :298 `resolve_item_id`, :305 `upsert_item_master`, :15 `SCHEMA_VERSION`, `_MIGRATIONS` | 정본 |
| `eval_engine/pipeline/ingest.py` | :207 `_resolve_item_identity` (이미 `value_type` 을 인자로 받고 있다 — 전달만 하면 된다), :198 `item_class` 조립 | 엔진 런타임 |
| `eval_engine/cli.py` | :159 | 시드 CLI |
| `web_report/eval_export.py` | :305, :424, :426 | 서버 (코멘트 export + `save_human_label`) |
| `eval_analyzer/db_input/import_csv.py` | :315 | 과거사례 적재기 |
| `eval_analyzer/tools/seed_demo_precedents.py` | :74 | 데모 시드 |
| `eval_analyzer/chatbot_prototype/test_smoke.py` | :30 | 스모크 (보류된 프로토타입 — 일반 pytest 수집 대상 아님) |
| 테스트 | `eval_analyzer/tests/test_store.py`(:19,:34,:39,:129,:183) · `test_calibrate.py`(:28,:96) · `test_e2e.py`(:83) · `tests/test_eval_admin_labels.py`(:52) · `tests/test_eval_unit_group.py`(:53) | 회귀 |

기존 데이터 관점에서는 **안전한 확장**이다 — 현재 `item_canonical` 이 이미
유니크하므로 `(item_canonical, value_type)` 으로 옮겨도 충돌이 없다.
`item_alias` 도 마찬가지다.

### 3-5. `item_class` 를 어떻게 저장할까

`item_class` 는 [ingest.py:198](../eval_analyzer/eval_engine/pipeline/ingest.py#L198)
에서 `f"{cat}|{value_type}|{bin_}"` 로 **엔진이 조립**한다.

권장: **적재 경로에서 `category_major|value_type` 2단으로 다시 써서 저장**하고
엔진 런타임(`_rules.thresholds_for`)은 건드리지 않는다. 대표 bin 을 item_class 에
박으면 같은 item 이 세션마다 대표 bin 이 달라져 스코프 키가 흔들린다. 지금
`thresholds.yaml` 의 `item_class: {}` 가 비어 있어 어느 쪽이든 실효는 없지만
(전부 default 폴백), 앞으로를 위해 안정된 키가 낫다.

---

## 4. 어떻게 적재할 것인가

### 4-1. 두 후보

§1-5 때문에 "콜드 빌드에 편승" 은 탈락이다. 남는 후보는 둘이다.

| | (A) 콜드 빌드 편승 | **(B) 업로드 시점 1회 전용 실행** ← 채택 |
|---|---|---|
| 실행 지점 | `ai_comment.build_ai_comments` 결과 재활용 | [web_report/ingest.py:263](../web_report/ingest.py#L263) `export_async` 훅 옆 |
| 커버리지 | **ai_comment 옵션 세션만 = 현재 사실상 0** | **전 web_report 세션** |
| 파이프라인 실행 | 0회 추가 | 1회 추가 (옵션이 꺼져 있으니 실제로는 **유일한** 실행) |
| L1/L2 확보 | `to_result` 확장 필요(안 내려줌 — §4-3) | 불필요 — `present.persist` 가 이미 전부 쓴다 |
| 빈도 | 콜드 빌드마다 | 세션당 1회 |

(B) 가 커버리지·구현량 양쪽에서 낫다. **채택.**

### 4-2. (B) 의 구체 형태

`evaluate(..., persist=True)` 를 **report_server 소유 DB(`REPORT_EVAL_DB_PATH`)**
대상으로 업로드 직후 비동기 1회 실행한다.

- **소유권 원칙은 불변**: eval_analyzer 소유 `eval.db`(`EVAL_DB_PATH`)에는 여전히
  아무것도 쓰지 않는다. 쓰는 대상은 report_server 소유 파일뿐이다.
  → [13 §4](13_eval_analyzer_integration.md) 는 "서버는 persist=False" 라고만
  적혀 있으므로 **이 문서와 함께 개정**해야 한다.
- **DB 지정은 파라미터로 한다 — `config.DB_PATH` 전역 대입 금지.**
  `present.persist` 는 `store.get_conn()`(= `config.DB_PATH`)을 직접 연다
  ([present.py:40](../eval_analyzer/eval_engine/pipeline/present.py#L40)). 이걸
  전역 대입으로 돌리는 방식은 [13 §10](13_eval_analyzer_integration.md) 이
  **subprocess 를 쓰는 이유로 명시한 바로 그 위험**(장수명 Flask 프로세스 오염)이다.
  엔진 동결이 풀렸으므로(2026-08-03) `evaluate`/`persist` 에 db 경로(또는 conn
  factory) 인자를 추가하는 정공법을 쓴다.
- **동시 쓰기는 기존 큐로 직렬화**: `eval_export` 의 단일 소비자 큐 + 데몬 스레드에
  합류시킨다. `evaluate` 내부의 ThreadPoolExecutor 동시 쓰기
  ([api.py:49-50](../eval_analyzer/eval_engine/api.py#L49-L50))도 같은 파일을 쓰는
  `eval_export` 와 겹치지 않게 된다.
- **실패 격리**: `safe_export` 와 같은 패턴 — 실패해도 업로드·조회에 무영향, 감사
  로그만 남긴다.
- **`ingest_run` 증식 없음**: 세션당 1회 실행이고 `_find_run_id` 로 재사용한다.

> 참고: `code_report/claude/prompts/P4_features_축적.md` 에 같은 목표의 구현
> 프롬프트가 이미 있다. 다만 그 문서는 **엔진 동결 시절**에 작성돼
> "`eval_analyzer/` 무수정" 을 전제로 `config.DB_PATH` 를 런타임 대입하는 방식을
> 택했다. 동결이 풀린 지금은 위의 파라미터 방식이 맞으므로, P4 를 그대로 실행하지
> 말고 이 문서 기준으로 갱신해서 쓸 것.

### 4-3. 참고 — `to_result` 는 L1/L2 를 안 내려준다

(A) 를 재검토할 일이 생길 때를 위해 기록한다.
[present.py:62-95](../eval_analyzer/eval_engine/pipeline/present.py#L62-L95) `to_result`
가 돌려주는 키는 case_id / item_* / bin / issue_category / status / signature /
confidence / comment / evidence / precedents 뿐이다 — **`raw_metrics`(L1)도
`features`(L2)도 없다.** (A) 를 택했다면 `to_result` 확장이 선행돼야 했다.
(B) 는 `present.persist` 가 L1/L2 를 직접 받으므로 이 문제가 없다.

### 4-4. 저장 게이트를 통과한 case 만 쌓인다

[api.py:56-57](../eval_analyzer/eval_engine/api.py#L56-L57) 의 `should_store` 를 못
넘은 case 는 `cases[]` 에 아예 없다. 따라서 스냅샷도 게이트 통과분만 담는다.
게이트는 `yield fail ∪ cpk<cpk_warn ∪ signature 발화`
([13 §12](13_eval_analyzer_integration.md) 에서 확장됨)라 사실상 "볼 만한 것"은
전부 들어온다. **"모든 후보 item 이 쌓인다"고 가정하지 말 것.**

### 4-5. 무엇을 쌓고 무엇을 안 쌓나

| 쌓는다 | 안 쌓는다 |
|---|---|
| `fail_case` / `run_case` (세션 역참조) | per-DUT 측정값 (불변 규칙) |
| `raw_metrics` (L1) | `signatures.applies` 트레이스 맵 |
| `features` (L2) | `reason_codes` |
| `evaluation` — status/confidence/completeness/comment | `dist` (트레이스 표시 전용) |
| `case_signature` (primary/secondary) | |
| `eval_evidence` | |
| `eval_precedent` (L5 가 참조한 선례 이력) | |

`ingest_run` 은 `eval_export._find_run_id` 와 같이 **세션당 1행 재사용**해 증식을 막는다.

소스가 여럿일 때 같은 item 이 중복되면 `ai_comment._rank` 가 이미 쓰는 규칙
(severity 최고, 동률이면 SUBPOP_GAP 발화 쪽)으로 하나만 남긴다.

**§1-3 (AI Comment 원문)은 (B) 로는 해결되지 않는다.** `evaluation.comment` 에
남는 것은 이 전용 실행이 만든 엔진 코멘트이지, 사용자가 IssueTable 셀에서 본
텍스트(`[STATUS][이봉] ...`)가 아니다 — 애초에 옵션이 꺼져 있으면 셀 자체가 없다.
옵션을 켠 세션에 한해 셀 원문을 별도로 남기는 것은 web_report O/X 피드백을
붙일 때 함께 다룬다(§5 보류 항목).

### 4-6. 알려진 공백 2건 (이번엔 손대지 않음)

- `features.shot_fail_ratio` — DDL 에는 있으나 `save_features` 컬럼 목록에 빠져
  있고 계산 경로도 없어 **항상 NULL**. 잔재.
- `value_gap_ratio` / `value_gap_minor_mass` — L2 가 계산하지만 의도적 미저장.
  SUBPOP_GAP `separated` 판정의 실제 기준값이라 **채점하려면 있어야 한다.**
  추가는 스키마 변경이므로 §3-4 승인에 묶어 함께 판단한다.

---

## 5. 로드맵

### Phase 0 — 측정 (코드 변경 전, 운영 서버에서)
- ~~`to_result` 가 features 를 내려주는지 확인~~ → **완료. 안 내려준다(§4-3).
  (B) 채택으로 무관해졌다.**
- 운영 세션 1건을 `/pe/eval` 트레이스로 돌려 ① **게이트 통과 case 행 수**
  ② **트레이스 소요시간**(= evaluate 1회 실행 비용의 대용치) 실측.
  → 용량 추정과 "업로드 후 1회 추가 실행" 부담을 숫자로 확정한다.
- 운영 `REPORT_EVAL_DB_PATH` 의 파일 크기 · `label` 행 수 · `fail_case` 행 수 확인
  → **기존 데이터를 마이그레이션할지 재입력할지 여기서 결정한다.**
  - 수십 행 → 재입력이 빠르고 안전
  - 수백~천 행 → `(product, lot, item, revision)` 로 묶어 case_id 재계산 UPDATE +
    같은 item 이 bin 별로 쪼개졌던 label 병합. **백업 필수.**

### Phase 1 — 스키마 변경 (승인 후)
- `item_master` UNIQUE → `(item_canonical, value_type)`,
  `item_alias` PK → `(raw_name, value_type)`,
  `SCHEMA_VERSION` 6→7, `_migrate_v6_to_v7` 추가 (§3-4 영향표 전체).
- 검증: 기존 eval DB **사본**에 마이그레이션 적용 → 행 수 보존,
  `resolve_item_id` 정상, `eval_analyzer/tests/test_store.py` 통과.

### Phase 2 — L1/L2 적재 (핵심)
- `evaluate`/`present.persist` 에 **db 경로(또는 conn factory) 인자 추가** —
  `config.DB_PATH` 전역 대입 금지 (§4-2).
- `case_id` 산출 인자에서 bin·wafer 제거 (§3-2). `make_case_id` **함수는 그대로**
  두고 호출부만 바꾼다 — [ingest.py:282/348/387](../eval_analyzer/eval_engine/pipeline/ingest.py#L282) ·
  `cli.py:166` · `eval_export.py:315/431`.
- `item_class` 를 2단으로 (§3-5).
- `web_report/ingest.py` 의 `export_async` 훅 옆에서 세션당 1회 비동기 실행,
  **같은 단일 소비자 큐에 합류**시켜 직렬화 (§4-2).
- [13 §4](13_eval_analyzer_integration.md) 개정 — "서버는 persist=False" → "엔진 소유
  eval.db 는 무기록, report_server 소유 DB 에는 업로드 시 1회 적재".
- 검증: 세션 1건 업로드 → `features` 행 수 = 트레이스 게이트 통과 case 수
  (bin 합쳐진 수), `case_id` 가 같은 세션의 `label` 과 실제로 join 됨,
  `eval_analyzer/data/eval.db` 는 **생성조차 되지 않음**.

### Phase 3 — `eval_export` 를 같은 case 규칙으로 정렬
- 코멘트 export 와 `save_human_label` 이 새 case_id 규칙을 쓰도록.
- Phase 0 결정에 따라 기존 데이터 마이그레이션 또는 재입력.
- 검증: 채점 탭 `agree_rate` 가 0 이 아닌 값으로 나옴(= 짝이 맞기 시작).

### Phase 4 — 사후 라벨링 화면
- `/pe/eval` 에 **DB 기반 케이스 목록**(세션/기간/status/signature 필터) 추가 →
  30분 TTL 트레이스에 묶이지 않고 라벨링.
- 기존 `save_human_label` 재사용(이미 `evaluation`+`label` 쌍을 쓴다).
- 검증: 재트레이스 없이 채점 표본이 늘어남.

### 보류 (범위 밖)
- **임계값 자동보정** `calibrate.recalibrate()` — 사용자 결정으로 제외.
  단 Phase 2 가 그 연료(`features`)를 쌓아두므로 나중에 켜는 것은 버튼 하나 문제가
  된다. (참고: `thresholds.yaml` 의 `calibration.quantiles` 스펙과 `item_class:` 섹션은
  이미 준비돼 있고, `item_class: {}` 가 비어 있는 것이 "한 번도 안 돌았다"는 증거다.)
- comment 채굴(`calibrate` 후속 2번), `precedent_client._rag_search`(현재 스텁).
- **web_report O/X 피드백** — 설계안은
  `code_report/claude/prompts/P3_AI코멘트_피드백.md` 에 이미 있다. 다만 **전제 2개가
  아직 없다**: ① AI Comment 옵션이 켜져 있어야 하고(§1-5) ② 사용자가 본 셀 원문이
  저장돼 있어야 한다(§4-5). Phase 2 는 이 둘을 만들어 주지 않는다.
  붙이게 되면 `label.engine_comment_accepted`(현재 하드코딩 `0`)를 채운다.
- ML 모델 학습 — 규모·필요 모두 시기상조.

---

## 6. "학습" 의 3층 — 무엇이 자동이고 무엇이 사람 루프인가

혼동을 막기 위한 정리다.

| 층 | 무엇이 바뀌나 | 필요한 데이터 | 자동/수동 | 현재 |
|---|---|---|---|---|
| A. 임계값 자동보정 | `thresholds.yaml item_class` | `features`(L2) | **자동** | 연료 없음 · 이번엔 보류 |
| B. 룰 정확도 채점 → 사람이 튜닝 | signature on/off, 임계값 | `evaluation`+`label` 쌍 | 반자동 | 화면은 있음([13 §11](13_eval_analyzer_integration.md) 탭 6), 표본 없음 |
| C. 선례 RAG 코멘트 | 코멘트 문장 품질 | `label.human_comment` | 자동(검색) | **이미 동작 중** |

진짜 "스스로"는 **A 하나뿐**이고, B 는 사람이 판단해 룰을 고치는 루프, C 는 이미
돌고 있다. 이번 로드맵은 **B 를 실효화하고 A 의 연료를 미리 쌓아두는 것**이다.
현 규모에서 ML 모델 학습은 시기상조이며 필요도 없다.
