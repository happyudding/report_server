# 자동 업데이트 테스트 (버전 폴더 + 런처 방식)

zip 을 매번 손으로 배포하는 대신, **서버 버전 체크 → 진행바 → 자동 재시작**으로
업데이트되게 하는 새 배포 구조를 개발 PC 에서 **미리 돌려보기** 위한 폴더다.

> **운영에는 아직 아무 영향이 없다.** 기존 릴리스 파이프라인
> (`build_zip.bat` / `buildandrelease.bat` / `release_honey.ps1` / `build_honey.spec`)과
> 서버(`server/`), `server/releases/` 는 **하나도 바뀌지 않았다**. 새 흐름은
> 환경변수 `HONEY_UPDATE_TEST=1` 이 있을 때만 켜진다.

---

## 1. 왜 이 구조인가

기존 방식은 실행 중인 `Honey.exe` / `_internal` 을 **덮어써야** 해서, 앱이 완전히 죽기를
배치 스크립트가 기다렸다가 폴더를 갈아끼웠다. 여기서 DLL 잠금·백신의 batch 차단·
cp949 인코딩 같은 문제가 계속 터졌다.

새 구조는 **실행 중인 파일을 아예 건드리지 않는다.**

```
Honey\                       <- 설치 루트
├── Honey.exe                런처 (8MB, 거의 안 바뀜) — versions 안의 앱을 띄운다
├── current.txt              1행 현재 버전, 2행 이전 버전(롤백용)
├── log\                     실행/업데이트/런처 로그 (버전과 무관하게 남는다)
├── updates\                 다운로드 중인 zip (설치 후 삭제)
└── versions\
    ├── 9.0.0\HoneyApp.exe + _internal\ + honey.env
    └── 9.0.1\ ...
```

업데이트 = 앱이 **떠 있는 채로** `versions\9.0.1.tmp-<pid>` 에 받아서 풀고 →
다 되면 `versions\9.0.1` 로 이름만 바꾸고 → `current.txt` 를 바꾸고 → 런처를 다시 띄운다.
어느 단계에서 실패하거나 취소해도 지금 쓰던 버전은 그대로다.

---

## 2. 이 폴더의 파일

| 파일 | 용도 |
|---|---|
| `build_test_release.ps1` | 테스트 릴리스 빌드 (런처+앱+zip+version.json). 결과는 `release\` 에만 |
| `test_update_server.py` | `/honey/version`·`/honey/download` 만 흉내내는 미니 서버 (127.0.0.1:8090) |
| `check_app_update.py` | 설치 로직 자동 점검 (빌드 없이 수 초) |
| `check_launcher.py` | 런처 폴백/롤백/대기 자동 점검 (빌드 없이 수 초) |
| `run_test_client.bat` | 테스트 설치본 실행 (환경변수 2개를 켜서 띄운다 — 4-5 참조) |
| `release\` | 빌드 결과 zip + version.json (git 에 안 올라감) |

관련 새 파일: `client\launcher.py`, `client\build_launcher.spec`,
`client\build_honeyapp.spec`, `client\transport\app_update.py`,
`client\build_test_update.bat`.

---

## 3. 이미 검증해 둔 것 (2026-08-07, 개발 PC)

전 과정을 한 번 돌려서 **9.0.0 → 9.0.1 자동 업데이트가 실제로 동작하는 것을 확인했다.**

```
[v2] OFFER remote=9.0.1 current=9.0.0 root=F:\HoneyUpdateTest\Honey
[v2] DOWNLOAD start dest=...\updates\Honey-9.0.1.zip url=/honey/download
INSTALL extract -> ...\versions\9.0.1.tmp-23696
INSTALL done ...\versions\9.0.1                      (334MB 압축 해제 17초)
SWITCH current=9.0.1 prev=9.0.0 -> relaunch ...\Honey.exe
launcher: wait for pid 23696 -> launch 9.0.1 -> ok 9.0.1 (running)
```

확인 클릭부터 새 버전 창이 뜨기까지 약 40초. `current.txt` = `9.0.1 / 9.0.0`,
`updates\` 비어 있음, `versions\` 에 두 버전 유지.

현재 남아 있는 상태:

* `client\update_test\release\` : 9.0.0 / 9.0.1 zip + version.json(9.0.1)
* `F:\HoneyUpdateTest\Honey\` : **이미 9.0.1 로 업데이트된 상태**

직접 다시 보려면 (둘 중 하나):

* `client\build_test_update.bat 9.0.2` 로 다음 버전을 만들고 4-4 부터 진행, 또는
* `F:\HoneyUpdateTest` 를 지우고 `Honey-9.0.0.zip` 을 다시 풀어 4-2 부터 진행

### 자동 점검 (빌드 없이 수 초)

```
python client\update_test\check_app_update.py     -> 마지막 줄 ALL OK
python client\update_test\check_launcher.py       -> 마지막 줄 ALL OK
```

빌드 없이 설치/롤백 로직만 먼저 확인하는 단계다. 실패하면 아래 진행할 필요 없다.

---

## 4. 테스트 절차

### 4-1. 첫 버전(9.0.0) 만들기

```
client\build_test_update.bat 9.0.0
```

* 버전은 운영(3.x)과 헷갈리지 않게 **9.x.x** 를 쓴다.
* `transport\config.py` 의 CURRENT_VERSION 을 잠깐 9.0.0 으로 바꿨다가 **끝나면 원복**한다
  (실패해도 원복된다).
* 결과: `client\update_test\release\Honey-9.0.0.zip` + `version.json`
* 앱 빌드라 **수 분** 걸린다. 진행 표시가 없는 구간이 있어도 정상이다.

### 4-2. 설치 (신규 설치와 같은 방식)

`Honey-9.0.0.zip` 을 예를 들어 `F:\HoneyUpdateTest\` 에 풀면
`F:\HoneyUpdateTest\Honey\` 가 생긴다. `Honey.exe`(런처)를 실행해 9.0.0 이 뜨는지 본다.
창 제목이 `Honey v9.0.0` 이면 성공.

### 4-3. 다음 버전(9.0.1) 만들기

```
client\build_test_update.bat 9.0.1
```

`release\` 가 9.0.1 로 갱신된다 (9.0.0 zip 은 남아 있어도 무방).

### 4-4. 테스트 서버 켜기

```
python client\update_test\test_update_server.py
```

`현재 배포 중인 버전: 9.0.1` 이 찍히면 준비 완료. (Ctrl+C 로 종료)

### 4-5. 업데이트 실행

```
client\update_test\run_test_client.bat
```

설치 폴더가 다르면 `run_test_client.bat D:\어딘가\Honey` 처럼 넘긴다.

> **이 배치를 꼭 쓸 것.** 이 PC 에는 사용자 환경변수
> `HONEY_SERVER_URL=http://12.81.220.117:8080`(운영 서버)이 설정돼 있고,
> `transport\config.py` 는 이 환경변수를 `honey.env` 보다 **우선**한다. 그냥
> `Honey.exe` 를 더블클릭하면 테스트 빌드본이 운영 서버를 보고 "3.1.1 이니 9.0.0 이
> 최신" 이라고 판단해 **업데이트 창이 아예 안 뜬다** (실제로 겪은 함정이다).
> 배치가 `HONEY_UPDATE_TEST=1` 과 `HONEY_SERVER_URL=http://127.0.0.1:8090` 을 함께 넣어준다.

