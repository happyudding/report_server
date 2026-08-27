# 22. HoneyApp 빌드 다이어트 — 외부 담당자(honey_parse) 확인 질문

> 상태: **질문 대기** (2026-08-26 작성). 답변을 받기 전까지 빌드 spec 은 수정하지 않는다.
> 답변이 오면 이 문서 하단 "답변 후 진행 방식"대로 [client/build_honey.spec](../client/build_honey.spec) 을 수정한다.
>
> ⚠️ **수정하게 되면 [client/LIB_HANDOFF.md](../client/LIB_HANDOFF.md) 변경 이력에 같은
> 커밋으로 남긴다** — 외부 담당자와 공유하는 lib 변경 단일 기록이고, 빌드 파일 머지
> 충돌이 났을 때 양쪽 의도를 합치는 기준이 그 문서다. 이 문서는 *왜 묻는가*(분석·질문
> 원문)를 담고, LIB_HANDOFF 는 *무엇이 실제로 적용됐나*를 담는다.

## 1. 배경 — 왜 묻는가

런처 레이아웃 배포본(HoneyApp)의 zip 압축 해제와 델타 업데이트가 느린 주원인은 용량이
아니라 **파일 개수(6,090개 / 746MB)** 다. 2026-08-26 개발 PC 테스트 빌드
(`build_test_release.ps1`, honey_parse 더미 포함)를 실측 분석한 결과, 약 4,000개(65%)가
**이 저장소 코드 기준으로는 런타임 사용처가 0건**이었다:

| 제거 후보 | 파일 수 | 용량 | 유입 경로 | 이 저장소 기준 사용처 |
|---|---|---|---|---|
| `botocore/` (데이터 JSON) | 1,901 | 17.4MB | `collect_all('pyarrow')` → `pyarrow/tests/parquet/conftest.py` 가 참조 | 0건 (boto3 본체는 아예 없음) |
| `PyQt6/Qt6/qml/` 플러그인 트리 | 1,341 | 8.2MB | WebEngine 훅 부산물 | 0건 (앱은 QtWidgets + .ui/uic 전용, QML 미사용) |
| `pyarrow/` include·src·tests (C 헤더·테스트) | ~609 | 7.7MB | `collect_all('pyarrow')` 의 datas | 0건 (pyarrow 는 parquet R/W 만 사용) |
| Qt 번역 ko/en 외 (`translations/`, qtwebengine_locales 포함) | ~190 | ~50MB | Qt 훅 기본 수집 | 언어 리소스 — ko/en 만 필요 |
| `plotly/` + kaleido | 24 | 13.2MB | `collect_all('xlwings')` — xlwings/utils.py 의 optional import (`plotly_go=None` 폴백) | 0건 (클라는 matplotlib Figure 만 xlwings 에 전달, Plotly 렌더는 내장 브라우저=서버 HTML) |
| cryptography (.pyd 1개) | 1 | 9.4MB | 간접 부산물 | 0건 (해시는 전부 hashlib, 서버 통신은 http) |

단, **운영 릴리스는 빌드 PC 의 honey_parse 실물(외부 담당자 최종본)이 포함**되며 이
저장소에는 더미만 있어 최종본의 import 를 알 수 없다. 과거에도 honey_parse 실물이
런타임에 읽는 `.ui` 파일이 빌드에서 빠져 에러가 났고 spec datas 에 등재해 해결한 선례가
있다(커밋 6822bf8, `honey_parse/mddi/datalog_parser/ui/optional_sheets_dialog.ui`).
그래서 정리 실행 전에 아래 3가지만 확인이 필요하다.

## 2. 외부 담당자에게 보낼 질문 (그대로 복사해 전달)

---

안녕하세요. Honey 클라이언트 배포본 용량/파일 수를 줄이는 작업 전에, honey_parse
최종본 기준으로 3가지만 확인 부탁드립니다.

**① honey_parse 가 import 하는 외부(서드파티) 패키지 전체 목록을 뽑아주실 수 있나요?**
honey_parse 최종본이 있는 폴더의 상위에서 아래 한 줄을 실행한 결과를 그대로 보내주시면
됩니다 (cmd 창):

```
findstr /s /r /c:"^import " /c:"^from " honey_parse\*.py
```

저희가 파악하고 있는 honey_parse 의존성은 **pystdf** 하나입니다. 그 외에 특히
**openpyxl / plotly / boto3·botocore / cryptography** 를 쓰는 파일이 있으면 알려주세요
(이 4개가 이번 정리 후보에 들어 있습니다).

**② honey_parse 의 UI(다이얼로그)는 전부 .ui 파일(uic / QtWidgets) 방식인가요?**
QML(QtQuick — `.qml` 파일이나 `PyQt6.QtQuick`/`QtQml` import)을 쓰는 곳이 있는지만
확인 부탁드립니다. 없으면 배포본에서 Qt 의 qml 폴더(파일 1,341개)를 제외할 예정입니다.

**③ honey_parse 가 런타임에 파일로 직접 읽는 데이터가 `optional_sheets_dialog.ui` 외에 더 있나요?**
(예: 다른 .ui, 사전/테이블/설정 파일 등 — 코드 옆에 두고 open() 으로 읽는 모든 것)
있으면 파일 경로를 알려주세요. 빌드에 동봉 목록으로 등재해야 배포본에서 누락되지
않습니다 (예전 optional_sheets_dialog.ui 누락 건과 같은 문제 예방입니다).

---

## 3. 답변 후 진행 방식 (참고 — 외부 담당자 코드는 건드리지 않음)

정리는 **"강제 차단(excludes)" 최소화, "유입 근원 자르기" 위주**로 한다. 즉
`pyarrow.tests` 승격과 qml/번역 수집만 끊으면, honey_parse 최종본이 실제로 import 하는
패키지는 PyInstaller 정적 분석이 **자동으로 다시 포함**하므로 ①에서 새 패키지가
나와도 빌드가 깨지지 않는다. 답변에 따라 확정할 것은 둘뿐이다:

- ①에서 미사용 확인 시: `plotly`/`kaleido`/`cryptography` 만 excludes 로 제외
- ②에서 QML 미사용 확인 시: `PyQt6/Qt6/qml/` 트리 + ko/en 외 번역 제외
  (Qt6Qml.dll 등 **DLL 과 파이썬 바인딩은 유지** — WebEngine 이 C++ 레벨에서 로드)
- ③의 데이터 파일: spec datas 에 등재 추가

예상 효과: 파일 6,090 → 약 2,000~2,100개(−65%), 746 → 약 650MB. 압축 해제·델타
업데이트 시간은 파일 개수에 비례하므로 설치/업데이트 체감이 가장 크게 개선된다.

수정 대상은 [client/build_honey.spec](../client/build_honey.spec) 한 파일이다
(`build_honeyapp.spec` 은 이 파일을 읽어 실행하는 래퍼라 자동 반영). 반영 후 검증은
`client/update_test/` 하네스로 연속 2회 업데이트 e2e + 산출물 검사, 운영 릴리스 직후
input 파일 1건 web_report 업로드(honey_parse 파싱 경로)와 내장 브라우저 렌더 확인.
