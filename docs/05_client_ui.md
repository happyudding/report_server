# 05 · 클라이언트 — Honey UI / 워크플로우

> PyQt5 메인 윈도우. "입력 선택 → 항목/시트 고르기 → 분석 실행(자동 저장) → 서버 업로드" 의 사용자 동선 전체.
> 계산은 [06 분석 엔진](06_analysis_engine.md), 전송은 [07 업로드](07_client_upload_chart.md), 업데이트는 [04](04_honey_update.md) 로 위임.

## 파일
- [client/honey_main.py](../client/honey_main.py) — `HoneyMainWindow` + 다이얼로그 4종 + `main()`
- UI 레이아웃(.ui, Qt Designer): `honey_main.ui`, `upload_dialog.ui`, `d1_browser.ui`, `file_order.ui` — 런타임 `uic.loadUi`
- [client/config.py](../client/config.py) — `SERVER_BASE_URL`, `D1_STORAGE_DIR`, `CONFIG_DIR`
- [client/chart_colors.py](../client/chart_colors.py) — distribution 차트 48색 팔레트(편집/저장)

## 화면 구성 / 다이얼로그
| 클래스 | .ui | 역할 |
|--------|-----|------|
| `HoneyMainWindow` | honey_main.ui | 메인: 입력목록·항목 선택(좌/우)·시트 체크·요약·버튼 |
| `UploadDialog` | upload_dialog.ui | 업로드 메타 팝업(ProductType 라디오/Product/LOT/Revision/**PW 4자리**) |
| `D1BrowserDialog` | d1_browser.ui | d1_storage(가상 서버 스토리지) 키워드 검색·다중선택 |
| `FileOrderDialog` | file_order.ui | 입력 2개↑ 시 순서 확정(첫 파일=기준 스키마) |
| `ColorEditorDialog` | (코드 생성) | 48색 그리드 편집 → chart_colors.json |

## 메인 워크플로우 (`HoneyMainWindow`)
1. **입력 선택** — `on_open_local`(로컬 파일대화) 또는 `on_browse_d1`(D1 검색) → `_intake` → 2개↑면 `FileOrderDialog` → `_load_paths`.
2. **그룹 구성** — `_rebuild_group` 가 `rg.DfHoneyGroup.from_csvs(paths)` 로드 + `validate()` 경고. **첫(맨 위) 파일이 units/항목명/limit 기준** ([06](06_analysis_engine.md)).
3. **항목 선택** — 좌(제외)/우(선택) 리스트 이동(`_move_*`). `_select_fail_only` 는 fail 발생 subject 만 우측. 원본 순서는 `UserRole` 로 보존(`_resort`).
4. **시트 선택** — `SHEET_OPTIONS = summary/yield/cpk/fail_item/issue_table/distribution`. `cb_sheet_yield` 해제 시 `fail_item`/`issue_table` 비활성(`_sync_yield_dependents`).
5. **데이터 정리 모드** — `_apply_modes`: `Bin1 Only`(`filter_rows_by_bin("1")`) → `DUT 정리`(`split_by_dut`, 입력 1개일 때만, `_update_dut_mode_availability`).
6. **분석 실행** — `on_analyze`: 검증 → `_apply_modes` → `rg.analyze(...)` → `_show_summary` 미리보기 → `xlsx_writer.write(...)` 로 입력폴더에 `<base>_report_YYMMDD_HHMM.xlsx` 자동 저장(`_build_output_path`). 진행바는 분석 1 + 시트 N. `cb_auto_upload` 면 곧장 업로드.
7. **서버 업로드** — `on_upload_local`(임의 xlsx 직접) 또는 분석 후 → `_do_upload` ([07](07_client_upload_chart.md)).
8. **차트 색 편집** — `on_edit_chart_colors` → 다음 분석부터 적용.
9. **업데이트** — 기동 500ms 후 `check_for_update` ([04](04_honey_update.md)).

## 핵심 상태 / 헬퍼
- 상태: `csv_paths`, `group`(DfHoneyGroup), `last_result`(AnalysisResult), `out_path`, `_last_upload`(메타 프리필).
- `_validate_meta` — Product/LOT 필수 + PIN 숫자 4자리.
- `_suggest_base_name`/`_build_output_path`/`_TS_RE` — 결과 파일명(`_YYMMDD_HHMM` 접미사 중복 방지).
- 경로 탐색: `_BASE_DIR = sys._MEIPASS or 스크립트폴더` (frozen 대응, .ui 위치).
- `main()`: `_apply_cute_font`(둥근 폰트), `_install_excepthook`(슬롯 미처리 예외를 메시지박스로 — PyQt5 기본 abort 방지).

## 주의
- **엔진 미설치 그레이스풀** — `import report_generator` 실패 시 `_disable_engine`: 분석 버튼만 비활성, **로컬 xlsx 직접 업로드는 유지**. 분석/생성엔 pandas/numpy/xlwings+Excel 필요.
- 모든 무거운 작업 사이 `QApplication.processEvents()` 로 UI 갱신 + 진행바.
- D1 검색은 매번 디스크 재스캔(`_scan` rglob csv/xlsx).
