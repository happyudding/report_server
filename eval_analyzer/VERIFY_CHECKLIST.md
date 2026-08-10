# eval_analyzer — 원본 대조 체크리스트

이 폴더의 현재 내용은 외부 담당자의 업데이트본을 **보안망 밖으로 못 가져와서 눈으로 보고 손으로
다시 친 사본**(구 `eval_analyzer2/`)이다. 사용자가 보안망 안에서 대부분 교차 검증한 뒤
2026-08-03 에 이 폴더로 병합했다. 이 문서는 **아직 원본과 대조하지 못한 것**을 남긴다.

- 손타이핑 delta 는 병합 전 구버전 대비 **py 7개 + yaml 2개 + 신규 `cross_source.py`** 였다.
  그 밖의 파일은 구버전과 동일하므로 대조 대상이 아니다.
- docstring 은 **일부러 안 친 것**이므로 대조 시 무시한다.
  - **2026-08-04**: 운영 코드(`eval_engine/`·`db_input/`·`chatbot/`→現 `chatbot_prototype/`·
    `tools/`)의 빠진 docstring
    119개를 **현재 코드를 읽고 새로 썼다**(원본 복원이 아니라 재작성 — 문구가 원본과 다르다).
    `compare_typing.py` 는 AST 비교라 이 추가를 감지하지 않으므로 대조 시 계속 무시하면 된다.
    `tests/` 는 자명한 테스트에 docstring 을 안 붙이는 기존 컨벤션을 유지해 헬퍼만 채웠다.
- **교차 검증 완료(2026-08-03)**: `aggregate_cross_source()` 를 뺀 나머지는 원본과 일치 확인됨.
  → 아래에서 실제로 남은 것은 **§1-1 / §1-2 / §1-3 / §3** 이다.

---

## 0. 먼저 할 일 — 대조 도구 실행

```
cd <원본_옆_어딘가>
python <repo>/tools/compare_typing.py <이_폴더> <원본_updated_폴더> --out cmp.txt
```

`.py` 를 **AST 로** 비교한다 — 주석·공백·줄바꿈·docstring 이 달라도 안 뜨고, 로직/이름/문자열이
다를 때만 파일명 + 정의 이름으로 찍힌다. 소스 내용은 출력하지 않는다.
한글이 깨지면 `chcp 65001` 하거나 `--out` 파일을 편집기로 열 것.

출력의 **"원본에만"** 줄이 가장 중요하다 — 통째로 안 친 함수/파일이 거기 나온다.

---

## 1. 반드시 확인 — 내용이 비어 있음

### 1-1. `aggregate_cross_source()` — 원본과 다를 수 있음 ★ (2026-08-03 재구성함)

**교차 검증 결과 이 함수만 원본과 대조하지 못했고, 사용자 요청으로 docstring + 남아 있던
헬퍼들의 계약을 근거로 재구성했다.** 원본 코드가 아니므로 동작이 다를 수 있다.

재구성 근거 — 아래 심볼들이 전부 미사용으로 남아 있었고, 이들이 쓰이도록 맞췄다:
`store.cases_for_runs` / `store.update_evaluation_comment` / `recommend` / `thresholds_for` /
`SOURCE_ONLY_FAIL` / `_signature_text()` / 인자 `engine_version`·`persist`.

재구성한 동작:
1. `store.cases_for_runs(run_ids)` 로 행을 모아 `_group_by_item()` 으로 item 별 묶음
2. 묶음마다 `_evaluate_item_group(rows, thresholds_for(rows[0]))` → 판정 없으면 skip
3. `_source_only_comment()` 로 `recommend.make_comment` 와 **같은 3-섹션 형식** 코멘트 생성
   (cross-source 는 선례검색을 안 하므로 `[과거사례]` 는 `recommend._NO_PRECEDENT_TEXT`)
4. `persist=True` 면 **불량 source 쪽 case 들만** `update_evaluation_comment` 로 갱신.
   `engine_version` 인자가 있으면 그것을, 없으면 각 행의 `engine_version` 을 사용.
   둘 다 없으면 대상 행을 특정할 수 없어 건너뛴다.
5. 반환 `{"evaluations": [ {item_id, item_canonical, signature, normal_sources, bad_sources,
   source_fail_rate_gap, bad_targets, dominant_phenomenon, comment, persisted}, ... ]}`

