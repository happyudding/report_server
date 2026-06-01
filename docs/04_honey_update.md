# 04 · Honey 자동 업데이트 채널 (서버 배포 + 클라 설치)

> Honey exe 가 시작할 때 서버에 새 버전을 묻고, 있으면 받아서 조용히 재설치하는 독립 채널.
> 업로드/분석과 완전 분리. 서버측 `honey_bp`, 클라측 `version_check`+`updater`.

## 파일
- 서버: [server/honey_routes.py](../server/honey_routes.py), [server/releases/version.json](../server/releases/version.json)
- 클라: [client/version_check.py](../client/version_check.py), [client/updater.py](../client/updater.py), [client/honey_main.py `check_for_update`](../client/honey_main.py#L715), [client/config.py `CURRENT_VERSION`](../client/config.py#L11)
- 빌드/배포: [client/build_honey.spec](../client/build_honey.spec), [client/installer.iss](../client/installer.iss), [client/release/release_honey.ps1](../client/release/release_honey.ps1), [client/release/RELEASE_GUIDE.txt](../client/release/RELEASE_GUIDE.txt)

## 흐름
1. **클라 기동** → `QTimer.singleShot(500, check_for_update)` ([honey_main.py:338](../client/honey_main.py#L338)).
2. `version_check.fetch_latest()` → `GET /honey/version` → server 가 `version.json` 그대로 반환(요청마다 재읽기, 서버 재시작 불필요).
3. `is_newer(remote, CURRENT_VERSION)` — semver 튜플 비교. `CURRENT_VERSION` 은 **exe 에 컴파일되어 박힌 값** ([config.py](../client/config.py#L11)).
4. 새 버전이면 사용자 확인 → `download_to(dest, url, expected_sha256, progress_cb)`:
   - `url` = manifest `url` 또는 폴백 `/honey/download`. 서버는 `version.json.file` 이름으로 `HONEY_RELEASES_DIR` 에서 exe 서빙([download_exe](../server/honey_routes.py#L35), 경로 traversal 차단).
   - 스트리밍 저장 + sha256 검증(manifest `sha256` 비어있으면 검증 skip). 취소 시 `DownloadCancelled`.
5. `updater.is_frozen()` 이면 `updater.run_installer(dest)` — Inno Setup 설치본을 `/SILENT /SUPPRESSMSGBOXES /NOCANCEL` + DETACHED 로 띄우고 `QApplication.quit()`. 설치본이 폴더 전체(_internal 포함) 교체 후 [Run] 으로 Honey 자동 재실행. 개발 모드(`python honey_main.py`)면 다운로드만.

## 배포 절차 ([release_honey.ps1](../client/release/release_honey.ps1))
`.\release_honey.ps1 -Version 0.2.0 -Notes "..."` 가 5단계 자동:
1. [config.py](../client/config.py) `CURRENT_VERSION` 교체 (**빌드 전 필수** — 안 하면 새 exe 가 자기 자신을 또 업데이트 권유).
2. `pyinstaller build_honey.spec` → onedir `dist/Honey/`.
3. `server/releases/` 로 버전명 복사.
4. `sha256` 계산.
5. `version.json` 갱신 (**BOM 없는 UTF-8** — 서버 `json.loads` 가 BOM 에 실패).

## version.json 필드
`version`(semver), `file`(서빙 파일명, `/ \ .` 선행 금지), `sha256`(빈값=검증 skip), `released_at`, `notes`, `url`(선택, CDN 외부호스팅용).

## 주의 / 함정
- **빌드 순서**: `CURRENT_VERSION` → 빌드 → 배포. 순서 틀리면 무한 업데이트 권유.
- **frozen 에서만 자동 교체**. 스크립트 실행은 다운로드까지만.
- 실행 중 exe 직접 덮어쓰기 금지(Windows 락) — 그래서 "설치본 재설치" 방식 (불변 규칙 §5).
- **문서/코드 불일치 주의**: [RELEASE_GUIDE.txt](../client/release/RELEASE_GUIDE.txt) 본문 일부는 옛 "단일 exe + updater.bat 교체" 서술이 남아있으나, 실제 [updater.py](../client/updater.py) 는 Inno Setup 설치본(`HoneySetup-*.exe`) `/SILENT` 재설치 방식. 현 진실은 updater.py + installer.iss.
- 구버전 exe 는 남겨도 됨(version.json 이 가리키는 것만 서빙) → 롤백은 version.json 만 되돌림.
