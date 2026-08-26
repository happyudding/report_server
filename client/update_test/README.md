# 자동 업데이트 (버전 폴더 + 런처 방식) — 테스트/검증 폴더

zip 을 매번 손으로 배포하는 대신, **런처가 앱을 띄우기 전에 새 버전을 받아 설치하고
곧바로 새 버전으로 실행**하는 배포 구조다. 이 폴더는 그 구조를 빌드본으로 미리
돌려보기 위한 것이다.

> **운영에는 아직 아무 영향이 없다.** 기존 릴리스 파이프라인
> (`build_zip.bat` / `buildandrelease.bat` / `release_honey.ps1` / `build_honey.spec`)과
> `transport/updater.py`, `server/releases/` 는 **하나도 바뀌지 않았다**.
>
> 새 흐름이 켜지는 조건은 **설치 구조 하나**다 — 실행 파일이
> `versions\<ver>\HoneyApp.exe` 여야 한다(`app_update.install_root()`). 현장에 깔린
> 배포본은 `Honey.exe` 단독이라 항상 종전 흐름(ZIP 다운로드 안내)으로 간다.
> 검증용 환경변수 게이트 `HONEY_UPDATE_TEST=1` 은 **2026-08-12 제거**됐다 —
> 이 구조 판정과 중복이라, 새 구조로 설치한 PC 에서 exe 를 그냥 더블클릭하면 된다.

---

## 1. 왜 이 구조인가

기존 방식은 실행 중인 `Honey.exe` / `_internal` 을 **덮어써야** 해서, 앱이 완전히 죽기를
배치 스크립트가 기다렸다가 폴더를 갈아끼웠다. 여기서 DLL 잠금·백신의 batch 차단·
cp949 인코딩 같은 문제가 계속 터졌다.

새 구조는 현재 버전 파일을 덮어쓰지 않는다. 이미 Honey가 실행 중이면 사용자에게 저장하지
않은 작업이 사라질 수 있음을 알리고, 동의받아 종료한 뒤 새 버전 폴더를 설치한다.
종료 뒤 `QtWebEngineProcess.exe`만 남은 경우에는 이를 실행 중인 Honey로 오인하지 않고
고아 보조 프로세스를 정리한 뒤 `current.txt`가 가리키는 최신 버전을 실행한다.

```
Honey\                       <- 설치 루트
├── Honey.exe                런처 (사용자가 누르는 것. 거의 안 바뀜)
├── current.txt              1행 현재 버전, 2행 이전 버전(롤백용)
├── log\                     실행/업데이트/런처 로그 (버전과 무관하게 남는다)
├── updates\                 다운로드 중인 zip (설치 후 삭제)
└── versions\
    ├── 3.1.1\HoneyApp.exe + _internal\ + honey.env + .files.json
    └── 3.2.0\ ...
```

**진짜 Honey UI 는 `versions\<ver>\HoneyApp.exe` 다.** 사용자는 그것을 직접 누르지 않고
루트의 `Honey.exe`(런처)를 누른다 — 이름이 종전과 같아서 바뀐 걸 눈치채기 어렵다.
⚠ **바탕화면 바로가기는 반드시 런처(`Honey.exe`)로 만들 것.** `versions\...\HoneyApp.exe`
를 직접 가리키면 앱은 뜨지만 업데이트를 영영 못 받는다.

## 2. 업데이트는 언제 일어나나 — 런처가 앱을 띄우기 전에

```
Honey.exe 더블클릭
  → current.txt 로 현재 버전 확인 → /honey/version 질의 (연결 2초/응답 3초로 짧게)
  ├─ 최신 · 서버 무응답 · 오프라인 → 곧바로 현재 버전 실행 (지연 없음)
  └─ 새 버전 있음
       → 작은 진행창(tkinter): 내려받는 중 / 설치 중 [취소]
       → current.txt 갱신 → 새 버전 실행  ← 사용자가 보는 창은 처음부터 새 버전
```

앱이 뜬 **뒤**에 묻던 종전 방식과 달리 구버전 창이 아예 뜨지 않는다(무거운
PyQt6+WebEngine 앱을 두 번 띄우지 않는다). Steam·Riot 런처와 같은 방식이다.

**앱은 실행 중에 업데이트를 제안하지 않는다** (2026-08-12 결정). 서버 질의 자체는 계속
하는데, 그건 접속 사용량 집계(`honey_run`)·"⚠ 서버 오프라인" 표시·공지 팝업이 거기
걸려 있기 때문이다.