임의로 정한 것 (원본과 다를 가능성이 높은 지점):
- **`_DEFAULT_ACTION` 문구** — `SOURCE_ONLY_FAIL` 이 `signatures.yaml` 에 없어서
  `_signature_text()` 가 빈 dict 를 준다. yaml 에 `SOURCE_ONLY_FAIL` 항목이 원본에 있는지
  확인하고, 있으면 그 `phenomenon_ko`/`action_ko` 가 자동으로 우선하도록 이미 짜 두었다.
- `[현상]` 문장 표현, 반환 dict 의 키 이름(`signature`/`comment`/`persisted` 는 추가한 것)
- `persisted` 카운트 필드는 원본에 없을 수 있다

### 1-2. `cross_source` 를 누가 호출하는가

지금 이 저장소 어디에서도 `cross_source` 를 import 하지 않는다.
원본에 `api.py` / `cli.py` 쪽 wiring(진입점 노출)이 더 있었는지 확인.

### 1-3. `store.py` `shot_fail_ratio` — 컬럼만 있고 값이 없음

- `SCHEMA` features 테이블: `shot_fail_ratio REAL` 있음
- `_migrate_v4_to_v5()`: `ALTER TABLE features ADD COLUMN shot_fail_ratio` 있음
- 그런데 `features.py` 에 계산 코드 없음, `save_features()` 컬럼 목록에도 없음 → **항상 NULL**

→ 원본 `features.py` 에 SHOT 기반 feature 계산이 있는지, `_FEATURE_KEYS` / `save_features()`
컬럼 목록에 `shot_fail_ratio` 가 들어 있는지 확인.

---

## 2. 애매해서 손대지 않은 것

### 2-1. `signatures.py` `_evaluate_subpop_gap()` — DENSITY_GAP 인데 값은 cdf_gap

**✅ 해결(2026-08-03)** — 오라벨로 확정하고 수정했다. DENSITY_GAP 에는 `density_gap` 값을
싣고, 값축 분리 지표는 별도 `VALUE_GAP`(`value_gap_ratio`) evidence 로 분리했다.
같은 날 separated 판정도 cdf_gap(동일값 질량) → `_value_gap`(값축 빈 구간) 기준으로 교체
(`subpop_value_gap_warn`/`subpop_minor_mass_min`, 구 `subpop_cdf_gap_warn` 제거).

### 2-2. `api.py` `ThreadPoolExecutor(max_workers=3)` 안에서 `present.persist()`

`persist=True` 경로에서 워커 스레드가 SQLite 에 동시 쓰기를 한다. 원본이 정말 이 모양인지,
아니면 persist 만 메인 스레드로 모으는 코드가 더 있었는지 확인.
(report_server 연동은 `persist=False` 라 당장 문제는 안 됨)

### 2-3. `recommend.py` `_build_prompt()` 의 지시문 8줄

지시문 8줄이 콤마 없이 암시적 문자열 연결로 한 덩어리가 되어 있다. 원본 의도일 수 있어
**그대로 두었다**. 원본에 콤마가 있는지 확인.
(그 뒤 `f"item: ..."` 앞과 `[현상]`/`[과거사례]`/`action_ko` 사이 콤마는 누락이 명백해 채워 넣었음)

### 2-4. `_past_case_text()` 의 이중 공백

`prefix` 가 `"... 에서 "` 로 끝나는데 뒤에 `f"{prefix} 유사 사례가..."` 라 공백이 2개.
사소해서 손대지 않음.

---

## 3. 갱신 안 된 것 — 원본에 변경이 있었는지 확인

### 3-1. `tests/` 가 구버전과 100% 동일 → **여기서 3건을 자체 판단으로 고쳤다**

원본 업데이트에 테스트 변경이 있었다면 그것도 못 옮긴 것이다. 병합 시점에 3건이 실패했고,
전부 "구버전 기준 테스트 vs 새 동작" 이라 **새 동작에 맞춰 갱신했다**(2026-08-03).
원본 tests 가 이걸 어떻게 고쳐 놨는지 확인해 대조할 것.

| 테스트 | 실패 원인 | 고친 방법 |
|---|---|---|
| `test_store.py::test_schema_v4_user_version_and_objects` | `SCHEMA_VERSION` 4→6 | `test_schema_user_version_and_objects` 로 개명 + `== store.SCHEMA_VERSION` 으로 바꾸고 v5/v6 features 컬럼 7개 검증 추가 |
| `test_signatures_status.py::test_no_signature_full_data_gives_ok` | 신규 `MISSING_LIMIT` 발화 (픽스처에 lsl/usl 이 없음) → OK 가 아니라 MINOR | `_case()` 기본값에 `lsl=0.0, usl=10.0` 추가 |
| `test_signatures_status.py::test_no_signature_incomplete_data_keeps_monitor` | 위와 동일 | 위와 동일 |

