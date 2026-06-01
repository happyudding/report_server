# 06 · 클라이언트 — 로컬 분석 엔진 (report_generator)

> CSV/xlsx 측정 데이터 → 통계 분석 → Excel xlsx 리포트 생성. 반도체 mass_data(웨이퍼/로트) 분석.
> 호출은 [05 UI](05_client_ui.md). 출력 xlsx 는 [07](07_client_upload_chart.md) 로 업로드되어 서버 [01 파서](01_server_upload.md)가 다시 읽는다.

## 계층 (PyQt/xlwings 비의존 순수 Python + xlsx_writer 만 Excel COM)
```
csv_loader  ──정규화──►  DfHoney (mass_data 1개)
                              │  여러 개 묶음
                              ▼
                        DfHoneyGroup ──select/filter──►
                              │
            analyzer.run() ──┼──► _builders (cpk/yield/fail/issue/summary/CDF)
                              ▼
                        AnalysisResult ──► xlsx_writer.write() ──► .xlsx (Excel)
```

## 파일 / 책임
| 파일 | 책임 |
|------|------|
| [constants.py](../client/report_generator/constants.py) | 표준 5-meta 포맷 상수(`META_COLUMNS`, `*_ROW`, `PASS_BIN="1"`) |
| [csv_loader.py](../client/report_generator/csv_loader.py) | raw 읽기 → 포맷 감지/정규화 → 구성요소 분리 |
| [df_honey.py](../client/report_generator/df_honey.py) | `DfHoney`: mass_data 1단위 + 검증 + 단건 분석 |
| [df_honey_group.py](../client/report_generator/df_honey_group.py) | `DfHoneyGroup`: 다중 묶음 + item/row 필터 + group 분석 |
| [_builders.py](../client/report_generator/_builders.py) | 순수 통계 함수(pandas/numpy) — **모든 수식의 진실** |
| [item_selector.py](../client/report_generator/item_selector.py) | `ItemSelector`: 선택 subject 목록 |
| [models.py](../client/report_generator/models.py) | `ReportMeta`/`DistSeries`/`AnalysisResult` dataclass |
| [analyzer.py](../client/report_generator/analyzer.py) | `run()`: 전체 orchestration |
| [xlsx_writer.py](../client/report_generator/xlsx_writer.py) | xlwings 로 6시트 + CDF 차트 출력 |

## 표준 입력 포맷 (5-meta, [constants.py](../client/report_generator/constants.py))
```
columns: DUT, XCoord, YCoord, Bin, Serial, item1, item2, ...
row0=헤더  row1=Units  row2=Lower  row3=Upper  row4/5=Lower/Upper(중복)  row6+=데이터
```
`csv_loader._detect_format`: A0=="dut"→standard, "site #"→test_rp. 4-meta CSV 는 Serial 컬럼을 삽입해 5-meta 로 승격(`_normalize_standard`). `test_rp` 는 행 라벨(test name/limit/units/site #) 탐색해 재구성. → `split_components` 가 subjects/units/limits/scores/meta dict 반환.

## 데이터 객체
- **DfHoney** = `name, subjects[], units[], lower_limits[], upper_limits[], scores(DataFrame 정수컬럼), meta(DataFrame: DUT/XCoord/YCoord/Bin/Serial)`. `validate()` 가 길이/컬럼/Bin 숫자 검사.
- **DfHoneyGroup** = `{source_name: DfHoney}`. `select_items`(subject 필터), `filter_rows_by_bin`(Bin1 Only), `split_by_dut`(단일파일 DUT별 분할 → DUT 가 source/legend). `subjects()`/`limits` 는 **첫 source 기준**.

## 분석 — `analyzer.run()` [analyzer.py:16](../client/report_generator/analyzer.py#L16)
선택 item 적용 → 전 테이블 계산 → `AnalysisResult`:
- `yield_rows` = `build_yield` (bin별 count/portion/avg/Main Fail subject)
- `cpk_rows` = `build_cpk` (subject×source + total, n/min/max/avg/stdev/cp/cpl/cpu/**cpk**)
- `fail_item_rows` = `build_fail_items` (yield + bin별 fail subject 랭킹)
- `issue_rows` = `build_issue_summary` (fail bin별 1순위 subject, avg 내림차순)
- `summary_rows` = `build_summary_rows` (per-subject + per-bin×item + bin전체)
- `distributions` = `_build_distributions` → source별 `cumulative_distribution_full` (CDF)
- `subjects_meta`, `total_dut`, `pass_yield`(Bin1 portion)

### _builders 핵심 수식 ([_builders.py](../client/report_generator/_builders.py))
- `_fail_mask`: `(value < lower) | (value > upper)` per subject (한계 NaN 이면 fail 아님).
- `_calc_stats`: `cp=(hi-lo)/(6σ)`, `cpl=(avg-lo)/(3σ)`, `cpu=(hi-avg)/(3σ)`, `cpk=min(cpl,cpu)` (n>1·σ≠0·limit 존재 시만).
- `PASS_BIN="1"` 은 합격 — yield/fail/issue 전반에서 제외 기준.
- `_subject_rankings_by_type`: bin타입별 fail subject 를 count/portion 으로 랭크.

## 출력 — `xlsx_writer.write()` [xlsx_writer.py:26](../client/report_generator/xlsx_writer.py#L26)
- `xw.App(visible=False)` 로 Excel 구동, 선택 시트만 순서대로 생성, `progress_cb(done,total,name)`.
- 시트별 writer: `_write_summary/_write_yield/_write_cpk/_write_fail_item/_write_issue_table/_write_distribution`.
- **distribution** 은 네이티브 Excel XY 산점(CDF) 차트: 숨김 `_dist` 헬퍼시트에 정렬 테이블(`_aligned_cdf_table`, 공통 x축, 150점 다운샘플) → `charts.add` + `set_source_data` + `_apply_series_colors`(팔레트) + LSL/USL 한계선(`_add_limit_lines`) + y축 0~100 고정. 차트 그리드는 5개/행, 오른쪽 `_INDEX_COL=40` 에 Ctrl+F 용 item 인덱스.

## ⚠️ 서버 파서와의 계약 (핵심)
`_write_summary` 는 서버 [xlsx_parser.py](../server/xlsx_parser.py) 의 **anchor 규약**에 맞춰 출력:
- A열 anchor: `Feature`(헤더+값 2행), `Yield Summary`(텍스트), `Major Fail Bins`(테이블), `Evaluation Summary`(헤더+값 2행).
- 빈 문자열 값은 xlwings 가 셀을 안 만들어 파서가 못 읽음 → 플레이스홀더 `"-"` 사용.
- `issue_table` 의 `Distribution` 열은 서버가 drop 하는 자리(이미지용).
→ **한쪽 레이아웃을 바꾸면 반드시 [01](01_server_upload.md) 파서와 같이 검증**. 이게 업로드 후 텍스트 추출이 깨지지 않게 하는 유일한 지점.

## 주의
- xlsx_writer 만 MS Excel + xlwings 필요. 나머지 계층은 pandas/numpy 만.
- numpy 스칼라는 `_to_native`/`_json_safe` 로 python 기본형 변환(Excel/JSON 호환).
