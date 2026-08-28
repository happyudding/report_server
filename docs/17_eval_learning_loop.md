# 17. eval 학습 루프 — L1/L2 적재와 case grain 재정의 (설계·로드맵)

> 2026-08-04 작성. [13](13_eval_analyzer_integration.md) 이 "무엇을 어떻게 연결했나"(현행
> 규약)라면, 이 문서는 **"무엇이 비어 있고 어떤 순서로 채우나"**(로드맵)다.
>
> **2026-08-10 진행 상황** — Phase 2(L1/L2 적재)가 **스키마 변경 없이** 구현됐다.
> 정본 규약은 [13 §4](13_eval_analyzer_integration.md) 로 옮겼고, 그 위에 표본 검수 +
> 승인형 룰 튜닝([13 §14](13_eval_analyzer_integration.md))이 얹혔다.
>
> | 항목 | 상태 |
> |---|---|
> | Phase 1 스키마 변경 (`item_master`/`item_alias` UNIQUE·PK) | **미착수** — v1 범위 밖 |
> | Phase 2 L1/L2 적재 (`db_path` 인자 + 업로드 훅) | **완료** |
> | §3-2 case 키에서 bin 제거 | **완료**(2026-08-19, 사용자 결정) — 아래 "bin 제거" 절 |
> | §3-5 `item_class` 2단화 | **보류** — 운영 챗봇이 3축을 집계 축으로 쓴다 |
> | Phase 3 `eval_export` case 규칙 정렬 | **완료**(2026-08-19, 보수안 — wafer 정합 + ETC bin) |
> | 룰 판정지표 저장 (eval.db **v9**) | **완료**(2026-08-19) — 표본함 층화 제약 해소 |
> | Phase 4 사후 라벨링 화면 | **하지 않는다**(2026-08-19 사용자 결정) — Close 코멘트 + signature 확정이 정답 |
> | 자동보정 `calibrate` | 계속 비활성 (라벨 없는 분위수라 v1 배제) |
>
> ---
>
> ## 2026-08-19 진행 — 되먹임 루프 실측 진단과 결정
>
> **한 줄 결론: 지금 구조는 "자가성장"이 아니라 "자가축적 + 수동성장"이다.** 축적(입력)은
> 완전 자동으로 잘 돈다(업로드마다 전 item 판정이 쌓여 `evaluation` 267행 / run 78회).
> 그런데 그 축적을 판정 개선으로 되먹이는 경로가 전부 사람 클릭에서 멈춰 있었고,
> 그중 **가장 앞단이 기계적으로 끊겨 있었다**.
>
> ### 실측된 단절 (운영 `DB/pe/report/eval/eval.db`)
> | # | 단절 | 실태 | 이번 조치 |
> |---|---|---|---|
> | 1 | **라벨↔판정 case_id 불일치** | 라벨 case 의 wafer=None 1건 vs 엔진 case 43건(wafer=1/2/3) — **교집합 0**. 채점·선례 부스트가 구조적으로 성립 불가 | **수정 완료**(아래 Phase 3 보수안) |
> | 2 | 룰 7종 판정지표 미저장 | OUTLIER·공간 4종·꼬리 룰·BIMODALITY 가 층화 불가 | **수정 완료**(eval.db v9, 방향별 꼬리 질량은 v10) |
> | 3 | 정확도에 시간축 없음 | `scoring()` 은 전체 누적 한 숫자, 이력 없음. 단 `created_at` 은 이미 저장돼 있음 | 예정 |
> | 4 | 선례 루프 미가동 | 스냅샷이 `generate_comment=False` + 클라 AI Comment 비활성 + `eval_precedent` 0행 | 외부 담당 |
> | 5 | 골든 회귀가 임계값 저장에 미연동 | 수동 버튼만 | 예정 |
> | 6 | eval.db 무한 증가 | cleanup 7작업에 eval 항목 없음. force 재수집이 옛 run 을 남김 | 예정 |
> | 7 | `calibrate.recalibrate` 배선 없음 | 호출 지점 0개, 대상 키도 1개 | 의도적 보류 유지 |
>
> ### bin 제거 — case 는 item 당 1개 (2026-08-19, 사용자 결정으로 **구현 완료**)
>
> **결정**: eval 엔진의 동일성 기준은 **value_type + item 명(공통토큰 제거 후 유사도 50% 이상)** 이다.
> bin 은 학습 식별 키(case_id·item_class)에서 뺀다. **버리는 것이 아니라** `fail_case.bin`
> 컬럼에 **대표 bin(최다 fail, 동률은 작은 bin)** 으로 보존하고 bin_taxonomy(severity_bias —
> bin18 같은 특이케이스)도 그 값으로 계속 적용한다.
>
> **근거 (실측)**
> | 관측 | 값 |
> |---|---|
> | 실업로드 raw 3,334 fail 그룹의 `(소스, FAILTNO)` 당 distinct BIN | **100% 가 1개** — bin 은 item 이 이미 결정하는 값이라 **식별 엔트로피 0** |
> | 반대로 `(소스, bin)` 당 item 수 | 평균 4.53 · 최대 60 — bin 은 item 을 가르는 축이 아니라 **묶는 더 굵은 축** |
> | 다제품 시드에서 같은 item 의 제품군별 bin 집합 | 전부 다름(`vout_ldo`: MDDI {18,31} / PMIC {18,20,21,31,40} / TCON {18}) — 사용자 주장대로 |
> | 운영 eval.db 의 사람 라벨 | 1건, **100% 고아**(bin=NULL case 에 붙었는데 판정은 bin=1/2 에) |
> | `severity_bias` 의 실제 판정 기여 | **0** — taxonomy 3항목(전부 PMIC 예시)뿐이고 운영 제품은 MDDI 라 전부 미매칭, 게다가 `round(rank±0.3)` 이 rank 를 못 바꾼다 |
> | 선례 top-k 5칸 | **90~94% 가 같은 item 의 bin 복제본** — dedup 이 case_id 단위였기 때문 |
> | item_class 버킷(calibrate 모집단) | bin 포함 34개(대부분 n<10) → 제외 6개(**전부 n≥10**) |
>
> **구현**
> - `pipeline/ingest.py` — bin 루프 제거, `fail_mask`/`fail_count` 는 그 item 을 fail 한
>   전 serial 합집합, `case["bin"]` = 대표 bin, `case_id` 의 bin 자리는 **항상 None**.
>   `item_class` 는 `category_major|value_type` **2단**. degrade/raw_table 경로도 동반.
> - `eval_export.py` — 라벨 case_id 도 bin=None. 같은 item 의 여러 섹션 행(Yield|2 / Yield|5 /
>   CPK)이 한 case 로 모이므로 **코멘트를 병합**하고(`_group_by_case`) signature 는 **합집합**.
>   ⚠ 안 묶으면 delete→insert 가 마지막 행만 남겨 **앞 코멘트가 사라진다**(규칙 12).
> - `ai_comment.py` — case 가 item 단위라 그 item 의 **모든 fail bin 행에 fan-out**
>   (`fail_bins_by_item` 이 화면 행 정본 `yield_tab.fail_counts_by_source` 에서 bin 목록을
>   가져온다 — 재계산 금지 규칙 13). 대표행만 채우면 나머지가 빈 셀이 되는 회귀.
> - 소비자: `signature_reason._fetch_cases` 의 bin 필터 제거(팝업 근거가 bin 행마다 달랐던
>   불일치 해소) · `review` dedup·`routes._case_key` 에서 bin 제거(허위 diff 방지) ·
>   `store.search_precedents` dedup 을 **(제품, lot, item)** 으로(중복행 제거) ·
>   `eval_admin._apply_value_type` 2단 재작성 · `AI_COMMENT_SCHEMA_VERSION` 2.
> - **판정값 등가 확인**: 단일-bin 데이터에서 case_id·item_class 를 뺀 나머지 판정 결과가
>   변경 전과 정준 JSON 완전 일치(5개 dtype 모드). 바뀌는 것은 multi-bin item 뿐이다.
> - 마이그레이션: 기존 case_id 는 전부 바뀌지만 label 1건(멱등 재-export 자동복구) ·
>   eval-panel/eval-review 라벨 0행 · 골든셋 `bin:` 0건이라 **비용이 사실상 0**이다.
>   배포 후 ① `collect-session force=true` ② 코멘트/서명 재-export ③ `purge_stale_snapshots`.
>
> ### (참고) 초기 검토에서 기각했던 보수안 — 사용자 재결정으로 대체됨
> 조사 결과 이 문서의 §3-2 실행 지시("`make_case_id` 함수는 그대로 두고 호출부만 바꾼다")는
> **검증 기준(§Phase 2 "bin 합쳐진 수")을 달성하지 못한다.** store 의 저장 함수가 전부
> upsert 라 같은 run 의 bin2/bin3 case 가 같은 case_id 가 되면 **예외 없이 조용히 섞인다** —
> `fail_case.bin` 은 최소 bin, 스칼라(raw_metrics/evaluation)는 마지막 bin, 자식
> (case_signature/eval_evidence)은 합집합인 "어느 bin 의 것도 아닌 행"이 된다. 크래시보다
> 나쁘다(발견이 안 된다). 제대로 하려면 ingest 를 item 당 case 1개로 병합해야 하는데 그러면
> yield 합집합 → trump CRITICAL 증가, 공간 feature 변화, AI Comment 행 fan-out,
> 골든셋 재작성, `signature_reason` bin 필터 수정이 연쇄한다.
>
> **그런데 정합에는 bin 제거가 필요 없다.** 어긋남의 주범은 wafer 축 하나였다(위 표 #1).
> 그래서 **wafer 만 맞추고 bin 은 그대로 두는 보수안**으로 Phase 3 를 달성했다:
> - `eval_export.collect_session_snapshot` — 스냅샷 meta 의 `wafer_number=None`
>   (종전엔 여기만 소스 순번 1,2,3… 이 들어갔다). 소스 구분은 `ingest_run.source_file` 이 한다.
> - `eval_export._parse_row_key` — ETC 행 라벨 bin `None→1`(엔진 PASS_BIN 좌표와 일치).
>   종전 None 은 엔진에 없는 좌표라 ETC 코멘트가 어떤 판정과도 짝이 되지 못했다.
> - TEMP 행은 현행 유지(엔진이 온도 조건을 평가하지 않아 구조적으로 join 불가 —
>   `test_condition` 축으로 계속 분리).
> - 회귀 가드: `tests/test_eval_snapshot.py` (f) — 라벨 case_id ⊆ 스냅샷 case_id.
>
> **저장 키를 item+value_type 으로 좁히는 것(bin 완전 제거)은 사용자 최종 목표로 유효하다.**
> 다만 판정 변화가 섞이지 않도록 **측정 체계(정합 + 정기 집계)를 먼저 세운 뒤** 별도 작업으로
> 진행한다. 이번 변경은 그 전환을 막지 않는다(라벨은 멱등 재-export 로 복구 가능).
>
> ### 구현된 것 (2026-08-19)
> | # | 무엇 | 어디 |
> |---|---|---|
> | 1 | **case_id 정합**(보수안) | `web_report/eval_export.py` — 스냅샷 `wafer_number=None`, ETC 행 bin `None→1`. 가드 `tests/test_eval_snapshot.py` (f) |
> | 2 | **판정지표 저장**(eval.db v9) | `store._V9_FEATURE_COLS` 14컬럼 + `_migrate_v8_to_v9`. 층화 해소 `review._METRIC_COLS`. 계산 경로 무변경(이미 반환하던 값) |
> | 3 | **정기 지표 집계·추이** | `server/database/eval_stats.py` → `report_eval_daily`(day×engine_version). `report_cleanup` 24h 편승(비파괴라 dry-run 무관). 화면 `/pe/eval` 채점 탭 추이 카드, 라우트 `GET api/eval/trend` |
> | 4 | **골든 회귀 저장 연동** | `eval_panel/routes.py` `_golden_auto_start` — 임계값이 실제로 바뀌면 백그라운드 실행, `GET api/golden/auto` 폴링. **비차단·사후 경고**(저장 롤백 안 함 — rev 가 이미 캐시를 무효화했다). `trace_store` 미사용(LRU 4런 축출 방지) |
> | 5 | **eval.db 옛 run 정리** | `admin_panel/eval_admin.py` `purge_stale_snapshots` — (세션,소스) 최신 아님 **AND** 라벨 참조 0 **AND** `eval-snapshot` run 만. `fail_case`·`label`·마스터 보존. `REPORT_EVAL_PURGE_STALE_RUNS`(기본 0) + dry-run 존중 |
>
> 집계(3) → 정리(5) **순서 고정** — 뒤집히면 집계 원재료를 먼저 지운다(`run_cleanup` 주석).
>
> ### 개선 우선순위 (라벨 획득 비용 낮추기가 리포트 자동화보다 먼저)
> 정확도 리포트를 먼저 자동화해도 표본이 없으면 `pairs: 0` 이 3개월마다 나올 뿐이다.
> 순서: ① case_id 정합(완료) → ② 판정지표 저장(완료) → ③ 정기 정확도·UNKNOWN 추이 →
> ④ 골든 회귀 저장 연동 + eval.db 정리. **사후 라벨링 화면(구 Phase 4)은 만들지 않는다** —
> 사용자 결정(2026-08-19): Issue Table 의 `[확정]` 버튼은 signature 확정 용도로 그대로 두고,
> **PTE comment 의 Close 가 곧 정답**이라는 현행 규약을 유지한다. 따라서 채점의 연료는
> (a) Close 코멘트 라벨과 (b) signature 확정(✓) 라벨이며, ③은 이 둘로 지표를 구성한다.
>
> ---
>
> ⚠ **§3-2/§3-5 를 미룬 이유**(2026-08-10 조사): `item_class` 를 2단으로 바꾸면
> `server/chatbot/tools_eval.py`·`planner.py` 가 **운영에서** 그 값을 집계·필터 축으로
> 쓰고 있어 기존 3축 행과 신규 2축 행이 섞인다. `eval_export.save_human_label`·
> `eval_admin.set_value_type`·`db_input/import_csv.py`·`test_e2e.py` 도 3축을 전제한다.
> case_id 에서 bin 만 빼는 것은 DDL 변경이 아니지만, 기존 라벨의 case_id 재계산
> 마이그레이션이 따라온다 — **운영 eval.db 의 label 행 수를 실측한 뒤** 재입력 vs
> 마이그레이션을 정하는 것이 맞다(§Phase 0). 개발 PC 기준으로는 `fail_case` 1행 /
> `label` 1행(그마저 `eval_id=NULL` 로 채점 대상 아님)이라 사실상 재입력 1건이다.

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
(severity 최고, 동률이면 BIMODALITY 발화 쪽)으로 하나만 남긴다.

**§1-3 (AI Comment 원문)은 (B) 로는 해결되지 않는다.** `evaluation.comment` 에
남는 것은 이 전용 실행이 만든 엔진 코멘트이지, 사용자가 IssueTable 셀에서 본
텍스트(`[STATUS][이봉] ...`)가 아니다 — 애초에 옵션이 꺼져 있으면 셀 자체가 없다.
옵션을 켠 세션에 한해 셀 원문을 별도로 남기는 것은 web_report O/X 피드백을
붙일 때 함께 다룬다(§5 보류 항목).

### 4-6. 알려진 공백 2건 (이번엔 손대지 않음)

- `features.shot_fail_ratio` — DDL 에는 있으나 `save_features` 컬럼 목록에 빠져
  있고 계산 경로도 없어 **항상 NULL**. 잔재.
- `value_gap_ratio` / `value_gap_minor_mass` — L2 가 계산하지만 의도적 미저장.
  BIMODALITY `separated` 판정의 실제 기준값이라 **채점하려면 있어야 한다.**
  추가는 스키마 변경이므로 §3-4 승인에 묶어 함께 판단한다.
  **같은 처지의 미저장 지표가 늘었다** — `fail_mad_min`·`fail_pass_gap_sigma`(OUTLIER 판정
  2축) · `e1/edge/center/ring_fail_share`(공간 4종 판정) · `tail_mass_3s`(꼬리 룰 밴드의
  둘째 축) + `tail_mass_3s_high/_low`(방향 분해, v10) · `fail_spread_norm`(SPOT_FAIL 단일 축) ·
  `rail_low/high_ratio`(CODE_RAIL
  evidence) — 전부 2026-08-13 기준. 그래서 표본함(docs/13 §14)이 이 7개 룰을 층화하지
  못한다. 승인 시 함께 올리는 것이 좋다.
  (신규 지표를 파생으로만 둔 것은 의도다 — eval.db 스키마 변경은 사전 승인 대상이라
  룰 재편과 스키마 변경을 한 커밋에 섞지 않았다.)

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
