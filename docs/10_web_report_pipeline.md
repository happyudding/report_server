# 10 · web_report — 파이프라인 (업로드 → ingest → 저장 → 로드)

> Honey 가 보내는 **7-meta honeyform parquet** 를 받아 세션 저장·재계산·렌더 데이터로
> 공급하는 신규 병행 흐름. xlsx grid 업로드([01](01_server_upload.md))와 **별개**다.
> 관련: 탭 계약 [11](11_web_report_tabs.md) · 캐시 [12](12_web_report_cache.md) ·
> 저장소 [03](03_storage.md) · 조회/접근제어 [02](02_server_query_edit.md)

`web_report/` 는 server/ 밖의 별도 Python 패키지다. **blueprint 가 아니라**
[report_routes](../server/report/report_routes.py) 가 `from web_report import service/...`
로 직접 import 하고, 저장소는 `runtime.storage()` 포트로 접근한다(storage_gateway 직접
import 금지). 서버 진입점은 [server/upload_webreport.py](../server/upload_webreport.py).

## 파일
- [server/upload_webreport.py](../server/upload_webreport.py) — 라우트 `POST /pe/report/upload_webreport`, `GET /web_report/<sid>`(→ `/view/<sid>` 리다이렉트)
- [web_report/ingest.py](../web_report/ingest.py) — `ingest_webreport()` (service 가 재노출)
- [web_report/honeyform.py](../web_report/honeyform.py) — honeyform 스키마·parquet 인코딩/디코딩
- [web_report/service.py](../web_report/service.py) — `load_webreport()` 및 조회/편집 오케스트레이션
- [web_report/loader.py](../web_report/loader.py) — 세션 → parquet 다운로드·디코드 → HoneyformTable
- [web_report/validation.py](../web_report/validation.py) — meta/mode 정규화, `client_identity`
- [server/storage_gateway/](../server/storage_gateway/) — parquet/manifest 저장(→[03](03_storage.md))

## honeyform 스키마 (불변 계약)
DataFrame 레이아웃 (`honeyform.py`, `META_COLUMNS`/`META_ROW_LABELS`):
- **메타 컬럼 7개** (좌측): `SERIAL, SHOT, DUT, XPOS, YPOS, BIN, FAILTNO`
- **메타 행 6개** (상단, `DATA_START_ROW=6`): `TSEQ, TNO, STEP, UNIT, HILIM, LOLIM`
- 8번째 컬럼부터가 **측정 항목(item)**, 7번째 행부터가 **측정 데이터**
- item 데이터는 디코드 시 numeric 으로 복원(정수 전용 컬럼 int64 / 그 외 float64 dtype 보존).
  parquet 인코딩 전 `validate_honeyform_df` 로 컬럼/행 라벨·중복·최소 행 검증.

## 업로드 흐름 (`ingest_webreport()`)
1. **정규화** — `validate_meta`(product_type/product/lot_id/revision/process/edm_link/password/
   file_name), `validate_mode`(Normal/Compare/DUT/Commonality), `client_identity(manifest)` →
   `(uploaded_by, client_host)`.
2. **모드 파일 수 검증** — Compare 는 파일이 **정확히 2개** 아니면 400 거부.
3. **디코드·검증·시딩** — 각 parquet 를 `decode_split_honeyform_parquet(keep_df=False)` 로
   검증하며 슬림 테이블로 만들고, 원본 bytes 는 그대로 보관.
4. **키 산출** —
   - `analysis_key = sha256(canon({files: [파일 sha256…], meta: {product_type, product,
     lot_id}, selected_items}))`
   - `content_hash = sha256(canon({files: […]}))`
   - `session_id = "<epoch>_<hex6>"`
   - **password·mode·신원은 analysis_key 에 불포함** (규칙 유지 — 같은 데이터면 같은 key).
5. **저장** — `storage.save_webreport_sources(akey, chash, [bytes…], manifest)` (S3 우선,
   실패 시 로컬 폴백, 저장 위치를 object_info 에 기록 → [03](03_storage.md)). 이어서
   manifest·tables 를 인메모리 캐시에 시딩(첫 조회 재디코드 제거).
