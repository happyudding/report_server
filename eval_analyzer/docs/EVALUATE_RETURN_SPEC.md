# evaluate() 반환 형식 — report_generator 연동 스펙

> eval_analyzer 는 **in-process 라이브러리**. report_generator 가 파일 1회 run 마다 `evaluate()` 호출.
> **결과 저장은 eval_analyzer 가 자체 eval.db 에 이미 수행** — 반환값은 UI/Issue Table 표시용이며,
> 쓰지 않으면 버려도 데이터는 남는다. 아래는 **실제 출력 기준** 필드 명세.
> 정본 계약: [INTEGRATION_CONTRACT.md §4](INTEGRATION_CONTRACT.md) · 소스: `pipeline/present.py:to_result`.

```python
def evaluate(run_input: dict, *, engine_version=None, model_version=None, persist=True) -> dict
```

## 1. 최상위 구조 (RunResult)

```jsonc
{
  "run_id": 123,            // persist=True → eval.db ingest_run PK / persist=False → null
  "engine_version": "ev1",
  "model_version": null,    // LLM 코멘트 사용 시에만 채워짐
  "cases": [ /* 저장 대상 case 당 1개(아래): ①yield fail item×bin + ②cpk<1.33 marginal item */ ]
}
```

`cases` 는 **저장 대상 case 만** = ①yield fail(FAILTNO==TNO) item×fail bin **∪** ②yield fail 은 없지만
**cpk<cpk_warn(1.33)** 인 marginal item(bin=PASS_BIN=1). 둘 다 아니면 제외 — 모두 없으면 `[]`.

## 2. cases[i] 필드

| 필드 | 타입 | 의미 · 활용 |
|---|---|---|
| `case_id` | str · sha256 | 자연키(재업로드 idempotent). 행 식별 / 중복 제거 키. |
| `item_canonical` | str | 정규화 item명 (`iddq_init`). 내부 매칭/선례용. |
| **`item_raw`** | str | ★ 원본 item명 (`IDDQ_INIT`). **Issue Table join 키** = `(item_raw, bin)`. |
| `item_class` | str | `category_major\|value_type\|bin` (`NON_TRIM\|A\|3`). 분류/필터. |
| `bin` | int | fail bin 번호. join 키의 일부. **cpk 트리거(yield fail 없는 marginal)면 PASS_BIN=1**. |
| **`issue_category`** | enum | ★ `YIELD \| CPK \| ETC`. Issue Table 버킷. ETC 자동 채움(§5). |
| `status` | enum | ★ `CRITICAL \| MAJOR \| MINOR \| MONITOR \| OK` 5단계. 심각도 뱃지 / 정렬 키. |
| `primary_signature` | str | 대표 발화 룰 (`LOW_CPK`). |
| `secondary_signatures` | list[str] | 보조 룰 목록. |
| `confidence` | float 0–1 | 판정 신뢰도 (0.9). |
| `data_completeness` | enum | `full \| partial \| low`. raw 부족 시 low(yield-only degrade). |
| **`comment`** | str | ★ 분석방향 한 문장 — 엔지니어가 보는 핵심 텍스트. Issue Table `comment` 열. |
| `evidence` | list[obj] | 근거 신호 flat 목록 `{signal_code, value, weight}`. 상세/툴팁. |
| `signatures` | list[obj] | 룰별 breakdown `{id, role, evidence[], action_ko}`. 상세 패널. |
| `precedents` | list[obj] | 과거 선례 `{action, result, comment, product_name, family_product}`. 없으면 `[]`. |

### status 심각도 (5단계)
severity rank: `OK < MONITOR < MINOR < MAJOR < CRITICAL`. UI 정렬·색상은 이 순서.
- **CRITICAL** — cpk 극저 + yield 붕괴 등 trump 조건. 최우선 조치.
- **MAJOR** — 유의 signature 발화. 근본 점검 필요.
- **MINOR** — 경미 이상. 모니터링 대상.
- **MONITOR** — signature 0건이지만 데이터 결측(partial/low) — 판단 유보, 추세 관찰.
- **OK** — signature 0건 + data_completeness=full — 통계적 정상 확정.

### evidence vs signatures (헷갈리기 쉬움)
같은 근거를 두 방식으로 노출. 표에는 `comment`+`status`만, 상세 패널에서 `signatures` 를 펼치는 구성 권장.
- **evidence** — 발화한 모든 신호를 한 배열로. `{signal_code, value, weight}` 만. 요약 툴팁용.
- **signatures** — 어떤 값이 **어느 룰의 근거**인지 묶이고, 각 룰의 `action_ko`(권장조치 문구) 포함.

## 3. 실제 출력 (cases[0], 샘플1 원본 덤프)

