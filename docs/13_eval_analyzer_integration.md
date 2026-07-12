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

## 2. 단방향 의존 — import 는 1곳만

```
report_server ──(web_report/ai_comment.py 1곳)──►  eval_analyzer(eval_engine)
              ◄──────────── 금지 ────────────────
```

- eval_analyzer 는 report_server 를 **import 하지 않는다** (eval_analyzer/CLAUDE.md 불변 규칙 1).
- report_server 에서 eval_engine 을 import 하는 곳은
  **[web_report/ai_comment.py](../web_report/ai_comment.py) 단 한 곳**이다.
  pip 미설치 — 이 모듈이 `sys.path.append(<repo>/eval_analyzer)` + 지연 import 로 연결한다
  (append 라 report_server 쪽 top-level 이름이 항상 우선, 컴퓨트 워커에서도 호출 시점 성립).
- 다른 서버 코드가 eval_engine 이 필요하면 **ai_comment.py 의 함수를 거친다**. 위반 = 리뷰 반려.

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