6. **세션 생성** — `create_session(source="web_report", uploaded_by, client_host, mode,
   password, …)` → `update_session(analysis_key, content_hash, status="done")`.
   `manifest.options`(Distribution 색 등) 있으면 `webreport_options` 컬럼에 영속.
7. **편집값 시드** — `manifest` 에 comment/override 가 실려 오면 세션 편집
   DB(`report_webreport_edit`)로 시드(`edits.seed_from_manifest`). 이후 manifest 는 불변.
8. **감사 기록** — `log_audit("upload", client_user=uploaded_by, client_host=…)`.
9. **프리웜** — `compute.prewarm` 이 첫 조회 산출물을 컴퓨트 풀에 미리 제출(실패해도 무해).

반환: `{session_id, analysis_key, status, mode, web_report_url, sources, item_count, storage}`.

## 조회 흐름 (`load_webreport()`)
1. 세션 로드 → `edits_rev = get_webreport_edit_rev(sid)` (작은 인덱스 SELECT 1회).
2. `cache_policy.report_key(session, sid, edits_rev)` 로 REPORT_CACHE 확인 → 미스면
   disk_cache → 콜드 빌드는 `compute.run(report_job)` 워커 오프로드(GIL 비점유).
3. 콜드 계산: `load_tables` → `mode_tables`(DUT 분할) → `build_report_payload`
   (TAB_REGISTRY 순회, [11](11_web_report_tabs.md)) → disk_cache 저장.
4. 반환 session 에서 `password` 제거, `has_password` 불린만 노출. **Distribution ECDF 는
   payload 에서 제외** — 프런트가 `/distribution` 으로 지연 로드(대용량 payload 회피).

single-flight 락으로 콜드 미스 동시 진입의 중복 계산을 막는다. 캐시 키 규약은
[12](12_web_report_cache.md).

## 분석 모드 (Normal / DUT / Compare / Commonality)
세션마다 모드를 가진다. Honey 업로드 시 파일 개수로 가용 모드가 제한되어 `manifest.mode`
로 전송되고 `report_session.mode` 컬럼에 저장된다. **mode 는 analysis_key 산출에 불포함,
캐시 키에는 포함**(dedup 세션 간 충돌 방지 — `cache_policy.py`).

| 모드 | 파일 수 | 요지 |
|------|---------|------|
| **Normal** | 1+ | 기존 동작. payload 에 `"mode":"Normal"`. |
| **DUT** | 1 | **서버에서** honeyform 을 DUT 컬럼으로 분할(`split_table_by_dut`) — DUT별 pseudo-source(`DUT <값>`)로 기존 multi-source 렌더 재사용. 다운샘플 없음. |
| **Compare** | 정확히 2 | `tabs/compare.py` 가 통계 delta·bin delta·공통/비공통 fail map + goodlog(테스트 프로그램 diff) 제공. ingest 가 2개 아니면 400. |
| **Commonality** | 1 | `tabs/commonality.py` chip 검색(serial/xpos/ypos/dut) + 항목별 값·누적%·wafer 좌표. chip 선택은 view-time(비영속). |

## 신원 / 업로더 잠금
`client_identity(manifest["client"])` → `uploaded_by = "<domain>\\<user>"`(또는 user),
`client_host`. **web_report 세션은 `uploaded_by` 를 채우므로 업로더 잠금이 실효**한다
(업로더/위임 편집자만 편집·삭제 — [02](02_server_query_edit.md)). 클라 신고값이라 위조
가능(사내망 감사 용도)이며 analysis_key 에는 포함되지 않는다. (xlsx 세션은 `uploaded_by`
를 채우지 않아 legacy 우회 — Honey 접속 사용자 전원이 편집/삭제 가능.)

## 자주 바뀌는 지점
- 새 탭 → [11](11_web_report_tabs.md) 의 TAB_REGISTRY 절차.
- 새 캐시/키 → [12](12_web_report_cache.md) 의 `cache_policy.py` 빌더.
- manifest 필드 추가 → `ingest.py` + 클라 업로드 payload. 편집값은 manifest 가 아니라 세션
  편집 DB 로 저장됨에 주의.
