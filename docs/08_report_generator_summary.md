# report_generator 요약

`client/report_generator/`는 Honey 클라이언트 안에서 CSV/xlsx 측정 데이터를 로컬로 분석하고, Excel COM(xlwings)을 통해 업로드 가능한 `.xlsx` 리포트를 생성하는 엔진이다. 서버, DB, S3, Flask에는 의존하지 않고, Honey UI(`client/honey_main.py`)가 이 엔진을 호출한 뒤 생성된 xlsx를 업로드 흐름으로 넘긴다.

이 문서는 코드 파악용 빠른 요약이다. 더 상세한 기능별 흐름은 [06_analysis_engine.md](06_analysis_engine.md)를 함께 본다.

## 한 줄 흐름

```text
CSV/xlsx 파일
  -> csv_loader.csvfile_to_df()
  -> df_honey
  -> df_honey_group
  -> analyzer.run()
  -> AnalysisResult
  -> xlsx_writer.write()
  -> Excel 리포트(.xlsx)
```

## 주요 진입점

| 목적 | 파일 / 함수 | 설명 |
|---|---|---|
| 외부 API | [__init__.py](../client/report_generator/__init__.py) `build_report`, `analyze` | CSV 경로 목록을 받아 분석하거나, `out_path`가 있으면 xlsx까지 생성한다. |
| 입력 정규화 | [csv_loader.py](../client/report_generator/csv_loader.py) | standard/test_rp 포맷을 감지하고 `df_honey` 표준 DataFrame으로 정규화한다. |
| 단일 입력 모델 | [df_honey.py](../client/report_generator/df_honey.py) `df_honey` | 입력 파일 1개 또는 sheet 1개를 나타낸다. subject, unit, limit, score, fail mask를 캐시로 파생한다. |
| 다중 입력 모델 | [df_honey_group.py](../client/report_generator/df_honey_group.py) `df_honey_group` | 여러 `df_honey`를 source 단위로 묶고, rename/filter/split/diff 계산 준비를 담당한다. |
| 순수 계산 | [_builders.py](../client/report_generator/_builders.py) | yield, CPK, fail item, issue, summary, distribution용 수치 계산을 담당한다. |
| 분석 조립 | [analyzer.py](../client/report_generator/analyzer.py) `run` | group과 selector를 받아 `AnalysisResult`를 만든다. |
| 결과 모델 | [models.py](../client/report_generator/models.py) | `ReportMeta`, `DistSeries`, `AnalysisResult` dataclass 정의. |
| xlsx 생성 | [xlsx_writer.py](../client/report_generator/xlsx_writer.py) `write` | 단일 Excel COM 세션에서 table/raw/distribution/PNG attach/save를 수행한다. |

## 입력 데이터 불변식

`df_honey`는 입력을 다음 canonical DataFrame 구조로 유지한다.

```text
columns: DUT, XCoord, YCoord, Bin, Serial, item1, item2, ...
row 0  : Units
row 1  : Lower Limit
row 2  : Upper Limit
row 3  : Lower Limit (duplicate)
row 4  : Upper Limit (duplicate)
row 5+ : DUT 측정 데이터
```

주의할 점:

- 헤더명은 `df.columns`에만 존재해야 한다.
- row0에 `DUT/XCoord/...` 헤더가 중복으로 들어가면 unit/limit/data 인덱스가 밀린다.
- `constants.py`의 `DATA_START_ROW = 5`, `PASS_BIN = "1"` 전제를 바꾸면 계산 전체에 영향이 간다.
- `csvfile_to_df.py`는 실제 파서를 `honey_parse.csvfile_to_df`에서 import한다. 없으면 호출 시 `ImportError`가 난다.

## 분석 단계

1. Honey UI가 선택된 파일들을 `df_honey.from_csv()`로 로드한다.
2. `df_honey_group`이 source 이름을 중복 제거하고, 여러 입력을 하나의 분석 단위로 묶는다.
3. 설정 팝업 결과에 따라 item 선택, Bin1 only, DUT split, source rename이 적용된다.
4. `analyzer.run()`이 `AnalysisResult`를 만든다.
5. `xlsx_writer.write()`가 선택된 sheet 목록에 따라 xlsx를 저장한다.

`AnalysisResult`에는 다음 결과가 함께 들어간다.

- `yield_rows`: bin별 count/yield/source별 yield
- `cpk_rows`: source별/total CPK 통계
- `fail_item_rows`: bin별 주요 fail subject
- `issue_rows`: fail bin 요약
- `summary_rows`: 서버 파서와 summary sheet를 위한 요약 행
- `distributions`: distribution chart metadata
- `dist_source_data`: distribution chart X/Y를 writer가 만들기 위한 source별 numeric frame
- `fail_value_rows`: fail_item sheet의 `FAIL_VALUES` 섹션 데이터

