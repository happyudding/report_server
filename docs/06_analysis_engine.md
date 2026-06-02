# 06 · 클라이언트 — 로컬 분석 엔진 (report_generator)

> CSV/xlsx 측정 데이터 → 통계 분석 → **openpyxl 로 직접 생성**하는 Excel 리포트. 반도체 mass_data(웨이퍼/로트).
> 호출은 [05 UI](05_client_ui.md). 출력 xlsx 는 [07](07_client_upload_chart.md) 로 업로드 → 서버 [01 파서](01_server_upload.md)가 다시 읽는다.

## 계층 (순수 Python + xlsx_writer 만 openpyxl/xlwings)
```
csvfile_to_df ──정규화──► df_honey (단일 DataFrame 보유)
                               │  여러 개 묶음
                               ▼
                         df_honey_group ──select/filter──►
                               │
            analyzer.run() ───┼──► _builders (cpk/yield/fail/issue/summary/major_fail/CDF)
                               ▼
                         AnalysisResult ──► xlsx_writer.write() ──► .xlsx
```

## 파일 / 책임
| 파일 | 책임 |
|------|------|
| [constants.py](../client/report_generator/constants.py) | 5-meta 포맷 상수(`META_COLUMNS`, `*_ROW`, `PASS_BIN="1"`) |
| [csv_loader.py](../client/report_generator/csv_loader.py) | raw 읽기 → 포맷감지/정규화. **`csvfile_to_df`**(단일 df) / `load_components`(dict) |
| [df_honey.py](../client/report_generator/df_honey.py) | **`df_honey`**: 단일 df_honey-포맷 DataFrame 보유, 컴포넌트는 cached property |
| [df_honey_group.py](../client/report_generator/df_honey_group.py) | **`df_honey_group`**: 다중 묶음 + select/filter (df 슬라이싱 위임) + `rename_sources`(legend명 교체) |
| [_builders.py](../client/report_generator/_builders.py) | 순수 통계 함수(pandas/numpy) — **모든 수식의 진실** |
| [item_selector.py](../client/report_generator/item_selector.py) | `ItemSelector`: 선택 subject 목록 |
| [models.py](../client/report_generator/models.py) | `ReportMeta`/`DistSeries`/`AnalysisResult` dataclass |
| [analyzer.py](../client/report_generator/analyzer.py) | `run()`: 전체 orchestration |
| [xlsx_writer.py](../client/report_generator/xlsx_writer.py) | **하이브리드**: table 시트는 openpyxl 로 워크북 직접 생성 + distribution 만 xlwings |

## 입력 → 단일 DataFrame (df_honey 포맷)
`csvfile_to_df(path)` = `_read_raw → normalize_raw` → **단일 DataFrame** 반환:
```
columns: DUT, XCoord, YCoord, Bin, Serial, item1, item2, ...
row0=헤더  row1=Units  row2=Lower  row3=Upper  row4/5=중복  row6+=데이터
```
`df_honey` 는 이 df **하나만** 보유하고, `subjects/units/lower_limits/upper_limits/meta/scores` 를 **위치(iloc) 기반 cached property** 로 파생한다 → 코드 재사용·슬라이싱이 단순.
(포맷 감지 `_detect_format`: A0=="dut"→standard, "site #"→test_rp. 4-meta CSV 는 Serial 컬럼 삽입해 5-meta 승격.)

## 데이터 객체
- **df_honey** = `df`(단일) + `name` + `report_meta`. property: `subjects[]/units[]/lower_limits[]/upper_limits[]/scores`(정수컬럼 DataFrame)`/meta`(DUT/XCoord/YCoord/Bin/Serial).
  - **`select_subjects(keep_idx)`** = meta 5열 + 선택 subject열 슬라이싱, **`subset_rows(mask)`** = 헤더 6행 유지 + 데이터행 필터 → 새 `df_honey`. `validate()` 길이/컬럼/Bin 검사.
  - `from_csv`/`from_dataframe` 생성, `to_df()` 로 보유 df 반환.
- **df_honey_group** = `{source_name: df_honey}`. `select_items`(subject 필터)/`filter_rows_by_bin`(Bin1 Only)/`split_by_dut`(단일파일 DUT별 분할 → DUT 가 source/legend) 는 위 슬라이싱 메서드에 **위임**. `subjects()`/`limits` 는 **첫 source 기준**. `rename_sources(names)` 는 source(=legend/Filename)명을 순서대로 교체(UI FileName Change → [05](05_client_ui.md)).

