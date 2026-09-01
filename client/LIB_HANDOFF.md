# Honey 클라이언트 빌드 lib 인수인계

> **이 문서의 목적**: Honey 클라이언트 배포본에 들어가는 **라이브러리 구성**이 바뀔 때마다
> 여기에 남긴다. 이 저장소 담당자와 외부 담당자(honey_parse / report_generator)가 각자
> 빌드 설정을 고치다 보니, 서로 무엇을 왜 바꿨는지 몰라 **되돌려 놓거나 중복으로 고치는
> 일**이 반복됐다. 이 파일이 그 단일 기록이다.
>
> **머지 충돌이 났을 때는 코드가 아니라 이 파일을 먼저 본다.** 아래 §4 변경 이력에서
> 양쪽이 각각 무엇을 왜 넣었는지 확인한 뒤, 두 의도를 모두 살리는 방향으로 합친다.
> 이력에 없는 변경은 "합쳐야 할 의도"가 아니라 **사고(잘못 되돌아온 것)** 로 간주한다.

---

## 1. 규칙 (양쪽 담당자 공통)

1. **lib 구성을 바꾸면 같은 커밋에서 이 파일 §4 에 한 줄 이상 남긴다.** 대상 파일은 §2 표에
   있는 4개다. 코드만 고치고 이 파일을 안 고치면 반쪽 변경이다.
2. **남길 내용은 "무엇을"이 아니라 "왜"다.** 무엇을 바꿨는지는 diff 가 이미 말해 준다.
   diff 가 말해 주지 않는 것 — 왜 넣었는지, 빼면 무엇이 깨지는지 — 을 적는다.
3. **충돌이 나면 한쪽을 고르지 말고 둘 다 살린다.** 대부분의 충돌은 서로 다른 것을 넣으려던
   것이지 같은 것을 두고 다툰 게 아니다. §4 에서 양쪽 의도를 읽고 합친 뒤, 합쳤다는 사실을
   §4 에 새 줄로 남긴다.
4. **§3 "빼면 안 되는 것" 목록은 지우지 않는다.** 전부 실제로 한 번 깨져서 등재된 항목이다.
   빼야 할 근거가 생기면 지우지 말고 §4 에 근거와 함께 기록하고 옮긴다.
5. 이 문서는 **클라이언트 빌드 lib 전용**이다. 서버 의존성은
   [server/README.md](../server/README.md) 소관이라 여기 섞지 않는다.

---

## 2. lib 이 정의되는 곳 — 이 4개 파일이 전부다

| 파일 | 무엇을 정하나 | 바꾸면 생기는 일 |
|---|---|---|
| [requirements.txt](requirements.txt) | 빌드 PC 에 설치되는 **패키지와 버전** | 여기 없는 패키지는 빌드 PC 에 없어 빌드가 실패하거나(가드가 있으면) 조용히 빠진 exe 가 나간다 |
| [build_honey.spec](build_honey.spec) | 배포본에 **실제로 들어가는 것** — hiddenimports / datas / excludes / collect_all | lib 구성의 정본. 아래 두 spec 은 전부 이 파일을 따라간다 |
| [build_honeyapp.spec](build_honeyapp.spec) | 위 spec 을 **텍스트로 읽어** 산출물 이름만 `HoneyApp` 으로 치환 (사본 아님) | 자체 lib 목록이 없다. `build_honey.spec` 을 고치면 자동 반영 |
| [build_launcher.spec](build_launcher.spec) | 런처(`Honey.exe`) — **표준 라이브러리 + tkinter 만** | 여기 excludes 가 뚫리면 런처가 수백 MB 가 되고 "런처는 거의 안 바뀐다"는 업데이트 구조의 전제가 무너진다 |

`launcher_version_up_release.bat` → `release/release_launcher.ps1` 은 위 spec 을 실행하는
**래퍼**다. lib 목록을 직접 갖고 있지 않으므로, lib 을 바꾸려고 이 두 파일을 고칠 일은 없다.

### 두 개의 exe 는 lib 이 정반대다

