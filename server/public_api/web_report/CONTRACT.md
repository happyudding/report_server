# web_report 공개 조회 API 규약 (`/pe/api/v1/web-report`)

챗봇·타 시스템·MCP 가 web_report 의 계산 결과를 읽어 가는 통로다. **기계가독 정본은
[contracts.py](contracts.py) `FUNCTION_SPECS`** 이고, 이 문서는 그것을 사람이 읽는 형태로
푼 것이다. 살아 있는 목록은 서버에서 직접 받는다:

```bash
curl "http://12.81.220.117:8080/pe/api/v1/web-report/capabilities"
```

`tests/test_public_api_web_report.py` 가 **규약 ↔ 실제 등록 라우트의 1:1** 을 검사하므로,
한쪽만 고치면 테스트가 깨진다. 새 엔드포인트는 라우트와 SPEC 을 같은 커밋에서 추가한다.

---

## 1. 공통 규약

| 항목 | 값 |
|---|---|
| Base URL | `http://12.81.220.117:8080/pe/api/v1/web-report` |
| 메서드 | **GET only**, 읽기 전용. 요청 본문 없음 |
| 인증 | 기본 **무인증**(공개 세션만). env `WEB_REPORT_API_KEY` 설정 시 헤더 `X-Report-Api-Key` 일치하면 비공개 세션까지 |
| 성공 응답 | `{"schema_version": 1, "data": {...}, "meta": {...}}` |
| CSRF | 없음 — 쿠키를 쥘 수 없는 프로그램 호출자가 대상이고 GET 이라 상태를 바꾸지 않는다 |

`meta` 는 **이 값이 언제 것인지**를 판단하는 근거다: `session_id` · `analysis_key` ·
`content_hash` · `edits_rev`(+ 페이지 정보 `total`/`returned`/`offset`). 같은 세션을 다시
불렀는데 `content_hash`·`edits_rev` 가 그대로면 값도 그대로다.

### 상태코드

| 코드 | 본문 | 뜻과 대응 |
|---|---|---|
| 200 | `{schema_version, data, meta}` | 정상 |
| 202 | `{building:true, blocked:false, status_url, retry_after_sec}` | **콜드 빌드 중.** 에러가 아니다 — `status_url` 을 폴링한 뒤 재요청 |
| 400 | `{error:"bad_request"\|"not_web_report", message}` | 파라미터 오류 / xlsx 세션(수치 payload 자체가 없음) |
| 404 | `{error:"session_not_found"}` | 미존재 **또는 권한 없음**(존재를 숨긴다 — 둘을 구분하지 말 것) |
| 404 | `{error:"item_not_found", subject}` | 세션은 보이는데 그 항목이 없다 |
| 429 | `{error:"busy"}` | 대용량 조회 동시 실행 상한. `Retry-After` 후 재시도 |
| 503 | `{error:"build_failed"}` | 연속 실패로 차단된 세션. 재시도해도 같다 |

### 호출 측 권장

- **404 는 정상 결과다**(그런 세션이 없거나 권한이 없다). 재시도하지 않는다.
- 202 는 `retry_after_sec`(기본 5초) 이상 간격으로 `build-status` 를 본 뒤 재요청한다.
  목록을 순회하며 훑는 폴러는 **콜드 세션에 빌드를 유발하지 않도록** 값싼
  `overview` 대신 `build-status` 를 먼저 보는 편이 좋다.
- 재시도는 5xx·429·네트워크 오류에만. 백오프 없이 연타하지 않는다.
- 응답 파싱은 "아는 키만 읽고 모르는 키는 무시" — 필드는 **추가만** 되고 삭제·개명되지 않는다.

### 비용 등급

| 등급 | 뜻 |
|---|---|
| `cheap` | DB/메모리 조회. 폴링해도 무방 |
| `warm` | 빌드된 payload 슬라이스. 콜드면 202 |
| `heavy` | 대용량 gzip 전량. 동시 실행 2개 상한(429), MCP tool 부적합 |

---

## 2. 엔드포인트

### 세션 찾기

| 경로 | 주요 파라미터 | 돌려주는 것 |
|---|---|---|
| `GET /sessions` | `product`·`product_type`·`lot_id`·`q`·`date_from/to`(epoch)·`sort`·`limit`(≤100)·`offset` | 세션 목록 + total. **다른 모든 호출의 출발점** |
| `GET /compare-sessions?sids=a,b,c` | `sids`(≤5)·`items` | 세션별 수율·worst CPK 를 나란히. 콜드 세션은 `building[]` 에 담아 알린다 |
| `GET /capabilities` | — | 이 규약 전체(JSON) |

### 세션 하나 읽기 (`/{session_id}/...`)

