# 05 · 클라이언트 — Honey UI / 워크플로우

> PyQt6 메인 윈도우. "입력 선택 → 항목/시트 고르기 → 분석 실행(자동 저장) → 서버 업로드" 사용자 동선.
> 계산 [06](06_analysis_engine.md) · 전송 [07](07_client_upload_chart.md) · 업데이트 [04](04_honey_update.md) 로 위임.
> **client/ 수정은 사전 승인 필요** ([../CLAUDE.md](../CLAUDE.md) §5).

## 파일
- [client/honey_main.py](../client/honey_main.py) — `HoneyMainWindow` + `main()`
- [client/honey_ui/](../client/honey_ui/) — 다이얼로그·진행바 헬퍼
- [client/report_flow/](../client/report_flow/) — 파일명 생성, 업로드 xlsx 전처리(Excel COM 추출)
- [client/embedded_browser.py](../client/embedded_browser.py) — 세션 열람 내장 브라우저 (`HoneyUser/<계정>` UA 삽입 → 서버 신원)
- [client/client_identity.py](../client/client_identity.py) — PC 계정/호스트 신고값 (web_report 업로더)
- [d1/](../d1/) — **(외부·검증용)** D1 입력 provider (`get_provider`/`list_files`/`D1BrowserDialog`). 기본 구현은 로컬 `d1_storage/` 검색 (Honey.exe 호환성 테스트)
- UI 레이아웃(.ui, Qt Designer): `honey_main.ui` 등 — 런타임 `uic.loadUi`

## 다이얼로그
| 클래스 | 역할 |
|--------|------|
| `HoneyMainWindow` | 메인: ProductType 라디오·입력목록·저장명·Status·버튼 |
| `ReportSettingsDialog` | 출력 시트·항목 선택·FileName·Color·Auto Upload |
| `UploadDialog` | 업로드 메타(ProductType/Product/LOT/Revision, password 선택) |
| `D1BrowserDialog` | `d1_storage` 검색·다중선택 (외부 provider 결과) |
| `FileOrderDialog` | 입력 2개↑ 시 순서 확정(첫 파일=기준 스키마) |

> **Product Type 라벨**(불변): 화면 표시·내부 키·서버 전송값 모두
> `MDDI / PDDI / PMIC / SECURITY / TCON` 을 그대로 사용한다.

## 워크플로우
1. **입력 선택** — 로컬 파일 열기 또는 D1 검색(`d1` provider) → 2개↑면 `FileOrderDialog`.
2. **저장명 제안** — 입력에서 출력 base 이름 채움.
3. **Start** — `df_honey_group` 재구성(첫 파일=기준 스키마, [06](06_analysis_engine.md)) → 설정 팝업.
4. **설정 팝업** — 출력 시트 선택(`summary/yield/cpk/fail_item/issue_table/distribution`;
   **`yield` 해제 시 `fail_item`/`issue_table` 비활성**), 항목 선택(제외/선택), FileName 변경,
   데이터 정리 모드(Bin1 Only / DUT 정리 — 입력 1개일 때만), Color, Server Auto Upload.
5. **분석 실행** — 결과를 입력폴더에 `<base>_report_YYMMDD_HHMM.xlsx` 로 저장. Auto Upload 면 곧장 업로드.
6. **서버 업로드** — 임의 xlsx 직접 또는 분석 후 → xlsx grid 전송([07](07_client_upload_chart.md)).
   web_report parquet 병행 업로드 경로도 있다(→[10](10_web_report_pipeline.md)).
7. **업데이트** — 기동 후 버전 확인([04](04_honey_update.md)).

## 주의
- **report generator 산출물은 .xlsx 1개** — 하나의 파일에서 모든 것을 관리하는 정책.
- **엔진 미설치 그레이스풀** — `import report_generator`(외부·무수정) 실패 시 분석 버튼만
  비활성, **로컬 xlsx 직접 업로드는 유지**. 분석/생성엔 pandas/numpy/xlwings+Excel 필요.
- 무거운 작업은 worker thread + poll 중 `processEvents()` 로 UI 갱신·진행바 drain.
- D1 검색은 외부 provider 가 매번 디스크 재스캔(rglob csv/xlsx). 외부 D1 프로젝트는 이
  패키지만 교체한다(무수정 원칙).
