# 05 · 클라이언트 — Honey UI / 워크플로우

> PyQt5 메인 윈도우. "입력 선택 → 항목/시트 고르기 → 분석 실행(자동 저장) → 서버 업로드" 의 사용자 동선 전체.
> 계산은 [06 분석 엔진](06_analysis_engine.md), 전송은 [07 업로드](07_client_upload_chart.md), 업데이트는 [04](04_honey_update.md) 로 위임.

## 파일
- [client/honey_main.py](../client/honey_main.py) — `HoneyMainWindow` + `main()` (워크플로우 연결)
- [client/honey_ui/](../client/honey_ui/) — `UploadDialog`, `ReportSettingsDialog`, `FileOrderDialog`, `ColorEditorDialog`, 진행바 헬퍼
- [client/d1/](../client/d1/) — D1 입력 provider 진입점 (`get_provider`, `list_files`, `D1BrowserDialog`); 외부 브랜치 가이드 [README](../client/d1/README.md)
- [client/report_flow/](../client/report_flow/) — 파일명 생성, 업로드 xlsx 전처리
- UI 레이아웃(.ui, Qt Designer): `honey_main.ui`, `upload_dialog.ui`, `d1_browser.ui`, `file_order.ui`, `report_settings.ui` — 런타임 `uic.loadUi`
- [client/config.py](../client/config.py) — `D1_STORAGE_DIR`, `CONFIG_DIR`
- [client/app_settings.py](../client/app_settings.py) — 사용자별 settings.json(Product Type 등 복원)
- [client/chart_colors.py](../client/chart_colors.py) — distribution 차트 48색 팔레트(편집/저장)