## 분석 — `analyzer.run()` [analyzer.py:16](../client/report_generator/analyzer.py#L16)
선택 item 적용 → 전 테이블 계산 → `AnalysisResult`:
- `yield_rows` = `build_yield`: bin별 count/portion + **`{src}_count`/`{src}_yield`**(소스별) + `avg` + `Main Fail subject`
- `cpk_rows` = `build_cpk`: subject×source + `total` (n/min/max/avg/stdev/cp/cpl/cpu/**cpk**)
- `fail_item_rows` = `build_fail_items` (yield + bin별 fail subject 랭킹)
- `issue_rows` = `build_issue_summary` (fail bin별 1순위 subject, avg 내림차순)
- `summary_rows` = `build_summary_rows` (per-subject + per-bin×item + bin전체)
- **`major_fail_subject_rows`** = `build_major_fail_subjects`: bin 무관 subject별 fail 합산, `ratio=fail/total_dut`, 상위 5 → summary 1st~5th Fail
- `distributions` = source별 `cumulative_distribution_full` (CDF)
- `total_dut`, `pass_yield`(Bin1 portion), `subjects`

### _builders 핵심 수식
- `_fail_mask`: `(value<lo)|(value>hi)` per subject (한계 NaN 이면 fail 아님).
- `_calc_stats`: `cp=(hi-lo)/6σ`, `cpl=(avg-lo)/3σ`, `cpu=(hi-avg)/3σ`, `cpk=min(cpl,cpu)` (n>1·σ≠0·limit 존재 시).
- `PASS_BIN="1"` 합격 — yield/fail/issue 제외 기준. `_subject_rankings_by_type`: bin타입별 fail subject 랭크.

## 출력 — `xlsx_writer.write()` (하이브리드) [xlsx_writer.py](../client/report_generator/xlsx_writer.py)
**Phase 1 (openpyxl)** — `openpyxl.Workbook()` 로 워크북을 **처음부터 생성**하고 셀 값·스타일을 직접 기입(서식은 모듈 상단 상수 `_HDR_FONT`/`_HDR_FILL`/`_TITLE_*` 등). 표는 **B열~·헤더 3행**, 1행은 시트 제목 배너(**좌측 정렬**, `_TITLE_ALIGN`). 눈금선 제거.
- **summary** 3 섹션(`_fill_summary`, 좌측 정렬): `1.Device Feature`, `2.Yield`(Lot NO/Yield/Major Fail 1st~5th = subject+ratio), `3.Evaluation Summary`(Yield/CPK/Temp/ETC).
- **yield** = `bin | Item | {src}_count …(전 소스) | {src}_yield …(전 소스) | avg | comment` — **count 들을 먼저 모두, 이어서 yield 들을 모두** 묶어 배치.
- **fail_item** = yield 와 동일 컬럼 그룹화 + 끝에 `Distribution`(Phase 2 PNG).
- **cpk** = `TEST NAME | LOW SPEC | HIGH SPEC | SCALE | 계열 | n … cpk | comment`.
- **issue_table** = `Category`(Yield 블록=yield 재사용, CPK/ETC 플레이스홀더) `| Bin | Item | avg | {src}_yield… | Distribution | comment…` (count 열 없음).

**Phase 2 (xlwings)** — distribution 시트 + 차트만 Excel COM.
- 차트당 CDF: x=value, y=0~1(`NumberFormatLocal="0%"`), source별 series + LSL/USL(**series 1,2**).
- 서식: y `MajorUnit 0.2`/`TickLabelPosition xlLow`, limit line 빨강 sysdash·마커없음, legend 에서 limit entry 삭제, TickLabel/Legend 8pt, Title Arial Black 10pt, PlotArea 280/30/167, 차트 **324×198 gap 0 밀착**, Y/X `HasMinorGridlines`.
- x축: Pass=`[lo,hi]`, Fail=±5% 가드밴드 후 limit 자릿수 `floor/ceil` (LIM None/nan 이면 data min/max). **Fail 차트 ChartArea 연노랑(255,255,204)**.
- 숨김 `_dist` 헬퍼시트(150점 다운샘플), AN열 Ctrl+F 인덱스.
- **fail_item PNG**: distribution 차트를 `Export(PNG)` 해 fail_item 시트 오른쪽에 **불량율 높은 순·1/3 크기**로 부착(차트 원본 재생성 X → 실행시간 단축).

## ⚠️ 서버 파서와의 계약
table 레이아웃(summary anchor·yield/issue 헤더)은 서버 [xlsx_parser.py](../server/xlsx_parser.py) 가 **2D anchor** 로 읽는 규약과 **짝**. 한쪽 바꾸면 [01](01_server_upload.md) 파서와 같이 검증.

## 주의
- distribution(+fail_item PNG)만 MS Excel + xlwings 필요. **table 5시트는 openpyxl 로 Excel 없이** 생성(속도↑).
- 템플릿 파일 없음 — 워크북·시트·스타일을 모두 코드로 생성한다(`templete.xlsx` 제거됨).
- numpy 스칼라는 `_sanitize_cell` 로 python 기본형 변환.