## fail 판정

`df_honey.fail_mask`는 세 가지 조건을 합친다.

- `value < lower_limit`
- `value > upper_limit`
- non-pass DUT에서 stop-on-fail 형태로 뒤쪽 값이 비는 break 지점

이 mask는 fail item ranking, issue table, major fail subject, fail value section에서 공통으로 쓰인다.

## diff compare

입력 source가 정확히 2개이고 subject 집합이 서로 다르면 diff compare 모드가 켜진다.

- common subject는 메인 CPK/Distribution에 출력한다.
- A에만 있는 subject는 `CPK_<source>`, `Distribution_<source>` 형태의 별도 sheet로 출력한다.
- B에만 있는 subject도 같은 방식으로 별도 sheet를 만든다.
- Yield, Fail Item, Issue, Summary는 기존 병합 기준 계산을 유지한다.

## xlsx 생성

`xlsx_writer.write()`는 openpyxl 기반 fallback 없이 Excel + xlwings가 필요하다.

주요 동작:

- `summary`, `yield`, `cpk`, `fail_item`, `issue_table`, `distribution` sheet를 선택적으로 생성한다.
- Raw Data 옵션이 켜지면 source별 `df_honey` DataFrame을 임시 CSV로 저장한 뒤 Excel 데이터 파싱으로 sheet 전체를 복사한다.
- Distribution은 같은 workbook 안에 숨김 helper sheet를 만들고, 모든 chart를 Excel native chart로 생성한다.
- 저장 후 `_wait_for_xlsx_ready()`와 `_validate_embedded_images()`로 파일 안정성과 이미지 관계를 확인한다.

## Distribution 차트 규칙

Distribution은 다운샘플링하지 않는다.

- 모든 DUT 값을 정렬 X 데이터로 반영한다.
- source data는 선이 아니라 점(marker)으로 표시한다.
- 동일값/정수형 데이터도 점 외 표현으로 바꾸지 않는다.
- LSL/USL은 빨간 dashed limit line으로 표시한다.
- fail chart는 배경색으로 구분한다.

이 규칙은 프로젝트 최상위 불변 규칙과 연결되어 있으므로 `_MAX_CDF_POINTS`, `_downsample`, `max_points` 같은 상한 로직을 추가하면 안 된다.

## Honey UI와의 연결

`client/honey_main.py` 기준 흐름:

- 파일 로딩: `df_honey.from_csv()`를 백그라운드 worker에서 실행한다.
- Start: `ReportSettingsDialog`에서 item/sheet/mode/raw/upload 설정을 받는다.
- 분석: `rg.analyze(work_group, selector=ItemSelector(...))`
- 생성: `xlsx_writer.write(..., raw_sheets=raw, dist_progress_cb=..., attach_progress_cb=...)`
- 업로드: 생성된 xlsx 또는 사용자가 고른 로컬 xlsx를 `client/report_flow/upload_prepare.py`로 전처리한 뒤 `client/transport/uploader.py`가 `/pe/report/upload_xlsx`에 multipart POST한다.

## 서버와의 계약

서버는 생성된 xlsx를 [server/xlsx_parser.py](../server/xlsx_parser.py)로 읽는다. 따라서 다음 변경은 서버 파서도 같이 확인해야 한다.

- sheet 이름 변경
- summary/yield/issue_table anchor 문구 변경
- table header 이름 변경
- table 시작 행/열 변경
- distribution chart PNG attach 위치나 issue_table 이미지 연결 방식 변경

## 변경 시 체크리스트

- 계산식 변경은 [_builders.py](../client/report_generator/_builders.py)에서 먼저 확인한다.
- 입력 포맷 변경은 [constants.py](../client/report_generator/constants.py), [csv_loader.py](../client/report_generator/csv_loader.py), [df_honey.py](../client/report_generator/df_honey.py)를 함께 본다.
- xlsx 레이아웃 변경은 [xlsx_writer.py](../client/report_generator/xlsx_writer.py)와 서버 [xlsx_parser.py](../server/xlsx_parser.py)를 함께 본다.
- UI 옵션 변경은 [client/honey_main.py](../client/honey_main.py)의 `_apply_modes`, `_run_analysis` 흐름을 확인한다.
- 업로드 전 xlsx 재구성이나 issue image 추출 변경은 [client/report_flow/upload_prepare.py](../client/report_flow/upload_prepare.py)를 확인한다.

