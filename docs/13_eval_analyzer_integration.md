# 13. eval_analyzer 통합 — AI Comment (단방향 의존 규약)

> 2026-07-12 흡수 통합. 이 문서는 한 달 이상 유지될 **굵직한 규약만** 담는다 —
> 엔진 내부 구현·알고리즘·스키마의 정본은 [eval_analyzer/docs/](../eval_analyzer/docs/) 다.

---

## 1. 배치·소유권

- `eval_analyzer/` 는 **독립 프로젝트의 운영 복사본**이다 (원본: `F:\COINAPI\eval_analyzer`,
  자체 git 이력은 이 repo 로 이어지지 않음). 반도체 fail-item 평가 엔진 —
  `evaluate()` 하나로 L0~L6 판단 파이프라인을 돌려 status/comment 를 반환한다.
- **report_server 작업 중에는 `eval_analyzer/` 하위 파일을 수정하지 않는다** (원본과
  diff 최소화 → 향후 원본 동기화 용이). eval_analyzer 자체 개발은 그쪽 CLAUDE.md 규칙을 따른다.
- 제외하고 복사한 것: `.git/`, `__pycache__/`, `*.egg-info/`, `.claude/`·`.agents/`,
  런타임 db(`data/*.db`, `db_input/output/*.db`). 중첩 `.gitignore` 가 런타임 db 를 계속 차단한다.

## 2. 단방향 의존 — import 는 2곳만

```
report_server ──(web_report/ai_comment.py — evaluate 호출)──►  eval_analyzer(eval_engine)
report_server ──(web_report/eval_export.py — store·ingest 헬퍼)──►  eval_analyzer(eval_engine)
              ◄──────────── 금지 ────────────────
```

- eval_analyzer 는 report_server 를 **import 하지 않는다** (eval_analyzer/CLAUDE.md 불변 규칙 1).
- report_server 에서 eval_engine 을 import 하는 곳은 **딱 2곳**이다:
  - [web_report/ai_comment.py](../web_report/ai_comment.py) — `evaluate()` 호출 (AI Comment, §3~§6)
  - [web_report/eval_export.py](../web_report/eval_export.py) — `store` CRUD + `pipeline.ingest`
    item 정규화 헬퍼 (사람 코멘트 export, §9)
  pip 미설치 — 두 모듈 다 `sys.path.append(<repo>/eval_analyzer)` + 지연 import 로 연결한다
  (append 라 report_server 쪽 top-level 이름이 항상 우선, 컴퓨트 워커에서도 호출 시점 성립).
- 다른 서버 코드가 eval_engine 이 필요하면 **위 두 모듈의 함수를 거친다**. 위반 = 리뷰 반려.

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
- §9 의 **사람 코멘트 export DB 는 이 규약과 별개** — eval_analyzer 소유 eval.db 가 아니라
  report_server 소유의 **별도 파일**(`REPORT_EVAL_DB_PATH`)이다. `EVAL_DB_PATH` 는 여전히
  건드리지 않으며 evaluate 의 선례검색 동작도 무변경이다.

## 5. 재계산·캐시 규약

- evaluate 호출 지점은 **콜드 빌드 1곳** — `service.load_webreport` 의 인라인/워커 빌드
  (`build_report_payload` 직전). 캐시 키 `cache_policy.report_key` 에 `content_hash` 와
  `webreport_options` 가 들어 있으므로 **rawdata 편집 시 자동 재평가**되고, 옵션 on/off
  세션은 캐시가 분리된다. 캐시 키에 새 요소를 추가할 필요 없음.
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

  셀 텍스트 = `[<status>] <comment>`. 여러 소스에서 같은 (item,bin) 이 나오면 severity
  높은 쪽이 남는다. 미사용 키는 그냥 버려진다 (CPK/ETC 행이 없으면 무해).

## 7. 클라이언트 옵션 (Honey)

- Web Report 그룹박스의 **"AI Comment" 체크박스** (honey_main.py) ↔
  `%APPDATA%/Honey/settings.json` 키 `webreport_ai_comment` 로 영속.
- 업로드 시 `manifest.options.ai_comment` 로 실려 서버 `report_session.webreport_options`
  에 고정 저장된다 — **업로드 후 토글 불가** (옵션이 캐시 키·dedup 에 묶이는 세션 불변값).
- 현재 **setEnabled(False) 비활성 노출** 상태 — 서버 파이프라인 실사용 검증 후
  `setEnabled(True)` 한 줄로 활성화한다.

## 8. 의존성

- eval_engine 런타임 의존: numpy·pandas(기존 충족) + **pyyaml** (server/requirements.txt 반영).

## 9. 사람 코멘트 export — Issue Table PTE/개발 comment → eval 스키마 DB (2026-07-15)

eval_analyzer 가 엔지니어 코멘트를 선례(precedent)로 소비할 수 있도록, Issue Table 의
PTE/개발 comment 를 **eval.db 스키마(17테이블, SCHEMA_VERSION=4) 그대로의 별도 SQLite**
로 적재한다. 구현 [web_report/eval_export.py](../web_report/eval_export.py),
검증 [tests/test_eval_export.py](../tests/test_eval_export.py).