- **`HoneyApp.exe` (앱 본체)** — 무거워도 된다. PyQt6/pandas/pyarrow 전부 들어간다.
- **`Honey.exe` (런처)** — 가벼워야 한다. 무거운 패키지는 `build_launcher.spec` 의
  `excludes` 가 **의도적으로 전부 막고 있다**. 런처에 뭔가 필요해 보이면 넣기 전에
  "정말 런처가 써야 하나"부터 확인할 것.

---

## 3. 빼면 안 되는 것 (전부 실제로 깨졌던 항목)

lib 정리·다이어트를 할 때 후보로 올라오기 쉬운데 **빼면 안 되는** 것들이다.

| 대상 | 어디 | 빼면 생기는 일 |
|---|---|---|
| `PyQt6==6.11.0` / `PyQt6-WebEngine==6.11.0` 의 **`==` 버전 핀** | requirements.txt | `>=` 로 풀면 빌드 PC 의 pip 이 최신을 끌어와 **코드를 한 줄도 안 고친 배포에서 Chromium 이 통째로 바뀐다**. 특정 PC 에서만 나는 렌더링 이상(깜빡임·렌더러 크래시)을 배포와 분리해 진단할 수 없게 된다. 올릴 때는 의도적으로 올리고 문제 PC 에서 확인 |
| `excludes=['PyQt5']` | build_honey.spec | 빌드 PC 에 PyQt5 잔재가 있으면 `multiple Qt bindings` 로 빌드 실패. 앱은 PyQt6 전용이라 PyQt5 재도입 금지 |
| `honey_parse/mddi/datalog_parser/ui/optional_sheets_dialog.ui` | build_honey.spec `datas` | honey_parse 실물이 **런타임에 파일로 직접 읽는다**. 빠지면 해당 다이얼로그에서 에러 (실제 발생 → 커밋 6822bf8 에서 등재로 해결) |
| spec 상단의 `import requests_toolbelt` / `xlwings` / `pyarrow` / `PyQt6.QtWebEngineWidgets` 4줄 | build_honey.spec | **빌드 환경 가드**다. 미설치 시 `collect_*` 이 조용히 빈 리스트를 돌려줘 *런타임에 ModuleNotFoundError 로 죽는 깨진 exe* 가 그대로 배포된다. WebEngine import 는 PyQt6 ↔ WebEngine 런타임 버전 어긋남(DLL 로드 실패)까지 빌드 시점에 잡는다 |
| `collect_all('xlwings')` / `collect_all('pyarrow')` | build_honey.spec | xlwings 는 자체 `.xlam`·dll 을, pyarrow 는 바이너리를 동봉해야 동작한다. `collect_submodules` 로 바꾸면 데이터/바이너리가 빠진다 |
| `build_launcher.spec` 의 `excludes` 목록 | build_launcher.spec | 런처 비대화 방지선. §2 참조 |
| `datas=[('honey.ico', '.')]` | build_launcher.spec | exe 아이콘(`icon=`)과 **별개로 런타임에도** 필요하다 — 진행창/기동 대기창 타이틀바 아이콘으로 tkinter 가 파일을 직접 읽는다 |

### 개발 PC 와 빌드 PC 의 차이 (오진 방지)

이 저장소에는 `client/honey_parse/` 가 **더미 폴백**만 있고, 운영 릴리스는 빌드 PC 의
**honey_parse 실물**로 만든다. 그래서:

- 개발 PC 빌드는 `build_honeyapp.spec` 이 없는 datas 를 건너뛰며 경고를 크게 찍는다.
  이건 정상이고, `release_launcher.ps1` 이 그 경고를 감지해 운영 릴리스를 막는다.
- **이 저장소 코드만 grep 해서 "사용처 0건"이라고 판단하면 안 된다.** honey_parse 실물이
  import 하는 패키지는 여기서 보이지 않는다. 제거 후보가 생기면 반드시 외부 담당자에게
  확인한다 → [docs/22_build_diet_questions.md](../docs/22_build_diet_questions.md) 에
  질문 양식과 현재 진행 상태가 있다.

