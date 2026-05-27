# Time Reduction — 5인 동시 시나리오 병목 분석 및 개선 계획

> **목적**: 동시 사용자 5명, 각자 3 × 25MB CSV 입력 시 응답 시간 단축안을 정리한다.
> 알고리즘/스키마/캐시 키 무변경, 사용자간 대기(락/세마포어) 절대 금지 원칙.
>
> 작성 시점 코드 기준: folder 재구성 직후 (`260526 after folder tree` / commit `f14123f`).

---

## 1. 시나리오 가정

- **서버 스펙**: 4–8 core CPU / 16GB RAM / SSD / 1Gbps NIC
- **S3 endpoint**: ~100MB/s 집계, boto3 client `max_pool_connections=30` (적용됨)
- **클라이언트**: 5명 동시 시작, 각자 3 × 25MB CSV (75MB/사용자, 375MB 집계)
- **입력 규모 (추정)**: ~2000 subject, ~4500 DUT
- **서버 모델**: Flask `app.run(threaded=True)` 단일 프로세스 다중 스레드
- **요청 경로**: `POST /pe/report/execute` → 백그라운드 thread A (`_run_analysis`) + thread B (`build_dataset`) 가 **병렬** 실행 → 둘 다 끝나면 임시 dir 삭제

---

## 2. Block 별 견적 (단일 사용자 → 5명 동시)

