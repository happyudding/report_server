# storage_gateway — 서버 산출물 저장소 진입점

> `ENTRYPOINT / EXTERNAL_OWNER`. 리포트 산출물(이슈 이미지·분포 PNG·web_report
> parquet/manifest)을 **S3 + 로컬 폴백**으로 저장/조회하는 서버 측 단일 경계.
> **외부 S3/서버 저장소 담당자는 이 패키지만 보면 된다.**
>
> **S3 는 외부 프로젝트 경계이며, 현재 코드에서는 검증용이다.** 기본 구현은 Honey.exe
> 호환성 테스트를 위해 S3 + 로컬 폴백 동작을 보존한다 — `REPORT_S3_BUCKET` 미설정 시
> 산출물은 `REPORT_UPLOAD_DIR` 로컬에 저장되고 조회도 로컬을 따른다(§4). 실서버 S3 로
> 전환하려면 환경변수만 채우거나 `_s3.py` 를 교체한다(§5).

## 1. 이 패키지가 단일 진입점인 이유

프로젝트의 나머지 코드(`upload_xlsx.py`, `report/report_routes.py`,
`storage_gateway/routes.py`)는 **이 패키지의 공개 함수와 예외에만 의존**한다.
내부 boto3 어댑터(`_s3`)나 이미지 백엔드(`_issue_images`)를 직접 import 하지 않는다.
따라서 저장 백엔드를 갈아끼울 때 **이 패키지 내부만 수정**하면 되고, 호출부는 건드릴
필요가 없다.

```
프로젝트 코드  ──▶  storage_gateway (facade)  ──▶  _s3 / _issue_images (내부 구현)
 (호출부)            ↑ 여기까지만 의존              ↑ 외부 담당자 영역
```

## 2. 공개 API (facade = `__init__.py`)

| 함수 | 용도 |
|------|------|
| `save_upload_artifacts(*, analysis_key, content_hash, meta_str, issue_images=None, dist_png=None, chart_pngs=None)` | 업로드 1건의 이미지 산출물 저장(원본 xlsx 는 받지 않음). `{s3_ok, warnings, issue_images_saved, distribution_combined, charts_saved}` 반환 |
| `save_distribution_png(analysis_key, content_hash, meta_str, data, s3_ok=True, warnings=None)` | 합성 분포 PNG 저장(S3 실패 시 로컬 폴백) |
| `load_json_object(objects, object_type)` | object_info 맵에서 JSON 다운로드. 실패 시 `None` |
| `load_chart_png(analysis_key, idx)` | 차트 PNG bytes |
| `load_distribution_png(analysis_key)` | 합성 분포 PNG bytes(S3→로컬 순) |
| `list_issue_image_rows(analysis_key)` | 이슈 이미지가 있는 행 인덱스 리스트 |
| `load_issue_image(analysis_key, row)` | 행별 이슈 이미지 bytes |
| `save_webreport_sources(analysis_key, content_hash, sources, manifest, upload_root)` | web_report parquet 원본 리스트 + manifest 저장. `{storage:"s3"\|"local", warnings}` 반환 |
| `load_webreport_sources(analysis_key, upload_root)` | web_report parquet + manifest 재조회 → `(list[bytes], manifest dict)` |
| `load_webreport_manifest(analysis_key, upload_root)` | manifest 만 재조회 (parquet 다운로드 없음) |
| `save_webreport_manifest(...)` | manifest 만 갱신. **현재 미사용**(manifest 불변화) — 외부 통합 호환용 API 만 유지 |
| `delete_report_artifacts(analysis_key, upload_root=None)` | akey 산출물 삭제 (S3 오브젝트 + 로컬 폴백 파일, best-effort) |

**저장 위치 기록 계약 (중요)**: web_report 산출물은 저장 위치를
`report_object_info.options_json` 에 `{"storage":"s3"|"local"}` 로 기록하고, **조회는 그
기록을 따른다**. 기록이 `s3` 인데 다운로드가 실패하면 침묵 로컬 폴백 대신 예외를 올린다
(S3 순단 중 로컬 저장 → 복구 후 과거 S3 파일 부활 방지). 기록 없는 legacy 행(`''`)만 종전
폴백(경고 로그 후 로컬)을 유지한다. manifest 는 업로드 후 불변 — 편집은 세션 편집
DB(`report_webreport_edit`)에 저장된다(§2 [../CLAUDE.md](../../CLAUDE.md)).

