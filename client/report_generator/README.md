# report_generator

Honey 로컬 리포트 분석/생성 엔진이다. CSV/xlsx 측정 데이터를 `df_honey` 표준 포맷으로 정규화하고, yield/CPK/fail/issue/distribution 데이터를 계산한 뒤 Excel COM(xlwings)으로 `.xlsx` 리포트를 만든다.

서버, DB, S3에는 직접 의존하지 않는다. 서버 업로드는 생성된 xlsx를 받은 `client/report_flow/`와 `client/transport/`가 담당한다.

## 빠른 사용 예

분석만 수행:

```python
import report_generator as rg

group = rg.df_honey_group.from_csvs(["sample.csv"])
result = rg.analyze(group, selector=rg.ItemSelector(selected_items=None))
print(result.yield_rows)
```

xlsx까지 생성:

```python
import report_generator as rg

out_path = rg.build_report(
    ["sample.csv"],
    out_path="report.xlsx",
    sheets=["summary", "yield", "cpk", "fail_item", "issue_table", "distribution"],
)
print(out_path)
```

실제 Honey UI는 `client/honey_main.py`에서 `df_honey.from_csv()`, `df_honey_group`, `rg.analyze()`, `xlsx_writer.write()`를 단계별로 호출한다.

## 모듈 구성

| 파일 | 역할 |
|---|---|
| `__init__.py` | 공개 API. `analyze`, `build_report`, 주요 모델을 export한다. |
| `constants.py` | canonical DataFrame row/column 상수와 `PASS_BIN`. |
| `csvfile_to_df.py` | 실제 `honey_parse.csvfile_to_df` import boundary. |
| `csv_loader.py` | raw CSV/xlsx를 canonical DataFrame으로 정규화한다. |
| `df_honey.py` | 단일 입력 파일/source 모델. meta, score, fail mask를 파생한다. |
| `df_honey_group.py` | 여러 source 묶음. rename/filter/split/diff/raw frame 제공. |
| `item_selector.py` | 선택 subject와 meta filter 설정 객체. |
| `_builders.py` | pandas/numpy 기반 순수 분석 계산. |
| `analyzer.py` | group을 `AnalysisResult`로 조립하는 orchestration. |
| `models.py` | `ReportMeta`, `DistSeries`, `AnalysisResult`. |
| `xlsx_writer.py` | Excel COM 기반 xlsx writer와 distribution chart 생성. |
| `_profile.py` | analyzer/writer 구간별 profile event 수집/저장 도우미. |

## 입력 포맷

정규화된 `df_honey.df`는 아래 구조를 유지해야 한다.

```text
columns: DUT, XCoord, YCoord, Bin, Serial, item1, item2, ...
row 0  : Units
row 1  : Lower Limit
row 2  : Upper Limit
row 3  : Lower Limit (duplicate)
row 4  : Upper Limit (duplicate)
row 5+ : DUT 측정 데이터
```

헤더는 `df.columns`에만 있어야 한다. row0에 헤더가 중복되면 `UNITS_ROW=0`, `DATA_START_ROW=5` 기준이 깨져 unit/limit/data 해석이 모두 밀린다.

## 주요 객체

- `df_honey`: source 1개를 나타낸다. `subjects`, `units`, `lower_limits`, `upper_limits`, `meta`, `scores`, `numeric_scores`, `fail_mask`를 cached property로 제공한다.
- `df_honey_group`: 여러 `df_honey`를 source 이름 기준 dict로 묶는다. source 이름 중복은 `_2`, `_3` suffix로 피한다.
- `ItemSelector`: 선택 subject 목록을 보관한다. `fail_only(group)`로 fail이 발생한 subject만 선택할 수 있다.
- `AnalysisResult`: xlsx writer가 필요한 table row, distribution metadata, source numeric frame, fail value frame을 모두 담는다.

## 분석 결과

`analyzer.run()`은 다음 계산을 수행한다.

- yield: bin별 count/yield, source별 count/yield, avg
- cpk: source별 통계와 total 통계
- fail item: fail bin별 주요 fail subject ranking
- issue summary: pass bin 제외, fail bin별 대표 fail subject
- summary rows: summary sheet와 서버 파서용 요약 행
- major fail subjects: 전체 DUT 기준 fail count 상위 subject
- distribution metadata: writer가 모든 DUT 값으로 ECDF chart를 만들 수 있는 metadata/source frame

## xlsx writer

`xlsx_writer.write(result, out_path, ...)`는 Excel + xlwings가 필요하다. openpyxl fallback은 없다.

생성 가능한 sheet:

- `summary`
- `yield`
- `cpk`
- `fail_item`
- `issue_table`
- `distribution`

옵션:

- `raw_sheets=[(name, df), ...]`: source별 raw data sheet를 추가한다.
- `colors=["#RRGGBB", ...]`: distribution source marker 색상을 지정한다.
- `progress_cb`, `dist_progress_cb`, `attach_progress_cb`, `profile_cb`: Honey UI progress/log와 연결된다.

## Distribution 불변 규칙

Distribution chart는 모든 DUT 값을 표시한다. 다운샘플링이나 point 상한을 넣으면 안 된다.

- source data는 점(marker)으로 표시한다.
- 동일값/정수형 데이터도 점 외 표현으로 변환하지 않는다.
- `_MAX_CDF_POINTS`, `_downsample`, `max_points` 같은 로직을 추가하지 않는다.

## 의존성

클라이언트 기준 주요 의존성은 `client/requirements.txt`에 있다.

- `pandas`, `numpy`: 분석 계산
- `xlwings`: Excel COM 기반 xlsx 생성
- `pywin32`: Windows COM/clipboard 처리
- `openpyxl`: 업로드 전 xlsx 재구성 fallback 쪽에서 사용
- `honey_parse`: `csvfile_to_df.py`가 실제 CSV parser로 import한다. 설치/경로가 없으면 CSV 로딩이 실패한다.

## 변경 시 주의

- 입력 포맷을 바꾸면 `constants.py`, `csv_loader.py`, `df_honey.py`를 같이 확인한다.
- 계산식을 바꾸면 `_builders.py`의 yield/CPK/fail/summary 계산과 `analyzer.py` 조립 순서를 같이 확인한다.
- xlsx sheet 이름, header, anchor 문구, 시작 위치를 바꾸면 서버 `server/xlsx_parser.py`와 업로드 문서도 확인한다.
- Distribution 성능 개선을 하더라도 데이터 포인트 수를 줄이면 안 된다.

더 큰 흐름은 `../../docs/08_report_generator_summary.md`와 `../../docs/06_analysis_engine.md`를 참고한다.