---

## 4. 변경 이력

새 항목은 **맨 위에** 추가한다. 형식은 아래를 따른다.

```
### YYYY-MM-DD — 한 줄 요약  (담당: 이 저장소 | 외부)
- 파일: 바꾼 파일
- 무엇: 추가/삭제/변경한 항목
- 왜: 이유 (빼면 무엇이 깨지는지)
- 확인: 어떻게 검증했는지
```

---

### 2026-08-28 — call_claude 최상위 패키지 수집 추가 (AI Comment 클라 대행)  (담당: 이 저장소)
- 파일: `build_honey.spec`
- 무엇: `hiddenimports` 에 `collect_submodules('call_claude')` 추가 (repo 최상위 신규
  패키지 — 표준 라이브러리만 사용, 서드파티 의존 0, requirements.txt 변경 없음).
- 왜: AI Comment [제안] 클라 대행(docs/23) — `transport/ai_suggest.py` 가 로컬 Claude
  CLI 를 subprocess 로 부르는 `call_claude` 를 **try/except 안에서** import 한다.
  pathex 에 repo 루트가 있어도 조건부 import 는 PyInstaller 정적 분석이 놓칠 수 있어
  명시 수집이 필요하다. 빠지면 배포본에서 AI Model=claude 업로드의 대행이 조용히
  안 돈다(에러 없이 폴백 문장 유지 — 발견이 늦는 유형).
- 확인: 표준 lib 만 쓰는 순수 파이썬 3파일이라 빌드 산출물 크기 영향 무시 가능.
  tests/test_call_claude.py + tests/test_ai_suggest.py self-run 통과.

### 2026-08-27 — 인수인계 문서 신설, 현재 구성을 기준선으로 고정  (담당: 이 저장소)
- 파일: `client/LIB_HANDOFF.md`(신규), `requirements.txt`·`build_honey.spec`·
  `build_launcher.spec`·`build_honeyapp.spec` 상단에 배너 주석 추가
- 무엇: lib 구성 변경 기록 규칙을 만들고, **이 시점의 구성을 기준선(baseline)으로 §5 에
  박제**했다. 빌드 파일 4개 상단에는 "lib 을 바꾸면 이 문서에 남길 것" 배너를 달았다.
- 왜: `launcher_version_up_release.bat` 계열을 외부 담당자가 고치면서 lib 정리가 한 차례
  들어왔는데, 무엇을 왜 바꿨는지 남은 기록이 없어 이 저장소 쪽 의도(§3 의 가드들)와
  충돌하는지 판단할 수 없었다. 앞으로는 충돌 시 이 문서를 기준으로 합친다.
- 확인: 문서 작업이라 빌드 산출물 변화 없음. 배너는 전부 주석이라 spec 실행에 영향 없음
  (`build_honeyapp.spec` 의 `name='Honey',` 2곳 카운트 규칙도 그대로).

### 2026-08-26 — 빌드 다이어트 검토, **외부 담당자 답변 대기 중 (미적용)**  (담당: 이 저장소)
- 파일: 없음 — **spec 미수정**
- 무엇: 배포본 6,090개 파일 중 약 4,000개(65%)가 이 저장소 코드 기준 사용처 0건으로
  분석됨 (botocore 데이터 JSON, PyQt6 qml 트리, pyarrow include/src/tests, ko/en 외
  Qt 번역, plotly+kaleido, cryptography).
- 왜: 압축 해제·델타 업데이트 속도는 용량이 아니라 **파일 개수**에 비례한다.
- 확인: **적용하지 않았다.** honey_parse 실물의 import 를 이 저장소에서 알 수 없어,
  외부 담당자에게 3가지(서드파티 import 목록 / QML 사용 여부 / 런타임에 파일로 읽는
  데이터)를 물어본 상태다. 질문 원문과 답변 후 진행 방식은
  [docs/22_build_diet_questions.md](../docs/22_build_diet_questions.md).
  → **이 항목이 열려 있는 동안 위 6개를 임의로 excludes 에 넣지 말 것.**