`_case()` 에 limit 이 없던 건 원래 픽스처 결함이다 —
`test_trump_low_cpk_low_yield_forces_critical` 의 "발화 signature 없음" 주석이
`MISSING_LIMIT` 때문에 거짓이 되어 있었다. limit 부재 자체는
신규 `test_missing_limit_fires_without_spec` 로 따로 덮었다.

### 3-2. `docs/` 도 구버전과 100% 동일

특히 `docs/DB_SCHEMA.md`(features 신규 컬럼 7개), `docs/EVALUATE_RETURN_SPEC.md`
(comment 포맷이 `[현상]/[과거사례]/[점검제안]` 3줄로 바뀜) 는 원본에서 갱신됐을 가능성이 크다.

---

## 4. 밖에서 이미 고친 것 (참고 — 다시 확인할 필요는 낮음)

전부 "원본이 이럴 리 없는" 기계적 오타라 밖에서 수정했다. 대조 시 이 부분이 안 뜨면 정상.

| 파일 | 고친 것 |
|---|---|
| `pipeline/features.py` | `np.histrogram`→`np.histogram` / dict 키 `"y_gradient_norm", None`→`: None` / `float(fm[ring_mask]).mean()`→`float(fm[ring_mask].mean() / overall_fail)` / `_classify_modality_ve`→`_classify_modality_v2` |
| `pipeline/signatures.py` | evidence dict `"note", f"..."` → `"note": f"..."` (2곳) |
| `pipeline/recommend.py` | `_subpop_gap_comment` 의 `return None` 이 for 루프 안 → 밖으로 / `_build_prompt` 콤마 3개 / **`find_precedents()` 복원** (아래 ★) |
| `eval_engine/cross_source.py` | `["signatrues"]`→`["signatures"]` / `[r["primary_signatrue"]] for ...]` 구문 / `representative_by_source`→`_representative_by_source` / `_dominant_phenomenon(bad_rows=)` / `def aggregate_cross_source(...))` 여분 괄호 |
| `eval_engine/store.py` | `rc.make_case_id`→`rc.case_id` / `fc.resolve_item_id`→`fc.item_id` / `case_signatrue`→`case_signature` / `role='primary'resolve_item_id` 꼬리 제거 |
| `rules/signatures.yaml` | `MISSING_LIMIT` evidence 따옴표 / `WAFER_GRADIENT` when_metric 닫는 `}` 누락 |
| `rules/thresholds.yaml` | `source_fail_rate_delat_warn`→`source_fail_rate_delta_warn` |

★ **`recommend.find_precedents()` 는 타이핑 중 통째로 사라졌던 함수**다.
`api.py:50` 이 호출하고, `precedent_client` import 와 모듈 docstring 도 남아 있어 누락이
확실하므로 구버전과 동일한 2줄로 복원했다. 원본과 같은지만 한 번 봐 둘 것.

```python
def find_precedents(case_ctx: dict, sig_result: dict) -> list:
    return precedent_client.search(case_ctx, sig_result)
```

---

## 5. 밖에서 이미 통과한 검사 (안에서 다시 안 해도 됨)

- 전 `.py` 구문 파싱 0 오류
- `rules/*.yaml` 6개 전부 파싱 성공
- `eval_engine` 전체 import 성공
- signature 21개 ↔ `status.SPECIFICITY_ORDER` 완전 일치, `when_metric` 이 참조하는
  thresholds 키 전부 존재, `phenomenon_ko` 전부 존재
- 모듈 간 `모듈.속성` 참조 전수 검사 → 누락 0건 (이 검사가 `find_precedents` 를 잡았다)
- 함수 내 미정의 이름 검사 → 0건 (§1-1 재구성 후)
- `aggregate_cross_source()` E2E: 임시 sqlite 에 item 1개 × source 3개(2%/3%/40%)를 심어
  → 정상 `[A,B]` / 불량 `[C]` 분리, gap 0.370, **불량 source 행의 comment 만** 갱신,
  `persist=False` 는 DB 무변경, source 1개·격차 미달·빈 입력은 전부 `{"evaluations": []}` 확인