기대 동작:

1. 시작 0.5초 뒤 `신규 버전 9.0.1 이(가) 있습니다. 지금 업데이트하시겠습니까?` → **예**
2. 다운로드 진행바 (MB / MB, %) — 취소 가능
3. 설치 진행바 (압축 해제, MB / MB, %) — 취소 가능
4. 앱이 스스로 종료되고 **9.0.1 로 다시 시작** (창 제목 `Honey v9.0.1`)

확인할 것:

* `F:\HoneyUpdateTest\Honey\current.txt` → 1행 `9.0.1`, 2행 `9.0.0`
* `versions\` 에 9.0.0, 9.0.1 둘 다 존재 (직전 버전 1개는 롤백용으로 남긴다)
* `updates\` 는 비어 있음 (받은 zip 은 설치 후 삭제)
* `log\update.log` 에 `[v2] ...` 줄, `log\launcher.log` 에 `launch 9.0.1`

### 4-6. 실패·취소 경로

* 다운로드 중 취소 → "업데이트 취소됨", 9.0.0 그대로 계속 사용 가능
* 설치(압축 해제) 중 취소 → 같은 결과. `versions\` 에 `*.tmp-*` 폴더가 **남지 않아야** 한다
* 서버를 끄고 실행 → "⚠ 서버 오프라인" 상태바만, 앱은 정상 동작

### 4-7. 롤백 (새 버전이 깨졌을 때)

`versions\9.0.1\HoneyApp.exe` 를 메모장으로 열어 아무 내용이나 넣어 망가뜨린 뒤
`Honey.exe` 실행 → 런처가 실행 실패를 감지해 **9.0.0 으로 되돌린다**.
`log\launcher.log` 에 `crash 9.0.1` / `rollback current -> 9.0.0` 이 남는다.

---

## 5. 알아 둘 것

* **`HONEY_UPDATE_TEST=1` 없이 실행하면** 새 흐름이 꺼져 종전대로 "ZIP 다운로드 / 나중에"
  안내가 뜬다. 테스트 설치본에서도 마찬가지다 — 운영 배포본은 이 변수가 없으니 무영향.
* 테스트 빌드본은 `honey.env` 에 `http://127.0.0.1:8090` 이 박혀 있어 **운영 서버로 가지
  않는다.** 다른 주소를 쓰려면 `build_test_release.ps1 -ServerUrl <주소>`.
* 앱 빌드 산출물은 `client\dist\HoneyApp\`, 런처는 `client\dist_launcher\` 로,
  기존 `client\dist\Honey\` 와 겹치지 않는다.
* 옛 버전 정리(직전 1개만 남기고 삭제)는 앱이 뜨고 **10초 뒤** 백그라운드에서 돈다.
* 이 개발 PC 의 `client\honey_parse\` 는 더미라 원본 spec 이 요구하는
  `honey_parse\mddi\datalog_parser\ui\optional_sheets_dialog.ui` 가 없다. 기존
  `build_zip.bat` 도 여기서 같은 이유로 실패한다. 테스트 빌드는 그 파일을 건너뛰고
  진행하며(빌드 로그에 크게 표시), 그 대화상자를 쓰는 기능만 테스트 빌드본에서 동작하지
  않는다. **운영 릴리스는 종전대로 실물이 있는 빌드 PC 에서 만든다.**
* 실행 로그(`log\<시각>.txt`)는 아직 버전 폴더 안(`versions\<ver>\log\`)에 남는다 —
  루트 `log\` 로 모으는 것은 운영 배선 단계에서 `run_log.py` 와 함께 정리한다.

## 6. 테스트가 끝나면

운영 배선(릴리스 스크립트 개편, 환경변수 게이트 제거, 기존 batch 업데이트 코드 정리,
기존 사용자 1회 수동 이전 안내)은 **다음 단계**다. 이 폴더의 것만으로는 운영 배포본이
바뀌지 않는다.
