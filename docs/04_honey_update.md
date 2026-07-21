# 04 - Honey 자동 업데이트 채널

Honey 업데이트는 PyInstaller onedir 결과물(`client/dist/Honey/`)을 ZIP으로 묶어
배포한다. 별도 설치 프로그램은 사용하지 않는다.

## 관련 파일

- 서버: `server/honey_routes.py`, `server/releases/version.json`
- 클라이언트: `client/transport/version_check.py`, `client/transport/update_policy.py`,
  `client/transport/updater.py`, `client/honey_main.py`
- 빌드/배포: `client/build_honey.spec`, `client/build_zip.bat`,
  `client/release/release_honey.ps1`, `client/release/RELEASE_GUIDE.txt`

## 설치 방법 선택 (자동 / 수동)

새 버전이 감지되면 클라이언트가 **[자동 설치] / [ZIP 다운로드] / [나중에]** 3버튼
다이얼로그를 띄운다. 서버는 방식을 강제하지 않는다 — 사용자가 매번 고른다.

| 버튼 | 동작 |
|---|---|
| 자동 설치 | 다운로드 후 앱 폴더를 교체하고 재실행 (아래 auto 흐름). 설치 폴더에 쓰기 권한이 없으면 이 버튼은 비활성 |
| ZIP 다운로드 | 새 버전 ZIP을 사용자 다운로드 폴더에 저장하고 탐색기로 열어줌. 설치는 사용자가 수동 (Honey 종료 → 압축 해제 → 설치 폴더 덮어쓰기 → 재실행) |
| 나중에 | 아무 것도 하지 않음 |

## 업데이트 흐름

1. Honey 실행 후 `/honey/version`을 조회한다.
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

## version.json 필드

- `version`: 클라이언트가 비교하는 semver
- `file`: `/honey/download`가 서빙할 ZIP 파일명
- `sha256`: 다운로드 무결성 검증값
- `released_at`: 배포 시간
- `notes`: 릴리스 설명
- `url`: 선택 필드. 없으면 클라이언트는 `/honey/download`로 폴백한다.

## 주의 사항

- 실행 중인 `Honey.exe`를 직접 덮어쓰지 않는다. 외부 배치 파일이 프로세스 종료 후 복사한다.
- UAC 승격은 사용하지 않는다. 설치 폴더가 쓰기 불가면 auto 라도 manual 로 강등된다.
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
