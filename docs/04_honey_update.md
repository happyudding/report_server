# 04 - Honey 자동 업데이트 채널

배포 산출물은 PyInstaller onedir 결과물을 묶은 ZIP 하나이고, 별도 설치 프로그램은 쓰지
않는다. 다만 **설치 레이아웃이 두 가지**이고 어느 쪽이냐에 따라 업데이트 주체가 다르다.

## 두 레이아웃 — 판별은 코드가 자동으로 한다

| | 구(舊) onedir | 신(新) 버전폴더 + 런처 |
|---|---|---|
| 트리 | `Honey\Honey.exe` + `_internal\` | `Honey\Honey.exe`(런처) + `current.txt` + `versions\<ver>\HoneyApp.exe` + `updates\` |
| 업데이트 주체 | **실행 중인 앱**(`transport/updater.py`) | **런처**(`client/launcher.py` + `transport/app_update.py`) |
| 언제 | 앱이 새 버전을 감지한 그 자리에서 | **다음 실행 때** 앱이 뜨기 전에 |
| 빌드 | `build_zip.bat` / `buildandrelease.bat` → `release_honey.ps1` | `build_zip_new.bat` / `launcher_version_up_release.bat` → `release_launcher.ps1` |

판별은 [app_update.py](../client/transport/app_update.py) `install_root()` 가 한다 —
실행 파일 이름이 `HoneyApp.exe` 이고 부모 폴더가 `versions` 일 때만 버전폴더 레이아웃으로
본다. **환경변수 게이트는 없다.** (구 `HONEY_UPDATE_TEST=1` 은 2026-08-12 제거됐고 지금은
테스트 하네스 `client/update_test/run_test_client.bat` 에만 남아 있다. `app_update.py`
모듈 docstring 이 아직 옛 문구를 담고 있으니 그 주석을 근거로 삼지 말 것.)

> **버전폴더 레이아웃에서 앱은 업데이트를 하지 않는다** (2026-08-12 결정,
> `honey_main._on_version_manifest`). 새 버전을 감지해도 `[v2] … 런처가 다음 실행에
> 처리(앱 무동작)` 로그만 남기고 아무 창도 띄우지 않는다 — 쓰고 있는 창을 끊지 않고,
> 업데이트 경로를 런처 한 곳으로 모으기 위해서다.

런처는 `--wait-pid` 로 이전 프로세스를 기다린 뒤 `try_update()` → `current.txt` 후보 실행 →
**15초 안에 죽으면 다음 후보로 폴백하고 current.txt 를 되돌린다**. 연속 실패
`MAX_UPDATE_FAILS=3` 이면 업데이트를 포기한다. 탈출구는 `--skip-update` · 루트에
`noupdate.txt` · `--no-ui` 세 가지다. 델타 업데이트(`plan_delta`/`install_delta`)는 바뀌지
않은 파일을 hardlink 로 잇는다.
런처는 `transport/config` 를 쓸 수 없다(frozen 기준 "exe 옆"이 런처에서는 설치 루트라
어긋난다) — `app_update.read_server_url()` 이 `versions\<ver>\honey.env` → 루트
`honey.env` 순으로 직접 읽는다.

## 관련 파일

- 서버: `server/honey_routes.py`, `server/releases/version.json`,
  `server/releases/announcement.txt`(업데이트 공지 원문)
- 클라이언트(구): `client/transport/version_check.py`, `client/transport/update_policy.py`,
  `client/transport/updater.py`, `client/honey_main.py`
- 클라이언트(신): `client/launcher.py`, `client/transport/app_update.py`
- 빌드/배포: `client/build_honey.spec` · `build_honeyapp.spec` · `build_launcher.spec`,
  `client/build_zip.bat` · `build_zip_new.bat`, `client/release/release_honey.ps1` ·
  `release_launcher.ps1`, `client/release/RELEASE_GUIDE.txt`
- 테스트 하네스: `client/update_test/`

## 설치 방법 선택 (구 레이아웃 전용)

구 레이아웃에서 새 버전이 감지되면 **[자동 설치] / [ZIP 다운로드] / [나중에]**
다이얼로그가 뜬다. 서버는 방식을 강제하지 않는다.

> ⚠️ **현재 [자동 설치] 버튼은 만들어지지 않는다** — `update_policy.AUTO_INSTALL_ENABLED`
> 가 `False`(2026-07-23 일시 비활성)라 실제로는 **[ZIP 다운로드] / [나중에] 2버튼**이다.
> 되살리려면 그 상수를 `True` 로 되돌린다. 자동 설치는 그 외에도 **쓰기 권한
> (`can_write_app_dir()`) + `sha256` 존재** 두 조건을 더 만족해야 활성화된다
> (sha256 없는 배포는 무결성 검증이 통째로 생략되므로 자동 설치를 막는다).

| 버튼 | 동작 |
|---|---|
| 자동 설치 | 다운로드 후 앱 폴더를 교체하고 재실행 (아래 auto 흐름). **현재 비활성** |
| ZIP 다운로드 | 새 버전 ZIP을 사용자 다운로드 폴더에 저장하고 탐색기로 열어줌. 설치는 사용자가 수동 (Honey 종료 → 압축 해제 → 설치 폴더 덮어쓰기 → 재실행) |
| 나중에 | 아무 것도 하지 않음 |

## 업데이트 흐름 (구 레이아웃)

1. Honey 실행 후 `/honey/version`을 조회한다. (이 요청의 UA `HoneyVer/<버전>` 토큰이
   서버 **버전 대장** `report_client_version` 의 유일한 입력이다 — 행이 없는 사람 =
   토큰을 안 보내는 3.2.0 미만 구버전.)
2. 서버는 `server/releases/version.json`을 그대로 반환한다.
3. 클라이언트는 `version`과 빌드에 포함된 `CURRENT_VERSION`을 비교한다.
4. 새 버전이면 위 3버튼 다이얼로그를 띄운다. 설치 폴더 쓰기 가능 여부는
   `updater.can_write_app_dir()`로 판단해 자동 설치 버튼 활성/비활성을 정한다.
5. 사용자가 자동/수동을 고르면 `/honey/download`에서 `Honey-<version>.zip`을
   다운로드하고 sha256을 검증한다.
6. **ZIP 다운로드**: ZIP을 다운로드 폴더에 저장하고 탐색기로 파일을 선택해 보여준 뒤 끝.
7. **자동 설치** (frozen exe): `updater.apply_update_zip()`이 ZIP을 임시 폴더에 풀고 외부
   배치 파일을 띄운다. 배치는 현재 Honey 프로세스 종료를 기다린 뒤(최대 약 2분)
   ① `_internal/`을 `_internal.new`로 스테이징 복사 → ② 디렉토리 rename 2회로 swap
   (구버전은 `_internal.old`로 밀린 뒤 삭제) → ③ 루트는 `/E /XD _internal log`
   (미러 아님 — `log/` 등 보존)로 복사 → ④ `Honey.exe` 재실행 순으로 진행한다.
   각 단계 실패 시 구버전으로 롤백하고(`_internal.old` / `Honey.exe.bak` 복원)
   `<Honey.exe 폴더>\log\update.log`를 메모장으로 띄운다 — **반쯤 갱신된 설치본은
   남기지 않는다.**

개발 모드(`python honey_main.py`)에서는 자동 설치를 골라도 ZIP 다운로드까지만 수행하고 자동 교체는 하지 않는다.

## 배포 절차

```powershell
cd F:\COINAPI\report_server\client\release
.\release_honey.ps1 -Version 3.0.1 -Notes "변경 사항 요약"
```

스크립트가 수행하는 작업:

1. `client/transport/config.py`의 `CURRENT_VERSION` 갱신
2. `python -m PyInstaller --clean --noconfirm build_honey.spec`
3. `client/release_dist/Honey-<version>.zip` 생성
4. ZIP을 `server/releases/`로 복사
5. `server/releases/version.json`의 `version`, `file`, `sha256`, `released_at`, `notes` 갱신
6. `server/releases/release_log.txt` 기록

더블클릭 빌드가 필요하면 `client/build_zip.bat`을 실행한다.

## 업데이트 공지 팝업 (announcement.txt)

새 버전을 설치한 뒤 **처음 실행할 때 무엇이 바뀌었는지 1회 알려주는** 안내창이다.
업데이트 권유창(위 3버튼)과는 별개 — 이건 업데이트를 마친 사람에게 뜬다.

- 내용 정본: `server/releases/announcement.txt` **1개**. 운영자가 직접 편집하며
  `GET /honey/announcement` 가 **가공 없이 그대로**(text/plain) 서빙한다. 파일 내용이
  곧 팝업 본문이므로 주석·머리말 문법 같은 건 없다. 서버 재시작 없이 즉시 반영된다.
- 인코딩: UTF-8 권장. BOM 이 있어도, 메모장에서 "ANSI"(cp949)로 저장해도 서버가
  읽어낸다 ([honey_routes.py](../server/honey_routes.py) `get_announcement`).
- 표시 조건 (클라 [honey_main.py](../client/honey_main.py) `_maybe_show_announcement`):
  1. `/honey/version` 비교 결과 **업데이트할 게 없을 때**(= 최신을 실행 중). 업데이트가
     남아 있으면 공지 대신 업데이트 권유창이 뜨고, 공지는 업데이트 후 첫 실행에서 뜬다
     → 구버전 사용자에게 신버전 공지가 새는 일이 없다.
  2. `%APPDATA%\Honey\settings.json` 의 `announcement_seen_version` != `CURRENT_VERSION`.
     기록은 Windows **계정별**이라 같은 PC 라도 계정이 다르면 각각 1회 뜨고, 같은 계정은
     재실행해도 다시 뜨지 않는다. 다시 띄우고 싶으면 이 키를 지우면 된다.
  3. 파일이 비어 있으면(공백만이어도) 아무 것도 띄우지 않는다 — 공지 없는 릴리스는
     파일을 비워두면 된다.
- fetch 실패(서버 오프라인 등)는 조용히 넘어가고 다음 실행 때 다시 시도한다.

> ⚠️ **릴리스마다 announcement.txt 를 갱신하거나 비워라.** 갱신을 잊으면 새 버전
> 사용자에게 **이전 버전 공지가 새 공지처럼 1회** 뜬다(파일에 버전 표기가 없어 서버가
> 구분하지 못한다). `release_honey.ps1` 은 이 파일을 건드리지 않는다.

작성 예 (이 내용이 그대로 팝업된다):

```
Honey 3.1.1 업데이트