---

## 5. 기준선 — 2026-08-27 시점 구성

충돌·되돌림 사고가 났을 때 "원래 뭐였나"를 대조하는 스냅샷이다. 실제 정본은 항상 파일
자신이며, 여기와 어긋나면 **파일이 옳고 이 표가 낡은 것**이니 §4 에 이력이 있는지 먼저 본다.

### requirements.txt

| 패키지 | 핀 | 용도 |
|---|---|---|
| PyQt6 | `==6.11.0` | UI 전반 (§3 — 핀 유지) |
| PyQt6-WebEngine | `==6.11.0` | 내장 브라우저 Chromium — `embedded_browser.py`, Plotly 렌더 (§3 — 핀 유지) |
| requests | `>=2.28` | 서버 통신 |
| requests-toolbelt | `>=1.0` | 업로드 진행률(%) — `transport/uploader.py` |
| pywin32 | `>=306` (win32) | Excel COM 시트/이미지 추출 — `report_flow/upload_prepare.py` |
| pandas | `>=1.5` | 로컬 리포트 분석 엔진 — `report_generator` |
| pyarrow | `>=15.0` | web_report parquet bytes encoding |
| numpy | `>=1.23` | 분석 계산 (cpk/yield/distribution) |
| matplotlib | `>=3.5` | `map_report` — wafer bin map → PNG |
| xlwings | `>=0.30` (win32) | 로컬 xlsx 리포트 생성 (Excel COM) |
| XlsxWriter | `>=3.1` | Excel Download 기본 엔진 — `excel_download/_xlsx.py` (Excel 없이 xlsx 생성) |
| pystdf | (핀 없음) | honey_parse(외부 담당자 영역) STDF 파싱 의존성 |

### build_honey.spec — 앱 본체 (HoneyApp)

- **빌드 가드 import (4)**: `requests_toolbelt`, `xlwings`, `pyarrow`,
  `PyQt6.QtWebEngineWidgets` — §3 참조
- **collect_all (2)**: `xlwings`, `pyarrow` (datas + binaries + hidden)
- **hiddenimports 명시**: `PyQt6.sip`, `PyQt6.uic`, `win32com`, `win32com.client`,
  `pythoncom`, `pywintypes`, `pandas`, `numpy`
- **collect_submodules (11)**: `requests_toolbelt`, `report_generator`, `honey_parse`,
  `pystdf`, `transport`, `d1`, `honey_ui`, `report_flow`, `excel_edit`,
  `excel_download`, `xlsxwriter`
- **datas**: `honey_main.ui`, `upload_dialog.ui`, `d1/d1_browser.ui`, `file_order.ui`,
  `report_settings.ui`, `honey_parse/mddi/datalog_parser/ui/optional_sheets_dialog.ui`,
  `Honey_img.png`
- **excludes**: `PyQt5` 하나뿐
- 형태: onedir + windowed → `dist/Honey/`

### build_launcher.spec — 런처 (Honey.exe)

- **hiddenimports**: `tkinter`, `tkinter.ttk`, `transport.app_update`,
  `transport.config`, `client_identity`
- **datas**: `honey.ico` (§3 — 런타임에도 필요)
- **excludes (13)**: `PyQt6`, `PyQt5`, `pandas`, `numpy`, `requests`, `xlwings`,
  `pyarrow`, `PIL`, `botocore`, `boto3`, `matplotlib`, `plotly`, `win32com`
- 형태: onefile, `workpath`/`distpath` 를 앱 spec 과 분리 (둘 다 산출 이름이 `Honey`)

### build_honeyapp.spec

자체 lib 목록 없음. `build_honey.spec` 을 텍스트로 읽어 `name='Honey',`(정확히 2곳)를
`name='HoneyApp',` 으로 치환해 실행한다. **원본 spec 에서 이 문자열의 등장 횟수가 2가
아니게 되면 즉시 빌드가 실패**하도록 되어 있으니, `build_honey.spec` 의 `name=` 을
건드릴 때는 이쪽 치환 규칙도 함께 본다.