- **DB 파일**: `REPORT_EVAL_DB_PATH` (기본 `DB/pe/report/eval/eval.db`) — report_server
  소유, session DB(report.db)와 분리. eval_analyzer 쪽은 실행 시 `EVAL_DB_PATH` 를 이
  파일로 지정해 읽는다(코드 무수정). 스키마는 엔진 `store.SCHEMA` 를 그대로 적용 —
  **스키마 변경 금지**.
- **트리거 3곳** (모두 try/except + 데몬 스레드 `export_async` — 실패해도 업로드/저장
  무영향): ① 세션 업로드 ingest 의 시드 직후, ② `service.update_issue_comments`,
  ③ `service.update_issue_etc_items`. 매번 세션 **전체 코멘트 상태 재적재**(멱등).
- **매핑**: PTE+개발 comment 를 `"[PTE] ...\n[개발] ..."` 로 **병합해 label 1행**
  (labeler=`web_report`, label_quality=`manual`, reviewer=마지막 편집자). row_key →
  bin: `Yield|<bin>|<item>`→bin, `CPK|<item>`→1(PASS_BIN 관례), `ETC|<item>`→NULL,
  Pass 요약행(`Yield|1|…`)은 skip. wafer_number=NULL(lot 수준 case).
  item/unit/limit 은 honeyform tables 에서, fail/total/cpk 통계는 best-effort
  (rawdata 에 없는 자유입력 ETC 항목은 코멘트만). `ingest_run.session_id` 로 세션
  역참조, run_case 차집합으로 **삭제된 코멘트의 label 정리**(fail_case 는 보존).
- **엔진 코드 재사용** (eval_analyzer 무수정): `store` CRUD 는 전부 `conn=` 주입 —
  `eval_engine.config.DB_PATH` 는 절대 변경하지 않는다. item 정규화는
  `pipeline.ingest._alias_map/_canonicalize/_classify_category_major/_classify_value_type`
  재사용(=db_input/import_csv.py 와 동일 패턴 → 선례 fuzzy 매칭 일관).
  **사설 API 의존 핀**: `store._migrate` / `store._seed_bin_taxonomy` /
  `pipeline.ingest._*` — 원본 동기화로 시그니처가 바뀌면 eval_export 만 고치면 된다
  (실패는 safe_export 가 격리).
- **관리**: `/pe/admin-pte/` **Eval DB 탭** — overview(파일/건수), label 목록 검색
  (product/family/lot/item/comment/세션ID), 컬럼 표시 토글(lot 기본 숨김, 선택은
  localStorage `adminEvalHiddenCols`), 케이스 단위 완전 삭제, 세션 재적재,
  **코멘트 CSV 다운로드**(§10). 세션 삭제 시 export 데이터는 자동 삭제하지
  않는다(선례 보존) — 정리는 이 탭에서 수동.

## 10. 과거 사례 수동 적재 — db_input 5컬럼 CSV (2026-07-28)

엔지니어가 손으로 정리한 과거 코멘트를 같은 eval DB(`REPORT_EVAL_DB_PATH`)에 넣는 경로.
구현은 **`eval_analyzer/db_input/` 안에서만** 한다(엔진 무수정 — §2 규약 유지).

- **입력 계약**: `Product type, Family Product, unit, Item, comment` 5컬럼(헤더 대소문자·
  공백 유연). 기존 20컬럼 레거시 CSV 도 헤더 자동감지로 계속 동작한다.
  정본 설명은 [eval_analyzer/db_input/CLAUDE.md](../eval_analyzer/db_input/CLAUDE.md).
- **unit 정규화**: 원문(VOLTS/HERTZ/AMPS…)을 엔진 어휘(V/A/Hz/CODE/Ohm/Sec/P_F)로 매핑한다.
  매핑표 = 엔진 `UNIT_TO_VALUE_TYPE` + db_input `EXTRA_UNIT_ALIASES`.
  **모르는 단위가 하나라도 있으면 아무것도 적재하지 않고 중단**(행번호+원문 출력) —
  `search_precedents` 가 `value_type` 을 등호 하드필터로 쓰기 때문에 조용한 P_F 폴백은
  선례를 영구 미매칭으로 만든다.
- **실행**: 서버에서 `eval_analyzer\db_input\run_import.bat` 더블클릭 → CSV 선택.
  bat 이 report_server 안의 사본임을 감지해(`..\..\server\config.py`) `EVAL_DB_PATH` 를
  서버 소유 eval.db 로 잡고 `--to-eval-db` 를 붙인다 → 관리자 탭에 바로 보인다.
  원본 저장소(F:\COINAPI\eval_analyzer) 단독 실행은 기존 per-family output 동작 그대로.
- **왕복**: 관리자 탭 **CSV 다운로드**(`GET /api/eval/labels.csv`)가 같은 5컬럼으로 내보내고
  (unit 은 `im.value_type` = 엔진 어휘), 고쳐서 재적재하면 같은 case 의 label 이 갱신된다.
  ⚠ 단순 포맷은 lot/wafer/bin 이 없어 `product_name=<pt>_<fp>`·`bin=0` 으로 **case 를 합성**
  하므로 왕복은 의도적으로 lossy 다 — web_report 라벨을 재적재하면 합성 case 가 1건 생긴다
  (labeler 가 달라 서로 지우지 않는다: 세션 재적재 reconcile 은 `labeler='web_report'` 만 본다).
