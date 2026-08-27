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
| `UploadDialog` | 업로드 메타(ProductType/Product/LOT/Revision, password 선택) + 맨 위 `Save Name`(서버 file_name — 호출부가 defaults["file_name"] 로 메인창 Save Name/파일명을 미리 채우고 여기서 한 번 더 수정, 2026-08-26) |
| `SessionMetaDialog` | 업로드 **후** 세션 메타 수정 — `UploadDialog` 상속(Part ID 자동완성·Family 콤보·맨 위 이름 칸 그대로, 라벨만 `Session Name`), password 행 숨김, ProductType 은 세션 값 고정. 세션 페이지 ✏️ → `honey_main._handle_honey_action` → `on_session_meta_edit` → `PATCH .../meta` → [02](02_server_query_edit.md) |
| `D1BrowserDialog` | `d1_storage` 검색·다중선택 (외부 provider 결과) |
| `FileOrderDialog` | 입력 2개↑ 시 순서 확정(첫 파일=기준 스키마) |
| `SourceNameDialog` | **Web Report 생성 직전 공통 창** (Normal·Commonality·Temperature. Compare 는 `CompareArrangeDialog`, DUT 는 창 없음). 표 한 줄 = source 하나 — `입력 파일`(읽기 전용, 뒤에서 폴더 2개+파일명으로 축약, 툴팁에 전체 경로) / `Legend`(편집, 12자·Temperature 는 15자). `↑`/`↓`(Alt+↑/↓)·`↑↑`/`↓↓`(Alt+Home/End = 최상단/최하단)로 바꾼 순서가 곧 업로드 순서이고 **최상단 = limit 기준**이라 1행을 초록으로 강조한다. **Ctrl/Shift 다중 선택 이동**(2026-08-10) — 선택 블록이 경계에 닿아도 블록 안에서 뒤섞이지 않는다(`_shift` 의 blocked). **표의 위→아래 = web_report 표시 순서**이고 Temperature 는 그룹 순서·그룹 안 member 순서까지 표 순서로 나간다(그래서 그룹 번호도 표 순서로 다시 매긴다 — `_renumber_groups`). 삭제 없음. 21행까지 스크롤 없이 보이고 그 이상은 세로 스크롤바. **Temperature 는 열 3개(`Group`·`Role`·`색`)와 Limit 파일 영역이 더 생긴다** — 구 `TemperatureGroupDialog`(드래그앤드랍 배치 창)를 흡수한 것으로, 열은 `setColumnHidden` 이 아니라 **columnCount 자체가 2 또는 5**고 Limit 영역은 컨테이너째 숨겨 레이아웃이 높이를 회수한다. `색` 칸 더블클릭 = 이 리포트에만 적용되는 Distribution 색(옵션 F10 팔레트를 기본값으로 읽고 **창 값이 우선**, `chart_colors.json` 은 안 건드린다). 자동 그룹 배치는 `temperature_pairing.suggest_groups(_by_role)` — **짝 키(입력 파일 base 이름) 우선 매칭**(2026-08-24, `pair_keys` 인자·`honey_main._temp_pair_keys`)이라 dedupe(_2)로 legend 가 갈려도 같은 웨이퍼끼리 묶인다. Temperature 는 입력 파일 열이 22자 더 넓다(창 가로 +15%, 2026-08-24). **그룹 소속 변경은 `Group` 칸 드롭다운**(2026-08-24 — 다른 그룹을 고르거나 그 그룹 이름을 적으면 그 행이 이동. 행 순서 ↑/↓는 소속을 바꾸지 않는다). **그룹 이름 타이핑 = 같은 그룹 전원 일괄 개명**(필수 기능 — 유지 규칙과 GC 함정은 아래 [주의](#주의)). OK 시 Role 짝 검증 — 같은 그룹에 같은 Role 2개면 차단, 그룹별 구성이 다르면 확인 질문 → [10](10_web_report_pipeline.md). 결과 → `honey_main._apply_source_arrangement` 가 `rename_sources(names)` 먼저 → 순서는 **이름이 아니라 `order_index`** 로 잇는다(dedupe 규칙 차이로 `mass_data_map` 키와 어긋나는 것을 막는다). 표에 뜨는 Legend **기본값**은 product_type 별 파일명 규칙([client/honey_ui/source_naming.py](../client/honey_ui/source_naming.py) `_SOURCE_NAME_RULES`)이 만든다 → [10](10_web_report_pipeline.md) |
| `CompareArrangeDialog` | **Compare 모드 전용** — source 를 Before/After 두 리스트로 배치(`>>` `>` `<` `<<`)하고 `↑`/`↓` 로 그룹 안 순서를 정한다. 항목 더블클릭 = Legend 이름 변경(중복은 `_2`,`_3`) — Compare 모드에선 이 창이 `SourceNameDialog` 를 **대신한다**. 순서가 의미를 가지므로(After 최상단 = limit 기준 + goodlog 대표) `RawdataHubDialog` 의 Item Select 와 달리 **이동 후 재정렬하지 않는다**. Confirm 시 양쪽 최소 1개 검증. 결과 → `rename_sources(names)`(원본 순서) + `options.compare` + 업로드 순서(After→Before) → [10](10_web_report_pipeline.md)·[11](11_web_report_tabs.md).<br>**상단 라디오 2종**(2026-08-27, 창 폭 1400→1820): `Normal Compare`(위 설명 그대로) / `Para Conversion` — Before/After 라벨이 `Single Mass Data`/`Para Mass Data` 로 바뀌고 **각 칸 정확히 1개**만 허용한다. Confirm 하면 `honey_main._prepare_para_conversion` 이 Para 파일을 `web_report.honeyform.split_honeyform_df_by_dut` 로 DUT 별 분할해 **`Single` + `DUT<라벨>` N개 source** 로 업로드한다(DUT 종류 ≤1 이면 안내 후 중단). Para 는 최종 source 가 이 창의 항목과 1:1 이 아니라 **이름 변경·색 지정을 쓰지 않는다**(DUT 모드와 같은 이유 — 색 버튼 비활성). `options.compare.para=True` 로 서버가 Para 세션임을 안다 |
| `RawdataHubDialog` | 열린 세션의 Rawdata 진입 허브 — **좌측 기능 버튼 + 우측 활성 패널**(`QStackedWidget`, 2026-07-28 개편. 종전 세로 grid 1장은 Item Select 2-리스트가 창을 다 먹었다). 페이지: `현재 상태`(적용 중인 전처리 목록 + 선택/전체 해제) / **`Options`**(조건을 짤 필요 없는 한 줄 옵션 — `Bin1 only` 체크박스. 내부적으로는 조건 규칙을 만들어 [현재 상태] 에 그대로 나타나고, 거기서 해제하면 체크도 함께 풀린다 — `_sync_options`) / `Item Select`(2-리스트 + 검색 — 검색은 **숨기기만**, 목록에서 빼면 저장 대상이 달라진다) / `Outlier 제거` / **`Yield 계산`**(소스별 수율 **분모** — 자동/Gross die/Test die 콤보 + 그 자리에서 다시 계산되는 수율. `GET .../web_report/yield_basis` 로 받은 pass/tested/gross 로 **왕복 없이** 계산하고, 저장은 같은 요청의 `yield_basis={"mode","sources"}` 필드 → [11](11_web_report_tabs.md)) / **`신규 Item(수식) 추가`**(주황, 2026-08-24 — Options 바로 밑. 아래 별도 행) / `Rawdata 원본 수정`(주황, Excel — **열 source 체크리스트** + `ACTION_EXCEL`, 선택은 `hub.excel_indices`. 진입 전 "셀 패치 N건이 해제된다" 확인) + 하단 저장·닫기. 창 크기는 **1040×600**(2026-08-24 — 신규 Item 페이지가 수식 칸·자동완성·통계 7열 표를 한 화면에 편다). **서버 조회 3건은 창을 띄운 뒤 `_HubLoadWorker`(QThread)** 에서 — 생성자 동기 호출은 큰 세션에서 창이 뜨기 전 UI 를 멈춘다. Excel 을 뺀 나머지는 원본을 고치지 않는 **전처리**(서버 `.../web_report/preprocess`)라 전 탭이 그 기준으로 재계산되고 언제든 되돌릴 수 있다. *2026-07-28 임시 비활성(사용자 요청)*: `빠른 수정` 페이지·`Spec Out 빈값` 은 화면에서만 뺐다(코드 유지, 등록 3줄 복구로 되살아남) |
| `RawdataHubDialog` → **신규 Item(수식) 추가** 페이지 | 수식으로 파생 측정 item 을 만들어 **원본 parquet 에 컬럼을 추가**한다(2026-08-24). 다른 페이지가 전부 되돌릴 수 있는 전처리인 것과 정반대라 하단 공용 [저장] 을 쓰지 않고 자체 주황 버튼을 두며, **미리보기를 통과해야만** 그 버튼이 열린다(수식을 고치면 다시 잠긴다). 메타 7칸(ITEMNAME/TSEQ/TNO/STEP/UNIT/HILIM/LOLIM) 기본값은 **탭을 처음 열 때 받은 rawdata** 에서 채운다(`_NewItemLoadWorker` → `item_add.default_meta` — TSEQ/TNO 는 전 source 최대 +1, STEP 은 마지막 항목 승계, 숫자가 아니면 빈칸으로 두고 직접 입력받는다). 서버 `raw_data/columns` 에 필드를 추가하지 않는 이유는 그게 `web_report/tabs/` 라 perf_guard S01 이 `REPORT_SCHEMA_VERSION` bump 를 요구하기 때문이다(= 전 세션 콜드 폭풍). 미리보기·적용도 각각 QThread(`_NewItemPreviewWorker` / honey_main 의 `AddItemWorker`). 확정은 `ACTION_ADD_ITEM` + `hub.add_item_spec` 으로 honey_main 에 넘겨 Excel 왕복과 **같은 워커 필드**를 쓴다(중복 실행·이탈 취소 가드 재사용). → [11](11_web_report_tabs.md) |
| `FormulaEditor` | 신규 Item 페이지의 수식 입력 위젯(`honey_ui/formula_editor.py`). **읽기 전용 칩 스트립(읽은 결과) + 자유 타이핑 `QPlainTextEdit` + `@` 후보 목록** — 버튼이 없다(2026-08-25 개편, 종전 연산자·함수 버튼 22개 제거). 사용자는 수식을 줄글로 치고 서버와 같은 순수 모듈 `web_report.formula.lex` 가 그것을 토큰으로 해독한다. **항목은 `@` 로만 넣는다** — `@` 를 치면 후보가 뜨고 고르면 `@"VDD_A"` 인용 표기가 자동 삽입된다(이름 안의 `"` 는 `""` 로 escape). 인용을 쓰는 이유는 item 이름에 공백·괄호·연산자·따옴표가 전부 합법이라 글자만 보고 `VDD-VSS + 1` 을 가릴 수 없기 때문이다 — 명시적 구분자가 그 모호성과 `SUM(...)`=함수 / `@"SUM"`=항목 충돌을 함께 없앤다. 함수명·항목 조회는 **대소문자 무관**이되 토큰에는 목록의 **원본 이름**이 들어간다(소문자화 금지). **정본은 텍스트**이고 파란 기울임(항목)·빨간 물결 밑줄(오류)은 `QSyntaxHighlighter` 가 렉싱 결과를 보고 얹는 표시일 뿐이라 undo 스택을 오염시키지 않고 복사·붙여넣기에도 살아남는다. 항목 이름의 글자를 지우면 목록에 없는 이름이 되어 그 자리에 밑줄 + `'…' 라는 항목이 없습니다 (혹시 …?)`. 오류 안내는 2단 — 실시간 상태줄·밑줄 + 확정 시 `error_html()` 경고창(수식 원문에서 문제 구간을 빨갛게 칠한다. 캐럿 `^^^` 는 한글·전각 폭 때문에 어긋나므로 쓰지 않는다). **줄바꿈은 어떤 경로로도 들어가지 않는다**(Enter=후보 확정 전용, 붙여넣기 개행→공백) — 문자 오프셋 계산이 블록 1개 전제다. **한글 IME 조합 중에는 키를 가로채지 않는다**(`_ImeTextEdit._composing` — 없으면 조합 확정과 동시에 엉뚱한 항목이 들어간다). ⚠ `textChanged` **재진입 가드 필수** — `rehighlight()` 가 문자 서식을 고치면 문서가 다시 `textChanged` 를 쏴 무한 재귀(스택 넘침)가 된다. 칩 색은 항목=파란 기울임·함수=보라·비교=청록(웹 `.gc-tok-*` 와 같은 팔레트). 회귀 고정 `tests/test_new_item_dialog.py` |
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

## 불변 규칙 (클라 전역 — 3개월 이상 안정)

### UA 토큰 — `HoneyUser/<계정> HoneyVer/<버전>`
서버가 **신원과 클라 버전을 아는 유일한 통로**다([02](02_server_query_edit.md) 인증 ·
`report_client_version` 대장). 계약:
- 계정은 `urllib.parse.quote(user, safe="")` 로 **퍼센트 인코딩**한다(서버 파싱 정규식이
  `HoneyUser/(\S+)` 라 공백이 들어가면 토큰이 잘린다).
- 형식이 **8곳에 복제**돼 있다(공용 함수가 없는 것이 현 상태다):
  `transport/version_check.py`(버전 대장의 유일한 입력 — 앱 시작 시 1회) ·
  `transport/uploader.py` · `transport/error_report.py` · `excel_download/_fetch.py` ·
  `excel_edit/excel_session.py` · `honey_ui/rawdata_hub_dialog.py` ·
  `honey_ui/rawdata_quick_dialog.py` · `embedded_browser.py`(Chromium 기본 UA 에 **1회만**
  append — `if "HoneyUser/" in ua: return`). 형식을 바꾸려면 8곳을 함께 고쳐야 한다.
- **예외 1곳**: `transport/app_update.py`(런처)는 requests 를 못 써서 `python-urllib` +
  HoneyVer 없음. 런처는 신원 통계 대상이 아니라 문제되지 않는다.
- **계정 수집에 실패하면 토큰 없이 진행한다** — 업로드를 절대 깨뜨리지 않는다(서버는
  `ip:<addr>` 로 집계). 신원은 편의 기능이지 인증이 아니다.

### 스레드·종료
- **워커 스레드는 UI 객체에 직접 접근하지 않는다** — 보고는 `queue.Queue` 로만
  (`q_prep`, `_dist_stage_q`, `download_events`). 표준 패턴은
  `ElapsedProgress`(+`mirror`) + `ThreadPoolExecutor(max_workers=1)`(FIFO = 제출순=실행순) +
  `wait_for_future(fut, progress, poll_cb=, cancelled=)`
  ([honey_ui/progress.py](../client/honey_ui/progress.py)). **새로 추가하는 무거운 작업은
  이 패턴을 따른다** — 7초 이상 UI 를 막으면 사용자는 죽은 줄 안다.
- 취소는 `OperationCancelled` 예외로 올라오며 **`except Exception` 보다 먼저** 잡아야 한다
  (안 그러면 사용자 취소가 오류 팝업이 된다).
- **종료는 `os._exit()` 로 강제한다**(`_final_exit`). `QApplication` 이 `QWebEngineView`
  보다 먼저 파괴되면 access violation 이 난다 — 정상 종료 경로로 되돌리지 말 것.

### Excel COM
- COM 은 **워커 스레드에서만** 다루고 그 스레드에서 `CoInitialize` 한다
  (`honey_main._init_com_for_worker` / `_co_uninitialize`).
- COM 을 쓰는 곳: `report_flow/`(DRM/NASCA 해제 — Excel 설치 PC 필수) · `excel_edit/`
  (xlwings 가시 창) · `excel_download/`(차트 PNG·기입) · `map_report/`(xlsx 부착 2함수).
- **서버에는 COM 도 openpyxl 도 없다**(CLAUDE.md §5 규칙 1) — Excel 이 필요한 일은 전부
  클라 몫이다. 반대로 `excel_download` 는 XlsxWriter 기본 엔진이라 Excel 없이도 생성된다.
- PyInstaller + `ProcessPoolExecutor`(차트 렌더) 조합 때문에
  `if __name__ == "__main__": multiprocessing.freeze_support()` 가 필수다.

### 빌드·패키징
- **PyQt6 전용, PyQt5 재도입 금지** — `build_honey.spec` 의 `excludes=['PyQt5']` 는 영구
  유지한다(잔재가 있으면 "multiple Qt bindings" 로 빌드가 깨진다). enum 은 PyQt6 스코프드
  표기(`Qt.Orientation.Vertical` 등).
- `requirements.txt` 의 `PyQt6==6.11.0` / `PyQt6-WebEngine==6.11.0` 은 **`==` 핀 고정**이다 —
  범위로 두면 Chromium 이 배포마다 바뀌어 렌더링 이상을 배포와 분리할 수 없다.
- 런처(`build_launcher.spec`)는 **Qt·pandas·numpy·requests 를 전부 배제**한다(그래서 진행창이
  tkinter 다). 런처에 무거운 의존을 넣지 말 것.
- **`.bat` 은 순수 ASCII + CRLF**(cmd.exe 가 LF·한글에서 조용히 창을 닫는다), **`.ps1` 은
  UTF-8 BOM**(한글 허용). 업데이트 배치 파일 자체는 mbcs 로 쓴다.

## 주의
- 🔒 **Temperature 배치 창 `Group` 칸 — "이름 한 번 = 그 그룹 전원 개명" 은 필수 기능이다**
  (2026-08-25 회귀 수정). `SourceNameDialog._on_group_text` → `_sync_group_name(gid)` 가
  그 그룹 **모든 행**의 legend 앞부분을 그 이름으로 바꾸고 역할 접미사(`_RT`/`_CT`/`_HT`)만
  남긴다. 그룹이 7개면 source 가 21개라, 이게 없으면 사용자가 legend 를 21번 손으로
  고쳐야 한다 — Group 칸 UI 를 어떻게 바꾸든 **이 동작은 유지**한다.
  세 가지 입력이 한 칸에서 갈린다: 새 이름 = 그 그룹 일괄 개명 / 다른 그룹의 이름 =
  그 그룹으로 이동(▼ 드롭다운 선택과 같은 뜻) / 미지정 행의 새 이름 = 새 그룹 생성.
  - ⚠️ **함정 — 이 기능은 예외 없이 조용히 죽는다**: 입력칸은 편집 가능 `QComboBox` 의
    `combo.lineEdit()` 이고, 이는 **C++ 이 소유한 객체의 임시 파이썬 래퍼**다. 거기 건
    `editingFinished` 연결(람다)은 그 래퍼가 GC 되는 순간 함께 사라진다 —
    `receivers()` 는 2 그대로라 **예외도 로그도 남지 않고**, 사용자에게는 "이름을 적어도
    아무 일이 안 일어난다" 로만 보인다. 같은 창의 ▼ 드롭다운·Role 콤보는 sender 가
    파이썬이 만든 위젯이라 멀쩡해서 원인을 더 가린다(2026-08-24 실제 회귀).
    → 창이 래퍼를 붙들어야 한다: `_render` 가 `self._group_edits` 를 비우고
    `_group_edit` 이 매 행 `edit` 를 거기 담는다. **그 append 를 지우지 말 것.**
  - 회귀 고정: [tests/test_source_group_rename.py](../tests/test_source_group_rename.py)
    (`python tests/test_source_group_rename.py` — PyQt6 offscreen, **gc.collect() 를 강제로
    돌린 뒤** 확인한다. 실기에서는 GC 시점이 무작위라 그게 없으면 통과해 버린다).
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
