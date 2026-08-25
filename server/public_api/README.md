# 공개 REST API (`/pe/api/v1`)

report_server 가 사내망에 공개하는 **읽기 전용 HTTP API**. 다른 서버·스크립트·엑셀 매크로
어디서든 일반 HTTP GET 으로 호출한다.

## 기본 규약

| 항목 | 값 |
|---|---|
| Base URL | `http://12.81.220.117:8080/pe/api/v1` |
| 인증 | **없음** — 사내망에서 도달만 하면 된다. 토큰·쿠키·User-Agent 불필요 |
| 메서드 | **GET only**. 요청 본문 없음, 파라미터는 전부 쿼리스트링 |
| 응답 | 항상 `application/json` (UTF-8). 성공/에러 모두 JSON |
| 상태코드 | 200 성공 / 400 파라미터 오류 / 404 미매칭 / 500 서버 오류 |

> Base URL 의 IP·포트 정본은 [server/env/server.env](../env/server.env) 의 `SERVER_BASE_URL`
> 이다. 서버 주소가 바뀌면 이 문서도 함께 고친다.

### 에러 응답 형식

```json
{"error": "bad_request", "message": "part_id is required"}   // 400
{"error": "not_found"}                                        // 404
{"error": "internal server error"}                            // 500
```

### 호출 측 권장 사항

- timeout 을 지정한다 (5초 내외면 충분 — 전부 메모리/단순 조회다).
- **404 는 정상 결과**다("그런 part_id 가 없다"). 재시도하지 말고 없는 것으로 처리한다.
- 재시도는 5xx 와 네트워크 오류에만. 백오프 없이 연타하지 않는다.

### 버저닝 약속

`/v1` 의 응답 필드는 **추가만** 하고 삭제·개명하지 않는다. 호출 측을 깨뜨리는 변경이
필요하면 `/v2` 를 새로 만든다. 따라서 응답 파싱은 "아는 키만 읽고 모르는 키는 무시"
하도록 작성하면 안전하다.

---

## 엔드포인트

### `GET /product-info/candidates`

기준정보에 등록된 part_id 검색 후보 전체 (part_id + sub_part_id 를 펼친 목록, 정렬·중복제거).

파라미터 없음.

```json
{
  "candidates": ["ABC123", "ABC123-1", "XYZ999"],
  "count": 3
}
```

기준정보 DB 파일이 서버에 없으면 에러가 아니라 **빈 목록 + 200** 이다.

### `GET /product-info/lookup?part_id=<part_id>`

part_id (또는 sub_part_id) 하나에 대한 기준정보 14개 컬럼.

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `part_id` | 예 | `/product-info/candidates` 가 돌려준 값 중 하나 |

```json
{
  "part_id": "ABC123",
  "sub_part_id": "{ABC123-1, ABC123-2}",
  "product_group": "MDDI",
  "wf_size": "12",
  "chip_size_x": "5.12",
  "chip_size_y": "4.08",
  "gross_die": "1234",
  "pkg_type": "COF",
  "e2f_fab_site": "...",
  "step": "...",
  "temperature": "...",
  "equip": "...",
  "para": "...",
  "flat_zone": "..."
}
```

- `part_id` 누락 → **400**
- 등록되지 않은 part_id → **404** (`{"error": "not_found"}`)
- 값은 전부 문자열이다. 숫자로 쓰려면 호출 측에서 변환한다.

### `GET /help/features`

HONEY와 web_report의 현재 사용자 기능 카탈로그. 파라미터 없이 호출하면 전체를 반환하고,
아래 선택 필터를 조합할 수 있다.

| 파라미터 | 필수 | 설명 |
|---|---|---|
| `q` | 아니오 | 제목·별칭·키워드·설명 자연어 검색(한글·영문 대소문자, 공백·기호 차이 무시, 최대 200자) |
| `category` | 아니오 | `getting_started`, `report_management`, `input_upload`, `rawdata_excel`, `report_tabs`, `support` |
| `surface` | 아니오 | `landing`, `search`, `honey`, `web_report`, `support`, `chatbot` |
| `status` | 아니오 | `available`, `conditional`, `coming_soon` |

```json
{
  "schema_version": 1,
  "catalog_version": "2026-08-12",
  "count": 1,
  "features": [{
    "id": "temperature-mode",
    "category": "input_upload",
    "title": "Temperature 모드",
    "aliases": ["Temperature 모드", "온도 분석 모드", "RT CT HT"],
    "keywords": ["Temperature", "RT", "CT", "HT", "ROOM", "COLD", "HOT", "Limit 파일"],
    "status": "conditional",
    "surfaces": ["honey", "web_report"],
    "audience": ["all"],
    "availability": "PMIC·SECURITY 전용",
    "summary": "RT를 기준으로 CT·HT corner를 그룹화하고 온도별 Fail을 분석합니다.",
    "usage": ["PMIC 또는 SECURITY를 선택한 뒤 Temperature를 고릅니다."],
    "cautions": ["각 그룹의 RT가 Limit 기준이며 Yield는 RT source만 계산합니다."],
    "help_anchor": "new-report",
    "related_ids": ["source-arrangement", "issue-table-temp", "map-analysis"]
  }]
}
```