## 3. 변경분만 받는다 (델타)

빌드는 6,000여 개 파일 / 730MB 인데 버전이 올라가도 실제로 바뀌는 건 보통
`HoneyApp.exe`(우리 코드가 전부 들어 있다) 하나뿐이다. 나머지(PyQt6 492MB 등)는 그대로다.

그래서 런처는 **바뀐 파일만 받고, 안 바뀐 것은 현재 버전 폴더에서 독립 복사**한다.
하드링크는 실패 복구 중 구버전까지 변할 수 있어 사용하지 않는다. 판정 기준은 각 버전
폴더에 들어 있는 `.files.json`(파일별 sha256)이다 — 디스크를
다시 해싱하면 수십 초가 걸려 빠르게 만들려는 목적과 어긋나기 때문에 캐시를 쓴다.

델타가 불가능하면(로컬 `.files.json` 없음 = 구조 전환 직후, 서버가 매니페스트 미제공,
중간 실패) **전체 zip 방식으로 자동 폴백**한다. 델타는 최적화일 뿐 필수 경로가 아니다.

서버 쪽은 **추가된 라우트 2개**가 전부다 (기존 라우트·응답 무변경):
`GET /honey/files/<ver>` (매니페스트) · `GET /honey/file/<ver>?path=` (zip 안 파일 1개 스트리밍).

## 4. 실패해도 반드시 구버전이 실행된다

업데이트는 부가 기능이고 앱이 뜨는 것이 본 기능이다. `try_update` 는 **모든 예외를
삼키고**, `current.txt` 는 설치가 완전히 끝난 뒤에만 바뀐다. 새 버전 폴더에는
`.installing`/`.ready` 작업 ID를 기록하며 둘이 일치하기 전에는 실행 후보로 보지 않는다.

* **진행창이 뜨기 전 실패**(오프라인 등) → 안내 없이 즉시 실행. 흔한 상황이다.
* **진행창이 뜬 뒤 실패** → 실패 화면으로 전환: 사유 + [지금 실행] + [설치파일 직접 받기]
  (브라우저로 `/honey/download`). **10초 뒤 자동으로 닫히고 앱이 뜬다** — 안내창이
  실행을 막아선 안 되기 때문이다.
* **쓰기 권한 없음**(Program Files 등) → 받기 **전에** 판정해 건너뛴다. 331MB 를 다 받고
  마지막에 실패하지 않게.
* **같은 버전 연속 3회 실패** → 그 버전은 더 시도하지 않는다(매 실행마다 받다 실패하면
  앱 기동이 계속 느려진다).
* **비상 탈출구**: 루트에 `noupdate.txt` 를 만들거나 `Honey.exe --skip-update` 로 실행하면
  업데이트 단계를 통째로 건너뛴다.

실패는 서버에도 남는다 — `POST /pe/report/api/client_diagnostic` 로 보고돼
관리자 화면 `/pe/admin-pte/` 의 **🚨 진단 사건** 탭에 `honey:update_failed` 로 뜬다
(누구의 PC 인지 UA 로 귀속). 보고는 완전 무음이라 실패해도 앱 실행에 영향이 없다.

---

## 5. 이 폴더의 파일

