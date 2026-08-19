# 13. eval_analyzer 통합 — AI Comment (단방향 의존 규약)

> 2026-07-12 흡수 통합. 이 문서는 한 달 이상 유지될 **굵직한 규약만** 담는다 —
> 엔진 내부 구현·알고리즘·스키마의 정본은 [eval_analyzer/docs/](../eval_analyzer/docs/) 다.

---

## 1. 배치·소유권

- `eval_analyzer/` 는 반도체 fail-item 평가 엔진이다 — `evaluate()` 하나로 L0~L6 판단
  파이프라인을 돌려 status/comment 를 반환한다.
- **2026-08-03 이 repo 가 원본으로 승격**됐다. 외부 사본 `F:\COINAPI\eval_analyzer` 는
  더 이상 참조·동기화 대상이 아니며, 하위 파일은 자유 수정이다
  (정본 [15_ownership.md](15_ownership.md)). 유지되는 제약은 **의존 방향 하나**뿐 —
  eval_analyzer 는 report_server 를 import 하지 않는다.
  - **단, eval.db 스키마(`eval_engine/store.py` DDL·컬럼) 변경은 사전 승인 대상**이다.
    운영 eval.db 에 누적 데이터가 있어 바꾸기 전에 영향을 설명해야 한다.
  - `/pe/eval` 패널을 위해 들어간 엔진 변경(§11): `pipeline/_rules.py`(캐시 키에 mtime
    포함 → 재시작 없이 yaml 반영 + family 오버레이 트리 병합 + `reload_rules`),
    `pipeline/signatures.py`(`enabled:false` skip + `build_ctx_values` 순수 추출).
    회귀 테스트 `eval_analyzer/tests/test_rules_scope.py`.
- 최초 흡수 시 제외한 것: `.git/`, `__pycache__/`, `*.egg-info/`, `.claude/`·`.agents/`,
  런타임 db(`data/*.db`, `db_input/output/*.db`). 중첩 `.gitignore` 가 런타임 db 를 계속 차단한다.

## 2. 단방향 의존 — import 는 3곳만

```
report_server ──(web_report/ai_comment.py — evaluate 호출)──►  eval_analyzer(eval_engine)
report_server ──(web_report/eval_export.py — store·ingest 헬퍼)──►  eval_analyzer(eval_engine)
report_server ──(web_report/eval_debug.py — 룰 리로드·L0~L6 트레이스)──►  eval_analyzer(eval_engine)
              ◄──────────── 금지 ────────────────
```

- eval_analyzer 는 report_server 를 **import 하지 않는다** (eval_analyzer/CLAUDE.md 불변 규칙 1).
- report_server 에서 eval_engine 을 import 하는 곳은 **딱 3곳**이다:
  - [web_report/ai_comment.py](../web_report/ai_comment.py) — `evaluate()` 호출 (AI Comment, §3~§6)
  - [web_report/eval_export.py](../web_report/eval_export.py) — `store` CRUD + `pipeline.ingest`
    item 정규화 헬퍼 (사람 코멘트 export, §9)
  - [web_report/eval_debug.py](../web_report/eval_debug.py) — 룰 경로/리로드 + L0~L6 단계
    직접 호출 트레이스 (`/pe/eval` 관리자 패널, §11). 운영 조회 경로에서 쓰이는 것은
    `rules_rev()` 하나뿐이다(캐시 키).
  pip 미설치 — 세 모듈 다 `sys.path.append(<repo>/eval_analyzer)` + 지연 import 로 연결한다
  (append 라 report_server 쪽 top-level 이름이 항상 우선, 컴퓨트 워커에서도 호출 시점 성립).
- 다른 서버 코드가 eval_engine 이 필요하면 **위 세 모듈의 함수를 거친다**. 위반 = 리뷰 반려.
  `server/eval_panel/` 은 이 규약대로 eval_engine 을 직접 import 하지 않고 eval_debug 만 쓴다.
- **사설 API 핀** (엔진 변경 시 함께 확인): eval_debug 는 `signatures.build_ctx_values` /
  `signatures._eval_condition` / `signatures._HIGH_MOMENT_METRICS` / `_rules.thresholds_for` /
  `_rules.threshold_overlay_path` / `status.SPECIFICITY_ORDER` 에 의존한다
  (eval_export 가 `store._migrate` 에 의존하는 것과 같은 성격).

## 3. JSON 계약 요약 (정본: eval_analyzer/docs/)

호출 형태 (persist=False 고정 — §4):

```python
result = evaluate({"meta": meta, "raw_df": df}, persist=False)
```

**입력 meta** (필수 6필드 — [INTEGRATION_CONTRACT.md](../eval_analyzer/docs/INTEGRATION_CONTRACT.md)):
`product_name` / `product_type`(MDDI·PDDI·PMIC·SECURITY·TCON, 서버 enum 과 동일) /
`family_product`(product_type 별 허용표 강제, 불일치 ValueError) / `revision`(float) /
`lot_id` / `wafer_number`(int). 세션에 없는 필드의 서버측 폴백: `family_product=*_ETC`
(product_taxonomy.yaml 의 범용값), `revision` 변환 실패=0.0, `wafer_number`=소스 순번.

**입력 raw_df** (레이아웃 불변 — eval_analyzer/CLAUDE.md 규칙 7):
```
columns: SERIAL,SHOT,DUT,XPOS,YPOS,BIN,FAILTNO, <item...>   (meta 7 + item)
row0 TSEQ  row1 TNO  row2 STEP  row3 UNIT  row4 HILIM(USL)  row5 LOLIM(LSL)  row6+ 측정
```
서버는 HoneyformTable(`data` + tseq/tno/step/units/hilim/lolim dict)에서 이 레이아웃을
재조립한다. **함정**: 엔진 `_is_num` 은 파이썬 int/float 만 인정 — item 블록을
`astype("float64")` 로 강제해야 np.int64 정수 컬럼이 무시되지 않는다 (ai_comment.py 가 수행).

**출력 RunResult.cases[]** ([EVALUATE_RETURN_SPEC.md](../eval_analyzer/docs/EVALUATE_RETURN_SPEC.md)):
서버가 소비하는 키는 `item_raw`(Issue Table join 키), `bin`(int, 1=PASS),
`status`(CRITICAL>MAJOR>MINOR>MONITOR>OK), `comment`(분석방향 한 문장). cases 는
게이팅(yield fail ∪ cpk<cpk_warn) 통과분만 반환된다.

## 4. eval.db 소유권 — 엔진 소유 DB 는 무기록, 서버 소유 DB 에는 업로드 시 1회 적재

**2026-08-10 개정.** 종전엔 "서버는 어디에도 persist 하지 않는다" 였는데, 그 결과
L1/L2/evaluation 이 영구 0행이라 룰 채점·표본 검수의 재료가 통째로 없었다. 소유권 원칙은
그대로 두고 **쓰는 대상만** report_server 소유 파일로 갈랐다.

| 경로 | 대상 DB | persist | 언제 |
|---|---|---|---|
| `ai_comment.build_ai_comments` (조회) | — | **False** | 콜드 빌드마다 |
| `eval_export.collect_session_snapshot` (수집) | `REPORT_EVAL_DB_PATH` | **True** | 업로드 직후 1회 |

- **엔진 소유 `eval.db`(`EVAL_DB_PATH`, `eval_analyzer/data/eval.db`)는 여전히 무기록**이며
  생성조차 되지 않는다. 회귀 가드는 [tests/test_eval_snapshot.py](../tests/test_eval_snapshot.py) (d).
- **DB 지정은 인자로 한다** — `evaluate(..., db_path=...)` → `ingest`/`present.persist` →
  `store.get_conn(db_path)`. `eval_engine.config.DB_PATH` **전역 대입 금지**(장수명 Flask
  프로세스 오염 — §10 이 subprocess 를 쓰는 이유와 같은 위험).
- **`generate_comment=False`** 로 부른다 → L5(선례검색 + `make_comment`)를 통째로 건너뛰어
  LLM·선례 조회 비용이 0 이다. 그 대신 `evaluation.comment` 는 NULL 이고
  `eval_precedent` 는 안 쌓인다(v1 의 의도된 축소 — 목표는 룰 정확도).
- **동시 쓰기**: `api.evaluate` 는 case 를 ThreadPoolExecutor 로 도는데, `persist=True` 면
  **워커를 1로 줄여** 직렬화한다(`workers = 1 if persist else _MAX_WORKERS`). 세션 간
  직렬화는 `eval_export` 의 단일 소비자 큐가 맡는다(수집 작업이 그 큐에 합류한다).
- **중복 방지**: `(session_id, source_file=eval-snapshot#<idx>, engine_version)` 에
  evaluation 이 이미 있으면 건너뛴다. `ingest_run.ingested_by='eval-snapshot'` 표식으로
  코멘트 export run 과 구분한다. `force=True` 재수집은 **지우지 않고 새 run 으로 쌓는다**
  — 기존 evaluation 을 지우면 거기 달린 사람 라벨까지 잃기 때문이다.
- **AI Comment 옵션과 무관**하게 전 web_report 세션에서 돈다(옵션이 꺼져 있어 콜드 빌드
  편승은 커버리지가 0 이 된다 — [17 §1-5](17_eval_learning_loop.md)). 기존 세션은 자동
  백필하지 않고 `/pe/eval` 표본함에서 세션을 지정해 수집한다.
- 실패는 `safe_collect_snapshot` 이 격리한다 — 업로드 응답·리포트 조회에 무영향, 감사
  로그(`action=eval_snapshot`)만 남는다.
- 선례검색(sql)은 DB 파일이 없으면 빈 목록을 반환하므로 이 DB 없이도 조회는 동작한다.
- §9 의 **사람 코멘트 export DB 는 이 규약과 별개** — eval_analyzer 소유 eval.db 가 아니라
  report_server 소유의 **별도 파일**(`REPORT_EVAL_DB_PATH`)이다. `EVAL_DB_PATH` 는 여전히
  건드리지 않으며 evaluate 의 선례검색 동작도 무변경이다.

## 5. 재계산·캐시 규약

- evaluate 호출 지점은 **콜드 빌드 1곳** — `service.load_webreport` 의 인라인/워커 빌드
  (`build_report_payload` 직전, `service._ai_comment_cached` 경유). 실측(2026-08-13)에서
  이 단계가 **콜드 빌드의 80%** 였으므로(5.9s 중 4.7s) 두 겹으로 분리했다
  ([docs/12 "AI Comment 비동기 분리"](12_web_report_cache.md)):
  - **분리 캐시**: 평가 결과 dict 를 `cache_policy.ai_comment_key`
    (`(akey, chash[, prep], mode, meta_digest[, rules_rev][, "evalfail"], aiver)` —
    **sid·edits_rev 불포함**)로 RAM+디스크에 저장한다. comment 편집(rev+1)·
    `REPORT_SCHEMA_VERSION` bump·dedup 형제 세션의 payload 재빌드는 캐시 히트로
    evaluate 를 건너뛴다. rawdata 편집(chash)·전처리(prep)·**세션 메타 PATCH**
    (meta_digest — `_session_meta` 필드의 digest, akey 는 메타 수정을 감지 못 하므로)·
    룰 편집(rules_rev)만 재평가를 강제한다. `safe_build` 예외 폴백(빈 결과)은
    캐시하지 않는다(일시 오류 영구화 방지 — `safe_build_ex` 가 성공/폴백 구분).
  - **비동기(pending)**: 분리 캐시 미스의 사용자 대기 콜드 빌드는 evaluate 를 돌리지
    않고 `ai_comment_pending` payload 로 리포트를 먼저 연다. evaluate 는 온디맨드
    `"ai"` 잡(`report_job(ai_inline=True)`, 워커 강제 오프로드)이 백그라운드로 돌리고,
    프리웜·ingest 경로는 종전처럼 동기다.
- payload 캐시 키(`cache_policy.report_key`)에는 `content_hash` 와 `webreport_options` 가
  들어 있어 **rawdata 편집 시 자동 재평가**되고, 옵션 on/off 세션은 캐시가 분리된다.
- **룰(threshold/signature) 편집도 재평가시킨다** (2026-08-03). `/pe/eval` 저장이
  `rules/.rules_rev` 를 +1 하고 `report_key` 와 `ai_comment_key` 가 그 값을 키에
  덧붙인다(`cache_policy._eval_rules_suffix` 공유). **ai_comment 옵션 세션에만** 덧붙고
  rev 파일이 없으면 아무것도 붙지 않으므로, 패널을 안 쓰는 서버·일반 세션의 기존 캐시는
  그대로 유효하다(`REPORT_SCHEMA_VERSION` 은 건드리지 않는다 — 그건 코드 배포용).
- 엔진 쪽 반영은 캐시 키에 파일 mtime 을 넣어 해결한다 — 웹 프로세스든 컴퓨트 워커든
  다음 호출에서 자동 재파싱이라 **프로세스 간 리로드 신호가 필요 없다**.
- evaluate 실패(메타 부적합·의존 미설치 등)는 `ai_comment.safe_build` 가 빈 dict 로
  격리한다 — **IssueTable 빌드는 절대 죽지 않는다** (컬럼은 뜨되 빈 값 + warning 로그).
