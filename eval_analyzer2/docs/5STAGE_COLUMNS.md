# 판단 5단계 + 컬럼 의미 사전

> 물리 테이블/DDL 은 [DB_SCHEMA.md](DB_SCHEMA.md). 이 문서는 "각 컬럼이 무슨 뜻인지" 사전.
> 엔지니어의 fail 판단 흐름 = 5단계. 각 단계가 어느 테이블에 들어가는지 매핑한다.

```
1 INDEX        무엇을 보는가      → product_master / item_master / item_spec / bin_taxonomy / fail_case / ingest_run
2 RAW MEASURE  무엇을 가져오는가  → raw_metrics
3 JUDGMENT     어떻게 calc 하는가 → features            (룰이 임계하는 대상)
4 VERDICT      무엇을 도출하는가  → evaluation + eval_evidence + case_signature
5 HUMAN        사람이 어떻게 판단 → label + case_outcome (정답·결과·선례)
```
흐름: 1·2·3(기계 입력/계산) → 4(기계 판정) → 5(사람 정답). **4 는 5 와 같은 모양** → 예측 vs 정답 →
calibration 피드백. 노하우는 전부 (3)에 쌓인다. [3↔4] 변환기 = thresholds.yaml + signatures.yaml + bin_taxonomy.
★룰 인덱스 = `item_class`(category_major + value_type + bin), 선례 = (bin + value_type + item명 퍼지≥70%).

---

## [1] INDEX
**product_master** (제품당 1행)
- product_name : PARTID 13자리. fail_case FK.
- product_type : MDDI/PDDI/PMIC/SECURITY/TCON. spec·임계 분기.
- family_product : product_type 별 허용 제품군(DB_SCHEMA §10, 드롭다운 1:1). cross-product 이력 비교 키(모과제).
- pkg_type : 패키지 타입.
- process : 공정(BCD1370F…). · inch : 8/12. · gross_die : 웨이퍼당 총 die. · fab_line : 생산라인.
- tester / para : 제품 고정 속성.

**item_master** (item당 1행)
- item_id : PK. · item_name_raw : 원본 이름. · item_canonical : 정규화 이름(이력 traceability).
- item_base : phase 뗀 본체(family 키, 예 vref). · item_phase : init/code/trim/p2.
- category_major : TRIM/NON_TRIM ← item_class 구성. · category_mid : 중분류.
- value_type : V/A/Hz/CODE/P_F/Ohm/Sec ← item_class 구성(=unit계열). · unit : 단위.

**item_spec** ((item,product,revision)당 1행)
- lsl/usl : 하한/상한 spec. revision별 변경 이력 보존. spec_margin 기준.

**bin_taxonomy** ((product_type,bin)당 1행)
- bin_class : defective/abnormal/parametric… · severity_bias : status 보정 가중.

**fail_case** (fail instance당 1행)
- case_id : PK = sha256(product|lot|wafer|item_id|bin|revision). 재업로드 idempotent.
- lot_id / wafer_number(INTEGER) : 로트/웨이퍼(둘이 물리 웨이퍼). · bin : 불량 분류 번호(식별+의미).
- revision : FLOAT(0/0.1/1.0/2.1…). · item_class : category_major|value_type|bin. ★룰 스코프 키.

**ingest_run** (업로드당 1행)
- run_id : 업로드 id(파이프라인 전 구간에 전파). · source_file : 업로드 파일. · edm_link : EDM 링크.
- temperature(INTEGER) / corner(NN/SS/FF) : [req0] 세션 입력(우선 입력만, 분석 미사용).
- analysis_key : (선택) report_server 역참조 — report.db 개편 예정이므로 의존 금지, 단순 링크용.

## [2] RAW MEASURE (raw_metrics, (case,run)당 1행)
- cpk/cpl/cpu/cp : 공정능력 지수(표준 보고치). · mean(=average) : 평균. · stdev : 표준편차.
- min/max : 최소/최대. · yield : 수율. · fail_count/total_count : 불량수/총수. · bimodality : 이봉성(기본).
- ※ raw(per-DUT)는 저장 안 함. 최초 계산값만 보관. run별 보관(재측정 이력).

## [3] JUDGMENT METRIC (features, (case,run,engine_version)당 1행)
분포형: spread_norm(=robust_σ/(USL−LSL)), skewness(robust, 부호=꼬리방향), kurtosis,
  outlier_ratio(IQR/modified-z>3.5), modality(uni/bi), bimodality_score, density_gap(이봉 저밀도), cdf_gap(ECDF 점프).
spec margin: spec_margin_low/high((mean−limit)/stdev), nearest_spec_side(LOW/HIGH), limit_hit_ratio.
공간: edge_fail_ratio, center_fail_ratio, radial_gradient, quadrant_imbalance, x_gradient, y_gradient, wafer_zone_signature.
기타: n_dut(min-n 가드), site_cpk_delta(site간 cpk 편차), code_edge_hit(CODE/TRIM 레일 포화).
- ※ engine_version별 보관 → 공식 바뀌면 새 버전 적재. estimator=표준 robust, 임계=정규화+분위수.

## [4] VERDICT
**evaluation** ((case,run,engine,model)당 1행)
- status : CRITICAL/MAJOR/MINOR/MONITOR/OK (OK=signature 0건+full 데이터, MONITOR=signature 0건+결측).
  · confidence : 신뢰도. · data_completeness : full/partial/low.
- comment : 엔진 생성 코멘트(재생성 가능 캐시). · engine_version/model_version : 결정로직/LLM 버전.
**eval_evidence** ((eval,signal)당 1행) — JSON 대신 정규화
- signal_code(LOW_CPK 등) / value / weight / note.
**case_signature** ((eval,signature)당 1행)
- signature / role(primary·secondary) / score.
- signature 목록(정본 = rules/signatures.yaml): EQUIPMENT_SUSPECT, EDGE_FAIL, CENTER_FAIL, CLUSTER_FAIL,
  CODE_RAIL, SUBPOP_GAP(이봉), SEVERE_OUTLIER, OUTLIER_WARN, MEAN_SHIFT, TAIL_RISK, HEAVY_TAIL,
  BIDIR_TAIL, WIDE_DISTRIBUTION, LOW_CPK, SPEC_TOO_TIGHT, GROSS_FAIL.
  (보류) gradient(radial/x/y)·TRIM_INEFFECTIVE·RETEST_RECOVERY.

## [5] HUMAN
**label** (라벨 이벤트당 1행, case당 다중)
- human_status / root_cause_category(equipment/process/design/spec/unknown) / root_cause_detail.
- engine_comment_accepted / comment_modified(0/1) / human_comment(← 선례 RAG 본체).
- labeler / reviewer / label_quality.
**case_outcome** (실제 조치·결과)
- action(retest/condition_change/spec_release/dev_feedback/trim_adjust/scrap/monitor).
- condition(예 "UVLO_TEST_EN=H") / result(recovered_normal/confirmed_defective/improved/pending) / resolved_by/at/note.
- ※ 엔지니어는 규칙이 아니라 판정 라벨+결과만 입력. 규칙은 누적에서 calibration/mining 으로 채굴.
  선례 = (bin + value_type + item명 퍼지≥70%)로 과거 outcome 회수 → 코멘트 근거.