`schema_version`은 응답 필드 계약 버전이고 `catalog_version`은 기능 내용의 갱신 버전이다.
v1 소비자는 모르는 필드를 무시해야 하며 기존 필드는 삭제·개명하지 않는다.

검색 결과가 없으면 200과 빈 `features`를 반환하고 `help_url`을 함께 준다. 알 수 없는
category·surface·status 또는 200자를 넘는 `q`는 `400 bad_request`다.

```json
{"error": "bad_request", "message": "unknown status: disabled (...)"}
```

### `GET /web-report/...` — web_report 조회 API

세션의 계산 결과(수율·CPK·Issue Table·Map·Distribution·raw data 등)를 읽는 함수 22개.
**규약 정본은 [web_report/CONTRACT.md](web_report/CONTRACT.md)** 이고, 살아 있는 목록은
서버에서 직접 받는다:

```bash
curl "http://12.81.220.117:8080/pe/api/v1/web-report/capabilities"
```

위 product-info/help 와 다른 점 세 가지만 여기 적는다:

- **202 가 정상 흐름이다.** 리포트가 아직 계산되지 않은 세션(콜드)은 200 도 500 도 아니라
  `202 {"building":true, "status_url", "retry_after_sec"}` 다. 서버는 요청 스레드에서
  계산을 기다리지 않고 백그라운드 빌드만 예약한다 — 기다리게 하면 외부 폴러 하나가
  사람 요청까지 굶긴다. 호출 측은 `status_url` 을 폴링한 뒤 재요청한다.
- **인증이 선택적으로 붙는다.** 기본은 다른 기능과 같은 무인증이며 **공개 세션만** 보인다.
  서버에 env `WEB_REPORT_API_KEY` 가 설정돼 있고 요청이 헤더 `X-Report-Api-Key` 로 같은
  값을 제시하면 비공개 세션까지 조회된다. 키가 틀려도 차단이 아니라 공개 범위다.
- **404 는 "없음"과 "권한 없음"을 합친 응답이다.** 비공개 세션의 존재 자체를 숨기기 위해
  둘을 구분하지 않는다.

대용량 응답(ECDF 전량·map die 전량·raw CSV)은 동시 2개까지만 처리하고 넘으면 `429` 다.

### `GET /help/features/<id>`

기능 ID 한 건을 같은 envelope 형식으로 반환한다. 없는 ID는 `404 not_found`다.
`help_anchor`는 `/pe/report/help#<help_anchor>`로 연결할 수 있다.

```json
{"error": "not_found"}
```

---

## 호출 예시

```bash
curl "http://12.81.220.117:8080/pe/api/v1/product-info/candidates"
curl "http://12.81.220.117:8080/pe/api/v1/product-info/lookup?part_id=ABC123"
curl "http://12.81.220.117:8080/pe/api/v1/help/features?q=Temperature%20모드"
curl "http://12.81.220.117:8080/pe/api/v1/help/features/temperature-mode"
```

```python
import requests

BASE = "http://12.81.220.117:8080/pe/api/v1"

r = requests.get(f"{BASE}/product-info/lookup",
                 params={"part_id": "ABC123"}, timeout=5)
if r.status_code == 404:
    info = None                  # 미등록 part_id — 에러 아님
else:
    r.raise_for_status()
    info = r.json()              # 14개 컬럼 dict
```

```javascript
const BASE = "http://12.81.220.117:8080/pe/api/v1";
const res = await fetch(`${BASE}/product-info/candidates`);
const { candidates } = await res.json();
```

---

## 데이터 원천과 갱신

기준정보는 서버의 `product_info.db` (SQLite, 읽기 전용)에서 읽는다. 원본 CSV 가 DRM 으로
암호화돼 있어 Excel 이 설치된 별도 PC 에서
[tools/product_info_import](../../tools/product_info_import/README.md) 로 만든 `.db` 를
서버에 복사하는 방식이다. 파일을 갈아끼우면 **서버 재기동 없이** 자동 재로딩된다
([server/product_info.py](../product_info.py)).

즉 이 API 의 데이터는 그 `.db` 를 복사한 시점의 스냅샷이며, 실시간 기준정보 시스템이 아니다.

---

## 코드 구조 (기능 추가 규칙)

**기능 하나 = 하위 폴더 하나 = Blueprint 하나.**

