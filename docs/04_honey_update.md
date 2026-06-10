# 04 - Honey 자동 업데이트 채널

Honey 업데이트는 PyInstaller onedir 결과물(`client/dist/Honey/`)을 ZIP으로 묶어
배포한다. 별도 설치 프로그램은 사용하지 않는다.

## 관련 파일

- 서버: `server/honey_routes.py`, `server/releases/version.json`
- 클라이언트: `client/transport/version_check.py`, `client/transport/updater.py`,
  `client/honey_main.py`
- 빌드/배포: `client/build_honey.spec`, `client/build_zip.bat`,
  `client/release/release_honey.ps1`, `client/release/RELEASE_GUIDE.txt`

## 업데이트 흐름

1. Honey 실행 후 `/honey/version`을 조회한다.
2. 서버는 `server/releases/version.json`을 그대로 반환한다.
3. 클라이언트는 `version`과 빌드에 포함된 `CURRENT_VERSION`을 비교한다.
4. 새 버전이면 `/honey/download`에서 `Honey-<version>.zip`을 다운로드하고 sha256을 검증한다.
5. frozen exe에서 실행 중이면 `updater.apply_update_zip()`이 ZIP을 임시 폴더에 푼다.
6. 외부 배치 파일이 현재 Honey 프로세스 종료를 기다린 뒤 앱 폴더에 새 파일을 복사하고
   `Honey.exe`를 다시 실행한다.

개발 모드(`python honey_main.py`)에서는 ZIP 다운로드까지만 수행하고 자동 교체는 하지 않는다.

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
- `CURRENT_VERSION`은 빌드 전에 반드시 올려야 한다. 순서가 틀리면 클라이언트가 계속 업데이트를 권유할 수 있다.
- `version.json`은 BOM 없는 UTF-8로 저장한다. `release_honey.ps1`은 자동으로 그렇게 저장한다.
