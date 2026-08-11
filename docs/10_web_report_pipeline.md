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
- [web_report/dist_blob.py](../web_report/dist_blob.py) — Distribution ECDF compact 공용 빌더
  (서버 폴백 계산 + Honey 클라 업로드 시 프리컴퓨트가 같은 코드 사용 — 값 일치 보장)
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
- **item 컬럼 이름은 메타 컬럼명과 겹칠 수 없다**(예약어) — 겹치면 `tail[meta_labels]` 가
  같은 라벨로 2개 컬럼을 뽑아 TNO/HILIM 이 엉뚱한 항목에 배정된다. `validate_honeyform_df` 가 거부.
- **메타 7컬럼 라벨은 encode·decode·split 모두에서 canonical 대문자로 정규화**된다
  (`canonicalize_meta_columns`). 검증이 대소문자를 무시하므로 'Bin' 같은 표기도 통과하는데,
  하류는 `data["BIN"]` 대문자 하드코딩이라 정규화가 없으면 저장은 성공하고 조회만 500 이 난다.
  decode 에도 적용해 이미 저장된 오염 parquet 을 마이그레이션 없이 구제한다.
- **값(측정값·BIN·좌표 등)은 이 검증 대상이 아니다.** `validate_honeyform_df` 는 조회
  경로(`_decode_parts`)가 공유하므로 값 규칙을 넣으면 기존 세션이 열리지 않는다. 편집 시
  값 검증은 [rawvalues.py](../web_report/rawvalues.py) 가 편집 경로에서만 수행한다
  (→ [11 · Raw Data 값 검증](11_web_report_tabs.md)). ingest 는 종전대로 값을 검증하지 않는다.

## parquet 소스는 어디서 오나 (클라 측)
[client/honey_main.py](../client/honey_main.py) `_build_webreport_parquets` 가
`work_group.names()` 를 순회하며 **source 1개당 parquet 1개**를 만든다. 각 source 의 df 는
**`honey_parse.file_to_df` 가 돌려준 7-meta honeyform(`md.df`) 그대로**다 (→ [06](06_analysis_engine.md)).

- **원본 입력 파일을 디스크에서 다시 읽지 않는다.** 여러 input 의 병합은 honey_parse 안에서
  일어나므로, 원본을 재-read 하면 병합 결과를 버리고 파일 1개만 올리게 된다.
  (2026-07-21 이전에는 `read_honeyform_file(source_path)` 로 원본을 재-read 했고, 그래서 raw
  입력 파일이 honeyform 규격으로 검증되어 "컬럼 부족/메타 행 레이블" 에러가 났다.)
- 검증은 `encode_honeyform_parquet` 안의 `validate_honeyform_df` 1회 — 즉 **parquet 으로
  넘어가는 순간의 honeyform** 이 검증 대상이다. 실패하면 `[파일명]` 이 앞에 붙어 표시된다.
- ⚠️ `client/honey_parse/` 더미 폴백은 아직 구형 5-meta 를 반환하므로 **개발 PC 에서는 이
  단계가 실패하는 것이 정상**이다(실제 honey_parse 가 붙은 운영은 정상).

## 업로드 흐름 (`ingest_webreport()`)
1. **정규화** — `validate_meta`(product_type/product/lot_id/revision/process/edm_link/password/
   file_name), `validate_mode`(Normal/Compare/DUT/Commonality), `client_identity(manifest)` →
   `(uploaded_by, client_host)`.
2. **모드 source 수 검증** — Compare 는 source(=업로드 parquet)가 **2개 미만**이면 400 거부
   (상한 없음 — Before/After 두 그룹으로 나눈다).
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
   - **클라 Distribution pack 저장 (2026-07-23, 현행)**: Honey 가 multipart 에
     `dist_pack_index`(form JSON) + `dist_pack_chunk_<n>`(gzip) 을 첨부하면
     `ingest.save_client_dist_pack` 이 검증(index 포맷 + chunk gzip CRC/프리픽스) 후
     **영구 저장**한다(`dist_pack_store`, 캐시 아님 → 재조회·재시작에도 재정렬 없음).
     정렬(np.unique)이 클라에서 끝나 서버는 조회 때 덧셈만 한다 →
     [12](12_web_report_cache.md). 미첨부/검증 실패는 기존 계산 폴백.
   - **클라 dist blob 시딩 (2026-07-15, 구 Honey 하위호환)**: Honey 가 multipart 에 `dist_blob`(전체)/
     `dist_blob_bin1`(양품만) — 업로드 parquet 로 미리 계산한 Distribution ECDF gzip —
     을 첨부하면, 검증(gzip CRC + 포맷 프리픽스, `dist_blob.validate_dist_blob`) 후
     dist 캐시(disk+RAM)에 그대로 시딩한다. 서버 콜드 dist 빌드(대용량 세션 수십 초
     CPU + RAM 스파이크)가 사라진다. 클라 계산은 서버 폴백과 같은
     `dist_blob.compute_dist_compact` 공용 코드라 값이 동일하다. 미첨부(구 Honey)/검증
     실패는 기존 서버 계산 폴백 — 하위호환 무변경.
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