- **엔진 내부 최적화 (2026-08-13)**: L0 ingest 의 item별 파싱 벡터화(빠른 경로 —
  `_FAST_NUM_TYPES` 만일 때, 판정은 `_is_num` 과 동치) + 같은 item 의 case(bin)들이
  배열·통계·좌표 전처리를 공유(`case["_shared"]` — metrics/features 의 `_memo`) +
  `ufunc.at`→`reduceat`·히스토그램 1회화 등. **완전 등가**가 계약이다 — 등가 픽스처로
  evaluate 결과 canonical JSON 완전 일치를 확인했고, 값이 달라지는 최적화는 넣지 않는다
  (부동소수 합산 순서가 바뀌는 `_gradient` 벡터화는 그래서 제외).

## 6. 컬럼·매핑 규약 (IssueTable "AI Comment")

- 컬럼은 **ai_comment 옵션 세션에만 생성**된다 (`ai_comments=None` → 키 자체 미생성 →
  기존 세션 payload 불변). 위치는 comment 블록 선두 = **PTE comment 바로 앞**
  (프런트 orderColumns 가 /comment/i 컬럼을 payload 키 순서대로 우측 블록에 배치 — JS 무수정).
- **읽기 전용**: `issue_table.COMMENT_COLS` 와 프런트 `ISSUE_COMMENT_COLS` 에
  **절대 추가하지 않는다** — 미포함이 곧 서버·프런트 양쪽 편집 차단이다.
- cases → row_key 매핑 (`ai_comment._to_row_keys`):

| 엔진 case | IssueTable row_key |
|---|---|
| `bin != 1` (fail bin) | `Yield\|<bin>\|<item_raw>` |
| item 별 worst-case (severity 최고) | `CPK\|<item_raw>` / `ETC\|<item_raw>` 폴백 |

  셀 텍스트 = `[<status>][<modality>] <comment>` (modality 는 발화 시에만).
  여러 소스에서 같은 (item,bin) 이 나오면 severity 높은 쪽이 남고, **동률이면 이봉
  발화 쪽**이 남는다 (`_rank`). 미사용 키는 그냥 버려진다 (CPK/ETC 행이 없으면 무해).

### 6-1. 이봉 배지 `[이봉]`/`[다봉]`/`[분리]` (2026-08-03)

엔진은 BIMODALITY(구 SUBPOP_GAP)가 **primary_signature 일 때만** 코멘트 본문에 이봉 문구를
쓴다(`recommend._phenomenon_text`). 그런데 BIMODALITY 는 `status.SPECIFICITY_ORDER` 에서
뒤쪽이라 같은 MAJOR 인 공간 룰 등에 밀리기 쉽고, 이봉 분포는 산포도 넓어 그 동시발화가
흔하다 → **발화해도 코멘트에 안 보이던 문제**.

`ai_comment._modality_tag` 가 `case["signatures"]` 에서 BIMODALITY 항목의
`evidence[signal_code=="MODALITY_V2"].note`(`"modality_v2 <label>"`)를 직접 읽어
**primary/secondary 구분 없이** status 뒤에 배지를 붙인다. 엔진은 수정하지 않는다.

- 접두인 이유: `report_view.html` 의 `.kind-issue td.st-comment` 가 `white-space: normal`
  + 330px 고정이라 comment 의 개행이 붕괴된다 — 말미 추가 문장은 문단에 묻힌다.
- note 포맷이 바뀌면 `[분포분리]` 로 degrade한다 (조용한 미표시 방지).
- 수치(BC/n_modes/density_gap) 확인은 셀이 아니라 `/pe/eval` 트레이스가 정본 (§11).
- **엔진 사설 계약 핀**: `present.to_result` 의 `signatures[].evidence[].note` 포맷.
- 캐시: 셀 **값**이 바뀌므로 `cache_policy.REPORT_SCHEMA_VERSION` 22 로 올렸다.

### 6-2. 평가 범위 — fail item 만 (2026-08-11, env 토글)

기본은 **fail 이 1chip 이상인 item 만** 평가한다 (`WEB_REPORT_EVAL_FAIL_ONLY=1`,
server.env). `0` 이면 종전대로 전체 item.

- **item 컬럼만 줄이고 chip 행은 전량 유지한다** — 필터는 `table.item_columns` 축소뿐이라
  엔진 L1/L2 가 전체 분포(cpk·이봉·outlier) 대비 fail 을 그대로 본다. fail chip 만 남기는
  행 필터는 만들지 말 것.
- fail 판정 = Yield 탭·Issue Table 과 같은 규칙(`FAILTNO == item 의 TNO`, 소스 합집합) —
  `ai_comment.eval_fail_scope` 하나가 정본이고 운영 경로와 트레이스가 이걸 공유한다
  (`_eval_items`). Temperature 도 같은 기준이다(CT/HT 재판정 불일치는 아래 6-3 으로 회피).
- 부작용(의도): 수율·cpk 정상 + 룰만 위반한 item 이 평가 대상에서 빠지므로 **ETC 자동 행
  (`etc_auto_items`)이 생기지 않는다**. 되돌리려면 플래그를 0 으로.
- **적용 안 되는 2곳** — 표본함 수집(`collect_session_snapshot`)과 골든셋 검사
  (`golden_check.check_session` 은 `fail_only=False` 고정)는 **항상 전체 item**이다.
  표본이 한쪽으로 마르거나, fail 없는 골든 항목이 `[케이스없음]` 오탐으로 잡히는 걸 막는다.
- 캐시: ai_comment 옵션 세션 키에 `evalfail` 표식이 붙는다(`cache_policy.report_key`) —
  env 토글은 rules_rev 로 감지되지 않으므로. 되돌리면 종전 키의 캐시가 그대로 재사용된다.
- `/pe/eval` 트레이스는 범위를 요청별로 바꿀 수 있다(`scope`=fail|all|미지정=서버 기본).
  범위가 다르면 **직전 run 과의 diff 를 건너뛴다** — 모집단이 달라 added/removed 가 오보다.

### 6-3. Signature 컬럼 + ENGR 정답 라벨 (2026-08-11)

Issue Table 의 **AI Comment 왼쪽**에 `Signature` 컬럼이 붙는다 (AI Comment 와 같은 조건 =
ai_comment 옵션 세션에만). 코멘트 본문은 primary 하나만 서술하지만 이 컬럼은
`case["signatures"]` 의 **발화 전체**를 보여준다(엔진 무수정 — `ai_comment._case_sig_ids`).

- 행 필드: `Signature`(표시 텍스트) + `_sig`(id 배열) + `_sigrev`(ENGR 확정 여부).
  뒤 둘은 화면 컬럼이 아니다(`sheets.js orderColumns` 가 제외). payload 최상위
  `signature_options` 가 dropdown 선택지(정의된 전체 룰 — 비활성 포함 — + `UNKNOWN`).
- **미분류**: fail 인데 발화 0건이면 셀에 `미분류` 로 표시한다. **2026-08-12 부터 엔진이
  그 자리에 `UNKNOWN` 을 명시 발화**하므로(§15) 실제로는 `Unknown` 칩이 뜬다 — `미분류`
  표시는 엔진 UNKNOWN 룰을 꺼 둔 경우의 폴백으로 남는다. 사람이 고른 `UNKNOWN` 라벨과는
  편집행 유무(`_sigrev`)로 계속 구분되고, **커버율 집계에서는 UNKNOWN 을 빼고 센다** —
  자동 발화를 성과로 세면 커버율이 가짜로 100% 가 되기 때문이다.
- ENGR 편집: 드랍다운 N개(가로 추가 `+`) + `확정`. **엔진 제안과 값이 같아도 확정하면
  저장한다** — 안 그러면 정정 사례만 쌓여 통계가 편향된다. 해제하면 편집행과 라벨이 함께
  사라져 "미검수 + 엔진 제안" 으로 돌아간다.
- 저장 순서: 세션 편집 DB(`kind=issue_signature`, value=JSON 배열로 순서 보존)가 **진실**,
  eval DB 반영은 비동기 큐(`_JOB_SIGNATURE`). 워커는 요청값이 아니라 **편집 DB 의 최신
  전체 상태를 다시 읽어** 멱등 재적재하므로 연속 편집도 마지막 상태로 수렴하고, 서버를
  내렸다 올린 뒤 `/pe/eval` 재동기화 버튼으로 복구된다.
- 서버 검증: signature 카탈로그에 있는 id 또는 UNKNOWN 만, 중복 금지, 최대 8개
  (`service._norm_issue_signature`). 정규식만으로는 UI 우회를 못 막는다.
- eval DB: `label_signature(label_id, signature, rank)` — **eval.db v7**(사용자 승인).
  라벨은 `labeler='web-signature'`, `human_status` 는 비운다(관리자 채점 오염 방지).
  case_id 에는 세션이 없으므로 **세션 전용 `ingest_run`**(`ingested_by='web-signature'`)을
  만들고 그 run 의 evaluation 에 `label.eval_id` 를 매달아 세션을 구분한다 — 안 그러면 같은
  lot·item·bin 을 다른 세션에서 확정할 때 서로 덮어쓴다.
- 조회: `/pe/eval` → **ENGR Signature** 탭. 기본이 `UNKNOWN` 목록(= 새 불량유형 후보).
  룰은 여기서 자동으로 바뀌지 않는다 — 사람이 보고 정의하는 재료다.
- **Issue Table Temp 시트는 AI Comment·Signature 를 만들지 않는다** — CT/HT 는 RT limit
  재판정으로 fail 을 다시 정하는데 엔진 평가는 저장된 FAILTNO 기준이라 두 판정이 어긋난다.
- 캐시: `REPORT_SCHEMA_VERSION` 34 (UNKNOWN 명시 발화로 35 — §15).

## 7. 클라이언트 옵션 (Honey)

- Web Report 그룹박스의 **"AI Comment" 체크박스** (honey_main.py). 현재
  **setEnabled(False) 비활성 노출** 상태 — "AI Comment" 글자를 10번 클릭하면 그 실행
  동안만 활성화된다(숨김 스위치). 서버 파이프라인 실사용 검증 후 `setEnabled(True)`
  한 줄로 상시 활성화한다.
- **상태를 settings.json 에 영속하지 않는다** (2026-08-04 변경). 종전엔
  `webreport_ai_comment` 키로 저장했는데, 한 번 켠 뒤 저장된 True 가 다음 실행에서
  "화면은 비활성인데 체크는 켜짐"으로 복원돼 **사용자가 켠 적 없는 세션에도 AI Comment
  컬럼이 붙었다**. 이제 매 실행 꺼진 상태로 시작한다.
- 업로드 시 `manifest.options.ai_comment` + **`ai_comment_optin`** 두 키가 함께 실려
  서버 `report_session.webreport_options` 에 고정 저장된다 — **업로드 후 토글 불가**
  (옵션이 캐시 키·dedup 에 묶이는 세션 불변값).
- 서버 판정(`validation.webreport_ai_comment`)은 **두 키가 모두 참일 때만** 컬럼을
  만든다. 구 클라가 보낸 `ai_comment=True` 세션은 optin 키가 없어 자동으로 미표시가
  된다 — 운영 DB 를 고치지 않고 되돌리기 위한 장치다(캐시는
  `REPORT_SCHEMA_VERSION` 25 로 무효화).

## 8. 의존성

- eval_engine 런타임 의존: numpy·pandas(기존 충족) + **pyyaml** (server/requirements.txt 반영).

## 9. 사람 코멘트 export — Issue Table PTE/개발 comment → eval 스키마 DB (2026-07-15)

eval_analyzer 가 엔지니어 코멘트를 선례(precedent)로 소비할 수 있도록, Issue Table 의
PTE/개발 comment 를 **eval.db 스키마(17테이블, SCHEMA_VERSION=8) 그대로의 별도 SQLite**
로 적재한다. 구현 [web_report/eval_export.py](../web_report/eval_export.py),
검증 [tests/test_eval_export.py](../tests/test_eval_export.py).

- **DB 파일**: `REPORT_EVAL_DB_PATH` (기본 `DB/pe/report/eval/eval.db`) — report_server
  소유, session DB(report.db)와 분리. eval_analyzer 쪽은 실행 시 `EVAL_DB_PATH` 를 이
  파일로 지정해 읽는다(엔진 코드 변경 없이). 스키마는 엔진 `store.SCHEMA` 를 그대로 적용 —
  **스키마 변경은 사용자 사전 승인 대상**(§1, 누적된 운영 데이터 때문).
- **적재 게이트 = Issue Table Status** (2026-08-04): **Status 가 `Close` 인 이슈의
  코멘트만** 적재한다. Open 은 아직 조사 중인 미확정 코멘트라 선례로 쓰면 안 된다는
  요구다. Status 키는 이슈 단위(`Yield|<bin>` / `CPK|<item>` / `ETC|<item>`)이므로
  코멘트 row_key 에서 item 을 떼어 맞춘다(`eval_export._status_key`). Close→Open 으로
  되돌리면 아래 run_case 차집합 정리가 그 case 의 label 을 지운다. 편집 DB 이전
  세션(rev==0)은 Status 를 저장한 적이 없어 전부 Open = 적재 대상 없음이다.
- **트리거 5곳** (모두 try/except + 단일 소비자 큐 `export_async` — 실패해도 업로드/저장
  무영향): ① 세션 업로드 ingest 의 시드 직후, ② `service.update_issue_comments`,
  ③ `service.update_issue_etc_items`, ④ `service.update_issue_status`,
  ⑤ `service.update_issue_status_bulk`(④⑤ 는 위 게이트 때문에 필수 — Status 를
  바꾸는 순간 적재/삭제가 갈린다). 매번 세션 **전체 코멘트 상태 재적재**(멱등).