예외(역시 facade 에서 재노출 — 호출부는 `from storage_gateway import S3NotConfigured`):
- `S3NotConfigured` — `REPORT_S3_BUCKET` 미설정. 호출부가 그레이스풀 폴백.
- `S3ObjectCorrupted` — 다운로드 JSON 파싱 실패.

이미지 URL 라우트(`/pe/report/chart/...`, `/issue_image/...`,
`/distribution_combined/...`)는 [routes.py](routes.py) 에 있으며 **공개 URL 계약은 불변**.

## 3. 내부 모듈 (외부 담당자가 교체하는 영역)

| 모듈 | 역할 |
|------|------|
| [_s3.py](_s3.py) | boto3 호환 client(`get_s3_client`) + 버킷/키 빌더(`make_*_s3_key`) + 입출력(`upload/download_bytes/json`) + 예외. **저장 백엔드 교체 시 1차 대상** |
| [_issue_images.py](_issue_images.py) | 이슈 이미지 저장/조회. S3 설정 시 S3, 아니면 `REPORT_UPLOAD_DIR/issue_img/` 로컬 폴백 |
| [_png_drive.py](_png_drive.py) | 외부 프로젝트(`S3/s3_drive.py`)와 동일 시그니처의 PNG 헬퍼 스캐폴드. **현재 어디서도 import 되지 않음**(브랜치 시 호출부 활성화용) |

내부 모듈끼리는 **상대 import**(`from . import _s3 as report_s3`). 공유 top-level 모듈
(`config`, `database`)은 절대 import 유지.

## 4. 설정 (server/config.py)

자격증명/엔드포인트:
```
REPORT_S3_BUCKET       필수 (비우면 S3NotConfigured → 로컬 폴백)
REPORT_S3_ENDPOINT     호환 스토리지 endpoint (AWS면 비움)
REPORT_S3_REGION       기본 us-east-1
REPORT_S3_ACCESS_KEY   비우면 boto3 기본 자격증명
REPORT_S3_SECRET_KEY
REPORT_S3_MAX_POOL_CONNECTIONS  기본 30
```

키 prefix(모두 `pe/report_server/` 네임스페이스, plotly legacy 와 충돌 회피):
```
issue_img/<akey>/<row>.png            issue_img/<akey>/index.json
chart_png/<akey>/<idx>.png            chart_png/<akey>/index.json
distribution_combined/<akey>.png
web_report_source/<akey>/source_<idx>.parquet
web_report_manifest/<akey>/manifest.json
```
텍스트 JSON(`summary_text`/`issue_table_text`/`yield_text`)은 보관하지 않는다 — 텍스트는
DB(`report_sheet_data`)에 저장한다. legacy 세션의 기존 S3 텍스트 객체는
`report_object_info.s3_key` 로 계속 읽는다.
prefix 별 환경변수(`REPORT_S3_*_PREFIX`)는 [config.py](../config.py) 참조 (web_report·
distribution_combined prefix 는 [_s3.py](_s3.py) 상수).

## 5. 외부 담당자 교체 가이드

1. **다른 S3 호환 스토리지**: 환경변수만 채우면 코드 변경 없이 동작.
2. **저장 구현 자체 교체**(예: 다른 SDK/스토리지): `_s3.py` 의 함수 시그니처를 유지한
   채 본문만 갈아끼우면 facade·호출부 무수정. 입출력 계약(`upload_bytes_to_s3`,
   `download_bytes_from_s3`, `upload_json_to_s3`, `download_json_from_s3`,
   `s3_object_exists`, `make_*_s3_key`, `bucket_name`, 예외)을 그대로 노출할 것.
3. **facade 시그니처 변경 금지**: 위 공개 함수의 이름/인자가 호출부 계약이다.

## 6. 불변 규칙 (반드시 보존)

- `/pe/report/...` 이미지 URL·multipart 필드·응답 JSON 형태 **불변**.
- `analysis_key` = `sha256(canonical(sheet_grids) + canonical(meta))`. 저장 경로/키는 바꿔도
  **analysis_key 산출은 건드리지 말 것**.
- **Distribution 차트 데이터 다운샘플링 절대 금지** (프로젝트 CLAUDE.md 규칙 #6).