```
server/public_api/
├── __init__.py            register_public_api(app) — 기능별 Blueprint 등록만
├── metrics.py             호출 계측 (관리자 패널 'public API' 탭 — 아래 절)
├── README.md              이 문서 (외부 소비자용 접근 규약)
├── product_info/
│   └── routes.py          public_api_product_info — /pe/api/v1/product-info/*
├── help/
│   └── routes.py          public_api_help — /pe/api/v1/help/*
├── web_report/            public_api_web_report — /pe/api/v1/web-report/*
│   ├── contracts.py       ★규약 정본(FUNCTION_SPECS) — /capabilities·MCP·관리자 탭이 파생
│   ├── facade.py          Flask 무의존 조회 함수층 (MCP·챗봇도 이걸 쓴다)
│   ├── routes.py          HTTP 층 (분기키→상태코드 변환 + 대용량 세마포어)
│   └── CONTRACT.md        사람이 읽는 규약 문서
└── client_functions/      (껍데기) client 기능의 서버 이전 자리 — 미등록, 외부 담당자 소유
```

규약(contracts.py)이 있는 기능은 **관리자 패널 'public API' 탭 맨 위**에 함수·입출력 표로
자동 표시되고, 규약↔실제 라우트의 어긋남(문서 누락/앞서감)도 거기서 바로 보인다.

새 기능(예: eval 이력 조회)을 붙일 때:

1. `public_api/<기능>/routes.py` 에 Blueprint 를 만든다. 라우트 경로는 prefix 를 뺀
   나머지만 준다 (예: `@bp.get("/candidates")`).
   **Blueprint 이름은 반드시 `public_api_<기능>` 으로 시작해야 한다** —
   `Blueprint("public_api_eval_history", __name__)`. 관리자 패널의 부하 계측이
   Flask endpoint 이름의 이 접두로만 공개 API 요청을 식별하므로, 이름이 어긋나면
   그 기능은 **모니터링에서 통째로 누락된다**(서버 기동 로그에 경고가 남는다).
2. `public_api/__init__.py` 의 `register_public_api()` 에 `_register(app, <기능>_bp,
   "<url-경로>")` 한 줄을 추가한다. URL prefix 와 이름 검사는 `_register` 가 처리한다.

기존 기능 폴더는 건드리지 않는다. 하위 폴더에 `__init__.py` 는 두지 않는다
(namespace package — 등록 진입점은 `public_api/__init__.py` 하나뿐이다).

## 모니터링 (관리자 패널 'public API' 탭)

공개 API 호출은 `/pe/admin-<secret>/` 의 **public API** 탭에서 endpoint 별 호출수·
응답시간·에러·호출자 IP·분당 추이로 볼 수 있다. 기능이 늘어도 별도 배선 없이
위 이름 규약만 지키면 자동으로 잡힌다.

**"서버에 부담인가"는 호출수가 아니라 `busy_pct` 로 본다** — 구간 내 총 소요시간을
`WAITRESS_THREADS × 구간`으로 나눈 값, 즉 공개 API 가 요청 처리 스레드를 몇 % 점유
했는가다. 호출이 잦아도 응답이 짧으면 0 에 가깝고, 이 값이 두 자릿수로 올라가면
사람 요청이 밀리기 시작한다는 뜻이다.

공개 API 요청은 관리자 패널의 **사람 트래픽 지표에서 제외**된다 — 응답시간 p50/p95
표본과 '실시간 접속 사용자' 목록에 넣지 않는다. 무인증 폴러가 섞이면 표본 대부분을
차지해 실사용자의 체감 악화를 가리기 때문이다
([server/admin_panel/metrics.py](../admin_panel/metrics.py)).

관련 환경변수: `PUBLIC_API_METRICS_ENABLED`(기본 1, `0`이면 계측 끔) ·
`PUBLIC_API_SLOW_MS`(기본 1000 — 느린 호출 기록 기준) ·
`REPORT_METRICS_FILE_KEEP_DAYS`(기본 14 — `server/log/publicapi_*.log` 보관 일수).

## 향후 확장 (아직 없음)

ENGR 이력·평가 결과(eval.db) 조회를 같은 방식으로 추가할 예정이다. 그때 반드시 지킬 것:

- `chatbot/tools_report.py` 호출 시 `viewer=""`, `see_all_private=False` 를 **하드코딩**한다.
  `viewer=None` 은 [database/sessions.py](../database/sessions.py) `_history_where` 에서
  비공개 필터를 아예 생략하는 함정이다 (`""` 는 공개 세션만 통과 — 안전).
- eval.db 에는 비공개 세션에서 유래한 코멘트·측정값이 섞여 있다 (`web_report/eval_export.py`
  에 `is_private` 검사가 없어 전 세션을 export 한다). 노출 전에 결과 행의 `session_id` 를
  report.db 에서 조회해 비공개/삭제 세션 유래 행을 걸러야 한다.