- **매핑**: PTE+개발 comment 를 `"[PTE] ...\n[개발] ..."` 로 **병합해 label 1행**
  (labeler=`web_report`, label_quality=`manual`, reviewer=마지막 편집자).
  row_key → (bin, test_condition) 은 `eval_export._parse_row_key` 가 정본이다:

  | row_key | bin | `fail_case.test_condition` |
  |---|---|---|
  | `Yield\|<bin>\|<item>` | 그 bin | `''` |
  | `Yield\|1\|…` (Pass 요약행) | — | **skip**(적재 안 함) |
  | `CPK\|<item>` | 1 (PASS_BIN 관례) | `''` |
  | `TEMP\|<item>` | NULL | **`'TEMP'`** |
  | `ETC\|<item>` | NULL | `''` |

  wafer_number=NULL(lot 수준 case). item/unit/limit 은 honeyform tables 에서,
  fail/total/cpk 통계는 best-effort (rawdata 에 없는 자유입력 ETC 항목은 코멘트만).
  `ingest_run.session_id` 로 세션 역참조, run_case 차집합으로 **삭제된 코멘트의
  label 정리**(fail_case 는 보존).
- **온도 평가 구분 = `test_condition`** (2026-08-18, eval.db v8): `TEMP|` 와 `ETC|` 는
  둘 다 bin=NULL 이라 예전엔 case_id 가 겹쳤고, 같은 item 에 두 코멘트가 있으면 뒤에
  적재된 쪽이 앞 label 을 **조용히 덮어썼다**. 이제 조건을 case_id 재료에 넣어 가른다
  (`make_case_id(..., condition)` — 빈 값이면 재료에서 빠져 **기존 case_id 는 불변**).
  같은 값을 `sync_session_signatures`(ENGR Signature 라벨)에도 넘겨야 한다 — 안 그러면
  같은 TEMP 행의 코멘트 라벨과 signature 라벨이 서로 다른 case 로 갈라진다.
  기존에 붕괴돼 적재된 행은 소급 복구하지 않는다(원본은 세션 편집 DB 에 있고, 그 세션이
  다시 export 될 때 갈라진다).
  ✅ **2026-08-19 갱신**: `case_id`·`item_class` 에서 **bin 이 빠졌다**(사용자 결정 —
  동일성 기준은 value_type + item 명). 그래서 `Yield|<bin>|<item>` 과 `CPK|<item>` 은
  이제 **같은 case** 로 모이고, 코멘트는 병합·signature 는 합집합으로 저장된다.
  `test_condition` 은 **유일하게 남은 구분축**이라 위 규약이 더 중요해졌다 — TEMP 를
  빼면 온도 코멘트가 일반 코멘트와 한 case 로 붕괴한다. `item_class` 는
  `category_major|value_type` **2단**(구 3단 값은 읽기만 호환). 배경 → [17](17_eval_learning_loop.md).
  ⚠ **Corner(FF/SS/FS/SF) 평가는 아직 판별할 수 없다** — 분석 모드에도, manifest source
  메타(`{index,name,file_name}`)에도, 클라 업로드 입력에도 corner 개념이 없다.
  source 이름(legend)은 자유 텍스트라 토큰을 신뢰할 수 없고, source 가 많다고 corner 도
  아니다. `test_condition` 에 값만 예약해 두고 **판별할 수 없으면 `''` 로 비워 둔다**.
- **엔진 코드 재사용** (엔진을 고치지 않고 호출만): `store` CRUD 는 전부 `conn=` 주입 —
  `eval_engine.config.DB_PATH` 는 절대 변경하지 않는다. item 정규화는
  `pipeline.ingest._alias_map/_canonicalize/_classify_category_major/_classify_value_type`
  재사용(=db_input/import_csv.py 와 동일 패턴 → 선례 fuzzy 매칭 일관).
  **사설 API 의존 핀**: `store._migrate` / `store._seed_bin_taxonomy` /
  `pipeline.ingest._*` — 엔진 리팩터링으로 시그니처가 바뀌면 eval_export 만 고치면 된다
  (실패는 safe_export 가 격리).
- **unit → value_type 선보정** (2026-07-29): 엔진 `UNIT_TO_VALUE_TYPE` 은 **정확매칭
  표**라 `VOLTS`/`HERTZ`/`mAMP` 같은 표기를 놓치고 조용히 `PF` 로 떨어뜨린다.
  당시 엔진 무수정 원칙 때문에 보정을 서버 쪽에 뒀다 — `eval_export.unit_group()`
  (부분문자열 **VOLT→V / AMP→A / HERTZ→Hz**)을 **먼저** 보고, 안 걸리면 엔진 표로
  내려간다. (엔진이 자유 수정이 된 지금은 엔진 표를 직접 고쳐 일원화할 수도 있으나,
  기존 적재 데이터와의 정합 때문에 현행 2단 구조를 유지한다.) 짧은 표기
  (`v`/`hz`/`amp`)는 규칙에 안 걸려 엔진 결과 그대로 — 충돌 없음.
  이미 적재된 오분류는 관리자 탭 **Unit 별칭 재적용** 버튼으로 일괄 교정한다.
- **관리**: `/pe/admin-pte/` **Eval DB 탭** — overview(파일/건수), label 목록 검색
  (product/family/lot/item/comment/세션ID), 컬럼 표시 토글(lot/product/Bin 기본 숨김,
  선택은 localStorage `adminEvalHiddenCols.v2`), 행은 1줄 고정 + 긴 Item/comment 셀
  클릭 펼치기, **Unit 그룹 인라인 수정**(`POST /api/eval/items/value_type` —
  item_master.value_type + fail_case.item_class 동시 갱신) 및 **Unit 별칭 재적용**
  (`POST /api/eval/items/remap_units`, `dry_run` 미리보기 후 적용), 케이스 단위 완전
  삭제, 세션 재적재, **코멘트 CSV 다운로드**(§10). 세션 삭제 시 export 데이터는 자동
  삭제하지 않는다(선례 보존) — 정리는 이 탭에서 수동.

## 10. 과거 사례 수동 적재 — db_input 5컬럼 CSV (2026-07-28)

엔지니어가 손으로 정리한 과거 코멘트를 같은 eval DB(`REPORT_EVAL_DB_PATH`)에 넣는 경로.
구현은 **`eval_analyzer/db_input/` 안에서만** 한다 — 적재기와 판단 엔진의 관심사를 분리해
`eval_engine/` 을 건드리지 않기 위해서다(§2 단방향 규약 유지).
(2026-08-03 이전에는 "하위 파일 무수정" 동결에 대한 명시적 예외(carve-out) 였다. 동결은
폐지됐지만 배치 규칙 자체는 그대로 유지한다.)

- **입력 계약**: `Product type, Family Product, unit, Item, comment` 5컬럼(헤더 대소문자·
  공백 유연). 기존 20컬럼 레거시 CSV 도 헤더 자동감지로 계속 동작한다.
  정본 설명은 [eval_analyzer/db_input/CLAUDE.md](../eval_analyzer/db_input/CLAUDE.md).