## 원본 교체 흐름 (Honey Excel 왕복 — `rawedit.py`)
업로드된 parquet 원본을 사후에 고치는 경로. `GET .../web_report/rawdata_export` 로
zip(manifest + `source_<idx>.parquet`)을 내려받아 Honey 가 **source 1개 = 시트 1장**으로 Excel
에 펼치고, 저장·닫으면 재인코딩해 `POST .../web_report/rawdata_replace` 로 전량 교체한다
(브라우저가 아니므로 CSRF 대신 `X-Honey-Agent` 헤더 + 편집자 가드).

- 시트↔source 매칭은 **시트 이름 기준**(`excel_session.match_sheets`) — 순서를 바꿔도 원본
  순서를 복원한다. 이름이 안 맞고 개수만 같으면 위치 기반 폴백(이름 변경 용인).
- **고칠 source 만 골라 열 수 있다**(2026-07-28, 허브 Excel 페이지 체크리스트 →
  `run_excel_edit(indices=)`). 고른 source 만 디코드·시트 작성하고, 나머지는 zip 에서 읽은
  **원본 parquet bytes 를 그대로 되올린다**(`_merge_sources`) — 서버는 업로드 목록에 없는
  source 를 지우므로 편집분만 올리면 안 연 source 가 사라진다. 시트 삭제 판정도 **고른
  source 안에서만** 하므로(`_read_and_encode` 의 expected_titles = 선택분), 안 연 source 가
  '삭제됨' 으로 오인되지 않는다. 회귀 고정:
  [tests/test_excel_sheet_match.py](../tests/test_excel_sheet_match.py) (h)~(j).
- **시트를 지우면 그 source 를 물리 제거**한다. 남긴 원본 idx 를 form 필드 `source_indices`
  (JSON 오름차순)로 보내고, 서버가 `manifest["sources"]` 도 함께 축소해 재저장한다
  (manifest 불변의 유일한 예외). 초과 idx 의 object_info 행·로컬 파일·S3 객체는
  `save_webreport_sources` 가 정리한다. 시트 추가·전량 삭제는 거부.
- 덮어쓰기 직전 1세대 백업(`<upload_root>/webreport_backup/<akey>/`) — 앱 내 undo 는 없고
  복구는 운영자 수동. 백업 실패 시 편집 자체를 거부해 원본을 지킨다.
- 새 `content_hash` 는 **같은 analysis_key 의 전 세션**에 반영한다(dedup 형제가 옛 hash 로
  stale 캐시를 서빙하지 않도록). 상세 계약은 [11](11_web_report_tabs.md) 편집 흐름 절.
- content_hash 가 바뀌면 구 Distribution pack 은 조회되지 않으므로(디렉토리명에 chash)
  `dist_pack_store.delete_stale` 이 회수하고, 클라가 **편집 결과로 다시 만든 pack** 을
  `dist_pack_index`/`dist_pack_chunk_<n>` 로 동봉한다(`excel_session._build_dist_pack` —
  업로드 경로와 같은 `dist_pack.build_pack_from_parquet`). 미첨부면 서버 폴백 계산.
- **웹 표 셀 편집(`service.edit_raw_data`)에는 클라가 붙여줄 pack 이 없다** — 브라우저에서
  고치는 경로라서다. 그래서 편집 후 서버가 새 세대 pack 재생성과 프리웜을 백그라운드로
  예약한다(`compute.request_dist_pack(base=True)` + `compute.prewarm`, 응답은 대기하지 않음).
  안 하면 Honey 로 Excel 왕복을 다시 하기 전까지 그 세션이 영구히 폴백 계산으로 열린다.

## 분석 모드 (Normal / DUT / Compare / Commonality / Temperature)
세션마다 모드를 가진다. Honey 업로드 시 **source 개수**(= `honey_parse.file_to_df` 가 돌려준
df 개수 = 업로드 parquet 개수, 입력 파일 개수가 아니다)로 가용 모드가 제한되어 `manifest.mode`
로 전송되고 `report_session.mode` 컬럼에 저장된다. **mode 는 analysis_key 산출에 불포함,
캐시 키에는 포함**(dedup 세션 간 충돌 방지 — `cache_policy.py`).