| 파일 | 용도 |
|---|---|
| `build_test_release.ps1` | 테스트 릴리스 빌드 (런처+앱+매니페스트+zip+version.json). 결과는 `release\` 에만 |
| `make_manifest.py` | 파일별 sha256 목록 생성 (빌드가 호출). 6,000여 파일에 약 5초 |
| `test_update_server.py` | `/honey/version`·`/honey/download` 만 흉내내는 미니 서버 (127.0.0.1:8090) |
| `check_app_update.py` | 설치·**델타** 로직 자동 점검 (빌드 없이 수 초) |
| `check_launcher.py` | 런처 폴백/롤백/대기 자동 점검 (빌드 없이 수 초) |
| `run_test_client.bat` | 테스트 설치본을 로컬 서버로 붙여 실행 |
| `release\` | 빌드 결과 zip + version.json + files.json (git 에 안 올라감) |

관련 파일: `client\launcher.py`(선체크·진행창), `client\transport\app_update.py`(설치·델타·
보고 — 표준 라이브러리만), `client\build_launcher.spec`, `client\build_honeyapp.spec`,
`server\honey_routes.py`(델타 라우트 2개).

> 미니 서버도 델타 라우트(`/honey/files/<ver>`, `/honey/file/<ver>?path=`)를 운영
> `honey_routes.py` 와 같은 규약으로 제공한다 — 그래서 파이썬도 네트워크도 없는 PC 에서
> **델타까지 그대로 검증**할 수 있다.

## 6. 자동 점검 (빌드 없이 수 초 — 항상 이것부터)

```
python client\update_test\check_app_update.py     -> 마지막 줄 ALL OK
python client\update_test\check_launcher.py       -> 마지막 줄 ALL OK
```

`check_app_update.py` 는 미니 HTTP 서버를 띄워 **델타 조립(재사용/다운로드/sha256 거부)**
까지 실제로 돌린다. 실패하면 아래로 진행할 필요가 없다.

---

## 7. 테스트 릴리스 만들기

```
client\update_test\build_test_release.ps1 -Version 3.1.1      REM 설치 시작점
client\update_test\build_test_release.ps1 -Version 3.2.0      REM 업데이트 대상
```

* `-ServerUrl` 기본값은 `http://127.0.0.1:8090` 이며 빌드본의 `honey.env` 에 박힌다.
* `transport\config.py` 의 `CURRENT_VERSION` 을 잠깐 바꿨다가 **성공·실패 무관하게 원복**한다.
* 결과: `release\Honey-<ver>.zip` + `Honey-<ver>.files.json` + `version.json`
* 앱 빌드라 **수 분** 걸린다. 진행 표시가 없는 구간이 있어도 정상이다.
* **두 번 빌드하는 이유**: 설치본(구버전) 자체가 런처+`versions\` 구조여야 한다.
  기존 운영 zip 은 구 구조라 이 흐름을 타지 않는다.

## 8. 다른 PC 한 대에서 단독 검증 (파이썬·네트워크 불필요)

개발 PC 와 **통신이 안 되는 PC** 에서도 전 과정을 확인하기 위한 방법이다. 서버와
클라이언트가 그 PC 안에서 `127.0.0.1` 로만 오간다 — 네트워크도 방화벽 설정도 파이썬
설치도 필요 없다.

가져갈 꾸러미(약 700MB, USB 로 복사):

```
HoneyUpdateTest\
├── READ_ME_FIRST.txt
├── Honey-3.1.1.zip                 <- 여기 풀고 Honey.exe 실행 (설치 시작점)
└── update_server\
    ├── HoneyUpdateServer.exe       <- 더블클릭하면 127.0.0.1:8090 에서 대기
    ├── test_update_server.py        (파이썬이 있는 PC 라면 이쪽을 써도 된다)
    └── release\
        ├── Honey-3.2.0.zip          서버가 나눠줄 새 버전
        └── version.json             "지금 배포 중인 버전은 3.2.0"
```

미니 서버 exe 는 이렇게 만든다:

```
python -m PyInstaller --noconfirm --onefile --console --name HoneyUpdateServer ^
  --distpath client\update_test\dist_server --workpath client\update_test\build_server ^
  --specpath client\update_test\build_server client\update_test\test_update_server.py