| # | Block | 위치 | 단일 (s) | 5명 동시 (s/사용자) | 병목 원인 |
|---|-------|------|----------|---------------------|-----------|
| 1 | HTTP multipart upload + 디스크 저장 | [report_routes.py:484-491](../report/report_routes.py#L484-L491) | 1.5 | 4 | NIC 대역 (375MB / 1Gbps ≈ 3s) |
| 2 | session insert + sha256 hash + analysis_key | [report_db.py:208-215](../database/report_db.py#L208-L215), [report_analysis_service.py:46-58](../report/report_analysis_service.py#L46-L58) | 0.2 | 0.5 | hash 75MB ~150ms, DB INSERT WAL ms |
| 3 | `load_table × 3` (pandas read_csv) — **1차 로드** | [report_analysis_service.py:521](../report/report_analysis_service.py#L521) | 5 | 10 | **CPU bound** — pandas wide-CSV 단일스레드 |
| 4 | `build_summary_rows` (cpk + yield + fail_items + fail_mask 사전캐시) | [report_analysis_service.py:190-277](../report/report_analysis_service.py#L190-L277) | 8 | 12 | **CPU bound** — 2000 subject × 3 school |
| 5 | `save_summary_batch` (~3000 row executemany) | [report_db.py:336-357](../database/report_db.py#L336-L357) | 0.2 | 1 | SQLite writer 직렬화 ~ms 누적 |
| 6 | `_upload_csvs_to_s3` (3 × 25MB PUT 순차) | [report_routes.py:64-74](../report/report_routes.py#L64-L74) | 2 | 4 | **S3/NIC 대역** |
| 7 | `upload_derived_if_absent` → **2차 CSV 로드** + `_build_issue_table` | [report_analysis_service.py:378-427](../report/report_analysis_service.py#L378-L427) | 8 | 14 | **재로딩 낭비 + CPU** |
| 8 | fail_items + issue_table JSON PUT | [report_analysis_service.py:401-416](../report/report_analysis_service.py#L401-L416) | 0.3 | 1 | JSON < 5MB, S3 PUT 2건 |
| 9 | `_upload_svgs_for_subjects` (~200 fail subject SVG 생성 + 8-thread PUT) | [report_analysis_service.py:345-376](../report/report_analysis_service.py#L345-L376) | 5 | 10 | SVG 빌드 CPU + S3 풀 |
| 10 | `build_dataset` — **3차 CSV 로드** + 2000 subject `cdf_svg` 단일스레드 루프 | [dataset_builder.py:58-182](../analysis/dataset_builder.py#L58-L182) | **100** | **180** | **압도적 1위** |

### 합계

- thread A (Block 2~9): 27s 단일 / 53s 5인동시
- thread B (Block 10): 100s 단일 / 180s 5인동시
- wall time per user ≈ Block 1 + max(A, B)

```
단일 사용자 :  1.5 + max(27, 100) = ~101 s
5명 동시  :    4   + max(53, 180) = ~184 s  (≈ 3 분)
```

체감 지연 거의 전부는 Block 10.

---

## 3. 개선안 우선순위 (모두 비-블로킹)

| 우선도 | 항목 | 예상 단축 | 변경 위치 | 위험도 |
|--------|------|-----------|----------|--------|
| 🔴 #1 | Block 10 cdf_svg loop ThreadPoolExecutor 병렬화 | 단일 100→35s, 5인 180→90s | [analysis/dataset_builder.py:123-153](../analysis/dataset_builder.py#L123-L153) | 낮음 |
| 🔴 #2 | Block 7 의 CSV 재로딩 제거 (schools 패스스루) | 5s/사용자 | [report/report_analysis_service.py:378-427](../report/report_analysis_service.py#L378-L427) + 호출부 | 낮음 |
| 🟡 #3 | `build_summary_rows` numpy 벡터화 | 2–3s/사용자 | [report/report_analysis_service.py:190-277](../report/report_analysis_service.py#L190-L277) | 중 |
| 🟢 #4 | 네트워크/디스크 — 코드로 해결 불가 | n/a | 인프라 | n/a |

#1 + #2 만 적용해도 **5인 동시 wall time ≈ 184s → ~90s** (절반 이하).

---

## 4. 🔴 #1 깊이 분석 — Block 10 cdf_svg loop 병렬화

### 4.1 현재 코드 골격

[analysis/dataset_builder.py:123-153](../analysis/dataset_builder.py#L123-L153)

```python
for idx in range(n_subjects):
    traces = []
    for name in names:
        xs, ys = cumulative_distribution_full(
            to_numeric_clean(schools[name].scores.iloc[:, idx])
        )
        traces.append({"school": name, "color": color_map[name], "xs": xs, "ys": ys})
    payload = build_payload(idx, first.subjects[idx], unit, lo, hi, traces)
    (charts_dir / f"{idx}.json").write_text(json.dumps(payload, **JSON_KWARGS), encoding="utf-8")
    chart_bytes += (charts_dir / f"{idx}.json").stat().st_size
    svg = build_subject_svg(idx, first.subjects[idx], unit, lo, hi, traces, payload["layout"])
    (thumbs_dir / f"{idx}.svg").write_text(svg, encoding="utf-8")
    svg_bytes += (thumbs_dir / f"{idx}.svg").stat().st_size
    if (idx + 1) % 10 == 0 or idx + 1 == n_subjects:
        _progress("charts+svg", idx + 1, n_subjects, progress_t0)
        emit("cdf_svg", idx + 1, n_subjects)
```

### 4.2 트레이드오프

#### 이득 폭은 부하 상황에 따라 다름

| 상황 | 현재 (직렬) | 4-worker 병렬 | 실질 단축 |
|------|------------|---------------|-----------|
| 1명 단독 | 100s | ~35s | **약 3배** ✅ |
| 5명 동시, 4코어 서버 | 180s | ~110–130s | 약 1.4–1.6배 |
| 5명 동시, 8코어 서버 | 180s | ~70–90s | 약 2배 |

이유: 직렬일 때 단독 사용자는 1코어만 사용 → 4코어 유휴 → 병렬화 효과 큼.
5명 동시는 직렬이라도 이미 5코어 사용 → 병렬화해도 CPU 자체가 한계.

#### GIL — Python 본질적 한계

| 작업 | GIL 해제? |
|------|----------|
| `cumulative_distribution_full` (numpy) | ✅ 해제 |
| `to_numeric_clean` (pandas) | ✅ 대부분 해제 |
| `json.dumps` (C 구현) | ✅ 해제 |
| 파일 write (디스크 I/O) | ✅ 해제 |
| `build_payload` (dict/list 조립) | ❌ 보유 |
| `build_subject_svg` (string concat / escape) | ❌ 보유 |

→ 약 50%만 GIL 해제. 이론치 4배가 아니라 실효 2.5–3배. 7-worker 이상은 의미 없음.

#### 공유 상태 race condition (코드 수정 필요한 부분)

현재 루프 안에서 누적되는 변수:
- `chart_bytes`, `svg_bytes` (파일 크기 누적)
- `cdf_s`, `payload_s`, `write_s`, `svg_s`, `write_svg_s` (5개 timing 변수)

병렬화 시 `+=` 는 race condition. 해결책 둘 중 하나:

1. worker 가 결과 튜플 반환 → 메인 스레드가 가산 (권장)
2. `threading.Lock` 으로 감싸기

#### 진행률 출력 순서

- subject 가 완성 순서대로 카운트 → 출력이 점프 (0/2000 → 4/2000 → 12/2000)
- ETA 계산은 정상 (counter 만 atomic 하면 됨)
- 디버깅 시 어느 subject 에서 죽었는지 추적이 살짝 번거로움 → 에러 시 subject_id 로그 박기

#### 디스크 I/O queue depth

- 5명 × 4 worker = 20 동시 write — 모두 **다른 파일** (dataset_id 와 idx 둘 다 다름)
- SSD: 트리비얼
- HDD: random seek 폭발 가능
- NAS / 네트워크 파일시스템이면 별도 고려

#### 메모리 증가 (무시 가능)

- worker 4개 동시 보유 `traces`: 1 subject 당 3 school × xs/ys ≈ 1MB
- × 4 worker × 5 사용자 = 20MB. 무시.

#### 에러 격리

- 직렬: subject #500 에서 예외 → 그 자리 stop, 0~499 디스크에 남음
- 병렬: 다른 worker 가 마저 끝내고 abort. 부분 출력 약간 더 남음
- 빌드 실패 시 디렉터리는 어차피 폐기 → 사실상 무관

#### 다른 사용자 영향

- 4-worker 가 4코어 점유 시 다른 GET API / Dash 콜백 latency 약간 증가
- 사용자간 "대기" 추가는 아니지만 CPU 경합 증가
- worker 수를 보수적으로 (`min(4, cpu_count // 2)`) 잡으면 완화

#### 결정성 / 재현성

- 결과 파일 내용은 100% 동일 (각 subject 독립)
- 처리 순서만 매번 달라짐 → 성능 프로파일링 시 약간 복잡

### 4.3 적용 가이드 (실제 구현 시)

#### Step 1: worker 함수 분리

```python
def _process_subject(idx, schools, names, color_map, first, charts_dir, thumbs_dir):
    """한 subject 의 chart JSON + SVG 를 디스크에 쓰고 통계 반환.

    이 함수는 어떤 공유 가변 상태도 변경하지 않는다.
    schools 는 read-only 로 취급 (pandas 는 read 동시 OK).
    """
    t = {}
    traces = []
    t0 = time.perf_counter()
    for name in names:
        xs, ys = cumulative_distribution_full(
            to_numeric_clean(schools[name].scores.iloc[:, idx])
        )
        traces.append({"school": name, "color": color_map[name], "xs": xs, "ys": ys})
    t["cdf"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    payload = build_payload(
        idx, first.subjects[idx], _idx_or(first.units, idx, ""),
        _idx_or(first.lower_limits, idx), _idx_or(first.upper_limits, idx), traces,
    )
    t["payload"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    chart_path = charts_dir / f"{idx}.json"
    chart_path.write_text(json.dumps(payload, **JSON_KWARGS), encoding="utf-8")
    t["write_json"] = time.perf_counter() - t0
    chart_b = chart_path.stat().st_size

    t0 = time.perf_counter()
    svg = build_subject_svg(
        idx, first.subjects[idx], _idx_or(first.units, idx, ""),
        _idx_or(first.lower_limits, idx), _idx_or(first.upper_limits, idx),
        traces, payload["layout"],
    )
    t["svg"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    svg_path = thumbs_dir / f"{idx}.svg"
    svg_path.write_text(svg, encoding="utf-8")
    t["write_svg"] = time.perf_counter() - t0
    svg_b = svg_path.stat().st_size

    return idx, chart_b, svg_b, t
```

#### Step 2: 메인 루프를 ThreadPoolExecutor 로 교체

```python
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

# config.py 에 추가하거나 여기서 env 직접 읽기
BUILD_WORKERS = int(os.getenv("REPORT_BUILD_WORKERS", "4"))

cdf_s = payload_s = write_s = svg_s = write_svg_s = 0.0
chart_bytes = svg_bytes = 0
completed = 0
progress_t0 = time.perf_counter()
_log(f"building JSON and SVG charts for {n_subjects} subjects (workers={BUILD_WORKERS})")
emit("cdf_svg", 0, n_subjects)

with ThreadPoolExecutor(max_workers=BUILD_WORKERS) as ex:
    futures = [
        ex.submit(_process_subject, idx, schools, names, color_map, first, charts_dir, thumbs_dir)
        for idx in range(n_subjects)
    ]
    for fut in as_completed(futures):
        idx, chart_b, svg_b, t = fut.result()  # 예외는 여기서 raise
        chart_bytes += chart_b
        svg_bytes += svg_b
        cdf_s += t["cdf"]
        payload_s += t["payload"]
        write_s += t["write_json"]
        svg_s += t["svg"]
        write_svg_s += t["write_svg"]
        completed += 1
        if completed % 10 == 0 or completed == n_subjects:
            _progress("charts+svg", completed, n_subjects, progress_t0)
            emit("cdf_svg", completed, n_subjects)

timings["cdf_s"] = round(cdf_s, 2)
timings["payload_s"] = round(payload_s, 2)
timings["write_json_s"] = round(write_s, 2)
timings["svg_s"] = round(svg_s, 2)
timings["write_svg_s"] = round(write_svg_s, 2)
```

#### Step 3: config.py 에 worker 수 환경변수 노출 (선택)

```python
# config.py
REPORT_BUILD_WORKERS = int(os.getenv("REPORT_BUILD_WORKERS", "4"))
```

#### Step 4: 검증 절차

1. 단일 사용자 빌드 후 charts/ 와 thumbs/ 디렉터리의 파일 개수 = n_subjects 확인
2. `diff` 로 직렬 빌드 결과와 병렬 빌드 결과 비교 — bit-exact 일치해야 함
3. 동시 2명 빌드 → 서로 다른 dataset_id 에 격리되어 충돌 없음 확인
4. CPU 코어 수보다 큰 worker 수에서 throughput 측정 → 1코어 = 1 worker 수렴 확인

#### 검증 명령 예시 (단일 사용자 비교)

```bash
# 1) 직렬 빌드
git stash  # 병렬 변경 임시 보관
python build.py before_parallel
md5sum output/datasets/before_parallel/charts/*.json | sort > /tmp/before.md5
md5sum output/datasets/before_parallel/thumbs/*.svg  | sort >> /tmp/before.md5

# 2) 병렬 빌드 적용 후
git stash pop
python build.py after_parallel
md5sum output/datasets/after_parallel/charts/*.json  | sort > /tmp/after.md5
md5sum output/datasets/after_parallel/thumbs/*.svg   | sort >> /tmp/after.md5

# 3) 차이 0 확인
diff /tmp/before.md5 /tmp/after.md5
```

---

## 5. 🔴 #2 — Block 7 CSV 재로딩 제거

### 5.1 문제

[report/report_analysis_service.py:378-427](../report/report_analysis_service.py#L378-L427) `upload_derived_if_absent` 는
- 이미 [get_or_compute_analysis](../report/report_analysis_service.py#L443-L557) 에서 `schools` 가 메모리에 있음에도 불구하고
- `file_paths` 만 받아서 다시 `load_table(p)` 호출

→ 5s/사용자 낭비.

### 5.2 적용 가이드

#### Step 1: `upload_derived_if_absent` 시그니처에 `schools=None` 추가

```python
def upload_derived_if_absent(analysis_key, content_hash, options_json, file_paths,
                              schools=None):
    """fail_items + issue_table + 필요한 SVG 썸네일만 → S3 업로드.

    schools: 호출자가 이미 load 한 schools dict (선택). None 이면 file_paths 에서
    재로딩한다 (하위 호환).
    """
    need_fail  = not report_db.get_object_info(analysis_key, "fail_items")
    need_issue = not report_db.get_object_info(analysis_key, "issue_table")
    need_thumbs = not report_db.get_object_info(analysis_key, "thumbs_fail_set")

    if not need_fail and not need_issue and not need_thumbs:
        return

    if schools is None:
        file_paths = [Path(p) for p in file_paths]
        schools = {p.stem: load_table(p) for p in sorted(file_paths, key=lambda x: x.name)}
        schools = _filter_schools_by_items(schools, _extract_selected_items(options_json))
    # 이미 호출자가 필터링 + 로드한 schools 를 신뢰

    # ... 이하 동일
```

#### Step 2: `get_or_compute_analysis` 가 schools 를 반환하도록 확장

```python
return {
    "reused": False,
    "analysis_key": analysis_key,
    "content_hash": content_hash,
    "options_json": options_json,
    "summary": report_db.get_summary_by_analysis_key(analysis_key),
    "schools": schools,   # NEW
}
```

캐시 hit 경로(`reused=True`)에서는 schools 가 없으므로 `None` 또는 dict 미포함.

#### Step 3: `_run_analysis` 가 schools 를 전달

```python
# report/report_routes.py execute() 안의 _run_analysis
result = get_or_compute_analysis(session_id, saved_paths, options)
analysis_key = result["analysis_key"]
try:
    _upload_csvs_to_s3(saved_paths, analysis_key)
    upload_derived_if_absent(
        analysis_key, result["content_hash"], result["options_json"],
        saved_paths,
        schools=result.get("schools"),  # NEW
    )
    ...
```

### 5.3 검증

- S3 에 올라간 `fail_items.json`, `issue_table.json`, 썸네일 SVG 들의 hash 가 변경 전후 동일해야 함
- `analyze` 후 `/pe/report/view/<sid>` 페이지 표시 확인

---

## 6. 적용 순서 권장

1. **#2 먼저** — 변경 범위 작고 리스크 낮음, 5s/사용자 즉시 회수
2. **#1 다음** — 실측 wall time 비교 후 적용 가치 검증
3. (선택) #3 numpy 벡터화 — 코드 복잡도 vs 추가 2-3s 트레이드오프 평가 후 결정

---

## 7. 결정성 체크리스트 (구현 후 확인)

- [ ] 단일 사용자 빌드 결과 (charts/*.json + thumbs/*.svg) 변경 전후 bit-exact
- [ ] 동시 2명 사용자: 각자 dataset_id 격리 → 출력 디렉터리 충돌 없음
- [ ] analyze + plot 의 analysis_key 동일 (캐시 키 무영향)
- [ ] DB `report_analysis_summary` row 수/내용 동일
- [ ] S3 객체 hash 동일
- [ ] 사용자간 대기 0 (락/세마포어 추가 없음)
- [ ] CPU 코어보다 worker 많이 잡아도 정상 종료

---

## 8. 참고 — 적용하지 않을 것 (사용자 정책)

- ❌ process-wide semaphore (분석 동시성 제한)
- ❌ S3 PUT global semaphore
- ❌ analysis_key 에서 session_id 제거 (캐시 재사용 → 사용자간 대기 유발)

원칙: **누군가 실행 중이라고 다른 누군가가 기다리는 메커니즘은 어떤 형태로도 추가하지 않는다.**