- **unit 정규화** (2026-07-29 부분일치로 확장): 원문(VOLTS/HERTZ/AMPS/PCT…)을 어휘
  (V/A/Hz/CODE/Ohm/Sec/PF/**%**)로 매핑한다. 2단계 —
  ① 정확일치: 엔진 `UNIT_TO_VALUE_TYPE` + db_input `EXTRA_UNIT_ALIASES`
  ② **부분일치**: db_input `UNIT_STEMS` 의 stem(`volt`/`amp`/`hertz`/`hz`/`ohm`/`sec`/
  `code`/`percent`/`pct`/`%`)이 문자열에 포함되면 그 그룹 (MILLIVOLT→V, AMPERE→A,
  KiloHertz→Hz, MOhm→Ohm, mSec→Sec, TCODE→CODE). 한 글자 stem(v/a/s)은 오탐이 커서 쓰지
  않는다 — 한 글자 표기는 ①이 담당.
  **모르는 단위가 하나라도 있으면 아무것도 적재하지 않고 중단**(행번호+원문 출력) —
  `search_precedents` 가 `value_type` 을 등호 하드필터로 쓰기 때문에 조용한 PF 폴백은
  선례를 영구 미매칭으로 만든다.
  - ⚠ **엔진 live-run 경로와 어긋난다**: `pipeline/ingest._classify_value_type` 은 여전히
    정확일치 + 모르면 `PF` 폴백이다(엔진 현행 동작). 그래서 `MILLIVOLT` 는 선례에선 `V`,
    같은 표기를 UNIT 행에 쓴 live case 는 `PF` 라 등호 필터에서 서로 안 잡힌다. 새 값
    `%` 도 엔진이 절대 생성하지 않으므로 `%` 선례는 **선례 조회·관리자 탭 표시 용도**다.
    `rules/*.yaml` 은 `item_class = category_major|value_type` 스코프라(2026-08-19 2단화) `%` 스코프가
    없어 기본으로 폴백한다. 완전 해소는 엔진 `UNIT_TO_VALUE_TYPE`/`_classify_value_type`
    을 같은 규칙으로 맞추면 되고, 엔진이 자유 수정이 된 지금은 **이 repo 에서 바로 할 수
    있다**(다만 기존 적재 데이터 재분류가 따라와야 하므로 여전히 별건 작업이다).
- **실행 ① 서버 콘솔**: `eval_analyzer\db_input\run_import.bat` 더블클릭 → CSV 선택.
  bat 이 report_server 안에 있음을 감지해(`..\..\server\config.py`) `EVAL_DB_PATH` 를
  서버 소유 eval.db 로 잡고 `--to-eval-db` 를 붙인다 → 관리자 탭에 바로 보인다.
  `eval_analyzer/` 폴더만 따로 떼어내 단독 실행하면 기존 per-family output 동작 그대로.
- **실행 ② Honey 'DB Input'** (2026-07-29): Honey 실행(&R) 메뉴 맨 아래 → CSV 선택 →
  **검증 미리보기 → 확정**. 서버 `POST /pe/report/api/eval/labels_import`
  ([routes_eval_input.py](../server/report/routes_eval_input.py))가
  `db_input/import_csv.py --to-eval-db --json [--dry-run]` 을 **별도 프로세스**로 실행한다.
  - **왜 subprocess 인가** (2가지): ① `_import_group` 이 `eval_engine.config.DATA_DIR/
    DB_PATH` 를 **모듈 전역에 대입**하는데 그 모듈은 Flask 프로세스에서 ai_comment.py 와
    공유된다 — 프로세스 경계가 곧 격리다. ② eval_engine import 지점을 2곳으로 유지(§2):
    **실행은 import 가 아니다.**
  - **JSON 계약**(깨지 말 것): stdout **마지막 줄에 JSON 1줄**
    `{ok, mode, format, rows, groups, errors, db_path}`, 종료코드 `0`=정상 / `2`=CSV 오류.
  - **가드**: `X-Honey-Agent: 1` 필수(브라우저 차단, CSRF 대체) + Honey 신원 필요.
    권한은 **Honey 접속자 전원** — 추적은 감사 로그로 한다. ≤5MB. **단순 5컬럼만**
    받는다(레거시는 `_import_group` 안에서 행 검증이라 부분 적재 위험).
  - **상태 없음**: Honey 가 같은 바이트를 validate/commit 두 번 보낸다(토큰·TTL 불필요).
    commit 도 쓰기 전에 dry-run 을 한 번 더 돌려, 승인한 검증을 지금 시점 DB 기준으로
    다시 증명한다. 응답에서 서버 내부 경로(`db_path`)는 제거한다.
  - **staged CSV 경로는 파일명 기반 고정**(`uploads/report/eval_input/`) — 랜덤 tmp 를 쓰면
    `_get_or_create_run` 이 `(source_file 문자열, session_id)`로 run 을 재사용하므로 재적재
    때마다 `ingest_run` 행이 쌓인다. 실행 직후 파일은 지운다(비교 대상은 경로 문자열).
  - **감사**: action=`eval_db_input` (validate 시도 포함, `client_user` 기록).
    관리자 User Action Monitoring 에 "선례 DB 적재" 로 보인다.
  - **동시 쓰기**: 프로세스 내부는 `threading.Lock` 으로 직렬화하지만, 같은 eval DB 를
    `eval_export.export_async`(데몬 스레드)도 쓴다. WAL + `busy_timeout=5000` 로 견디고
    최악의 경우 export 1회가 스킵되지만 `safe_export` 가 격리하고 다음 코멘트 편집이
    세션 전체를 재적재한다(멱등). 운영자가 서버 콘솔에서 run_import.bat 을 동시에 돌리는
    것은 프로세스 밖이라 Lock 이 막지 못한다.
  - 환경변수 `REPORT_EVAL_IMPORT_PYTHON` — 적재기를 돌릴 인터프리터(기본 `sys.executable`).
  - 검증: [tests/test_eval_db_input.py](../tests/test_eval_db_input.py) +
    [eval_analyzer/tests/test_db_input_json_mode.py](../eval_analyzer/tests/test_db_input_json_mode.py).
- **왕복**: 관리자 탭 **CSV 다운로드**(`GET /api/eval/labels.csv`)가 같은 5컬럼으로 내보내고
  (unit 은 `im.value_type` = 엔진 어휘), 고쳐서 재적재하면 같은 case 의 label 이 갱신된다.
  ⚠ 단순 포맷은 lot/wafer/bin 이 없어 `product_name=<pt>_<fp>`·`bin=0` 으로 **case 를 합성**
  하므로 왕복은 의도적으로 lossy 다 — web_report 라벨을 재적재하면 합성 case 가 1건 생긴다
  (labeler 가 달라 서로 지우지 않는다: 세션 재적재 reconcile 은 `labeler='web_report'` 만 본다).

## 11. 룰 관리자 패널 — `/pe/eval` (2026-08-03)

매번 yaml 을 손으로 고치고 **서버를 재시작해야 했던 디버깅 루프를 없애기 위한** 관리자
화면. [server/eval_panel/](../server/eval_panel/) (blueprint 등록은 `server/plugin.py`).

- **접근**: admin 과 같은 비밀번호(`REPORT_ADMIN_PASSWORD`)로 발급하는 별도 게이트 쿠키
  `pe_admin_gate_eval`(path=/pe/eval, 12h). admin 대시보드 `/login` 이 함께 발급하므로
  admin 로그인 상태면 바로 들어간다. 별도 토큰인 이유는 `voc_gate_token()` docstring 과
  동일(경로가 달라 admin 쿠키가 안 실리고, 값이 같으면 유출 시 서로 재사용됨).
  비-GET 은 admin 패널과 같은 `X-Admin-Request: 1` 헤더 요구(CSRF).
- **편집 범위 선택기는 페이지 상단 1쌍**(`#scPt`/`#scFam`, 2026-08-05) — Thresholds 와
  Signatures 가 공유한다. 탭마다 따로 두면 두 탭의 범위가 어긋난 채 "고친 값이 왜 안 먹지"
  가 되기 때문이다. 첫 옵션 **기준값(전 제품 공통)** 에서는 Signatures 가 signatures.yaml 을
  직접 편집하고, Thresholds 는 **읽기 전용**으로 default 를 보여준다(패널은 제품군 오버레이만
  저장한다 — `read_thresholds(pt="")` 가 그 뷰를 내려준다). 트레이스 탭은 세션의 제품군을
  따르므로 이 선택기와 무관하다.
- **탭 7개** (표본함은 §14):
  1. *Thresholds* — 상단 범위 선택기로 오버레이 편집. 병합 순서는
     `default → product_type(레거시 섹션) → thresholds/<PT>/_default.yaml →
     thresholds/<PT>/<FAMILY>.yaml → item_class`.
     **입력칸에는 "이 범위에 적용될 값"(상속 포함)을 채워 보여주고, 저장 시 상속값과 같은
     키는 파일에 쓰지 않는다** — 화면의 값이 곧 적용값이면서 오버레이 파일은 최소로 남고,
     상위 층을 고치면 따라간다. 칸을 비우거나 ↺ 를 누르면 상속으로 되돌아가고, 오버레이가
     비면 파일을 지운다. 각 행에 **설명**(thresholds.yaml 의 주석을 서버가 파싱 —
     `rules_io.threshold_descriptions`, 설명 정본은 yaml 주석 한 곳)과 **그 값을 쓰는 룰**
     (`rules_io.threshold_usage` 역인덱스 + 선언형이 아닌 코드 참조 라벨 `_CODE_REFS`)을 함께 찍는다.
     룰 칩을 누르면 Signatures 탭의 그 룰로 바로 이동한다. 한 줄 설명 아래 **"자세히"**
     접이식에는 통계 초보용 긴 설명이 붙는다 — 정본은 패널 옆 파일
     [server/eval_panel/threshold_help.yaml](../server/eval_panel/threshold_help.yaml)
     (`rules_io.threshold_help`, 키가 없으면 한 줄 요약만 나온다).
     키가 40개 가까이라 **키·설명 검색 + "직접 지정만" 토글**이 있다(2026-08-05).
     둘 다 재렌더가 아니라 **행 표시/숨김**이다 — 다시 그리면 입력 중이던 값이 날아간다.
     숨긴 행도 저장 대상에는 그대로 포함된다(저장은 `.th-val` 전체를 순회).
  2. *Signatures* — 21종 enable/disable + 조건(when_metric)·status_hint·issue_category·
     문구(phenomenon/action/evidence) 편집. 체크박스로 **여러 개 골라
     일괄 켜기/끄기**(`POST /api/signatures/enabled` — yaml 쓰기·백업·rev bump 1회).
     **편집 범위는 Thresholds 와 같은 제품군 × family_product 드롭다운**이다(2026-08-04).
     병합 순서 `signatures.yaml → signatures/<PT>/_default.yaml → signatures/<PT>/<FAMILY>.yaml`
     (`_rules.signatures_for`), 화면은 **그 범위의 적용값**을 보여주고 저장 시 상속값과 같은
     필드는 파일에 쓰지 않는다 — thresholds 와 똑같은 규약이라 기준값을 고치면 따로 지정하지
     않은 제품군은 따라간다. 카드의 `이 범위 전용:` 칩이 이 범위에서 직접 지정한 필드를
     보여주고 `↺ 상속으로`(`POST /api/signatures/<id>/reset`)가 그 항목을 통째로 지운다.
     드롭다운에서 **기준값(전 제품 공통)** 을 고르면 종전대로 signatures.yaml 을 고친다.
     구 per-signature `scope` 체크박스 UI 는 이 오버레이로 대체돼 사라졌다(엔진의
     `scope_matches` 는 하위호환으로 남아 있고 배포 룰 중 쓰는 것은 없다).
     목록은 **사용중이 위, 꺼진 룰은 아래 접이식**.
     조건은 행 단위로 추가/삭제하며 **모두 만족해야 발화(AND)** — OR 은 지원하지 않는다.
     조건 오른쪽에는 그 기준값이 **이 범위에서 실제로 얼마인지**(`= 1.1 (cpk_warn)`)를 찍는다.
     근거 문구는 `라벨 {지표}` 문법을 직접 쓰지 않고 [앞에 붙일 말]+[지표] 두 칸으로
     편집한다(중괄호가 여러 개인 기존 템플릿은 원문 1칸으로 남는다).
     **신규 추가/삭제는 지원하지 않는다** — `status.py SPECIFICITY_ORDER` 코드와 동기화가
     필요해 UI 만으로는 안전하지 않다.
     - 검색은 **id 뿐 아니라 현상·점검제안·조건 텍스트**까지 훑고(대소문자 무시), Thresholds
       와 같은 이유로 재렌더가 아니라 카드 표시/숨김이다(2026-08-05).
     - 저장 전에 **조건 문제를 카드 안에 인라인 표시**한다(지표 중복 → 마지막 행만 저장됨 /
       한쪽만 채운 행 → 저장 시 버려짐 / 없는 임계값 / 이름 형식). 표시일 뿐 저장을 막지는
       않는다 — 최종 권위는 서버 `_validate_signature_payload` 다.
     - **BIMODALITY 카드는 조건·근거가 읽기 전용**이다(2026-08-05). 이 룰은 `when_metric` 이
       판정에 쓰이지 않으므로(아래 트레이스 항목) 고쳐도 아무 일이 안 일어나는 칸을 열어두면
       오해가 쌓인다. 대신 실제로 효력이 있는 `subpop_*`·`bimodality_warn` 7종으로 가는
       바로가기 칩을 보여주고, 저장 시 두 필드를 payload 에서 아예 뺀다. id 는 하드코딩하지
       않고 `eval_debug.subpop_gap_id()`(엔진 `signatures._BIMODALITY_ID`)를 `/api/meta` 로
       받는다(함수명·API 키는 종전 그대로 — 반환값만 개명을 따라간다).
  3. *L0~L6 트레이스* — 세션 1건을 AI Comment 와 **같은 경로**(loader→mode_tables→
     `ai_comment._table_to_raw_df`)로 재현하되 `evaluate()` 대신 단계 함수를 직접 호출해
     raw_metrics/features/조건분해를 노출한다. signature 21행 매트릭스에 조건별
     `실제값 ⟨op⟩ 임계값(키=값)` 과 미발화 사유(disabled / min-n 가드 / 특수분기 / 결측)를
     찍는다. **`should_store` 게이팅 탈락 케이스도 포함**한다 — "왜 코멘트가 안 나왔나" 가
     이 화면의 주 용도다. 결과는 프로세스 메모리 LRU(4런/30분)에 두고 상세는 1건씩 조회.
     - **BIMODALITY 만 예외 처리**(2026-08-03): 이 룰은 `when_metric` 을 쓰지 않고
       `features.modality_v2` 로 판정하는 하드코딩 특수분기라, yaml 의 `when_metric`·
       `evidence` 선언은 **죽은 설정**이다(패널에서 고쳐도 무효 — `status_hint`/
       `phenomenon_ko`/`action_ko` 만 실효). 그래서 조건을 못 찍어 "왜 안 잡혔나" 를
       볼 수 없었다 → `eval_debug._subpop_conditions` 가 엔진
       `features._classify_modality_v2` 의 AND 체인을 10행으로 미러링해 찍는다
       (게이트 2행 + multimodal/bimodal/separated 분기 각각 — separated 는 2026-08-03
       부터 cdf_gap 대신 `value_gap_ratio`/`minor_mass` 기준). `skip_reason` 대신
       `branch_note` 필드로 내려보내 조건과 **함께** 렌더된다. 임계값은 키 이름으로만
       읽는다(하드코딩 금지) — **엔진이 분기 구조를 바꾸면 이 함수도 고쳐야 한다**.
     - **분포 미니차트**(2026-08-03, 2026-08-10 Plotly 산점으로 교체): 케이스 상세
       L2 features 아래에 ECDF(**선 없는 markers**)+도수 막대(보조 y축)+LSL/USL(빨간 점선)+
       mean/median(세로 점선) 을 그린다(`drawDist`). 수치 표만 보고 임계값을 고치면
       "왜 이 값이 나왔나"를 확인할 수 없어서 넣었다.
       - 렌더는 **web_report Item Detail 의 CDF 와 같은 규약**이다 — ECDF 를 선으로 잇지
         않고(누적분포 왜곡), 확대는 드래그 박스·원복은 더블클릭(모드바·scrollZoom 없음).
         Plotly 는 `/pe/report/vendor/plotly.min.js` 를 **케이스 상세를 처음 그릴 때만**
         주입한다(임계값만 편집하는 관리자에게 1.4MB 를 받게 하지 않으려고).
       - 데이터는 `_trace_case` 의 `dist` 필드 — `{x, y, sampled, hist, n, lsl, usl,
         mean, median, unit}`. **이 카드에 한해 표시용 다운샘플을 허용**한다
         (`_ECDF_POINTS`=400, 균등 stride+첫/끝 보존). 카드는 300px 폭이라 그 이상은 눈에
         보이지 않고, 측정값 전량 확인은 카드 아래 "Item Detail 열기" 링크
         (`?tab=item_detail&item=` — boot.js `applyDeepLink`)가 여는 세션 상세의 몫이다.
         CLAUDE.md §5-5 의 다운샘플 금지는 리포트 Distribution 차트를 지키는 규칙이고
         이 payload 는 관리자 디버그 표시용이라 **판정에는 무관**하다
         (perf_guard 면제 주석이 `_downsample_ecdf` 에 달려 있다).
       - **런 단위 점 예산**(`_DIST_POINTS_BUDGET`=160,000)이 함께 걸린다 — 전체 트레이스
         에서 케이스 수에 비례해 메모리가 늘지 않게 하는 상한(trace_store 4런 보관).
         기본 400 케이스 × 400점이라 기본 트레이스는 전량 점을 받고, 예산을 넘긴 케이스는
         막대만 싣는다.
       차트는 web_report Distribution 미니셀처럼 **좁은 카드**로 그린다(가로로 늘어진
       차트는 봉우리가 눌려 육안 구분이 어려웠다).
     - **전체 케이스 / 정렬**(2026-08-04): 기본은 상위 400건이지만 "전체 케이스" 를 켜면
       상한 없이 가져온다(`POST /api/trace {all:true}`). 표는 항목 순서(원본)·발화 많은
       순·심각도 순·코멘트 생성분 먼저로 정렬할 수 있다. L3 매트릭스는 **발화한 룰이 위**,
       평가에서 빠진 룰(꺼짐 / scope 밖)은 **맨 아래 접이식**으로 내려간다.
     - **무판정 진단**(2026-08-04): `value_type=PF` 로 분류된 케이스는 L1/L2 가 통계를
       전부 비워 어떤 룰도 발화하지 못한다. 케이스 상세 상단에 UNIT 원문과 함께 그 사유를
       경고로 찍는다(`_metrics_note`) — 대부분은 UNIT 표기가 엔진 정확일치 표
       `ingest.UNIT_TO_VALUE_TYPE` 에 없어서 생기는 오분류다.
     - 함께 렌더되는 필드: `secondary_signatures`(배지) · `evidence`(L4 판정 근거) ·
       `precedents`(L5 선례) · `ctx_values`(접힌 표 — 조건 분해의 actual 원천).
     - **케이스 상세는 엔진이 도는 순서로 배치**한다(2026-08-05): 결론 배지 → 정답 라벨(접이식,
       열림 상태 유지) → **L0 수집·분류**(item_class·`unit → value_type`·LSL/USL 유무·n·source)
       → L1 raw_metrics → L2 features + 분포차트 → L3(적용 임계값·ctx_values 접이식 + 매트릭스)
       → L4 evidence → L5 comment·선례 → L6 저장 게이트. 어디서 끊겼는지 위에서 아래로 따라
       읽게 하기 위함이고, **L0 의 "limit 없음" 이 무판정 원인 2순위**라 맨 위로 올렸다.
     - **전후 비교**(2026-08-05): 룰을 고치고 같은 세션을 다시 트레이스하면 직전 실행과의
       차이만 카드로 보여준다 — status/primary/L6 저장/**발화 집합**이 바뀐 케이스(＋/－ 칩),
       새로 생기거나 사라진 케이스. 서버가 `trace_store.latest_for_session()` 으로 직전 run 을
       찾아(`put()` 전에 조회) `_trace_diff` 를 계산해 응답 `diff` 로 내린다. 케이스 키는
       `(source_index, item_raw, bin)` 이고 **중복 키는 비교에서 뺀다**(어느 쪽과 비교할지
       알 수 없어 오보가 난다). 보관이 LRU 4런/30분이라 직전 run 이 밀려났으면 `diff:null` 로
       카드를 숨긴다(best-effort — 없어도 기능은 무손상). 케이스 상한(전체/400)이 다르면
       케이스 집합 차이가 룰 변화로 오인되므로 비교를 생략하고 사유만 표시한다.
     - **필터**(2026-08-05): 항목명 외에 status·**발화 룰**·source 별 필터. 후보는 이번
       트레이스 결과에서 뽑고(`fired_ids` 합집합 등), 재실행해도 고르고 있던 값을 유지한다
       (전후 비교 흐름에서 매번 다시 고르지 않게).
     - **바로가기**(2026-08-05): 매트릭스의 룰 id → Signatures 탭 그 카드, 조건의 임계값 이름
       → Thresholds 탭 그 행(둘 다 펼침+강조). "이 룰이 왜 안 떴지 → 고치러 간다" 동선이
       끊겨 있던 것을 잇는다. 상단 범위 선택기를 공유하므로 이동해도 범위가 유지된다.
     - **기존 라벨 프리필**(2026-08-05): 이미 검수한 케이스면 `GET .../case/<i>` 응답의
       `label` 필드(`eval_export.get_panel_label` — `save_human_label` 과 **같은 case_id
       산식**)로 폼을 채우고 "기존 라벨" 배지를 단다. 조회 실패는 상세 열람을 막지 않는다.
  4. *Eval DB* — **admin 대시보드에서 이관**(2026-08-03). 코멘트 라벨 목록·검색·컬럼 토글·
     CSV export·Unit 그룹 교정·세션 재적재·케이스 삭제. 마크업/JS 는 admin_panel.html 에서
     그대로 옮겼고 구현 모듈은 여전히 `admin_panel/eval_admin.py` 를 import 한다
     (라우트만 `/pe/eval/api/eval/*` 로 이동 — admin 쪽 구 라우트는 삭제).
     eval 관련 화면을 한 페이지에 모으기 위한 이동이다.
  5. *검증·백업* — 참조 무결성(`when_metric` 이 참조하는 임계값 키 존재, 오버레이 고아
     파일, 전 PT×family 조합 병합 시뮬레이션, SPECIFICITY_ORDER 정합) + 백업 목록/복원
     + **골든셋 회귀**(2026-08-05, §12-1).
  6. *채점* (2026-08-03) — **엔진 판정 vs 사람 정답** 집계. 트레이스 케이스 상세의
     "정답 라벨" 폼(수용/정정 + 코멘트/root cause)이 `POST /pe/eval/api/eval/label` →
     `eval_export.save_human_label` 로 export DB 에 **evaluation(엔진 스냅샷) +
     label(eval_id 연결, labeler=`eval-panel`)** 쌍을 저장하고(같은 case 재검수는 교체),
     채점 탭이 `eval_admin.scoring()` 으로 혼동행렬·status 일치율·MAJOR+ 정밀도/재현율·
     수용률·signature 별 집계를 보여준다. **이 쌍이 룰 정확도 검증(calibrate 후속 3번)의
     원재료**다. 검증: [tests/test_eval_label_scoring.py](../tests/test_eval_label_scoring.py).
- **저장 파이프라인**: **낙관적 잠금** → 검증 → **no-op 판정** → `rules/_backup/` 백업(파일당
  50개, 같은 초면 `-2` 접미사) → tmp+`os.replace` 원자적 쓰기(LF 유지) → `.rules_rev` +1 →
  감사 로그 `action=eval_rules_edit`, `client_user=eval-panel`.
  ⚠ signatures.yaml 재작성은 **선두 주석 블록만 보존**하고 인라인 주석은 잃는다(백업이 이력).
- **저장 안전장치 3종** (2026-08-05, 변경 5개 라우트 = thresholds PUT / signatures PUT /
  signatures enabled / signature reset / exclusions PUT):
  1. **낙관적 잠금** — 화면이 들고 있던 `base_rules_rev` 를 요청에 실어 보내고 현재 값과
     다르면 409(`conflict:true` + 현재 rev). **필드가 없어도 409** 다: 클라이언트가 이
     HTML 하나뿐이고 HTML 과 라우트는 같이 배포되므로, 필드 없는 요청 = 배포 전에 열어 둔
     구버전 화면 = 정의상 stale 이다. 프런트는 `getJSON`/`sendJSON` 이 모든 응답의
     `rules_rev` 를 한 곳에서 추적하므로(`RULES_REV`) 같은 탭 안의 연속 저장은 충돌하지
     않는다. 서버는 rev 검사~파일 쓰기를 `_rules_lock` 으로 직렬화한다(TOCTOU).
  2. **no-op 스킵** — 파싱된 값이 그대로면 백업·쓰기·rev 증가를 **전부 건너뛰고**
     `no_op:true` 를 돌려준다. rev 를 올리면 ai_comment 옵션 세션의 리포트 캐시가 통째로
     무효화되므로(§5) "저장 눌렀지만 안 바뀐" 경우까지 재평가시키지 않기 위함이다.
  3. **변경 사유**(선택) — 입력하면 감사 로그 `changed_fields` 에 `reason=…`(200자)로 남는다.
     필수로 하지 않은 것은 튜닝 중 저장이 잦아 마찰만 커지기 때문.
- **임계값 관계·타입 검증** (2026-08-05, `rules_io._check_threshold_values`): 엔진이 암묵
  전제하는 불변식(`cpk_bad ≤ cpk_warn`, `outlier_ratio_warn ≤ outlier_ratio_bad`,
  `center_region_pct < edge_region_pct`, `subpop_density_gap_warn ≤ …_strong`)과 값 종류
  (ratio 0~1 / count 양의 정수 / positive)를 검사해 위반이면 **400 으로 거부**한다. 위반은
  실험이 아니라 조용한 오동작이다. 두 가지 규약에 주의:
  - 검사는 **병합 결과(effective) 기준**이다 — 오버레이가 관계쌍의 한쪽만 덮으면 파일
    단독으로는 판정할 수 없다(`_inherited_thresholds` 를 read/save 가 공유한다).
  - 단, **이번 저장이 실제로 바꾼 키**가 쌍에 걸릴 때만 본다. 상위 층에 이미 있던 위반
    때문에 무관한 키 저장까지 막히지 않게 하기 위함이고, 그 위반은 `validate_all()`(검증
    탭)이 전 PT×family 조합에서 전역 보고한다.
  - `THRESHOLD_KINDS` 는 **opt-in 표**다 — 없는 키는 검사하지 않는다(새 임계값이 저장을
    막지 않게). "큰 값을 넣어 사실상 끄기" 가 정당한 키는 일부러 뺐다(그 용도는
    signature `enabled:false` 가 담당).
- **패널이 만드는 파일** (rules/ 하위는 없으면 엔진이 종전과 동일 동작):
  `thresholds/<PT>/*.yaml` · `signatures/<PT>/*.yaml` 오버레이 · `_backup/*.bak` ·
  `.rules_rev` · (rules 밖) `tools/eval_golden/golden.yaml` + 그 옆 `_backup/`.
- 검증: [eval_analyzer/tests/test_rules_scope.py](../eval_analyzer/tests/test_rules_scope.py)
  (트리 없을 때 무회귀 / 병합 우선순위 / mtime 자동 리로드 / enabled 미발화) ·
  [tests/test_rules_io_guards.py](../tests/test_rules_io_guards.py)(관계·타입 검증, no-op,
  배포 default 전 키 통과) · [tests/test_trace_diff.py](../tests/test_trace_diff.py)
  (전후 비교, 중복 키 제외, 직전 run 조회 TTL) ·
  [tests/test_eval_golden_io.py](../tests/test_eval_golden_io.py).

## 12. 룰 축소 디버깅 체제 (2026-08-03) — 2026-08-12 부분 해제

룰 21개를 동시에 굴리면 임계값 하나를 고쳤을 때 무엇이 좋아지고 나빠졌는지 볼 수 없어,
**SPEC_TOO_TIGHT / SEVERE_OUTLIER / OUTLIER_WARN / SUBPOP_GAP 4개만
남기고 나머지를 `enabled: false`** 로 껐다(2026-08-04 CONSTANT_VALUE 추가로 끔 — 5→4개). 개념이 잡히는 대로 `/pe/eval` Signatures 탭에서
하나씩 다시 켠다. 되돌리기는 그 탭의 일괄 켜기 한 번이다(코드 변경 없음).

> **2026-08-12 (1차)**: unknown 축소 작업(§15)으로 `LOW_CPK` · `WIDE_DISTRIBUTION` ·
> `MEAN_SHIFT` · `HEAVY_TAIL` 4개를 다시 켰다(활성 8 + UNKNOWN).
>
> **2026-08-12 (2차, 룰셋 재편)**: 사용자 v5 데이터 검토 결과 겹치는 룰을 통합하고 공간
> 룰을 살렸다 → 현재 활성 **10 + UNKNOWN**: `OUTLIER`(통합) · `LOW_CPK`(SPEC_TOO_TIGHT·
> WIDE_DISTRIBUTION 흡수) · `MEAN_SHIFT` · `HEAVY_TAIL` · `BIMODALITY`(개명) ·
> `E1_FAIL` · `EDGE_FAIL` · `CENTER_FAIL` · `RING_FAIL` · `CLUSTER_FAIL`.
> off: `SPEC_TOO_TIGHT`·`WIDE_DISTRIBUTION`·`SEVERE_OUTLIER`·`OUTLIER_WARN`(통합돼 꺼짐,
> 선언은 보존) · `MISSING_LIMIT`·`EQUIPMENT_SUSPECT`·`CONSTANT_VALUE`·`CODE_RAIL`·
> `TAIL_RISK`·`BIDIR_TAIL`·`GROSS_FAIL`·`LOW_SAMPLE_UNCERTAIN`·`WAFER_GRADIENT`.
> 상세는 §16-1 의 재편 항목.
>
> **2026-08-14 (3차, 사용자 v9 검토)** — 룰 목록은 그대로 두고 **판정축 2개**를 고쳤다.
> 룰을 켜고 끄는 단계가 끝나 이제는 "같은 룰이 무엇을 보고 판정하나" 가 문제였다.
> - `OUTLIER` 끊김 조건: `fail_pass_gap_sigma ≥ 1.5` → **`fail_body_jump_ratio ≥ 0.35`**.
>   구 지표는 `min(|z| of fail) − max(|z| of pass)` 라 **양쪽 꼬리를 한 자에 섞어** 쟀다 —
>   반대쪽 꼬리에 더 먼 pass 가 하나만 있어도 음수가 되어, 몸통과 뚝 끊긴 fail 덩어리가
>   통째로 미발화했다(사용자가 outlier 로 지목한 v9 관찰군 8건이 −3.4 ~ +1.5).
>   새 지표는 같은 쪽에서 몸통 경계(3σ)~최근접 fail 구간의 "최대 빈 폭 / 구간 폭" 이다.
>   부수 효과로 `HEAVY_TAIL_L3` 가 OUTLIER 에 primary 를 뺏기던 것도 해소됐다.
> - `RING_FAIL` 에 `fail_spread_norm > spot_cluster_spread_max` AND 추가. ring 밴드는
>   die 의 절반이라 국부 blob 이 거기 놓이면 점유율 1.0 이 되고, RING 이
>   `SPECIFICITY_ORDER` 에서 SPOT_CLUSTER 보다 앞이라 primary 까지 가져갔다
>   (v9 SPOT_CLUSTER 겨냥 L2~L5 **전부** RING_FAIL 로 판정).
>
> 남은 알려진 겨냥 오분류(v10 기준, 룰이 아니라 **겨냥 데이터**의 한계):
> `LOW_CPK_L2` · `MEAN_SHIFT_L2` · `MEAN_SHIFT_L3` 이 OUTLIER 에 primary 를 내준다.
> 이 겨냥들은 분포를 spec 안에 가두고(`bounded`) fail 을 `_push_out_of_spec` 로 limit
> 바로 밖에 만들어서, 값 축에서 실제로 "몸통과 끊긴 덩어리" 가 된다. 자연 꼬리로 바꾸면
> fail 이 0 이 되어 항목 자체가 사라지므로(cpk 1.25 → 5025 chip 에 0.5개꼴) 사다리 상수를
> 함께 재설계해야 한다 — 보류.

- **LOW_CPK 를 끈 것이 핵심**이다. SPEC_TOO_TIGHT 은 발화 조건에 `cpk < cpk_warn` 이
  들어 있어 LOW_CPK(MAJOR)와 항상 같이 뜨고, 자신은 MINOR 라 specificity 경쟁에서 져
  **primary 가 된 적이 없었다** — 코멘트에도 안 나왔다. LOW_CPK 를 꺼야 처음으로 관찰된다.
- **저장 게이트 확장**([pipeline/present.py](../eval_analyzer/eval_engine/pipeline/present.py)
  `should_store`): 종전 `yield fail or cpk<cpk_warn` 에 **`or signature 발화`** 를 더했다.
  수율·cpk 는 정상인데 분포만 이상한 케이스(이봉 등)는 코멘트가 아예 안 만들어져 디버깅이
  불가능했다. 이 부류는 Issue Table **ETC 섹션 자동 행**으로 올라간다:
  `ai_comment.build_ai_comments` 가 `{"comments", "etc_auto_items"}` 를 돌려주고
  (fail bin case 가 없으면서 signature 가 발화한 item), `issue_table._auto_etc_items` 가
  수동 ETC·CPK 섹션·Yield 행·숨김과 중복을 걷어낸 뒤 수동 추가분 뒤에 잇는다.
  **저장값이 아니라 매 조회 재계산**이라 룰이 조용해지면 행도 사라진다.
- **테스트는 배포 on/off 와 분리**한다 — `tests/conftest.py` 의 autouse fixture
  `all_signatures_enabled` 가 테스트에서 `enabled:false` 를 무시한다(룰을 껐다 켤 때마다
  로직 테스트가 깨지면 다시 켤 때 기댈 안전망이 사라진다). 비활성 메커니즘 자체는
  `rules_as_deployed` 마커를 단 `test_rules_scope.py` 가 검증한다.
- **골든셋 회귀**: [tools/eval_golden/](../tools/eval_golden/) — `golden.yaml` 에
  "이 세션의 이 항목은 이 룰이 떠야/뜨면 안 된다"를 사람이 적고,
  `python tools/eval_golden/golden_check.py` 가 실제 트레이스와 대조해 누락/오탐을 센다
  (불일치 있으면 exit 1). `eval_debug.trace_session` 만 쓰므로 import 3곳 규약 밖이 아니다.
  임계값을 만지기 **전에** 몇 줄이라도 적어 두는 것이 이 도구의 전부다.

### 12-1. 골든셋 ↔ 패널 연결 (2026-08-05)

"손으로 적어 두라"는 규약은 실제로 아무도 적지 않아 `golden.yaml` 이 계속 비어 있었다.
쌓는 비용과 돌리는 비용을 둘 다 패널로 옮겼다.

- **쌓기**: 트레이스 케이스 상세의 **"골든셋에 추가"** → `POST /pe/eval/api/golden/add
  {token, index}`. 그 케이스의 **현재 발화 상태**(뜬 룰 집합 + status)를 기대값으로 적는다.
  같은 `(item, bin, source)` 항목이 있으면 **교체**한다(룰을 고쳐 기대값 자체가 바뀌는 것이
  정상 흐름이라 중복을 쌓지 않는다). 발화 0건이면 `fire` 키를 넣지 않는다.
- **돌리기**: 검증·백업 탭의 **"회귀 실행"** → `POST /pe/eval/api/golden/check`. CLI 와
  **같은 대조 로직**(`golden_check._check_cases`)을 쓰고 세션마다 트레이스를 1회씩 돈다.
  동기 실행이며 `_trace_lock` 을 전 구간 보유한다(도는 동안 수동 트레이스는 409). 지금
  골든 세션은 손으로 늘리는 소수 건이라 요청 안에서 끝내지만, **10건/1분을 넘기기 시작하면
  trace_store 처럼 토큰 폴링으로 바꿔야 한다**(코드에 주석으로 핀). 세션별 예외는 `error`
  필드로 격리해 한 세션 실패가 전체를 죽이지 않는다.
- **CLI 무변경**: `check_session` 을 (트레이스 호출)과 (순수 대조 `_check_cases`)로 쪼개고
  finding 을 dict(`kind`/`item`/`bin`/`signature`/`text`)로 바꿨지만, `text` 에 종전 문장을
  그대로 담아 `main()` 이 그것만 출력한다 → **stdout·종료코드 동일**. 부수 개선으로
  `max_cases=None`(전체)로 트레이스한다 — 기본 상한 400 이면 뒤쪽 항목이 통째로
  `[케이스없음]` 오탐이 됐다.
- **파일 IO**: [server/eval_panel/golden_io.py](../server/eval_panel/golden_io.py).
  선두 주석 보존 + 원자적 쓰기는 `rules_io` 헬퍼를 재사용하지만 **백업만은 골든셋 옆
  `tools/eval_golden/_backup/`** 에 둔다 — `rules/_backup/` 에 섞이면 검증·백업 탭의 "복원"
  버튼이 golden.yaml 을 rules 디렉토리로 되돌리려 한다. 골든셋은 룰이 아니므로
  **`.rules_rev` 를 올리지 않는다**(엔진 판정이 안 바뀌니 세션 캐시를 갈 이유가 없다).
- 검증: [tests/test_eval_golden_io.py](../tests/test_eval_golden_io.py)
  (추가/교체/주석 보존/백업 위치 + `_check_cases` 4종 finding).

## 13. value_type 어휘 `P_F` → `PF` + 단위 별칭 확장 (2026-08-04)

**증상**: `TRIM_LDO23_1.1V` 같은 측정 항목이 아무 판정도 못 받고 조용히 넘어갔다.
원인은 UNIT 원문(`0V`/`mV` 등)이 엔진 정확일치 표
[`ingest.UNIT_TO_VALUE_TYPE`](../eval_analyzer/eval_engine/pipeline/ingest.py) 에 없어
`value_type` 이 양불로 떨어진 것 — 양불 항목은 [metrics.py](../eval_analyzer/eval_engine/pipeline/metrics.py)
가 cpk/stdev/mean 을 **전부 None 으로 비우므로** 모든 `when_metric` 조건이 결측→False 가
되어 어떤 signature 도 발화하지 못한다(→ 발화 0건 → `OK`).

- **단위 별칭 확장**: 배율 접두(`mv`/`uv`/`kv`/`nv`, `na`)와 테스터 표기(`0v`/`0a`)를
  정확일치 표에 등록했다. 선례 적재(db_input)의 부분일치(`UNIT_STEMS`)는 종전대로다.
  - **2026-08-12 추가**: `%`/`pct`/`percent` → **`%`**, `lsb` → **`CODE`**. 실측에서
    무판정 fail 404건 전부가 이 둘(`%` 245 · `LSB` 159)이었다. `%` 는 종전에 db_input
    선례 적재에만 있던 어휘를 엔진으로 승격한 것이라 §10 의 "엔진이 `%` 를 절대 생성하지
    않는다" 는 **더 이상 사실이 아니다**(선례 ↔ live case 의 value_type 불일치가 그만큼
    줄었다). `eval_export.VALUE_TYPES` 와 패널 드롭다운에도 `%` 를 추가했다.
    이미 PF 로 적재된 과거 행은 관리자 **Unit 별칭 재적용** 버튼으로 교정한다.
- **어휘 개명**: 양불 value_type 을 `P_F` → **`PF`** 로 바꿨다. 엔진(ingest/metrics/
  features/status) · export(`eval_export.VALUE_TYPES`) · 관리자 UI · db_input 별칭이 모두 같은 값이다.
- **기존 데이터 마이그레이션** (스키마 DDL 변경 없음, 값만):
  ```
  python -m tools.migrate_value_type_pf <eval.db 경로>            # 미리보기
  python -m tools.migrate_value_type_pf <eval.db 경로> --apply     # 적용
  ```
  대상은 `item_master.value_type` 과 `fail_case.item_class`(`…|P_F|<bin>` 의 가운데 축).
  **DB 가 둘일 수 있다** — eval_analyzer 운영 `eval.db`(`EVAL_DB_PATH`)와 report_server
  코멘트 export DB(`REPORT_EVAL_DB_PATH`). 안 옮기면 `search_precedents` 의 value_type
  등호 하드필터에서 옛 행이 새 케이스와 매칭되지 않는다.
- **진단 노출**: `/pe/eval` 트레이스 케이스 상세가 UNIT 원문과 "PF 라서 통계가 비었다"는
  경고를 함께 찍는다. 목록에도 `PF` 배지가 붙는다.

## 14. 표본 검수 + 승인형 룰 튜닝 — `/pe/eval` 표본함 (2026-08-10)

세션 1건 트레이스에 발화가 수백 건이라 전수 검토가 불가능하고, 그렇다고 안 보면 "이 룰이
과하게 뜨는가" 를 판단할 근거가 없다. **룰당 8건만 검수**해 임계값 강화안을 만드는 화면.
구현 [server/eval_panel/review.py](../server/eval_panel/review.py) + 라우트 4개
(`/api/review/queue|label|collect-session|proposal`), 검증
[tests/test_eval_review.py](../tests/test_eval_review.py).

- **표본 구성**: 활성 룰마다 최대 8건 = 임계값 경계 3 / 중간 3 / 극단 2.
  정렬 키를 `(초과율, case_id)` 로 고정해 **재현 가능**하고, 같은 `analysis_key` 의 dedup
  형제 세션은 하나만 올린다(판정이 같아 검수해도 새로 알게 되는 것이 없다).
  ⚠ 경계를 일부러 과대표집한 **층화표본**이라 여기서 나온 precision 은 전체 precision 보다
  낮게 나온다 — 화면·추천에 그렇게 표기한다.
- **층화 기준은 하드코딩하지 않는다**: 그 룰 `when_metric` 중 임계값 키를 참조하는 첫
  조건에서 뽑고, when_metric 이 판정 기준이 아닌 룰만 yaml `review_metric` 으로 지목한다
  (현재 `BIMODALITY` 하나 — 판정에는 무영향, 표본 정렬 전용).
  ⚠ 그 지표를 스냅샷에서 되살릴 수 없으면(`review._STRATIFIABLE` 밖) **층화하지 않고
  사유를 표시**한다. 뒤에 오는 가드 조건(`fail_count` 등)으로 대신 정렬하면 판정 축이 아닌
  다른 축으로 표본을 뽑게 되어 검수 결과가 임계값 판단에 쓸 수 없게 된다.
  ✅ 2026-08-19(eval.db **v9**, 사용자 승인): 판정 기준값 14종을 `features` 에 저장하게 되어
  `OUTLIER`·공간 4종·`SPOT_FAIL`·꼬리 룰·`BIMODALITY`·`CODE_RAIL` 도 층화된다.
  단 **v9 이전에 수집된 행은 그 컬럼이 NULL** 이라(소급 채움 불가 — per-DUT 원본에서만
  나온다) 재수집 전까지는 표본이 얇게 보이는 것이 정상이다.
- **라벨**: 기존 `label` 테이블 재사용, `labeler='eval-review'`,
  `engine_comment_accepted` 에 맞음(1)/과다발화(0). **`human_status` 는 비운다** — 그래야
  전체 status 채점(`eval_admin.scoring()`, `labeler='eval-panel'` 로 한정)과 섞이지 않는다.
  **DB 스키마 변경 없음**(인덱스 `idx_case_signature_sig` 1개만 추가 — 룰별 조회가 full
  scan 이 되지 않게).
- **"맞음" 은 골든셋에 자동 등록**된다. 이게 없으면 아래 강화안의 안전망이 무효다 —
  골든셋이 비면 회귀가 항상 통과하므로, **골든 항목 0건이면 추천 적용 자체를 막는다**.
- **무판정 트랙**: 저장 게이트는 통과했지만 통계가 비어(`cpk IS NULL` + 발화 0건) 어떤
  룰도 뜰 수 없는 항목을 UNIT 원문과 함께 따로 보여준다. 임계값이 아니라 **엔진 단위표
  등록**으로 고치는 문제라 여기부터 줄이는 것이 임계값 튜닝보다 효율이 높다(§13).
- **추천은 강화 방향만**: 게이트는 룰별 검수 20건 + 맞음·과다발화 각 5건. 후보는
  "과다발화로 표시된 케이스의 지표값" 이고, 라벨된 표본 전체를 재판정해 precision 90% 를
  넘는 것 중 **기존 맞음을 가장 많이 보존**하는 값을 고른다. 느슨하게 만드는 후보는
  v1 에서 만들지 않는다(검수하지 않은 케이스가 새로 뜨는 건 검증된 변경이 아니다).
- **적용은 사람이 누를 때만**, 그리고 **기존 Thresholds 저장 API 를 그대로 탄다** —
  백업·`rules_rev`·낙관적 잠금·감사 로그(`eval_rules_edit`)를 전부 거친다. 오버레이는
  병합 저장이라 한 키만 보내도 그 범위의 다른 임계값은 지워지지 않는다.
  패널은 제품군 오버레이만 저장하므로 **기준값(전 제품 공통) 범위에서는 적용할 수 없다**.
- **`calibrate.recalibrate()` 는 계속 비활성** — 라벨 없이 분위수만으로 룰을 바꾸므로.

### 14-1. signature 포함관계 억제 `suppressed_by`

`OUTLIER` 가 뜨면 `HEAVY_TAIL`(kurtosis)은 **거의 항상** 함께 뜬다 — 멀리 튄 값 하나가
kurtosis 를 4제곱으로 밀어올리기 때문이다. 같은 현상의 약한 표현이 secondary 를 채우고
primary specificity 경쟁까지 흐리므로, 임계값은 그대로 두고 중복 의미만 걷어낸다.

```yaml
- id: HEAVY_TAIL
  suppressed_by: [OUTLIER]            # 이 룰이 발화하면 나는 발화하지 않는다
```

- **코드에 쌍을 박지 않는다** — 같은 모양이 더 있다(`LOW_CPK ← [MEAN_SHIFT, OUTLIER,
  BIMODALITY]`). 선언형이라 룰을 껐다 켜도 관계가 유지된다.
  (구 `OUTLIER_WARN ← [SEVERE_OUTLIER]` 쌍은 두 룰이 `OUTLIER` 로 통합되며 무의미해졌다.)
- 판정은 **억제 적용 전(원본) 발화 집합 기준 1패스**다 — 전이·순환을 원천 차단하기 위해서.
  참조 무결성과 상호 참조는 검증 탭(`rules_io.validate_all`)이 잡는다.
- 트레이스는 그 룰을 "조건은 만족했으나 X 발화에 가려짐" 으로 찍는다(조건만 보면
  "떠야 하는데 안 떴다" 로 읽히므로).
- ⚠ **캐시**: `secondary_signatures`·`evidence`·`reason_codes` 가 줄고
  `ai_comment._rank` 가 그 목록을 보므로 AI Comment 셀 값이 바뀔 수 있다. 룰 yaml 이 아니라
  코드 변경이므로 `rules_rev` 가 아니라 **`cache_policy.REPORT_SCHEMA_VERSION`(→30)** 을
  올렸다.
- 검증: [eval_analyzer/tests/test_signature_suppression.py](../eval_analyzer/tests/test_signature_suppression.py).

## 15. 미분류(unknown) 명시 발화 + 축소 (2026-08-12)

**목표**: 모든 fail 은 signature 로 설명되고, 설명되지 않은 fail 은 `UNKNOWN` 으로
드러나며, 그 UNKNOWN 을 계속 줄여 간다. 종전엔 발화 0건인 fail 이 화면에서만 "미분류" 로
보이고 엔진 판정은 `OK`(정상 확정)로 나갈 수 있어, **설명 못 한 fail 과 정상이 구분되지
않았다**.

### 15-1. 엔진 — UNKNOWN 특수분기

`signatures.yaml` 에 `UNKNOWN` 룰을 선언하고 `signatures._evaluate_unknown` 이 판정한다.
`BIMODALITY` 와 같은 **특수분기**라 `when_metric` 을 쓰지 않는다(패널에서도 조건이 읽기
전용 — `data-nocond`).

- 발화 조건: **억제까지 끝난 최종 발화 집합이 비었고** `fail_count > 0`. fail 을 모르면
  (키 부재) 발화하지 않는다 — 모름을 fail 로 읽지 않는다. 평가 제외 목록(exclusions)에
  걸린 item 도 발화하지 않는다(완전 제외 유지).
- `status_hint: MONITOR` → fail 케이스가 `OK` 로 새지 않는다. **판정이 바뀌는 지점**이라
  골든셋·채점 표본에 영향이 있다.
- evidence `signal_code = UNKNOWN_<사유>` 로 **왜 못 떴는지**를 남긴다. 사유마다 고쳐야 할
  것이 다르다:

| 사유 | 뜻 | 줄이는 방법 |
|---|---|---|
| `NO_STATS_PF` | value_type=PF → L1/L2 가 통계를 전부 비움 | 엔진 UNIT 표 등록 (§13) |
| `NO_LIMIT` | LSL/USL 둘 다 없음 → cpk·spec margin 산출 불가 | limit mapping 확인 |
| `LOW_SAMPLE` | `n_dut < n_min` | 표본 확보 |
| `NO_MATCH` | 통계는 정상인데 어떤 조건에도 해당 없음 | 임계값 조정 / 새 룰 |

- **커버율에서는 UNKNOWN 을 빼고 센다** (`eval_debug._coverage`) — 자동 발화를 성과로
  세면 커버율이 가짜로 100% 가 된다. 트레이스 탭이 사유별 건수를 함께 찍는다.
- **표본함(§14)에는 UNKNOWN 이 뜨지 않는다** — 임계값이 없어 강화할 대상이 없다.
  그 부류의 재료는 무판정 트랙이다.
- Issue Table **ETC 자동 행은 UNKNOWN 만으로는 생기지 않는다**(`ai_comment._to_row_keys`)
  — 자동 행의 취지는 "표에 안 나오는데 룰이 뭔가 잡은 항목" 이라, 설명 못 했다는 표시로
  표를 늘리면 취지에 반한다.

### 15-2. 축소 — 실측 46.8% → 6.9%

로컬 web_report 세션 14건(PMIC), fail case 2,985건 기준 (`fail_only=1`):

| 단계 | 미분류 |
|---|---|
| 종전(활성 4룰) | 1,396건 **46.8%** |
| ① UNIT 표에 `%`/`LSB` 등록 (§13) | 1,143건 38.3% |
| ② `LOW_CPK` + `WIDE_DISTRIBUTION` 재활성 | 341건 11.4% |
| ③ `MEAN_SHIFT` + `HEAVY_TAIL` 재활성 | 207건 **6.9%** |

남은 207건은 전부 `NO_MATCH`(통계 정상·조건 미달)다 — 다음 축소는 임계값 조정이나 새 룰의
몫이고, 근거는 표본함·골든셋으로 쌓는다.

- ⚠ **`CLUSTER_FAIL` 은 켜지 않았다.** 켜면 미분류가 0% 가 되지만 fail case 의 87%
  (2,594/2,985)를 이 룰 하나가 먹는다 — 다른 공간 룰(EDGE/RING/GRADIENT)에는 다 있는
  `fail_count > spatial_fail_count_min` 가드가 이 룰에만 빠져 있어서다. **가드는 yaml 에
  넣어 뒀고**(넣으면 발화 2,594→14건) `enabled:false` 는 유지했다. 나중에 켤 때 이
  지뢰를 다시 밟지 않게 하기 위한 조치다.
- 이 표를 재현하려면 `/pe/eval` 트레이스 탭에서 세션을 돌려 커버리지 줄을 읽으면 된다
  (사유별 건수 포함).

### 15-3. 함께 바뀐 것

`REPORT_SCHEMA_VERSION` **35** (룰 yaml 을 손으로 고쳤으므로 `.rules_rev` 로는 무효화되지
않는다 — 패널 저장 카운터라서). 검증:
[eval_analyzer/tests/test_unknown_signature.py](../eval_analyzer/tests/test_unknown_signature.py)
(발화/미발화·사유 우선순위·제외·LOW_CPK 억제) + `tests/test_eval_review.py`
(표본함에 UNKNOWN 미노출 — 기대 룰 목록을 배포 yaml 에서 유도하도록 바꿨다).

## 16. signature 축 재편 + 공간 존 세분 (2026-08-12)

합성 테스트 데이터([tools/eval_testdata](../tools/eval_testdata/README.md))로 룰별 분포를
나란히 보니 **여러 signature 가 같은 현상을 다른 통계로 본 것**이라 화면에서 갈리지 않았다.
근거는 수식이다 — 대칭 limit 에서 `cpk = 1/(6·spread_norm)` 이므로 `WIDE_DISTRIBUTION` 이
뜨면 `LOW_CPK` 는 **항상** 따라 뜨고, `SPEC_TOO_TIGHT` 은 그 여집합(좁은데 cpk 낮음)일 뿐이다.
`outlier_ratio` 와 `kurtosis` 도 "튀는 값" 하나를 두 통계로 본 것이다.

### 16-1. 현상 5축 — 축당 primary 1개

| 축 | primary | 양보해 secondary 로 (목록에는 남는다) |
|---|---|---|
| 중심 | `MEAN_SHIFT` | — |
| 산포/여유 | `LOW_CPK` \| `BIDIR_TAIL` | — |
| 형태 | `BIMODALITY` \| `OUTLIER` \| `CODE_RAIL` | `USL_TAIL` \| `LSL_TAIL` |
| 공간 | `E1_FAIL` \| `EDGE_FAIL` \| `CENTER_FAIL` \| `RING_FAIL` \| `SPOT_FAIL` | — |
| 데이터 품질 | `MISSING_LIMIT` \| `CONSTANT_VALUE` | — |

양보는 전부 yaml `suppressed_by` 선언이다:
`LOW_CPK ← [MEAN_SHIFT, OUTLIER, BIMODALITY]` · `USL_TAIL`/`LSL_TAIL` `← [OUTLIER]`.
**결과 지표(cpk)는 primary 가 되지 않는다** — "왜 낮은가"를 말하는 룰에 자리를 내준다.
단 **목록에서 사라지지는 않는다**(2026-08-13) — 두 현상이 실제로 다 있으면 둘 다 보여야 한다.

> **2026-08-19 4차 재편(사용자 지시)** — 위 표는 이것까지 반영한 상태다.
>
> **① `CLUSTER_FAIL` 삭제.** 사분면 격자는 실제 결함 모양과 무관한 **인공 경계**라 같은
> blob 이 위치만 달라져도 값이 반토막 났다(축 경계 2.20 vs 한가운데 4.00). 45° 격자를 함께
>재는 보완을 넣어도 원점 근처 뭉침은 여전히 다른 룰 몫이었다. "좁은 한 곳에 뭉쳤나" 는
> `SPOT_FAIL` 이 위치·모양과 무관하게 직접 본다 — 축이 겹치는 룰을 둘 유지할 이유가 없다.
> 임계값 `quadrant_imbalance_warn` 도 함께 삭제(지표 `quadrant_imbalance` 는 참고용으로 계속
> 계산·저장). `features._classify_zone` 의 `CLUSTER` 라벨도 뺐다 — 라벨만 남기면 "zone 은
> CLUSTER 인데 그런 룰이 없다" 가 된다.
>
> **② `SPOT_CLUSTER` → `SPOT_FAIL` 개명** (공간 룰 이름을 `<영역>_FAIL` 로 통일).
> 임계값 키도 `spot_cluster_spread_max` → `spot_fail_spread_max`.
>
> **③ CENTER + SPOT 은 CENTER 만 보인다 — 신규 `hidden_by`.** 중심부에 뭉친 fail 은 두 룰이
> 구조적으로 함께 뜬다(center 점유율 1.0 이면서 좌표도 붙어 있다). `suppressed_by`(목록
> 유지·primary 양보)로는 같은 사실이 두 줄로 남아, 사용자가 "CENTER 만 나오게" 를 요청했다.
> 그래서 **목록에서 통째로 제거**하는 세 번째 관계를 만들었다(`SPOT_FAIL: hidden_by:
> [CENTER_FAIL]`). ⚠ 제거된 발화는 화면 어디에도 안 남으므로 사유는 `/pe/eval` 트레이스에만
> 있다 — 새 선언을 추가할 때는 "정말로 정보가 0 인가" 를 먼저 볼 것.
>
> **④ `HEAVY_TAIL` → `USL_TAIL` / `LSL_TAIL` 방향 분리 + 양쪽이면 `BIDIR_TAIL` — 신규
> `replaces`.** 상한 쪽으로 튀는 것과 하한 쪽으로 처지는 것은 조치가 다른데 `|z|` 하나로
> 재면 구분이 사라진다. L2 에 방향별 꼬리 질량 `tail_mass_3s_high`/`_low` 를 신설했다
> (eval.db **v10** 컬럼 2개). 양쪽이 모두 두꺼우면 "USL 문제 + LSL 문제" 두 건이 아니라
> 분포가 양방향으로 퍼진 한 건이므로 `BIDIR_TAIL` 하나로 접는다 —
> `BIDIR_TAIL: replaces: [USL_TAIL, LSL_TAIL]`(나열한 것이 모두 뜨면 그것들을 지우고
> 자기 `when_metric` 이 성립하지 않아도 대신 발화).
>
> ⚠ **판정 밴드는 여전히 `tail_mass_3s`(양쪽 합)에 건다.** 방향별 질량에 밴드를 걸면
> 대칭 분포의 판정 범위가 사실상 두 배가 된다 — 총 질량 8%(한쪽 4%)면 밴드 상한 5% 를
> 넘겨 일부러 제외했던 "몸통이 벌어진" 항목이 통째로 발화한다(실측: 생성기의 UNKNOWN
> 겨냥 5건 **전부**가 BIDIR_TAIL 로 뒤집혔다). 방향은 파생값
> `tail_side_share_high/low`(그 방향이 가진 꼬리 질량 몫) ≥ `tail_side_share_min`(0.2)
> 로만 가른다 — 한쪽으로만 접힌 꼬리는 1.0/0.0, 대칭이면 0.5/0.5 다.
>
> **⑤ `LOW_SAMPLE_UNCERTAIN` 삭제**(꺼진 채였다. 표본 부족은 `n_min` 가드와
> `data_completeness` 가 이미 말한다) · **⑥ `outlier_sigma` 4.5 → 2.5.**
> ⚠ ⑥ 은 룰 조건식에 직접 쓰이지 않지만 `outlier_ratio` 를 통해 **BIMODALITY 게이트**
> (`subpop_outlier_ratio_max` 0.03)에 물려 있다 — 낮추면 이봉 판정이 보류되는 항목이 는다.
>
> 안전 확인: 운영 eval.db `case_signature` 에 `SPOT_CLUSTER`/`CLUSTER_FAIL` **0건**,
> `label_signature` **0건** → 개명·삭제 마이그레이션 불필요. `HEAVY_TAIL` 은 6건 있으나
> 방향 정보가 없어 소급 치환이 불가능하다(재평가로 갱신된다).
> 회귀 데이터는 `data/eval_testdata_7meta_v12*`(115/115 의도대로, 누락·오발화 0).

> **2026-08-13 3차 재편(사용자 v8 검토 반영)** — 현재 상태는 아래를 다 반영한 것이다.
>
> **① 룰 5종 완전 삭제** — `SPEC_TOO_TIGHT`·`SEVERE_OUTLIER`·`WIDE_DISTRIBUTION`·
> `OUTLIER_WARN`(통합돼 꺼져 있던 것) + `WAFER_GRADIENT`. `enabled:false` 보존을 그만두고
> yaml·SPECIFICITY_ORDER·생성기에서 지웠다. 안전 확인: 운영 eval.db `label_signature` 0행,
> report.db `issue_signature` 편집 0건이라 **마이그레이션 불필요**(과거 `case_signature`
> 발화 기록은 읽기에 영향 없음). 이제 ENGR 라벨 카탈로그에도 이 5개가 안 나온다.
>
> **② HEAVY_TAIL = kurtosis>10 AND 꼬리질량 1~5%.** 초과첨도는 4제곱이라 양쪽으로
> 오해를 만든다 — 점 몇 개만 튀어도 치솟고(질량 0.9% 인데 kurt 21.5 → 그건 outlier),
> 몸통이 갈라진 다봉에서도 커진다(질량 17% 에 kurt 112 → 그건 이봉). 진짜 heavy tail
> 항목의 3σ 밖 질량은 1.55~1.89% 였다. 신규 feature `tail_mass_3s`.
> ⚠ 같은 지표에 상·하한을 걸어야 해서 `when_metric` 값에 **조건 목록(AND)** 을 허용했다
> (`tail_mass_3s: [">=heavy_tail_mass_min", "<=heavy_tail_mass_max"]`) — 엔진
> `_eval_condition`, 패널 `rules_io._validate_condition`, 트레이스 `eval_debug._cond_rows`
> 세 곳이 같은 규약을 안다.
>
> **③ `SPOT_CLUSTER` 신설 + CLUSTER 45° 보완.** 사분면 편중은 **축에 걸친 뭉침을 놓친다** —
> 같은 blob 이 사분면 한가운데면 4.00, x축 경계면 2.20(임계 2.5 미달)이었다. 그래서
> `quadrant_imbalance` 를 0°·45° 두 격자의 max 로 재고, 위치·모양과 무관하게 "fail 좌표가
> 서로 붙어 있나" 만 보는 `SPOT_CLUSTER`(`fail_spread_norm ≤ 0.25`)를 신설했다
> (사용자 제안). fail 무게중심 기준 RMS 거리 / 웨이퍼 반경 — 전면에 흩어지면 ≈0.6.
>
> **④ CODE(이산) 정비** — 신규 룰 없이: `CODE_RAIL` 활성화(레일 처박힘, evidence 를 상·하단
> `rail_low_ratio`/`rail_high_ratio` 로 분리) + **격자 데이터의 BIMODALITY 는 모드 사이 실제
> 빈 레벨 ≥2 를 요구**(계단으로 그린 정규분포는 이산이라 울퉁불퉁한 것이지 이봉이 아니다).
> tail·치우침·산포는 기존 HEAVY_TAIL·MEAN_SHIFT·LOW_CPK 가 그대로 받는다.
>
> **⑤ `BIDIR_TAIL` 재활성화**(양방향 초과는 조치가 다르다) · **AI Comment 에서 `[다봉]` 배지
> 제거**(`_MODALITY_TAG["multimodal"] = ""` — 판정·목록에는 남고 셀 표기만 생략).

> **2026-08-13 2차 재편(사용자 v6 검토 반영)** — 위 표에서 두 가지가 더 바뀌었다.
>
> **① OUTLIER 판정축을 "거리"에서 "거리 AND 끊김"으로.**
> `fail_mad_min ≥ outlier_fail_mad_min(4)` **AND** `fail_pass_gap_sigma ≥ outlier_fail_gap_sigma_min(1.5)`.
> 거리 하나로는 가릴 수 없다는 것이 실측으로 확인됐다 — robust z 13.2 인 항목이 heavy tail
> 이고 8.5 인 항목이 outlier 였다(순서가 뒤집힌다). kurtosis 가 20.1/20.0 으로 같은 두 항목의
> 라벨도 갈렸다. 가르는 것은 **연속성**이다: 꼬리가 몸통에서 limit 까지 이어져 넘어갔으면
> HEAVY_TAIL, 몸통과 끊겨 따로 놀면 OUTLIER.
> `fail_mad_min` 은 **중심에 가장 가까운** fail 의 `|x−median|/MAD` 이고(사용자가 말하는
> "MAD 4배"가 화면에 그대로 보이게 한 것), `fail_pass_gap_sigma` 는
> `min(fail 거리) − max(pass 거리)` 를 robust σ 단위로 잰 빈 구간이다.
> 구 `fail_robust_z_max` 는 evidence 참고값으로만 남고, `fail_value_gap_norm` 은 삭제했다.
>
> ⚠ **구조적 성질**: 정규 몸통에서 최대 pass 거리는 표본이 크면 ≈3.85σ 로 고정이므로
> `gap ≈ 3·cpk·(σ/robustσ) − 3.85` 다. 즉 **cpk 가 넉넉한데 fail 이 난 항목**은 fail 이
> 하나만 있어도 OUTLIER 가 성립한다(그게 맞는 판정이다 — 공정능력이 충분한데 죽었으면
> 산발 이상이다). 균등분포처럼 꼬리가 없는 모양은 최대 pass 거리가 1.35σ 뿐이라 낮은 MAD
> 배수에서도 성립한다. 이 지표는 **분포 모양을 본다.**
>
> **② `suppressed_by` 의 의미를 "목록에서 제거" → "primary 만 양보" 로.**
> 종전에는 결과 룰을 발화 목록에서 지웠는데, 그러면 "cpk 도 낮고 outlier 도 있다" 가 한 줄로만
> 보여 사용자가 나머지를 볼 수 없었다(실사용 피드백: "여러 개 걸리면 중복해서 잘 안 나온다").
> 지금은 `signatures._apply_suppression` 이 `demoted_by` 를 달기만 하고,
> `status.decide` 가 같은 severity 안에서 primary 후보에서만 뺀다. 부수 효과로 결과 룰의
> severity 가 status 에 그대로 반영된다(MAJOR 결과 룰이 사라지며 status 가 내려가던 문제 해소).
>
> **③ 양자화 오탐 제거** — 계단형(CODE·PCT) 값에서 히스토그램 bin 폭이 계단 간격보다 좁으면
> 빈 칸이 사이사이 끼어 가짜 봉우리가 생겨 BIMODALITY 가 오발화했다(실측: 8단 단봉이 봉우리
> 8개). `features._grid_step` 으로 격자를 검출해 bin 경계를 계단에 맞춘다. v6 전수에서
> BIMODALITY 20→13 건이 되고 **사라진 7건이 전부 양자화 항목**(진짜 이봉은 하나도 안 잃음).

> **2026-08-12 룰셋 재편(사용자 v5 데이터 검토 반영)** — 위 표는 재편 후 상태다.
> `SEVERE_OUTLIER`+`OUTLIER_WARN` → **`OUTLIER`** 통합(판정을 비율에서 **거리**로:
> `fail_robust_z_max ≥ 12`, MAD 기반 robust z) · `SPEC_TOO_TIGHT`·`WIDE_DISTRIBUTION` →
> `LOW_CPK` 로 통합(off) · 공간 4종을 **점유율 95%**(`*_fail_share ≥ region_fail_share_min`)로
> 재정의해 활성화 + `CLUSTER_FAIL` 임계 1.0→2.5 로 올려 활성화 ·
> `kurtosis_warn` 2.0→8.0 · `SUBPOP_GAP` → **`BIMODALITY`** 개명(누적 DB 는
> [server/tools/migrate_bimodality_rename.py](../server/tools/migrate_bimodality_rename.py)
> 로 1회 치환). 꺼진 구 룰은 **선언을 지우지 않는다** — 과거 발화·정답라벨이 그 이름으로
> eval.db 에 남아 있어 패널·트레이스가 계속 해석해야 한다.
>
> ✅ **표본함(§14) 제약 해소 — eval.db v9 (2026-08-19, 사용자 승인)**: 종전에는
> `fail_mad_min`·`*_fail_share` 같은 **판정 기준값**이 저장되지 않아 `OUTLIER` 와 공간 4종이
> "층화 불가"였다. 이 값들은 L2 `features.compute()` 가 이미 계산해 반환하고 있었고
> `store.save_features` 의 화이트리스트만 버리고 있었으므로, **계산 경로 변경 없이**
> 컬럼 14개(`store._V9_FEATURE_COLS`)를 추가해 저장한다(`ALTER TABLE ADD COLUMN` —
> PK 불변, 기존 행 재작성 없음). raw(per-DUT) 저장 금지 규칙(불변 규칙 3)은 그대로다 —
> 저장하는 것은 원본이 아니라 계산된 판정 지표다.
> ⚠ v9 이전 행은 NULL 이며 **소급 채움이 불가능**하다(원본에서만 나온다). 값이 필요하면
> 그 세션을 `force=true` 로 재수집한다. 소비자(층화·calibrate·근거 팝업)는 전부 NULL 을
> "표본 제외"로 안전 처리한다.

### 16-2. 공간 존 세분 — E1(최외곽 1 chip line) 신설

`EDGE`(반경 상위 20%) 하나로는 "가장자리 한 줄"이 안 보였다. 한 줄의 두께는 웨이퍼·die
크기에 따라 달라 **반경 비율로 표현할 수 없다**. 그래서 좌표만으로 판정하되
**각 행의 좌·우 끝 + 각 열의 위·아래 끝** die 를 E1 으로 본다(`features._e1_mask`).

> ⚠ 처음엔 4-이웃(x±1, y±1) 조회로 만들었다가 바로 고쳤다 — 그 방식은 **die pitch 가 1**
> 이라는 가정을 숨기고 있어서, 좌표 간격이 2 이거나 격자를 띄엄띄엄 측정한 map 에서
> **모든 die 를 최외곽으로 오판**한다(실측 100%). 그러면 `edge = 반경밴드 & ~E1` 이 비어
> **EDGE·RING 룰이 조용히 죽는다**. 지금 정의는 간격을 전혀 가정하지 않고, E1 이 절반을
> 넘으면(한 줄짜리 배치 등) 판정 불가로 보고 결측 처리한다.
> 회귀 테스트: `test_e1_mask_is_die_pitch_agnostic` / `test_e1_mask_undecidable_when_degenerate`.

**함께 고친 것 — 웨이퍼 중심**: 공간 feature 가 반경을 **원점(0,0) 기준**으로 재고 있었다.
`XPOS`/`YPOS` 는 실데이터에서 **항상 양수**(0/1-based die 인덱스)이므로 원점은 웨이퍼의 한
귀퉁이다 — edge/center/ring/quadrant 가 전부 어긋난 값이었다. 이제 좌표 범위의 중앙
(bounding box 중심)을 웨이퍼 중심으로 잡는다. 이미 중심 정렬된 입력에는 no-op 이라 과거
동작과 충돌하지 않는다. 회귀 테스트 `test_spatial_features_are_translation_invariant`.
좌표 규약은 [../CLAUDE.md §5-9](../CLAUDE.md) 에도 박아 뒀다(PMIC 은 YPOS ≤ 200).

- 새 feature `e1_fail_ratio` + 임계 `e1_fail_ratio_warn`(2.0) + 룰 `E1_FAIL`.
- `edge_fail_ratio`/`ring_fail_ratio` 는 **E1 을 뺀** 영역 기준으로 의미가 좁아졌다.
- `wafer_zone_signature` 에 `E1` 값이 추가됐다(TEXT 컬럼이라 DDL 변경 없음).
- **eval.db 스키마는 그대로다** — 새 feature 는 `store.save_features` 의 cols 목록에 없어
  메모리 계산으로만 쓰인다(저장이 필요해지면 그때 별도 승인).

### 16-3. 발화 불가였던 룰 2종 수선

| 룰 | 문제 | 조치 |
|---|---|---|
| `TAIL_RISK` | 지표 `skewness` 가 **비모수 왜도**((mean-median)/stdev) 라 수학적 상한이 1.0 — 임계 `skew_warn: 1.0` 을 넘을 수 없어 켜도 영원히 미발화 | 상한 없는 **모멘트 왜도** `skewness_moment` 신설 후 룰을 그쪽으로 교체(기존 `skewness` 컬럼은 의미 보존을 위해 그대로 둔다) |
| `RING_FAIL` | ring 영역이 die 의 절반 이상이라 `ring_fail_ratio` 상한이 ~1.8 — 임계 2.0 에 도달 불가 | 임계 **1.5** 로 하향 |

같은 종류의 상한이 다른 공간 룰에도 있다: `edge_fail_ratio` 상한 ≈ 1/edge면적비,
`center_fail_ratio` ≈ 11, `quadrant_imbalance` ≤ 4. **임계값을 상한 위로 두면 그 룰은
영원히 침묵한다** — `/pe/eval` 에서 임계를 올릴 때 이 점을 확인할 것.

### 16-4. 캐시·검증

- 룰 yaml 을 손으로 고쳤으므로 `.rules_rev` 를 **직접 올렸다**(`eval_debug.bump_rules_rev`).
  ai_comment 옵션 세션의 캐시만 무효화되므로 `REPORT_SCHEMA_VERSION` 은 건드리지 않았다.
- 검증: `eval_analyzer` 엔진 테스트 189 passed(E1 존·pitch 무관 테스트 추가·TAIL_RISK 지표 갱신),
  `rules_io.validate_all()` 무결성 0 problems, 합성 데이터 정답표 대조
  (단독 CSV 85/85, 전체 세트 496/500 — 나머지는 조합 항목의 구조적 상충).
- `label_signature`(사람 확정 라벨)는 조회 결과 **0행**이라 id 마이그레이션이 필요 없었다.