```

### 외부 PC 에서 (순서대로)

1. `update_server\HoneyUpdateServer.exe` 더블클릭 → `현재 배포 중인 버전: 3.2.0`.
   **이 창은 켜 둔 채로 둔다.**
2. `Honey-3.1.1.zip` 을 아무 데나 푼다 → `...\Honey\` 가 생긴다.
3. `Honey\Honey.exe` 더블클릭.

기대: **구버전 창은 뜨지 않고** 진행창만 잠깐 보였다가 곧바로 `Honey v3.2.0` 이 뜬다.

### 확인 시나리오

정상 경로

| # | 시나리오 | 기대 |
|---|---|---|
| 1 | 그냥 실행 | 진행창 → 바로 3.2.0. `current.txt` 1행 3.2.0 / 2행 3.1.1 |
| 2 | 최신 상태에서 다시 실행 | 지연 없이 바로 실행 |
| 3 | 3.1.0 을 만들어 두고 3.1.2 로 업데이트 | 앱이 뜨고 10초 뒤 3.1.0 자동 삭제(현재/직전 2개만 유지) |

실패 경로 — **전부 "구버전이 정상 실행"이면 합격** (이쪽이 검증의 본체)

| # | 만드는 방법 | 기대 |
|---|---|---|
| 4 | 서버를 끄고 실행 | 몇 초 안에 포기하고 3.1.1 실행, 안내창 없음 |
| 5 | `version.json` 을 깨진 JSON 으로 | 포기하고 3.1.1 실행 |
| 6 | `version.json` 의 sha256 을 한 글자 변조 | 받은 것 삭제 후 3.1.1, **실패 화면** 표시 |
| 7 | 다운로드 중 서버 창 강제 종료 | 부분 파일 정리 후 3.1.1 |
| 8 | 설치 중 런처 강제 종료 | 다음 실행에서 3.1.1 정상, 불완전 폴더 제자리 복구 |
| 9 | 진행창에서 취소 | 3.1.1 실행, 대상 폴더는 `.ready` 불일치로 실행 제외 |
| 10 | 루트에 `noupdate.txt` 생성 후 실행 | 업데이트 시도 없이 즉시 3.1.1 |
| 11 | 업데이트 성공 후 `versions\3.2.0\HoneyApp.exe` 손상 | 런처가 감지해 3.1.1 로 자동 롤백 |
| 12 | 6번 실패 화면에서 아무것도 안 누름 | 10초 뒤 3.1.1 자동 실행 |
| 13 | 6번 실패 화면에서 [설치파일 직접 받기] | 브라우저가 열려 다운로드 시작, 앱도 실행됨 |
| 14 | 설치 폴더를 읽기 전용으로 만들고 실행 | 받기 전에 판정해 즉시 3.1.1 실행 |
| 15 | **빌드된 런처(onefile)를 빈 폴더에 단독으로 놓고 `--no-ui` 로 실행** | log 에 "다른 Honey 런처 대기" 가 **없어야** 하고 "no runnable version" 까지 진행. 이 줄이 보이면 런처가 자기 onefile 부트로더 부모를 "다른 런처"로 오인하는 회귀다 (2026-08-26 실제 장애 — `running_honey_processes` 의 `os.getppid()` 제외가 빠짐). ※ `check_launcher.py` 는 python 으로 실행해 부트로더 부모가 없으므로 이 회귀를 못 잡는다 — 반드시 **빌드된 exe** 로 확인할 것 |

각 경우 `log\launcher.log` / `log\update.log` 에 **원인이 한 줄로 남는지**까지 본다 —
조용히 실패하면 현장에서 원인을 못 찾는다.

> **함정**: 그 PC 에 사용자 환경변수 `HONEY_SERVER_URL` 이 설정돼 있으면 `honey.env` 를
> 이겨서, 클라가 엉뚱한 서버를 보고 "최신입니다"라며 **에러 없이 아무 일도 안 한다**.
> 업데이트가 안 뜨면 `echo %HONEY_SERVER_URL%` 부터 확인할 것.

---

## 9. 알아 둘 것

* 빌드본은 `honey.env` 에 `127.0.0.1:8090` 이 박혀 있어 **운영 서버로 가지 않는다.**
  다른 주소를 쓰려면 `build_test_release.ps1 -ServerUrl <주소>`.
* 앱 빌드 산출물은 `client\dist\HoneyApp\`, 런처는 `client\dist_launcher\` 로,
  기존 `client\dist\Honey\` 와 겹치지 않는다.
* 새 릴리스의 `versions\<ver>\launcher\Honey.exe`를 앱이 기동 뒤 루트 `Honey.exe`로
  교체한다. 실행 중 교체가 실패하면 다음 실행에서 다시 시도한다.
* 옛 버전 정리(직전 1개만 남기고 삭제)는 앱이 뜨고 **10초 뒤** 백그라운드에서 돈다.
  "10초를 버텼다 = 새 버전이 정상 기동했다"를 확인한 뒤에 지우려는 것이다.
* 이 개발 PC 의 `client\honey_parse\` 는 더미라 원본 spec 이 요구하는
  `honey_parse\mddi\datalog_parser\ui\optional_sheets_dialog.ui` 가 없다. 테스트 빌드는
  그 파일을 건너뛰고 진행하며, 그 대화상자를 쓰는 기능만 테스트 빌드본에서 동작하지
  않는다. **운영 릴리스는 종전대로 실물이 있는 빌드 PC 에서 만든다.**
* 실행 로그(`log\<시각>.txt`)는 아직 버전 폴더 안(`versions\<ver>\log\`)에 남는다 —
  루트 `log\` 로 모으는 것은 운영 배선 단계에서 `run_log.py` 와 함께 정리한다.

## 10. 남은 것 (운영 배선)

릴리스 스크립트 개편(`release_honey.ps1` 에 런처+매니페스트 포함), `build_honey.spec`
개명, 기존 batch 업데이트 코드(`transport/updater.py`) 정리, 기존 사용자 1회 수동 이전
안내는 **다음 단계**다. 이 폴더의 것만으로는 운영 배포본이 바뀌지 않는다.
