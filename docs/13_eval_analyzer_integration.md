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

## 4. eval.db 소유권 — 서버는 무기록(persist=False)

- eval.db 는 **eval_analyzer 전용** (`eval_analyzer/data/eval.db`, env `EVAL_DB_PATH`).
  report_server 의 `report_` prefix 테이블 규칙 대상이 아니다.
- 서버 호출은 **persist=False 고정**: eval.db 에 아무것도 쓰지 않는다 → 컴퓨트 워커
  동시 실행에 안전하고 ingest_run 증식이 없다. 선례검색(sql)은 DB 파일이 없으면 빈
  목록을 반환하므로 eval.db 없이도 동작한다 (comment 는 룰 템플릿 기반).
- persist=True 로 전환하려면: 워커 동시 쓰기(WAL+busy_timeout 은 있음)·콜드 빌드마다
  ingest_run 행 증식·preview↔persist 간 case_id 불일치(엔진 docstring)를 먼저 검토할 것.
- ⚠ **그래서 L1(raw_metrics)·L2(features)·evaluation 이 운영에서 0행이다.** 판단 근거를
  쌓아 채점·보정에 쓰려면 별도 경로가 필요하다 → 설계·로드맵은
  [17_eval_learning_loop.md](17_eval_learning_loop.md) (2026-08-04, 아직 미구현).
- §9 의 **사람 코멘트 export DB 는 이 규약과 별개** — eval_analyzer 소유 eval.db 가 아니라
  report_server 소유의 **별도 파일**(`REPORT_EVAL_DB_PATH`)이다. `EVAL_DB_PATH` 는 여전히
  건드리지 않으며 evaluate 의 선례검색 동작도 무변경이다.

## 5. 재계산·캐시 규약

- evaluate 호출 지점은 **콜드 빌드 1곳** — `service.load_webreport` 의 인라인/워커 빌드
  (`build_report_payload` 직전). 캐시 키 `cache_policy.report_key` 에 `content_hash` 와
  `webreport_options` 가 들어 있으므로 **rawdata 편집 시 자동 재평가**되고, 옵션 on/off
  세션은 캐시가 분리된다.
- **룰(threshold/signature) 편집도 재평가시킨다** (2026-08-03). AI Comment 는 payload 안에
  박혀 캐시되므로 rules yaml 을 고쳐도 그 자체로는 무효화되지 않는다 → `/pe/eval` 저장이
  `rules/.rules_rev` 를 +1 하고 `report_key` 가 그 값을 키에 덧붙인다. **ai_comment 옵션
  세션에만** 덧붙고 rev 파일이 없으면 아무것도 붙지 않으므로, 패널을 안 쓰는 서버·일반
  세션의 기존 캐시는 그대로 유효하다(`REPORT_SCHEMA_VERSION` 은 건드리지 않는다 — 그건
  코드 배포용).
- 엔진 쪽 반영은 캐시 키에 파일 mtime 을 넣어 해결한다 — 웹 프로세스든 컴퓨트 워커든
  다음 호출에서 자동 재파싱이라 **프로세스 간 리로드 신호가 필요 없다**.
- evaluate 실패(메타 부적합·의존 미설치 등)는 `ai_comment.safe_build` 가 빈 dict 로
  격리한다 — **IssueTable 빌드는 절대 죽지 않는다** (컬럼은 뜨되 빈 값 + warning 로그).

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

엔진은 SUBPOP_GAP 이 **primary_signature 일 때만** 코멘트 본문에 이봉 문구를 쓴다
(`recommend._phenomenon_text`). 그런데 SUBPOP_GAP 은 `status.SPECIFICITY_ORDER` 21개 중
18번째라 같은 MAJOR 인 WIDE_DISTRIBUTION·TAIL_RISK 등에 밀리기 쉽고, 이봉 분포는 산포도
넓어 그 동시발화가 흔하다 → **발화해도 코멘트에 안 보이던 문제**.

`ai_comment._modality_tag` 가 `case["signatures"]` 에서 SUBPOP_GAP 항목의
`evidence[signal_code=="MODALITY_V2"].note`(`"modality_v2 <label>"`)를 직접 읽어
**primary/secondary 구분 없이** status 뒤에 배지를 붙인다. 엔진은 수정하지 않는다.

- 접두인 이유: `report_view.html` 의 `.kind-issue td.st-comment` 가 `white-space: normal`
  + 330px 고정이라 comment 의 개행이 붕괴된다 — 말미 추가 문장은 문단에 묻힌다.
