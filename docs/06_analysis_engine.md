# 06 · 클라이언트 — 로컬 분석 엔진 (`report_generator`)

> CSV/xlsx 측정 데이터 → `df_honey` 정규화 → 통계 분석 → **xlwings 단일 Excel COM 세션**으로
> xlsx 리포트 생성. 호출은 [05 UI](05_client_ui.md), 업로드는 [07](07_client_upload_chart.md).

## 계층
```
csvfile_to_df ──정규화──► df_honey (단일 DataFrame 보유)
                               │
                         df_honey_group
                               │ select/filter/split/rename
                               ▼
                         analyzer.run()
                               │
              AnalysisResult (tables + distribution metadata/source frames)
                               ▼
                         xlsx_writer.write() ──► .xlsx
```

## 파일 / 책임
| 파일 | 책임 |
|------|------|
| [constants.py](../client/report_generator/constants.py) | 5-meta df_honey 포맷 상수. 헤더는 `df.columns` 에만 있고 `row0=Units`, `DATA_START_ROW=5`. |
| [csv_loader.py](../client/report_generator/csv_loader.py) | raw CSV/xlsx 로드 → standard/test_rp 감지 → 헤더 중복 row 제거 → 정규화 DataFrame. |
| [df_honey.py](../client/report_generator/df_honey.py) | 단일 mass_data. `subjects/units/limits/meta/scores/numeric_scores/fail_mask` 를 cached property 로 파생. |
| [df_honey_group.py](../client/report_generator/df_honey_group.py) | 다중 source 묶음. source 이름 dedup/rename, item select, Bin1 filter, DUT split, diff split, raw/distribution frame 제공. |
| [_builders.py](../client/report_generator/_builders.py) | yield/cpk/fail/issue/summary/major fail 순수 계산. |
| [analyzer.py](../client/report_generator/analyzer.py) | group → `AnalysisResult` orchestration + `profile_cb` 단계 이벤트. |
| [models.py](../client/report_generator/models.py) | `ReportMeta`, `DistSeries`, `AnalysisResult`. Distribution 은 metadata + source frame 중심. |
| [xlsx_writer.py](../client/report_generator/xlsx_writer.py) | 단일 `xw.App` 세션에서 raw/table/distribution/PNG attach/save 수행. openpyxl fallback 없음. |
| [profile_run.py](../client/profile_run.py) | PyQt 없이 parse/analyze/xlsx 구간 측정 JSON 저장/비교. |

## df_honey 포맷
`csvfile_to_df(path)` 반환 df 는 아래 구조를 지킨다.
```
columns: DUT, XCoord, YCoord, Bin, Serial, item1, item2, ...
row0: Units
row1: Lower Limit
row2: Upper Limit
row3: Lower Limit (duplicate)
row4: Upper Limit (duplicate)
row5+: DUT 측정 데이터
```

중요 규칙:
- 헤더명은 `df.columns` 로만 존재한다. row0 에 `DUT/XCoord/...` 헤더를 다시 남기면 units/limit/data 인덱스가 모두 밀린다.
- `df_honey.to_df()` 는 이 보유 df 그대로 반환한다. Raw Data 시트는 이 df 를 임시 CSV 로 열어 Excel 시트로 복사한다.
- `df_honey.numeric_frame()` 은 `numeric_scores` 를 subject 이름 컬럼으로 바꾼 DataFrame 이며, distribution all-DUT ECDF 작성에 사용된다.

## 분석 흐름
`analyzer.run(group, meta, selector, profile_cb=None)` 는 선택 item 적용 후 전체 결과를 만든다.
- group 캐시는 `_compute_all_once()` 에서 per-source `fail_mask`, subject ranking, yield, cpk, fail item, summary 를 한 번 계산한다.
- `issue_rows` 는 `build_issue_summary(..., fail_items=fail_item_rows)` 로 fail item 캐시를 재사용한다.
- `major_fail_subject_rows` 는 bin 무관 subject fail 합산 상위 항목이며 Summary 1st~5th Fail 에 사용한다.
- `fail_value_rows` 는 source별 `md.get_fail_detail()` 결과로, Fail_item 의 `FAIL_VALUES` 섹션에 들어간다.
- diff compare 는 입력 2개이고 subject 집합이 다를 때만 동작한다. common subject 는 메인 CPK/Distribution, a_only/b_only 는 별도 `CPK_*`, `Distribution_*` 시트로 출력한다.
- distribution 은 analyzer 에서 모든 ECDF 점을 미리 만들지 않는다. `DistSeries` 는 subject/limit metadata 중심이고, `dist_source_data=[(source, numeric_frame)]` 를 writer 에 넘긴다.

