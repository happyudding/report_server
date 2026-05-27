# 04. df_honey 클래스 블록

서버/HTTP 흐름 없이 **DataFrame 1개 또는 여러 개에 대해 동일한 통계 분석을 직접 호출**할 수 있는 분석 객체.
`ExcelData` 속성과 호환되어 `table_builder` 함수들을 그대로 재사용하면서, 분석 능력을 메서드로 캡슐화한다.

- **파일**: [df_honey.py](../df_honey.py)
- **의존**: [table_builder.py](../table_builder.py), [preprocess.py](../preprocess.py), [data_loader.py](../data_loader.py), [config.py](../config.py)

---

## 1. 입력 데이터 형식 (CSV 구조)

[config.py:11-15](../config.py#L11-L15) 에서 위치 상수 정의.

| 행 번호 | 내용 |
|---------|------|
| 0 | 과목명 — meta 4컬럼 이후 |
| 1 | Units |
| 2 | Lower Limit |
| 3 | Upper Limit |
| 4, 5 | (사용 안 함) |
| 6+ | DUT 데이터 (`DATA_START_ROW = 6`) |

좌측 4 컬럼: `META_COLUMNS = ["DUT", "XCoord", "YCoord", "Bin"]`. `Bin == "1"` 이 합격.

`from_df` 에 넣는 DataFrame 은 **반드시 `header=None` 으로 읽은 raw 상태** 여야 한다.

---

## 2. `df_honey` — 단일 파일 단위 분석 객체

[df_honey.py:22](../df_honey.py#L22)

### 2.1 생성

```python
from df_honey import df_honey

# CSV / Excel 경로에서
h = df_honey.from_file("data/a_school.csv")          # from_file: line 35

# header=None 으로 읽은 raw DataFrame 에서
import pandas as pd
raw = pd.read_csv("...", header=None)
h = df_honey.from_df(raw, name="a_school")            # from_df: line 51
```

내부 속성 (ExcelData 와 동일 → table_builder 함수와 바로 연동):

| 속성 | 타입 | 내용 |
|------|------|------|
| `name` | str | 파일 stem (예: `"a_school"`) |
| `subjects` | list[str] | 과목명 목록 |
| `units` | list[str] | 단위 목록 |
| `lower_limits` | list | 하한 (float or nan) |
| `upper_limits` | list | 상한 |
| `scores` | pd.DataFrame | 점수 (컬럼 0..N-1 정수 인덱스) |
| `meta` | pd.DataFrame | DUT / XCoord / YCoord / Bin |

### 2.2 분석 메서드

| 메서드 | 반환 | 라인 | 설명 |
|--------|------|------|------|
| `.cpk(subject_idx=None)` | `list[dict]` | [L75](../df_honey.py#L75) | CPK 통계. idx 지정 시 해당 과목만, None 이면 전체 |
| `.yield_rate()` | `list[dict]` | [L83](../df_honey.py#L83) | Bin 별 count / portion / Main Fail subject |
| `.distribution(subject_idx)` | `(xs, ys)` ndarray | [L87](../df_honey.py#L87) | 누적분포 CDF |
| `.fail_items()` | `dict` | [L92](../df_honey.py#L92) | yield + fail subject 목록 |
| `.fail_values()` | `list[dict]` | [L96](../df_honey.py#L96) | 비합격 DUT × 과목별 측정값 / lower_limit / upper_limit / fail 방향 |
| `.summary()` | `list[dict]` | [L151](../df_honey.py#L151) | `build_summary_rows` 결과 (DB 저장 직전 행) |

내부적으로 모두 `_as_schools()` → `{self.name: self}` dict 를 만들어 `_build_*` 호출.

---

## 3. `df_honey_group` — 여러 honey 묶음

[df_honey.py:164](../df_honey.py#L164)

```python
from df_honey import df_honey, df_honey_group

group = df_honey_group([
    df_honey.from_file("a_school.csv"),
    df_honey.from_file("b_school.csv"),
    df_honey.from_file("c_school.csv"),
])
```

내부는 `self._schools = {h.name: h for h in honeys}` — report 모듈의 `schools` dict 와 동일 구조.

| 메서드 | 반환 | 라인 | 설명 |
|--------|------|------|------|
| `.cpk()` | `list[dict]` | [L173](../df_honey.py#L173) | 전체 source 통합 CPK (per-source + total 행) |
| `.yield_rate()` | `list[dict]` | [L177](../df_honey.py#L177) | 전체 source 통합 수율 |
| `.fail_items()` | `dict` | [L181](../df_honey.py#L181) | 전체 source 통합 fail items |
| `.summary()` | `list[dict]` | [L185](../df_honey.py#L185) | 통합 summary rows |
| `.distribution(idx, school_name=None)` | `(xs,ys)` 또는 `dict` | [L190](../df_honey.py#L190) | school_name 지정 시 단일, None 이면 `{name:(xs,ys)}` |
| `.compare_cpk()` | `pd.DataFrame` | [L203](../df_honey.py#L203) | subject × source pivot (source 간 CPK 비교) |
| `.names()` | `list[str]` | [L209](../df_honey.py#L209) | source 이름 목록 |
| `len(group)` | `int` | [L212](../df_honey.py#L212) | source 수 |

---

## 4. report 흐름과의 관계

```python
# 서버 분석 흐름 (report_analysis_service.py)
schools = {p.stem: load_table(p) for p in csv_paths}
rows = build_summary_rows(schools)
report_db.save_summary_batch(analysis_key, session_id, rows)

# 동등한 df_honey 흐름 (서버 없이)
group = df_honey_group([df_honey.from_file(p) for p in csv_paths])
rows = group.summary()
# rows 구조가 동일 → report_db.save_summary_batch 에 바로 사용 가능
```

df_honey 는 **순수 계산만** 담당. S3 / DB / 락 / analysis_key 캐시는 service 레이어가 처리.

---

## 5. 활용 예시

```python
# 특정 과목 CPK 만
df_honey.from_file("a_school.csv").cpk(subject_idx=5)

# 빠른 fail 확인
h = df_honey.from_file("a_school.csv")
print(len(h.fail_values()), "fail records")

# source 간 CPK 비교 pivot
group.compare_cpk()                         # pd.DataFrame
group.compare_cpk().to_csv("compare.csv")

# 과목 0의 분포 — source별
for name, (xs, ys) in group.distribution(0).items():
    print(name, xs.shape)

# 특정 source 만
xs, ys = group.distribution(0, school_name="a_school")
```

---

## 6. 주의

- `from_df` 입력은 `pd.read_csv(..., header=None)` 과 동일한 raw 구조여야 한다.
- `subject_idx` 는 0-based 정수 (subjects 리스트 인덱스). 과목명 문자열이 아님.
- `to_numeric(errors='coerce')` 로 변환 실패값은 분석에서 자동 제외.
- `PASS_BIN = "1"` 이 합격 기준. `_fmt_type` 이 `1.0` → `"1"` 정규화.
- df_honey 는 DB / S3 에 **아무것도 쓰지 않는다.** 순수 메모리 분석.
