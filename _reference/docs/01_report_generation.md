# 01. Report 생성 블록

`/pe/report` 모듈의 핵심 — CSV 업로드 → 분석 → DB 저장 → S3 저장 → 결과 페이지 표시 흐름.
원본 CSV, 통계 요약, Plotly JSON, SVG 썸네일을 **세 저장소**(파일/DB/S3)에 분리 저장하여 같은 입력은 재계산하지 않는다.

---

## 1. 책임 분리

| 파일 | 역할 |
|------|------|
| [report_extension.py](../report_extension.py) | Blueprint(`/pe/report`) 정의 + DB init |
| [report_routes.py](../report_routes.py) | HTTP 엔드포인트 (analyze, plot, result, history, annotation 등) |
| [report_analysis_service.py](../report_analysis_service.py) | analysis_key 산출, summary/issue_table/fail_items 빌드, 락 흐름 |
| [report_plot_service.py](../report_plot_service.py) | Plotly JSON 생성·캐시 흐름 (S3 우선) |
| [table_builder.py](../table_builder.py) | yield/cpk/fail_items raw 빌더 (모든 service 가 의존) |
| [chart_payload.py](../chart_payload.py) + [figure_builder.py](../figure_builder.py) | Plotly trace/layout dict 생성 |
| [svg_builder.py](../svg_builder.py) | per-subject 정적 SVG 썸네일 |
| [dataset_builder.py](../dataset_builder.py) | cumulative dashboard 빌드 (별도 흐름, /upload, /api/<id> 시 사용) |
| [preprocess.py](../preprocess.py) | `to_numeric_clean`, `cumulative_distribution_full` |
| [data_loader.py](../data_loader.py) | CSV → `ExcelData(subjects, units, lo_limits, hi_limits, scores, meta)` |
| [report_view.html](../report_view.html) / [report_analysis_index.html](../report_analysis_index.html) | 분석 결과 UI |

---

## 2. 키 개념: session_id vs analysis_key