`profile_cb`/`_profile` 구간:
- analyzer: `build_yield`, `build_fail_items`, `build_issue_summary`, `build_summary_rows`, `build_major_fail_subjects`, `build_cpk`, `build_distributions`, `fail_detail <source>` 등.
- xlsx writer: `workbook_init`, `fill_*`, `fill_raw_data[...]`, `finalize_layouts`, `distribution_xlwings_phase`, `workbook_save` 등.
- GUI 는 이 이벤트를 Log 영역에 `done: n.nn s` 형식으로 append 하고, `profile_run.py` 는 JSON 으로 저장/비교한다.

## xlsx 생성 흐름
`xlsx_writer.write()` 는 하나의 `xw.App(visible=False, add_book=False)` 안에서 모든 작업을 끝낸다.
- workbook 생성 후 table 시트(`Summary/Yield/Cpk/Fail_item/Issue_table`)를 만들고, 범위 단위 bulk write/style 을 적용한다.
- table border 는 외곽선 + 내부 가로/세로선(`xlEdge*`, `xlInsideVertical`, `xlInsideHorizontal`)을 적용한다.
- Raw Data ON 이면 source별 df_honey DataFrame 을 임시 CSV 로 저장한 뒤 Excel 네이티브 파싱으로 시트 전체를 복사한다. 성능 때문에 Raw Data 에는 별도 column width, 중앙정렬, wrap, title 배너를 적용하지 않는다.
- `finalize_layouts` 는 Summary/Raw Data 를 제외한 table 시트에 used_range 중앙정렬/wrap + 1행 제목 배너를 적용한다.
- Distribution ON 이면 같은 workbook/session 안에서 distribution 시트와 숨김 helper 시트를 만들고 차트 및 PNG attachment 를 처리한다.
- 저장 후 `_wait_for_xlsx_ready()` 로 Excel 종료 후 파일 안정화를 기다리고, `_validate_embedded_images()` 로 이미지 relationship 무결성을 확인한다.

## Distribution / PNG
- Distribution 데이터 다운샘플링은 하지 않는다. 모든 DUT 값이 정렬 X 데이터와 rank/count Y 데이터로 반영된다.
- writer 는 source별 `numeric_frame` 을 subject 열 기준으로 정렬해 숨김 helper 시트 `정리` 에 bulk write 하고, `정리_Y` 는 source/test item 별 valid count case 기반 compact helper 로 작성한다.
- 차트 series 는 `정리/정리_Y` 의 source별 valid 행 구간만 참조한다. LSL/USL 은 range 가 아니라 배열 literal 로 넣어 series index 를 안정화한다.
- 차트는 5개/행, 324×198 크기로 배치하고, AG열에 `Item Index (Ctrl+F)` 를 둔다.
- fail chart 는 연노랑 배경, limit line 은 빨간 sysdash, 모든 source data 는 점(marker)으로 표시한다.
- `fail_item`/`issue_table` 의 Distribution 열에는 distribution 차트를 `Chart.Export(PNG)` 후 `pictures.add` 로 붙인다. Export 실패 시 `CopyPicture` fallback 을 사용한다.
- `distribution_xlwings_phase` 시간은 차트 생성뿐 아니라 helper 데이터 쓰기, 차트 1000개 생성, fail_item/issue_table PNG export+attach 전체를 포함한다.

## 서버 파서와의 계약
- 서버 [xlsx_parser.py](../server/xlsx_parser.py)는 summary/yield/issue_table 을 anchor 기반으로 읽는다.
- table 시작 위치, 헤더 행, yield/issue_table 컬럼명, summary anchor 문구를 바꾸면 [01_server_upload.md](01_server_upload.md) 파서 계약도 같이 확인해야 한다.
- Distribution 차트와 Raw Data 시트는 서버 텍스트 파서의 핵심 DB 저장 대상이 아니다. 업로드 시 별도 chart PNG multipart/combined distribution PNG 흐름은 [07](07_client_upload_chart.md) 참고.

## 주의
- xlsx 생성에는 Excel + xlwings 가 필수다. openpyxl fallback 은 없다.
- Distribution 데이터 포인트 상한, downsample, max_points 류 로직을 추가하지 않는다.
- Raw Data 중복 헤더 문제는 `df.columns` 와 row0 역할을 분리하는 포맷 규칙에서 관리한다.