| 모드 | source 수 | 요지 |
|------|-----------|------|
| **Normal** | 1+ | 기존 동작. payload 에 `"mode":"Normal"`. |
| **DUT** | 1 | **서버에서** honeyform 을 DUT 컬럼으로 분할(`split_table_by_dut`) — DUT별 pseudo-source(`DUT <값>`)로 Yield/CPK/Distribution 등은 DUT 비교 렌더. **단 Map Analysis 는 예외**: `build_map_analysis_rows(mode="DUT")` 가 DUT 를 하나의 맵(`source="All DUT"`)으로 병합하고 die 마다 `dut` 태그를 달아 프런트가 DUT Legend 로 강조한다. 다운샘플 없음. |
| **Compare** | 2+ | source 를 **Before / After 두 그룹**으로 나눠 비교 (2026-07-23 재정의). 배치는 Honey `CompareArrangeDialog` 가 정해 `manifest.options.compare = {"before":[이름…],"after":[이름…]}` 로 싣고 세션 `webreport_options` 에 저장된다. **업로드 순서 = [After…, Before…]** 라 `tables[0]` = After 최상단이고, 이것이 web_report 전체의 limit(HiLIM/LoLIM) 기준 source 다(`_first_table_for`/`item_meta` 가 첫 등장 테이블을 쓰므로 서버 분기 없음). `tabs/compare.py` 가 공통성 Map(전 source·die hover 에 source 별 Bin)·Bin Yield·Bin 불일치 좌표표·goodlog(그룹 대표 2개)·산포 비교/동일성 검증(그룹 pool)을 만든다. 옵션이 없는 legacy 세션은 `after=[s0], before=[s1]` 폴백. ingest 는 2개 미만이면 400. |
| **Commonality** | 1 | `tabs/commonality.py` chip 검색(serial/xpos/ypos/dut) + 항목별 값·누적%·wafer 좌표. chip 선택은 view-time(비영속). |
| **Temperature** | 1+ | **PMIC / SECURITY 전용**(Honey 라디오가 이 둘에서만 노출 — `honey_main._TEMPERATURE_PRODUCT_TYPES`, 서버는 product_type 을 보지 않는다). source 를 RT/CT/HT 그룹으로 묶고 **그룹의 RT 가 Limit 기준**이다. 배치는 Honey `SourceNameDialog`(Temperature 모드 — `Group`/`Role` 열. 2026-08-05 구 `TemperatureGroupDialog` 흡수) 가 정해 `manifest.options.temperature = {"groups":[{"rt":이름,"members":[CT,HT],"member_roles":["CT","HT"]}],"limits_file":{…}}` 로 싣는다(업로드 순서 = 그룹마다 RT→CT→HT). **`groups` 배열 순서와 그룹 안 `members` 순서는 배치 창 표의 등장 순서 그대로**이고(2026-08-10), 서버가 그 순서로 `temp_group` 번호와 Temp Fail 컬럼 순서를 만든다 — 창에서 그룹 블록을 위로 올리면 리포트에서도 그 그룹이 먼저 나온다. **rawdata 정리는 업로드 전 클라에서 끝난다** — `web_report/temperature.py clean_frames` 가 ① CT/HT 를 RT 의 BIN==1 좌표(XPOS,YPOS)만 남기고 ② RT 의 HILIM/LOLIM 으로 Pass/Fail 재판정(CT/HT 자신의 limit 메타행은 원본 유지) ③ fail bin 을 .lt/.pds 매핑(LSL/USL 방향별) → RT 에서 죽은 bin → 999 순으로 채운다. 서버는 정리된 parquet 을 받는다. **단 판정은 조회 시점에 서버가 다시 한다**(2026-08-05) — `tabs/temp_fail.py` 가 좌표 필터가 반영된 CT/HT 위에서 **전 항목**을 RT limit 으로 재판정해 `sheets["Issue Table Temp"]` 를 만든다(위 ②의 '첫 fail 하나만' 제한이 사라져 가려지는 항목이 없다. 합이 100% 초과 가능). Yield 계열은 **RT source 만** 계산하고, 비RT 소스의 수율 분모는 남은 die 수로 강제한다(`resolve_source_basis(force_test=…)`). Bin 표기용 항목별 fail bin 은 `manifest["temperature_limits"]`(신규 업로드만, `.lt` `20:19` 의 **오른쪽**만 사용)에 실리고 없으면 관측 bin 폴백. payload `sources[].temp_role`/`temp_group`/`temp_corner` + `payload.temperature` + 신규 시트/라우트 → [11](11_web_report_tabs.md). |