- **session_id**: 사용자가 분석 버튼을 누른 1회의 실행 기록. `time + token_hex(3)` 으로 매번 새로 생성.
- **analysis_key**: `sha256(content_hash + ":" + options_json)`. 파일 내용 + 옵션이 같으면 동일.
  - `content_hash`: 정렬된 파일들의 (이름 + null + 본문 + null) 스트리밍 sha256 ([report_analysis_service.py:46-58](../report_analysis_service.py#L46-L58))
  - `options_json`: `json.dumps(options, sort_keys=True, ensure_ascii=False, separators=(",", ":"))`
- **재사용 규칙**: session_id 가 다르더라도 analysis_key 같으면 summary/Plotly/fail_items/SVG 모두 재사용.

---

## 3. analyze 흐름 — `POST /pe/report/analyze`

라우트 본체: [report_routes.py:77-150](../report_routes.py#L77-L150)
서비스 본체: [report_analysis_service.py:350-429](../report_analysis_service.py#L350-L429)

1. multipart `files` 수신, `options` 파싱, `product_type` 추출
2. session_id 생성, `uploads/report/<session_id>/` 만들어 CSV 저장 (path traversal 차단)
3. `report_db.create_session(...)` — status=`pending`
4. `get_or_compute_analysis(session_id, saved_paths, options)`
   - `hash_files_streaming` → `content_hash`
   - `normalize_options` → `options_json`
   - `compute_analysis_key` → `analysis_key`
   - `report_db.update_session(analysis_key, content_hash, status="running")`
   - **캐시 hit**: `report_db.has_summary(analysis_key)` 면 즉시 `reused=True` 반환
   - **lock 획득**: `try_acquire_analysis_lock` 실패 시 `_wait_for_summary` (60s, 0.5s poll)
   - lock 안에서 캐시 재확인 (race 회피)
   - `schools = {p.stem: load_table(p) for p in ...}` 로 분석 객체 생성
   - `build_summary_rows(schools)` → `save_summary_batch` (executemany)
   - `release_analysis_lock` (finally)
5. `_upload_csvs_to_s3(saved_paths, analysis_key)` — 원본 CSV S3 업로드 + `report_csv_files` 기록
6. `upload_derived_if_absent(analysis_key, ...)` — fail_items / issue_table / 필요한 SVG 만 S3 업로드
7. `shutil.rmtree(session_dir)` — 로컬 임시 CSV 삭제 (S3 가 원본 소유)
8. JSON 응답: `session_id, analysis_key, reused, status, summary`

> **주의**: `_upload_csvs_to_s3` 후 로컬 CSV 가 사라지므로, plotly 생성 시 CSV 가 필요하면 S3 에서 받아 복원해야 한다. 현재 `report_plot_service._resolve_inputs_for_analysis` 는 `report_session.file_path` 로 로컬 디렉토리를 본다 — 동일 세션 내 plot 호출은 가능하지만, 다른 session 에서 plot 만 호출하면 CSV 가 없을 수 있다.

---

## 4. summary 행 구조 — `build_summary_rows`

[report_analysis_service.py:117-185](../report_analysis_service.py#L117-L185)

세 종류 행을 `report_analysis_summary` 한 테이블에 적재:

| 종류 | item_name | bin_number | 채워지는 컬럼 |
|------|-----------|-----------|--------------|
| per-subject overall | 과목명 | NULL | yield_percent, fail_count, cpk_val, mean_val, stdev_val, lsl, usl, unit |
| per-bin × subject | 과목명 | bin 번호 | yield_percent(=portion%), fail_count |
| bin 전체 | `__bin_total__` | bin 번호 | yield_percent, fail_count |

`UNIQUE(analysis_key, item_name, bin_number)` 로 중복 방지, `INSERT OR IGNORE` 사용.

---

## 5. plot 흐름 — `POST/GET /pe/report/plot`

서비스: [report_plot_service.py:134-187](../report_plot_service.py#L134-L187)

1. `s3_key = make_plotly_s3_key(analysis_key)` (= `pe/report/plotly/{key}.json`)
2. **DB 캐시 hit**: `report_db.get_object_info(analysis_key, 'plotly')` + `s3_object_exists` 면 즉시 다운로드 반환
3. **S3 corrupted**: `delete_s3_object_if_corrupted`, 재생성 경로로 진입
4. **lock**: `_wait_for_object_or_lock` (60s)
5. lock 안에서 입력 복원 → CSV 정합성 검증 (`compute_analysis_key(content_hash, options_json) == analysis_key`)
6. `_build_plotly_for_analysis` 로 subject 별 payload 생성 (scatter sample 옵션 지원)
7. `upload_json_to_s3` + `upsert_object_info('plotly', ...)`
8. release lock (finally)

옵션 형식: `{"scatter": {"sample": 5000}}` — 시각화용 다운샘플링. `report_object_info.options_json` 에 보존되어 후속 호출 시 자동 사용.

---

## 6. 파생 데이터 (fail_items / issue_table / SVG)

`upload_derived_if_absent`: [report_analysis_service.py:286-334](../report_analysis_service.py#L286-L334)

- 3종 모두 `report_object_info` 의 별도 `object_type` 으로 관리 (`fail_items`, `issue_table`, `thumbs_fail_set`)
- **fail_items**: `_build_fail_items(schools)` — student_type 별 비합격 과목 카운트 JSON
- **issue_table**: `_build_issue_table(schools)` — 비합격 학생 × 과목별 측정값 / lo / hi / fail 방향 레코드
- **SVG 썸네일**: fail_subjects 에 등장하는 subject_id 만 (수십 개) `ThreadPoolExecutor` 로 병렬 업로드
  - `REPORT_THUMB_WORKERS` 환경변수로 워커 수 조절 (기본 8)
  - 키 패턴: `pe/report/thumbs/{analysis_key}/{subject_id}.svg`
- 셋 다 이미 `object_info` 에 있으면 스킵 — load_table 자체를 안 한다.

---

## 7. 조회 / 복원 엔드포인트

| Method · Path | 설명 |
|---------------|------|
| `GET /pe/report/result/<session_id>` | 세션 + summary 반환 |
| `GET /pe/report/session/<session_id>` | session row 만 반환 |
| `GET /pe/report/session/<session_id>/full` | 세션 완전 복원 (session + summary + csv_files + objects + annotations + thumb URL 템플릿) |
| `GET /pe/report/csv/<analysis_key>` | S3 CSV 목록 |
| `GET /pe/report/fail_items/<analysis_key>` | S3 → JSON 다운로드 |
| `GET /pe/report/issue_table/<analysis_key>` | S3 → JSON 다운로드 |
| `GET /pe/report/thumb/<analysis_key>/<sid>` | S3 → SVG 응답 (24h immutable cache) |
| `GET /pe/report/api/history` | report_session + csv_files 합산 + 필터 (product_type/process/product/revision) |
| `POST/PATCH/DELETE /pe/report/annotation` | 사용자 코멘트 CRUD (session_id 기준) |
| `POST /pe/report/execute-debug` | `data/*_school_updated_call.csv` 로 전체 파이프라인 실행 (dashboard 빌드 + analyze) |

---

## 8. 동시 요청 / 락

`report_analysis_lock` 테이블에 `(analysis_key, owner, locked_at, expires_at)` 저장.
- TTL: 300s (`REPORT_LOCK_TTL_SEC`)
- 만료 락은 acquire 시점에 `DELETE WHERE expires_at <= now()` 로 청소
- analyze / plot 양쪽에서 같은 락 키 공유 — 동일 analysis_key 동시 분석 방지

---

## 9. 보안 / 검증

- `_validate_analysis_key`: `^[0-9a-f]{64}$` 정규식
- `_validate_session_id`: `^[A-Za-z0-9_-]{1,80}$`
- `_resolve_under_upload_dir`: 모든 업로드 경로가 `REPORT_UPLOAD_DIR` 하위인지 `Path.resolve().relative_to()` 로 확인
- CSV 확장자 화이트리스트 (`.csv`)

---

## 10. cumulative dashboard 빌드 (`/upload`, `/view/<id>`)

`/pe/report` 와 별개 흐름, [server.py](../server.py) + [dataset_builder.py](../dataset_builder.py).
- 2000개 과목 × 3 school CSV → `output/datasets/<id>/charts/<idx>.json` + `thumbs/<idx>.svg` + `cumulative.html`
- 백그라운드 스레드(`_bg_build`)에서 빌드, `_build_status` 로 폴링
- HTML 페이지가 priming(2000셀 SVG 사전 렌더) 후 IndexedDB 캐싱하는 클라이언트 위주 설계
- `/pe/report/execute-debug` 가 이 빌드와 `/pe/report` analyze 를 동시에 트리거

> 이 빌드 결과(`output/datasets/<id>/`)는 S3 가 아니라 **로컬 디스크**에 영구 저장된다. report 모듈의 S3 흐름과는 별개.
