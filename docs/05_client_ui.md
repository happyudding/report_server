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
| `SessionMetaDialog` | 업로드 **후** 세션 메타 수정 — `UploadDialog` 상속(Part ID 자동완성·Family 콤보 그대로) + 맨 위 `Session Name`(서버 file_name) 칸, password 행 숨김, ProductType 은 세션 값 고정. 세션 페이지 ✏️ → `honey_main._handle_honey_action` → `on_session_meta_edit` → `PATCH .../meta` → [02](02_server_query_edit.md) |
| `D1BrowserDialog` | `d1_storage` 검색·다중선택 (외부 provider 결과) |
| `FileOrderDialog` | 입력 2개↑ 시 순서 확정(첫 파일=기준 스키마) |
| `CompareArrangeDialog` | **Compare 모드 전용** — source 를 Before/After 두 리스트로 배치(`>>` `>` `<` `<<`)하고 `↑`/`↓` 로 그룹 안 순서를 정한다. 항목 더블클릭 = Legend 이름 변경(중복은 `_2`,`_3`) — Compare 모드에선 이 창이 기존 "SourceName 변경" 창을 **대신한다**. 순서가 의미를 가지므로(After 최상단 = limit 기준 + goodlog 대표) `RawdataHubDialog` 의 Item Select 와 달리 **이동 후 재정렬하지 않는다**. Confirm 시 양쪽 최소 1개 검증. 결과 → `rename_sources(names)`(원본 순서) + `options.compare` + 업로드 순서(After→Before) → [10](10_web_report_pipeline.md)·[11](11_web_report_tabs.md) |
| `RawdataHubDialog` | 열린 세션의 Rawdata 진입 허브 (세로 grid 1장) — `Item Select`+Item List / `Outlier 제거`+stdev 입력 / `Rawdata 원본 수정`(주황, Excel) + 저장·닫기. 앞의 둘은 원본을 고치지 않는 **조회 필터**(서버 `.../web_report/preprocess`)라 전 탭이 그 기준으로 재계산된다 → [11](11_web_report_tabs.md). 하단 체크박스 **"Yield 계산 기준 - Test data 개수"** 는 수율 **분모**를 고른다(해제=기본: 제품 기준정보 Gross Die, 체크: rawdata 개수) — 같은 저장 요청의 `yield_basis` 필드로 세션 DB 에 남는다 |
| `ChangeReviewDialog` | Excel 왕복 반영 전 변경 확인. 탭 2개 — **개요**(구조 변경·자동 교정·경고·시트 삭제) / **셀 변경**(`TableListView` 표, 열 = source·SHOT·DUT·X·Y·BIN·항목·이전·이후). 셀 변경 0건이면 표 탭을 만들지 않는다. 하단 [반영] [취소] [전문 저장…](CSV=표 / txt=요약 평문), 기본 버튼은 **취소** |
| `TableListDialog` / `TableListView` | 긴 목록 공용 표 — 검색(디바운스 250ms)·정렬(숫자는 숫자로)·Ctrl+C TSV 복사·CSV 저장(UTF-8 BOM). 5만 행에서도 생성/검색/정렬 각 0.2초 미만(정렬·검색을 파이썬 리스트 위에서 처리). 쓰는 곳: `ChangeReviewDialog` 셀 변경, `honey_main._warn_duplicate_items`(항목명 중복 자동 개명 내역) |

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