- note 포맷이 바뀌면 `[분포분리]` 로 degrade한다 (조용한 미표시 방지).
- 수치(BC/n_modes/density_gap) 확인은 셀이 아니라 `/pe/eval` 트레이스가 정본 (§11).
- **엔진 사설 계약 핀**: `present.to_result` 의 `signatures[].evidence[].note` 포맷.
- 캐시: 셀 **값**이 바뀌므로 `cache_policy.REPORT_SCHEMA_VERSION` 22 로 올렸다.

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
PTE/개발 comment 를 **eval.db 스키마(17테이블, SCHEMA_VERSION=4) 그대로의 별도 SQLite**
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
  (labeler=`web_report`, label_quality=`manual`, reviewer=마지막 편집자). row_key →
  bin: `Yield|<bin>|<item>`→bin, `CPK|<item>`→1(PASS_BIN 관례), `ETC|<item>`→NULL,
  Pass 요약행(`Yield|1|…`)은 skip. wafer_number=NULL(lot 수준 case).
  item/unit/limit 은 honeyform tables 에서, fail/total/cpk 통계는 best-effort
  (rawdata 에 없는 자유입력 ETC 항목은 코멘트만). `ingest_run.session_id` 로 세션
  역참조, run_case 차집합으로 **삭제된 코멘트의 label 정리**(fail_case 는 보존).
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
    `rules/*.yaml` 은 `item_class = category_major|value_type|bin` 스코프라 `%` 스코프가
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
- **탭 5개**:
  1. *Thresholds* — 제품군 × family_product 드롭다운으로 오버레이 편집. 병합 순서는
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
  3. *L0~L6 트레이스* — 세션 1건을 AI Comment 와 **같은 경로**(loader→mode_tables→
     `ai_comment._table_to_raw_df`)로 재현하되 `evaluate()` 대신 단계 함수를 직접 호출해
     raw_metrics/features/조건분해를 노출한다. signature 21행 매트릭스에 조건별
     `실제값 ⟨op⟩ 임계값(키=값)` 과 미발화 사유(disabled / min-n 가드 / 특수분기 / 결측)를
     찍는다. **`should_store` 게이팅 탈락 케이스도 포함**한다 — "왜 코멘트가 안 나왔나" 가
     이 화면의 주 용도다. 결과는 프로세스 메모리 LRU(4런/30분)에 두고 상세는 1건씩 조회.
     - **SUBPOP_GAP 만 예외 처리**(2026-08-03): 이 룰은 `when_metric` 을 쓰지 않고
       `features.modality_v2` 로 판정하는 하드코딩 특수분기라, yaml 의 `when_metric`·
       `evidence` 선언은 **죽은 설정**이다(패널에서 고쳐도 무효 — `status_hint`/
       `phenomenon_ko`/`action_ko` 만 실효). 그래서 조건을 못 찍어 "왜 안 잡혔나" 를
       볼 수 없었다 → `eval_debug._subpop_conditions` 가 엔진
       `features._classify_modality_v2` 의 AND 체인을 10행으로 미러링해 찍는다
       (게이트 2행 + multimodal/bimodal/separated 분기 각각 — separated 는 2026-08-03
       부터 cdf_gap 대신 `value_gap_ratio`/`minor_mass` 기준). `skip_reason` 대신
       `branch_note` 필드로 내려보내 조건과 **함께** 렌더된다. 임계값은 키 이름으로만
       읽는다(하드코딩 금지) — **엔진이 분기 구조를 바꾸면 이 함수도 고쳐야 한다**.
     - **분포 미니차트**(2026-08-03): 케이스 상세 최상단에 히스토그램(막대)+ECDF(주황선)+
       LSL/USL(빨간 점선)+mean/median(삼각) 을 vanilla canvas 로 그린다(`drawDist`,
       외부 라이브러리 없음 — 페이지에 Plotly 가 없다). 수치 표만 보고 임계값을 고치면
       "왜 이 값이 나왔나"를 확인할 수 없어서 넣었다. 데이터는 `_trace_case` 의
       `dist` 필드 — 측정값 전량을 정렬해 내리되 5,000 초과 소스는 서버에서 60-bin
       히스토그램으로 축약한다(trace_store 4런 보관, **표시용이며 판정에는 무관**).
       2026-08-04 부터 **런 단위 값 예산**(`_DIST_VALUES_BUDGET`)도 함께 걸린다 —
       전체 트레이스에서 케이스 수에 비례해 메모리가 늘지 않게 하는 상한이다.
       차트는 web_report Distribution 미니셀처럼 **좁은 카드**로 그린다(가로로 늘어진
       캔버스는 봉우리가 눌려 육안 구분이 어려웠다).
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
  4. *Eval DB* — **admin 대시보드에서 이관**(2026-08-03). 코멘트 라벨 목록·검색·컬럼 토글·
     CSV export·Unit 그룹 교정·세션 재적재·케이스 삭제. 마크업/JS 는 admin_panel.html 에서
     그대로 옮겼고 구현 모듈은 여전히 `admin_panel/eval_admin.py` 를 import 한다
     (라우트만 `/pe/eval/api/eval/*` 로 이동 — admin 쪽 구 라우트는 삭제).
     eval 관련 화면을 한 페이지에 모으기 위한 이동이다.
  5. *검증·백업* — 참조 무결성(`when_metric` 이 참조하는 임계값 키 존재, 오버레이 고아
     파일, 전 PT×family 조합 병합 시뮬레이션, SPECIFICITY_ORDER 정합) + 백업 목록/복원.
  6. *채점* (2026-08-03) — **엔진 판정 vs 사람 정답** 집계. 트레이스 케이스 상세의
     "정답 라벨" 폼(수용/정정 + 코멘트/root cause)이 `POST /pe/eval/api/eval/label` →
     `eval_export.save_human_label` 로 export DB 에 **evaluation(엔진 스냅샷) +
     label(eval_id 연결, labeler=`eval-panel`)** 쌍을 저장하고(같은 case 재검수는 교체),
     채점 탭이 `eval_admin.scoring()` 으로 혼동행렬·status 일치율·MAJOR+ 정밀도/재현율·
     수용률·signature 별 집계를 보여준다. **이 쌍이 룰 정확도 검증(calibrate 후속 3번)의
     원재료**다. 검증: [tests/test_eval_label_scoring.py](../tests/test_eval_label_scoring.py).