· 웹 리포트 첫 로딩 속도 개선
· Map Analysis 크게보기 오류 수정
```

## version.json 필드

- `version`: 클라이언트가 비교하는 semver
- `file`: `/honey/download`가 서빙할 ZIP 파일명
- `sha256`: 다운로드 무결성 검증값
- `released_at`: 배포 시간
- `notes`: 릴리스 설명
- `url`: 선택 필드. 없으면 클라이언트는 `/honey/download`로 폴백한다.

## 주의 사항

- 실행 중인 `Honey.exe`를 직접 덮어쓰지 않는다. 외부 배치 파일이 프로세스 종료 후 복사한다.
- **실행 중 Honey 종료** — 같은 설치 루트의 프로세스를 실제 앱(`HoneyApp.exe`), 다른
  런처(`Honey.exe`), Qt 보조 프로세스(`QtWebEngineProcess.exe`)로 구분한다. 실제 앱이
  실행 중이고 업데이트가 있을 때만 저장하지 않은 작업이 사라질 수 있음을 알리고,
  동의받은 경우에만 창 닫기 요청 → 제한 대기 → 강제 종료 순으로 정리한다. 앱 없이 남은
  Qt 보조 프로세스는 고아 프로세스로 자동 정리하며, 정리 실패만으로 최신 버전 실행을
  막지 않는다. 업데이트가 없으면 기존 앱 창을 활성화하고 중복 실행하지 않는다.
- **UAC 승격 (2026-08-26 신설, 런처 레이아웃 한정)** — 구 batch 경로는 여전히 승격을
  쓰지 않지만, 런처는 직접 설치 중 `LocalWriteError`가 나면 UAC 를 한 번 묻는다.
  네트워크 실패와 로컬 쓰기 실패는 반드시 구분한다.
  - 승격 프로세스는 `Honey.exe --elevated-update <ver>` 로 자기 자신이며, **업데이트만
    하고 종료한다.** 앱(`HoneyApp.exe`)은 언제나 일반 권한으로 실행한다 — 관리자
    권한으로 앱을 띄우면 앱이 만드는 파일까지 전부 관리자 소유가 되어 문제가 되돌아온다.
  - 승격 패스는 설치 **전에** `icacls /grant Users:(OI)(CI)M` 로 ACL 을 정상화한다.
    순서가 뒤집히면 새로 만든 폴더가 관리자 소유로 남아 다음 업데이트가 또 막힌다.
    이 한 번으로 그 PC 는 이후 UAC 없이 업데이트된다(= 근본 해결).
  - UAC 취소·자격증명 없음이면 아무것도 바꾸지 않고 기존 버전을 실행한다(10초 카운트다운
    자동 선택 — 무인 PC 에서 창이 앱 기동을 막지 않게).
  - `icacls` 반환 코드와 표준 오류를 로그에 남기며, 실패하면 실제 파일 쓰기 결과와 함께
    사용자에게 정확한 실패 작업·경로·WinError 를 표시한다.
- **새 버전 폴더를 rename 하지 않는다.** `versions\<ver>` 에 바로 설치하고 작업 ID가
  일치하는 `.installing`/`.ready`가 생긴 뒤에만 실행 가능하게 본다. 설치가 중단되면
  current.txt 는 구버전을 계속 가리키고 다음 시도에서 같은 폴더의 누락·손상 파일을
  복구한다. 설치 완료 전환 때는 매니페스트 전 파일의 SHA-256을 확인한다.
- 델타 재사용 파일은 구버전에서 일반 복사한다. 실패 복구 중 새 버전 파일을 덮어써도
  정상 구버전이 함께 바뀌지 않도록 하드링크는 사용하지 않는다.
- 구버전/실패 잔재 삭제는 정상 실행 뒤 best-effort 정리다. 삭제 실패는 업데이트 성공을
  되돌리지 않으며, 직접 설치 중단 폴더는 다음 실행의 복구를 위해 보존한다.
- **런처(`Honey.exe`)는 자동 업데이트 대상이 아니다.** 릴리스 payload 에 동봉된
  `versions\<ver>\launcher\Honey.exe` 사본으로, 앱이 기동 10초 뒤 루트 런처를 교체한다
  (`transport/launcher_selfupdate.py`). 런처는 앱을 띄우고 곧바로 종료하므로 그 시점에
  파일이 잠겨 있지 않다 — 런처 자신은 실행 중인 자기 파일을 절대 못 바꾼다.
- **지원 범위**: 로컬 고정 드라이브(C:/D:)의 완전한 설치 트리. UNC/NAS/USB, `Honey.exe`
  만 다른 곳에 복사한 경우, `versions\...\HoneyApp.exe` 직접 실행은 보장하지 않는다.
- 런처를 고쳐 배포하면 `launcher.LAUNCHER_BUILD` 를 올린다 — 그래야 "3회 연속 실패로
  포기" 상태(`.update_fail`)가 자동으로 풀려, 그 버그로 멈춰 있던 PC 가 새 런처를 받는
  즉시 다시 시도한다.
- 배치 파일은 ANSI(cp949)로 저장된다. 배치 문자열에 cp949 로 표현 안 되는 문자
  (예: em dash `—`)를 넣지 말 것 — 과거 이 문자 때문에 업데이트가 통째로 크래시했다.
  같은 이유로 **배치의 `echo` 문구는 ASCII 만** 쓴다(사용자가 메모장에서 읽는 안내 1줄 제외).
- **배치는 `CREATE_NO_WINDOW`로 띄운다 (`DETACHED_PROCESS` 금지).** DETACHED 로 띄운
  배치는 부모 Honey.exe 가 종료될 때 Ctrl+C 를 받아 **함께 죽는다** — 2026-07-21 현장
  실패의 직접 원인이었다(로그에 시작 한 줄만 남고 아무 일도 안 일어남).
- 배치가 부모 종료를 판정하는 `tasklist`/`find` 는 **절대경로**로 부른다. PATH 에 Git 등의
  GNU `find` 가 먼저 잡히면 판정이 통째로 뒤집힌다(실측 확인).
- 진단 로그는 파이썬·배치가 `<Honey.exe 폴더>\log\update.log` 한 파일에 함께 남긴다
  (종전 `%TEMP%\honey_update.log` — 현장 사용자가 `%TEMP%` 를 찾지 못했다).
  cmd 자체 오류는 `log\update_cmd.log`, robocopy 원문은 `log\update_robocopy.log`.
- `CURRENT_VERSION`은 빌드 전에 반드시 올려야 한다. 순서가 틀리면 클라이언트가 계속 업데이트를 권유할 수 있다.
- `version.json`은 BOM 없는 UTF-8로 저장한다. `release_honey.ps1`은 자동으로 그렇게 저장한다.