## 화면 구성 / 다이얼로그
| 클래스 | .ui | 역할 |
|--------|-----|------|
| `HoneyMainWindow` | honey_main.ui | 메인: ProductType 라디오·입력목록(좌측 open 버튼/우측 ▲▼)·저장명·Status·버튼 |
| `ReportSettingsDialog` | report_settings.ui | Start 후 설정 팝업: 출력 시트(Option)·항목 선택(좌/우)·FileName Change·Color·Auto Upload·Confirm |
| `UploadDialog` | upload_dialog.ui | 업로드 메타 팝업(ProductType 라디오/Product/LOT/Revision/**PW 4자리**) |
| `D1BrowserDialog` | d1_browser.ui | d1_storage(가상 서버 스토리지) 키워드 검색·다중선택 |
| `FileOrderDialog` | file_order.ui | 입력 2개↑ 시 순서 확정(첫 파일=기준 스키마) |
| `ColorEditorDialog` | (코드 생성) | 48색 그리드 편집 → chart_colors.json |

> **Product Type 라벨**: 화면 표시, 내부 키, 서버 전송값은 모두
> `MDDI / PDDI / PMIC / SECURITY` 를 그대로 사용한다.

## 메인 워크플로우 (`HoneyMainWindow`)
1. **입력 선택** — `on_open_local`(LOCAL FILE OPEN, 로컬 파일대화) 또는 `on_browse_d1`(D1 검색: `client/d1` provider) → `_intake` → 2개↑면 `FileOrderDialog` → `_load_paths`. open 버튼 2종은 입력목록(`list_csv`) **왼쪽 칼럼**, ▲▼ 순서이동은 오른쪽.
2. **저장명 제안** — `_load_paths` 가 `_suggest_base_name` 으로 `le_outname` 채움(확장자 `.xlsx` 는 화면 라벨로 별도 표기).
3. **Start** — `on_start`: `_rebuild_group`(`df_honey_group.from_csvs` + `validate()` 경고, **첫 파일=기준 스키마** [06](06_analysis_engine.md)) → `ReportSettingsDialog` 팝업.
4. **설정 팝업(`ReportSettingsDialog`)**
   - **출력 시트(Option)** — `SHEET_OPTIONS = summary/yield/cpk/fail_item/issue_table/distribution`. `yield` 해제 시 `fail_item`/`issue_table` 비활성(`_sync_yield_dependents`).
   - **항목 선택** — 좌(제외)/우(선택) 이동(`_move_*`), `Fail only` 는 fail subject 만 우측. 원본 순서는 `UserRole` 보존(`_resort`).
   - **FileName Change** — `on_edit_filenames`: 입력 파일별 legend명(Filename)을 콤마로 구분해 한 줄 편집. Confirm 시 `group.rename_sources(...)` 로 source 명 교체(빈칸=기존명, 중복=`_n`). DUT 정리 모드면 자체 명명 사용으로 미적용.
   - **데이터 정리 모드** — `Bin1 Only`(`filter_rows_by_bin("1")`) / `DUT 정리`(`split_by_dut`, 입력 1개일 때만, `_update_dut_mode_availability`).
   - **Color Change** — `on_edit_chart_colors`(48색) / **Server Auto Upload** 체크.
5. **분석 실행** — Confirm → `_apply_modes` → `_run_analysis`.
   - Log(`txt_summary`) 는 실행 시작 시 초기화되고 `[mm:ss] [step xx/yy] <step> done: n.nn s` 형식의 debug timer 를 append 한다. `running...` 시작 로그는 표시하지 않는다.
   - `rg.analyze(..., profile_cb=...)` 와 `xlsx_writer.write(..., profile_cb=...)` 의 단계 이벤트를 Log 에 누적하고, 최종 summary 는 기존 로그를 덮지 않고 아래에 append 한다.
   - 진행바: 준비/분석/요약 + 시트 생성 + distribution chart 개수 진행률 + PNG attach 개수 진행률. distribution chart 100% 는 chart 생성 완료이며, 같은 phase 의 PNG attach/save 는 별도 시간이 더 걸릴 수 있다.
   - 결과는 입력폴더에 `<base>_report_YYMMDD_HHMM.xlsx` 로 저장한다(`_build_output_path`). `cb_auto_upload` 면 곧장 업로드.
6. **서버 업로드** — `on_upload_local`(임의 xlsx 직접) 또는 분석 후 → `_do_upload` ([07](07_client_upload_chart.md)).
7. **업데이트** — 기동 500ms 후 `check_for_update` ([04](04_honey_update.md)).

## 핵심 상태 / 헬퍼
- 상태: `csv_paths`, `group`(df_honey_group), `last_result`(AnalysisResult), `out_path`, `_last_upload`(메타 프리필).
- `_validate_meta` — Product/LOT 필수 + PIN 숫자 4자리.
- `_suggest_base_name`/`_build_output_path`/`_TS_RE` — 결과 파일명(`_YYMMDD_HHMM` 접미사 중복 방지).
- 경로 탐색: `_BASE_DIR = sys._MEIPASS or 스크립트폴더` (frozen 대응, .ui 위치).
- `main()`: `_apply_cute_font`(둥근 폰트), `_install_excepthook`(슬롯 미처리 예외를 메시지박스로 — PyQt5 기본 abort 방지).
- Log(`txt_summary`) 는 read-only QTextEdit 이며 드래그 선택/Ctrl+A/C 복사가 가능하다. 실행 로그와 최종 summary 가 같은 영역에 남는다.

## 주의
- **report generator 산출물은 .xlsx 1개**. 클라이언트는 하나의 파일에서 모든 것을 관리하는 정책이므로, 분석 결과물 xlsx 는 단일 파일로만 존재해야 한다.
- **엔진 미설치 그레이스풀** — `import report_generator` 실패 시 `_disable_engine`: 분석 버튼만 비활성, **로컬 xlsx 직접 업로드는 유지**. 분석/생성엔 pandas/numpy/xlwings+Excel 필요.
- 모든 무거운 작업은 worker thread + `_wait_for_future(..., poll_cb=...)` 로 돌리고, poll 중 `QApplication.processEvents()` 로 UI 갱신 + 진행바/Log 이벤트를 drain 한다.
- D1 검색은 `client/d1`의 기본 provider가 매번 디스크 재스캔(rglob csv/xlsx)한다. 외부 D1 프로젝트는 이 패키지만 교체한다.