- **저장 파이프라인**: 검증 → `rules/_backup/` 백업(파일당 50개, 같은 초면 `-2` 접미사)
  → tmp+`os.replace` 원자적 쓰기(LF 유지) → `.rules_rev` +1 → 감사 로그
  `action=eval_rules_edit`, `client_user=eval-panel`.
  ⚠ signatures.yaml 재작성은 **선두 주석 블록만 보존**하고 인라인 주석은 잃는다(백업이 이력).
- **패널이 만드는 파일** (전부 rules/ 하위, 없으면 엔진은 종전과 동일 동작):
  `thresholds/<PT>/*.yaml` 오버레이 · `_backup/*.bak` · `.rules_rev`.
- 검증: [eval_analyzer/tests/test_rules_scope.py](../eval_analyzer/tests/test_rules_scope.py)
  (트리 없을 때 무회귀 / 병합 우선순위 / mtime 자동 리로드 / enabled 미발화).

## 12. 룰 축소 디버깅 체제 (2026-08-03)

룰 21개를 동시에 굴리면 임계값 하나를 고쳤을 때 무엇이 좋아지고 나빠졌는지 볼 수 없어,
**SPEC_TOO_TIGHT / SEVERE_OUTLIER / OUTLIER_WARN / SUBPOP_GAP 4개만
남기고 나머지를 `enabled: false`** 로 껐다(2026-08-04 CONSTANT_VALUE 추가로 끔 — 5→4개). 개념이 잡히는 대로 `/pe/eval` Signatures 탭에서
하나씩 다시 켠다. 되돌리기는 그 탭의 일괄 켜기 한 번이다(코드 변경 없음).

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

## 13. value_type 어휘 `P_F` → `PF` + 단위 별칭 확장 (2026-08-04)

**증상**: `TRIM_LDO23_1.1V` 같은 측정 항목이 아무 판정도 못 받고 조용히 넘어갔다.
원인은 UNIT 원문(`0V`/`mV` 등)이 엔진 정확일치 표
[`ingest.UNIT_TO_VALUE_TYPE`](../eval_analyzer/eval_engine/pipeline/ingest.py) 에 없어
`value_type` 이 양불로 떨어진 것 — 양불 항목은 [metrics.py](../eval_analyzer/eval_engine/pipeline/metrics.py)
가 cpk/stdev/mean 을 **전부 None 으로 비우므로** 모든 `when_metric` 조건이 결측→False 가
되어 어떤 signature 도 발화하지 못한다(→ 발화 0건 → `OK`).

- **단위 별칭 확장**: 배율 접두(`mv`/`uv`/`kv`/`nv`, `na`)와 테스터 표기(`0v`/`0a`)를
  정확일치 표에 등록했다. 선례 적재(db_input)의 부분일치(`UNIT_STEMS`)는 종전대로다.
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