```jsonc
{
  "case_id": "2c89662dfdee10bc…cf5cc96e",
  "item_canonical": "iddq_init",
  "item_raw": "IDDQ_INIT",          // join 키 (원본 item명)
  "item_class": "NON_TRIM|A|3",
  "bin": 3,
  "issue_category": "CPK",          // LOW_CPK → CPK 버킷
  "status": "MAJOR",
  "primary_signature": "LOW_CPK",
  "secondary_signatures": ["MEAN_SHIFT"],
  "confidence": 0.9,
  "data_completeness": "full",
  "comment": "공정능력지수 미달(cpk<1.33) — 산포/센터링 근본 점검",
  "evidence": [
    { "signal_code": "CPK", "value": 0.6496, "weight": 1.0 },
    { "signal_code": "CENTER_BIAS", "value": 0.5556, "weight": 1.0 },
    { "signal_code": "NEAREST_SPEC_SIDE", "value": null, "weight": 1.0 }
  ],
  "signatures": [
    { "id": "LOW_CPK", "role": "primary",
      "evidence": [{ "signal_code": "CPK", "value": 0.6496, "note": "cpk 0.6496" }],
      "action_ko": "공정능력지수 미달(cpk<1.33) — 산포/센터링 근본 점검" },
    { "id": "MEAN_SHIFT", "role": "secondary", "evidence": [ … ],
      "action_ko": "분포 중심이 한쪽 spec 으로 치우침 — offset/센터링 trim 검토" }
  ],
  "precedents": []                  // 매칭 선례 없으면 빈 배열
}
```

## 4. report_generator 처리 방법

**핵심: 저장은 eval_analyzer 가 이미 함. 반환값은 표시용이고, 무시해도 데이터는 eval.db 에 남는다.**

```python
from eval_engine import evaluate

def after_report_generated(df_honey_group, meta):
    run_input = build_run_input(df_honey_group, meta)   # ← report_server 측에서 작성
    try:
        result = evaluate(run_input)                     # persist=True 기본 → eval.db 자동 적재
    except ValueError as e:
        log.warning("eval skip: %s", e)                  # 잘못된 meta → ValueError
        return
    # result["cases"] 를 UI/Issue Table 에 첨부 (표시 안 하면 버려도 됨)
```

담당자가 신경 쓸 것:
1. **`build_run_input` 어댑터 작성** — 유일한 실작업. df_honey → 정본 `raw_df`(SERIAL/SHOT/DUT/XPOS/YPOS/
   BIN/FAILTNO + 6 메타행) + meta dict. 레퍼런스: `tests/integration/adapter.py:sample_csv_to_run_input`.
2. **예외 격리** — 잘못된 meta(product_type/family_product taxonomy 밖)에 `ValueError`. try/except.
3. **`cases` 빈 배열 처리** — fail 0개면 `[]`. "이상 없음" 분기.
4. **표시 매핑** — 표는 `status`+`comment` 중심, 상세는 `signatures[].action_ko`, `precedents==[]` 면 "과거사례 없음".

**안 해도 되는 것:** cpk/산포 계산, 결과 저장, LLM 호출 — 전부 eval_analyzer 가 함. raw 를 버리지 않고 `raw_df` 로 넘기는 것만.

## 5. Issue Table 연결 (join 레시피)

> **지금 바로 붙일 필요 없음** — report_server Issue Table 포맷 개편 후 wiring 가이드. eval 은 나중에 join
> 쉽게 `item_raw`·`issue_category` 만 미리 맞춰뒀다. 방향은 **보강**(기존 Yield/CPK threshold scan 유지 +
> eval 로 채움), 대체 아님. 상세: [HANDOFF_TO_REPORT_SERVER.md §6](HANDOFF_TO_REPORT_SERVER.md).

- **join 키** = `(case.item_raw, case.bin)` ↔ Issue Table 행의 `(Item/subject, Bin)`. `comment` 열 ← `case.comment`.
- **카테고리 버킷** ← `case.issue_category`: GROSS_FAIL→`YIELD`, LOW_CPK·SPEC_TOO_TIGHT→`CPK`, 그 외→`ETC`.
  특히 지금 **수기 입력인 ETC 를 eval 이 자동 생성**(EDGE_FAIL/CLUSTER_FAIL/SUBPOP_GAP/OUTLIER 등 + comment).
- **scope(저장 기준):** eval 케이스 = ①yield fail(FAILTNO==TNO) item×fail bin **∪** ②yield fail 은 없지만
  **cpk<cpk_warn(1.33)** 인 marginal item(bin=PASS_BIN=1, `issue_category=CPK`). 저장 판단은 rule(L3)
  계산 뒤에 수행(`pipeline/present.py:should_store`) — 향후 "전체 rule 위반 시 저장" 으로 이 판단식만 확장 예정.

## 6. ⚠ dtype 계약 (raw_df 입력)

데이터행(row6+)의 **item 셀은 실제 숫자(float/int) 객체**여야 한다 — 문자열이면 파서가 무시해 케이스가
0개가 된다. 메타행(TNO/UNIT/HILIM/LOLIM)·XPOS·YPOS·BIN·FAILTNO 는 문자열이어도 변환된다.
CSV 재구성 예시는 `tests/integration/adapter.py:sample_csv_to_run_input`.
