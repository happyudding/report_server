# eval_analyzer2 — 원본 대조 체크리스트

`eval_analyzer2/` 는 보안망 밖으로 코드를 못 가져와서 **원본(업데이트본)을 눈으로 보고 손으로
다시 친 사본**이다. 이 문서는 보안망 **안에서 원본과 대조할 때 확인할 것**만 모았다.

- 작성일: 2026-08-03
- 기준선: `report_server/eval_analyzer/` (구버전 운영 복사본). 손타이핑 delta 는
  **py 7개 + yaml 2개 + 신규 `cross_source.py`** 로 좁혀진다. 그 밖의 파일은 구버전과 동일.
- docstring 은 **일부러 안 친 것**이므로 대조 시 무시한다.

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

### 1-1. `eval_engine/cross_source.py` → `aggregate_cross_source()` 본문 전체 누락 ★최우선

현재 상태: docstring 과 `return {"evaluations": resultst}` 만 있고 **본문이 없다**
(`resultst` 는 어디에도 정의되지 않음 → 호출하면 NameError).

방증:
- `store`, `recommend`, `thresholds_for` import 가 전부 미사용
- 모듈 상수 `SOURCE_ONLY_FAIL` 미사용
- 헬퍼 `_signature_text()` 미사용
- 인자 `engine_version`, `persist` 미사용

→ **원본의 함수 본문을 그대로 옮겨 적을 것.** 위 미사용 심볼들이 전부 쓰이게 되면 정상.

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

```python
{"signal_code": "DENSITY_GAP", "value": features.get("cdf_gap"),
 "note": f"cdf_gap {features.get('cdf_gap')}"},
```

`signal_code` 는 `DENSITY_GAP` 인데 읽는 feature 는 `cdf_gap`. 원본이 `density_gap` 인지,
아니면 의도적으로 cdf_gap 인지 확인.

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

### 3-1. `tests/` 가 구버전과 100% 동일

원본 업데이트에 테스트 변경이 있었다면 그것도 못 옮긴 것이다. 현재 **3건 실패**하는데,
전부 "구버전 기준 테스트 vs 새 동작" 이라 **강제로 통과시키지 않았다**:

| 실패 테스트 | 원인 | 판단 |
|---|---|---|
| `test_store.py::test_schema_v4_user_version_and_objects` | `SCHEMA_VERSION` 4→6 | 의도된 변경 |
| `test_signatures_status.py::test_no_signature_full_data_gives_ok` | 신규 `MISSING_LIMIT` 발화 (테스트 픽스처에 lsl/usl 이 없음) → OK 가 아니라 MINOR | 의도된 변경 |
| `test_signatures_status.py::test_no_signature_incomplete_data_keeps_monitor` | 위와 동일 | 의도된 변경 |

→ 원본 tests 가 이 3개를 어떻게 고쳐 놨는지 확인해 옮길 것.

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
- 함수 내 미정의 이름 검사 → `cross_source.resultst` 1건만 (= §1-1 의 누락 본문)
