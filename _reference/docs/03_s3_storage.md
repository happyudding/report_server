# 03. S3 저장소 블록

원본 CSV, Plotly 전체 JSON, fail_items JSON, issue_table JSON, per-subject SVG 썸네일을 모두 S3 호환 객체 저장소에 넣는다. boto3 호환 endpoint 가정 (AWS S3 / MinIO / 호환 서비스).

- **단일 모듈**: [report_s3.py](../report_s3.py)
- **호출자**: [report_analysis_service.py](../report_analysis_service.py), [report_plot_service.py](../report_plot_service.py), [report_routes.py](../report_routes.py)
- **DB 인덱스**: [report_db.py](../report_db.py) 의 `report_object_info`, `report_csv_files` (→ [02_database.md](02_database.md))

---

## 1. 설정 (env vars → config)

[config.py:34-46](../config.py#L34-L46) 에서 환경변수 → 모듈 상수로 import.

| 환경변수 | 기본값 | 용도 |
|----------|--------|------|
| `REPORT_S3_ENDPOINT` | "" | S3 호환 endpoint URL. 비우면 AWS S3 |
| `REPORT_S3_BUCKET` | "" | **필수**. 비우면 `S3NotConfigured` |
| `REPORT_S3_REGION` | `us-east-1` | |
| `REPORT_S3_ACCESS_KEY` / `REPORT_S3_SECRET_KEY` | "" | 비우면 boto3 기본 자격증명 (IAM role 등) |
| `REPORT_S3_PREFIX` | `pe/report/plotly` | Plotly JSON |
| `REPORT_S3_CSV_PREFIX` | `pe/report/origin_csv_files` | 원본 CSV |
| `REPORT_S3_FAIL_PREFIX` | `pe/report/fail_items` | fail_items JSON |
| `REPORT_S3_ISSUE_PREFIX` | `pe/report/issue_table` | issue_table JSON |
| `REPORT_S3_THUMB_PREFIX` | `pe/report/thumbs` | SVG 썸네일 |
| `REPORT_THUMB_WORKERS` | `8` | SVG 동시 업로드 스레드 수 |

---

## 2. 클라이언트 초기화

```python
def get_s3_client():
    _require_config()                       # bucket 비면 S3NotConfigured
    config = Config(signature_version="s3v4", retries={"max_attempts": 3})
    kwargs = {"region_name": REGION, "config": config}
    if ENDPOINT:        kwargs["endpoint_url"] = ENDPOINT
    if ACCESS_KEY+SECRET_KEY: kwargs["aws_access_key_id"] = ..., ...
    return boto3.client("s3", **kwargs)
```

[report_s3.py:33-54](../report_s3.py#L33-L54). 모듈 전역 `_client` 에 캐시 — 첫 호출 시 1회만 생성.

---

## 3. 키 빌더 함수

객체 종류별 결정적 키 패턴. SHA-256 64자 `analysis_key` 가 디렉토리 분리자.

```python
make_plotly_s3_key(akey)         # pe/report/plotly/{akey}.json
make_csv_s3_key(akey, filename)  # pe/report/origin_csv_files/{akey}/{filename}
make_fail_items_s3_key(akey)     # pe/report/fail_items/{akey}.json
make_issue_table_s3_key(akey)    # pe/report/issue_table/{akey}.json
make_thumb_s3_key(akey, sid)     # pe/report/thumbs/{akey}/{int(sid)}.svg
make_thumb_prefix_key(akey)      # pe/report/thumbs/{akey}/   (객체 아님, prefix)
make_s3_uri(key)                 # s3://{bucket}/{key}
```

---

## 4. 업로드 / 다운로드 API

### JSON
```python
upload_json_to_s3(key, data)        # dict|list|str|bytes → utf-8 compact JSON
                                    # ContentType: application/json; charset=utf-8
                                    # 반환: s3://bucket/key
download_json_from_s3(key)          # → dict/list. UTF-8 디코딩 실패 시 S3ObjectCorrupted
```

### Bytes (CSV, SVG 등 범용)
```python
upload_bytes_to_s3(key, data, content_type="application/octet-stream")
download_bytes_from_s3(key)         # raw bytes
```

CSV 업로드는 `text/csv; charset=utf-8`, SVG 는 `image/svg+xml; charset=utf-8` 명시.

### 존재 확인 / 손상 시 삭제
```python
s3_object_exists(key)               # head_object, 404 면 False, 그 외는 raise
delete_s3_object_if_corrupted(key)  # silent best-effort
```

---

## 5. 예외 두 종류

```python
class S3NotConfigured(RuntimeError):   # bucket 미설정 / boto3 미설치
class S3ObjectCorrupted(RuntimeError): # JSON 디코딩 실패
```

- `S3NotConfigured` → 라우트 레벨에서 503 + 에러 메시지. analyze 흐름은 `pass` (S3 없어도 분석은 진행).
- `S3ObjectCorrupted` → 플롯 서비스가 `delete_s3_object_if_corrupted` 후 재생성으로 진입.

---

## 6. 라이프사이클

### 6.1 analyze 시 (upload)

[report_routes.py:64-74](../report_routes.py#L64-L74), [report_analysis_service.py:286-334](../report_analysis_service.py#L286-L334).

```
analyze 라우트
  └─ _upload_csvs_to_s3
        ├─ s3_object_exists(csv_key) 면 스킵
        └─ upload_bytes_to_s3 + report_db.upsert_csv_file

  └─ upload_derived_if_absent (report_object_info 에 없을 때만)
        ├─ fail_items JSON     → upload_json_to_s3 + upsert_object_info('fail_items')
        ├─ issue_table JSON    → upload_json_to_s3 + upsert_object_info('issue_table')
        └─ SVG (fail subjects 만, ThreadPoolExecutor)
              ├─ build_payload + build_subject_svg
              ├─ upload_bytes_to_s3(svg_key, ...)
              └─ 끝나면 upsert_object_info('thumbs_fail_set', prefix_key=...)
```

> SVG 는 *세트 단위* 로 `thumbs_fail_set` 1행만 기록. 각 subject 별 row 는 만들지 않음. UI 는 `make_thumb_s3_key(akey, sid)` 로 직접 GET 한다.

### 6.2 plot 시 (cache-first)

[report_plot_service.py:134-187](../report_plot_service.py#L134-L187).

```
get_or_create_plot(analysis_key, ...):
  1. info = get_object_info(akey, 'plotly')
  2. info && s3_object_exists(info.s3_key)
       → download_json_from_s3 + touch_object_info → return
  3. S3ObjectCorrupted: delete_s3_object_if_corrupted + 재생성 흐름
  4. lock 획득 or 대기 (다른 워커 결과 재사용)
  5. lock 내에서 또 캐시 확인 (race)
  6. CSV 입력 복원 + analysis_key 정합성 재검증
  7. _build_plotly_for_analysis → upload_json_to_s3 + upsert_object_info('plotly')
```

### 6.3 다운로드 라우트

```
GET /pe/report/thumb/<akey>/<sid>     → download_bytes_from_s3 + cache 24h
GET /pe/report/fail_items/<akey>      → download_json_from_s3 + touch
GET /pe/report/issue_table/<akey>     → download_json_from_s3 + touch
GET /pe/report/plot/<akey>            → get_or_create_plot (위 흐름)
```

---

## 7. 운영 노트

- **Plotly JSON 본문은 절대 DB 에 저장하지 않는다.** 위치만 `report_object_info.s3_key`.
- **원본 CSV 도 S3 가 유일한 영속 저장소** — analyze 종료 시 `uploads/report/<sid>/` 는 `shutil.rmtree` 로 삭제됨.
- **로컬 캐시 파일 만들지 않음**. 모든 캐시는 (DB row 존재) + (S3 객체 존재) 두 단계로 검증.
- **JSON 직렬화 규칙**: `ensure_ascii=False, separators=(",", ":")` — compact + utf-8.
- **SVG 동시 업로드 워커 수**: `REPORT_THUMB_WORKERS` 로 조절. 4GB RAM 환경에서 너무 높이면 메모리 압박.
- **boto3 미설치 환경 호환**: import 실패도 `S3NotConfigured` 로 변환되어 analyze 분석 자체는 계속 진행 (UI 일부 기능 제한).
- **endpoint_url 사용 시 MinIO / Wasabi / R2 등 호환 서비스 가능**. signature_version=s3v4 고정.

---

## 8. 디버깅 팁

```python
# 객체 한 번에 모두 보기
from report_db import get_all_object_infos
get_all_object_infos(analysis_key)
# → [{object_type: 'plotly', s3_key: '...', last_accessed: ...}, ...]

# 클라이언트 강제 재초기화 (env 변경 후)
import report_s3; report_s3._client = None
```

S3 환경변수 미설정 시: `_upload_csvs_to_s3` / `upload_derived_if_absent` 가 `S3NotConfigured` 를 잡고 `pass` — 분석은 성공 응답하지만 후속 plot 호출은 503 반환.
