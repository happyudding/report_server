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
| `SourceNameDialog` | **Web Report 생성 직전 공통 창** (Normal·Commonality·Temperature. Compare 는 `CompareArrangeDialog`, DUT 는 창 없음). 표 한 줄 = source 하나 — `입력 파일`(읽기 전용, 뒤에서 폴더 2개+파일명으로 축약, 툴팁에 전체 경로) / `Legend`(편집, 12자·Temperature 는 15자). `↑`/`↓`(Alt+↑/↓)·`↑↑`/`↓↓`(Alt+Home/End = 최상단/최하단)로 바꾼 순서가 곧 업로드 순서이고 **최상단 = limit 기준**이라 1행을 초록으로 강조한다. **Ctrl/Shift 다중 선택 이동**(2026-08-10) — 선택 블록이 경계에 닿아도 블록 안에서 뒤섞이지 않는다(`_shift` 의 blocked). **표의 위→아래 = web_report 표시 순서**이고 Temperature 는 그룹 순서·그룹 안 member 순서까지 표 순서로 나간다(그래서 그룹 번호도 표 순서로 다시 매긴다 — `_renumber_groups`). 삭제 없음. 21행까지 스크롤 없이 보이고 그 이상은 세로 스크롤바. **Temperature 는 열 3개(`Group`·`Role`·`색`)와 Limit 파일 영역이 더 생긴다** — 구 `TemperatureGroupDialog`(드래그앤드랍 배치 창)를 흡수한 것으로, 열은 `setColumnHidden` 이 아니라 **columnCount 자체가 2 또는 5**고 Limit 영역은 컨테이너째 숨겨 레이아웃이 높이를 회수한다. `색` 칸 더블클릭 = 이 리포트에만 적용되는 Distribution 색(옵션 F10 팔레트를 기본값으로 읽고 **창 값이 우선**, `chart_colors.json` 은 안 건드린다). 자동 그룹 배치는 `temperature_pairing.suggest_groups(_by_role)` — **짝 키(입력 파일 base 이름) 우선 매칭**(2026-08-24, `pair_keys` 인자·`honey_main._temp_pair_keys`)이라 dedupe(_2)로 legend 가 갈려도 같은 웨이퍼끼리 묶인다. Temperature 는 입력 파일 열이 22자 더 넓다(창 가로 +15%, 2026-08-24). **그룹 소속 변경은 `Group` 칸 드롭다운**(2026-08-24 — 다른 그룹을 고르거나 그 그룹 이름을 적으면 그 행이 이동. 행 순서 ↑/↓는 소속을 바꾸지 않는다). 그룹 이름 타이핑 = 같은 그룹 전원 일괄 개명(종전 유지). OK 시 Role 짝 검증 — 같은 그룹에 같은 Role 2개면 차단, 그룹별 구성이 다르면 확인 질문 → [10](10_web_report_pipeline.md). 결과 → `honey_main._apply_source_arrangement` 가 `rename_sources(names)` 먼저 → 순서는 **이름이 아니라 `order_index`** 로 잇는다(dedupe 규칙 차이로 `mass_data_map` 키와 어긋나는 것을 막는다). 표에 뜨는 Legend **기본값**은 product_type 별 파일명 규칙([client/honey_ui/source_naming.py](../client/honey_ui/source_naming.py) `_SOURCE_NAME_RULES`)이 만든다 → [10](10_web_report_pipeline.md) |
| `CompareArrangeDialog` | **Compare 모드 전용** — source 를 Before/After 두 리스트로 배치(`>>` `>` `<` `<<`)하고 `↑`/`↓` 로 그룹 안 순서를 정한다. 항목 더블클릭 = Legend 이름 변경(중복은 `_2`,`_3`) — Compare 모드에선 이 창이 `SourceNameDialog` 를 **대신한다**. 순서가 의미를 가지므로(After 최상단 = limit 기준 + goodlog 대표) `RawdataHubDialog` 의 Item Select 와 달리 **이동 후 재정렬하지 않는다**. Confirm 시 양쪽 최소 1개 검증. 결과 → `rename_sources(names)`(원본 순서) + `options.compare` + 업로드 순서(After→Before) → [10](10_web_report_pipeline.md)·[11](11_web_report_tabs.md) |
| `RawdataHubDialog` | 열린 세션의 Rawdata 진입 허브 — **좌측 기능 버튼 + 우측 활성 패널**(`QStackedWidget`, 2026-07-28 개편. 종전 세로 grid 1장은 Item Select 2-리스트가 창을 다 먹었다). 페이지: `현재 상태`(적용 중인 전처리 목록 + 선택/전체 해제) / **`Options`**(조건을 짤 필요 없는 한 줄 옵션 — `Bin1 only` 체크박스. 내부적으로는 조건 규칙을 만들어 [현재 상태] 에 그대로 나타나고, 거기서 해제하면 체크도 함께 풀린다 — `_sync_options`) / `Item Select`(2-리스트 + 검색 — 검색은 **숨기기만**, 목록에서 빼면 저장 대상이 달라진다) / `Outlier 제거` / **`Yield 계산`**(소스별 수율 **분모** — 자동/Gross die/Test die 콤보 + 그 자리에서 다시 계산되는 수율. `GET .../web_report/yield_basis` 로 받은 pass/tested/gross 로 **왕복 없이** 계산하고, 저장은 같은 요청의 `yield_basis={"mode","sources"}` 필드 → [11](11_web_report_tabs.md)) / `Rawdata 원본 수정`(주황, Excel — **열 source 체크리스트** + `ACTION_EXCEL`, 선택은 `hub.excel_indices`. 진입 전 "셀 패치 N건이 해제된다" 확인) + 하단 저장·닫기. **서버 조회 3건은 창을 띄운 뒤 `_HubLoadWorker`(QThread)** 에서 — 생성자 동기 호출은 큰 세션에서 창이 뜨기 전 UI 를 멈춘다. Excel 을 뺀 나머지는 원본을 고치지 않는 **전처리**(서버 `.../web_report/preprocess`)라 전 탭이 그 기준으로 재계산되고 언제든 되돌릴 수 있다. *2026-07-28 임시 비활성(사용자 요청)*: `빠른 수정` 페이지·`Spec Out 빈값` 은 화면에서만 뺐다(코드 유지, 등록 3줄 복구로 되살아남) |
| `RawdataQuickDialog` | **빠른 수정** (2026-07-28) — Excel 없이 표·조건으로 고친다. ① source 체크 선택(체크한 것만 디코드) → ② 필터 조회(Source/SERIAL/DUT/SHOT/BIN/TNO/X/Y + 항목 값 조건·Spec Out) → 셀 직접 수정 / 선택 영역 값 지정·빈값·오프셋·배율 / 클립보드 TSV 붙여넣기 / 찾아 바꾸기 / 조건 일괄 규칙(**대상 건수 확인 후** 추가) → ③ 수율·worst CPK 미리보기 → ④ 저장(`edits`/`rules` 로 전처리 spec 에). rawdata 는 Excel 왕복과 **같은 zip·같은 ETag 캐시**(`excel_session.fetch_rawdata_tables`)로 받고 원본을 바꾸지 않아 2회차부터 서버는 304 만 응답한다. 값 검증·조건 판정·미리보기 통계는 서버와 같은 모듈(`rawvalues`/`preprocess`/`tabs.cpk`). 다운로드·디코드와 미리보기 계산은 QThread. **레이아웃**: `②조회 조건`·`③수정`·`적용 대기`·`미리보기` 는 접이식 구역(`_Section` — 접으면 제목 옆에 요약만 남는다)이고, 우측 항목 패널은 최소 340px·항목 목록이 세로 공간을 차지한다. 창 전체에 11px 스타일시트를 깔아 글자 크기를 한 단계 낮춘다 |
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
- **Web Report 진행 취소** — 버튼 클릭 시 패널 진행바 옆 [취소] 노출
  (`honey_main._begin_op_cancel`). 파싱·인코딩·분포·분석 대기는
  `wait_for_future(cancelled=)` 가 `OperationCancelled` 로 중단(스레드는 못 죽이므로
  결과 폐기 — 읽기 전용이라 무해). **업로드 시작부터는 취소 불가**(비멱등 — 끊어도
  서버가 계속 처리해 세션이 생길 수 있다) — 버튼을 내려서 알린다.
- D1 검색은 외부 provider 가 매번 디스크 재스캔(rglob csv/xlsx). 외부 D1 프로젝트는 이
  패키지만 교체한다(무수정 원칙).