| 경로 | 등급 | 돌려주는 것 |
|---|---|---|
| `/overview` | warm | 모드·source 목록·**수율 요약(+STEP 분해)**·수율 분모 기준·**ENGR 결론(사람이 쓴 3칸)** |
| `/build-status` | cheap | `state`/`stage`/`eta`/`cold`/`blocked` — 202 폴링용 |
| `/yield` | warm | Yield 표 행 + `step_groups`(P1/P2/P3) + `bin_groups` + 분모 근거 |
| `/fail-bins` | warm | Fail Bin 랭킹 + bin 요약 |
| `/cpk` | warm | 항목×source CPK 행(기본 나쁜 순). `item`·`source`·`worst_n`(≤200)·`offset` |
| `/issue-table` | warm | **Issue Table 계산본** — `table=main\|temp\|compare` |
| `/items` | warm | 측정 항목 카탈로그(표본수·limit·cpk). `keyword`·`limit`(≤500) |
| `/items/{subject}/stats` | warm | 항목 1개의 source 별 통계 + cpk + limit (**값 배열 없음**) |
| `/compare` | warm | Compare 세션의 source 간 비교. `section=summary\|dist_shift\|equivalence\|bin_delta\|bin_matrix\|goodlog\|new_items` |
| `/temperature` | warm | RT/CT/HT 구성 + 온도별 재판정 이슈 행 |
| `/map` | warm | Map 경량 메타(맵 목록·bin 집계) |
| `/input-info` | cheap | source 별 입력 파일·STDF 메타(manifest 만 읽는다) |
| `/raw-data/columns` | cheap | 컬럼 메타 + source 목록 + 전체 die 수 |
| `/raw-data` | heavy | Raw Data 저장값 페이지. `columns`(콤마, ≤60)·`search`·`bin`·`source`·`limit`(≤2000)·`offset` |

### 대용량 (`heavy` — 429 대상)

| 경로 | 돌려주는 것 |
|---|---|
| `/items/{subject}/values` | 항목 1개 측정값 **전량** + 좌표 (gzip) |
| `/distribution` | 전 항목 ECDF **전량** (gzip). 다운샘플하지 않는다 |
| `/map/dies` | Map die 좌표·bin **전량** (gzip) |
| `/full` | report payload 전체 (JSON) — 개별 함수로 충분하면 쓰지 말 것 |
| `/raw-data/sources/{index}.csv` | source 1개 raw CSV 스트림 |
| `/raw-data/all.zip` | 전 source raw CSV zip 스트림 |

gzip 경로는 `Accept-Encoding: gzip` 이면 압축 그대로, 아니면 서버가 풀어 준다.
`ETag` + `If-None-Match` 304 를 지원한다.

---

## 3. Issue Table 응답의 특이점 (가장 자주 오해하는 곳)

`/issue-table` 은 **계산본**이다 — 사람이 코멘트를 단 행뿐 아니라 수율·CPK 가 나빠
**자동 생성된 행까지 전부** 나온다. 각 행에는 다음이 함께 실린다:

- `row_key` — 저장 키(`Yield|<bin>|<item>` / `CPK|<item>` / `TEMP|<item>` / `ETC|<item>`).
  응답의 `Category` 셀은 섹션 첫 행에만 채워지므로(화면 병합) 서버가 이어서 재구성해 준다.
- `PTE comment` / `개발 comment` — 화면 전용 서식 토큰(`*[..]`·`*r[..]`)을 **벗긴 평문**.
- `AI Comment` · `Signature` — ai_comment 옵션 세션에만.
- `Status` — `Open` / `Close`.

주의 두 가지:

1. **사용자가 숨긴 행은 응답에도 없다** — 화면과 같은 목록이다(빌드 시점에 빠진다).
2. **섹션 머리행은 빠진다** — CPK subhead·ETC 헤더는 값이 아니라 구분선이다.

---

## 4. 설계 원칙 (고칠 때 지킬 것)

1. **새 계산을 만들지 않는다.** 값은 전부 `web_report/service.py` 와 payload 에서
   가져다 슬라이스만 한다. API 가 자체 계산을 하면 화면과 숫자가 갈라지고, 그 순간
   사용자는 리포트 전체를 신뢰하지 않는다(CLAUDE.md 규칙 13).
2. **`viewer=None` 을 만들지 않는다.** `database/sessions.py:_history_where` 는
   `viewer=None` 이면 비공개 필터를 통째로 생략한다. 항상 `""`(공개만) 또는 검증된 uid.
3. **콜드 빌드를 동기 대기하지 않는다.** waitress 스레드가 13개뿐이라 외부 폴러 하나가
   사람 요청까지 굶긴다. `build_if_cold=False` + 백그라운드 예약 + 202.
4. **캐시 공유 객체를 고치지 않는다.** `load_webreport` 가 돌려주는 report 는 캐시가
   들고 있는 그 객체다. 행을 고르거나 정렬하기 전에 반드시 `dict(row)` 로 복사한다.
5. **접근제어는 캐시 조회보다 먼저.** 캐시 키에 viewer 가 없으므로(`cache_policy.py`)
   캐시 계층은 권한을 막아 주지 않는다.

---

## 5. 아직 열지 않은 것

- **편집(쓰기) API** — 코멘트 작성·Status 변경 등. 인증 주체와 감사로그 규칙이 확정된
  뒤에 연다. 열 때는 저장 키 불변 규칙(CLAUDE.md 규칙 12 — row_key 파서가 4곳)과
  `X-Honey-Agent`/CSRF 관례를 함께 따져야 한다.
- **eval 이력·L1/L2 트레이스**(`raw_metrics`/`features` 축) — eval.db 는 외부 담당자
  환경에 의존하고 `eval_engine` import 단방향 규약(3곳)이 걸려 있어 별도 설계·승인이
  필요하다. web_report 쪽 "소스별 기초 통계"는 `/items/{subject}/stats` 가 대신한다.
- **Trim / Gap Chart / Commonality / 노트·차트 주석** — 규약만 잡아 두고 구현은 후속.
  전부 `web_report/service.py` 에 대응 함수가 이미 있어 라우트 1개씩 추가하면 된다.