### Temperature 입력 배치 (2026-08-05)
- **폴더 열기** — `LOCAL FILE OPEN` 버튼의 드롭다운("폴더 열기…") 과 창 **드래그앤드랍**
  (파일·폴더 혼합 가능)이 상위폴더(예 `EP1/`) 밑의 `RT|ROOM` / `CT|COLD` / `HT|HOT` 하위
  폴더를 인식해 파일과 **역할**을 함께 가져온다(대소문자 무관 + 토큰 경계 부분일치,
  `"RT_25C"`·`"Cold Temp"` 인식 / `"SHORT"` 미인식, 2개 역할 동시 매칭은 모호 → 건너뜀).
  구현은 Qt 무의존 순수 모듈 [client/honey_ui/folder_intake.py](../client/honey_ui/folder_intake.py).
  온도 폴더가 하나도 없으면 **일반 폴더**로 보고 하위 파일을 전부 재귀 수집한다 — 폴더
  열기는 **전 모드 공통**이다. **확장자 필터는 없다**(파일 열기의 `모든 파일 (*.*)` 와 같은
  규칙, 2026-08-05 사용자 확정) — `.lt`/`.pds` 나 로그도 딸려오므로 필요 없는 것은 파일
  리스트에서 행별 ✕ 로 뺀다. 화이트리스트를 다시 넣지 말 것.
- **역할 → source 매핑** — ⚠️ 입력 파일 개수 ≠ source 개수(불변 규칙 #9)라
  `honey_main._roles_for_names` 는 길이가 같을 때만 index 로 잇고 그 외에는 파일 stem
  부분일치로만 잇는다. 못 이은 source 는 **미배정으로 남겨** 사용자가 직접 놓게 한다.
- **그룹(pair) 유추** — [client/honey_ui/temperature_pairing.py](../client/honey_ui/temperature_pairing.py)
  `suggest_groups_by_role` 이 ① 이름 stem 일치(`WF1_RT`↔`WF1_CT`) ② 남은 것은 **역할별
  순번**(폴더마다 같은 파일명이면 source 이름이 `a`/`a_2`/`a_3` 로 갈리므로 순번이 유일한
  단서) 2단계로 짝짓는다. 2단계는 추정이라 창이 '배치'가 아니라 **'확인' 창**이다 —
  자동 배치가 표의 `Group`/`Role` 콤보를 채워 두고 사용자는 틀린 곳만 고른다. 자동 배치는
  **행 순서까지 그룹 순(RT→CT→HT)으로 재정렬**하므로 표시 순서가 곧 업로드 순서가 된다.
  계약은 `tests/test_temperature_pairing.py` 9개 검사가 고정한다.
- **source 명 변경 / 순서** — `SourceNameDialog` 표에서 legend 를 직접 편집하고 `↑`/`↓`
  (Alt+↑/↓)·`↑↑`/`↓↓`(Alt+Home/End = 최상단/최하단) 로 순서를 바꾼다. **Ctrl/Shift 다중
  선택**이라 그룹 블록을 통째로 옮길 수 있다(2026-08-10). Role 을 고르면 `_RT`/`_CT`/`_HT`
  접미사가 자동으로 붙고(이미 붙어 있으면 중복 부착하지 않고, Role 을 바꾸면 접미사만 교체)
  이름 본체는 보존된다. **표의 위→아래가 곧 web_report 표시 순서다** — source 순서는
  `order_index`(→ `tables`), Temperature 그룹 순서는 `groups` 배열 순서(→ `temp_group`
  번호·Temp Fail 컬럼)로 각각 이어진다. 그래서 창은 그룹 번호도 표 등장 순서로 다시 매긴다
  (`_renumber_groups`).
  결과 `names` → `rename_sources`(중복은 `_2` 접미), 순서는 `order_index` 로 잇는다.
  정합 검증은 rename 전 이름(`source_names`)으로 비교한다 — `order` 로 비교하면 rename 한
  순간 항상 불일치가 되어 창이 두 번 뜬다.
- **PMIC 자동 source 명** — [client/honey_ui/source_naming.py](../client/honey_ui/source_naming.py)
  `pmic_source_name` 이 파일명의 LOT 토큰(`60/61/62/6Z/80/81/82/8Z` 로 시작) + 바로 다음
  WF 토큰(숫자 또는 `W`+숫자, 1~3자리)을 이어 `602XX2_3` 을 만든다. `_parse_group_core` 가
  파싱 직후 `rename_sources` 로 덮어쓰고 `_guess_source_names`(파싱 전 추정)도 같은 규칙을
  쓴다 — 두 곳이 어긋나면 확인 창이 두 번 뜬다. 원 규칙(`report_generator/df_honey.py`
  `_sheetname_from_filename`)은 동결 영역이라 고치지 않고 덮어쓰는 방식이다.

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
