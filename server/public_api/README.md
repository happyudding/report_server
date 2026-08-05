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

---

## 호출 예시

```bash
curl "http://12.81.220.117:8080/pe/api/v1/product-info/candidates"
curl "http://12.81.220.117:8080/pe/api/v1/product-info/lookup?part_id=ABC123"
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
├── README.md              이 문서 (외부 소비자용 접근 규약)
└── product_info/
    └── routes.py          product_info_bp — /pe/api/v1/product-info/*
```

새 기능(예: eval 이력 조회)을 붙일 때:

1. `public_api/<기능>/routes.py` 에 Blueprint 를 만든다 (`<기능>_bp`, 라우트 경로는
   prefix 를 뺀 나머지만 — 예: `@bp.get("/candidates")`).
2. `public_api/__init__.py` 의 `register_public_api()` 에 등록 2줄을 추가한다.
   URL prefix 는 `f"{URL_PREFIX}/<기능>"` 으로 준다.

기존 기능 폴더는 건드리지 않는다. 하위 폴더에 `__init__.py` 는 두지 않는다
(namespace package — 등록 진입점은 `public_api/__init__.py` 하나뿐이다).

## 향후 확장 (아직 없음)

ENGR 이력·평가 결과(eval.db) 조회를 같은 방식으로 추가할 예정이다. 그때 반드시 지킬 것:

- `chatbot/tools_report.py` 호출 시 `viewer=""`, `see_all_private=False` 를 **하드코딩**한다.
  `viewer=None` 은 [database/sessions.py](../database/sessions.py) `_history_where` 에서
  비공개 필터를 아예 생략하는 함정이다 (`""` 는 공개 세션만 통과 — 안전).
- eval.db 에는 비공개 세션에서 유래한 코멘트·측정값이 섞여 있다 (`web_report/eval_export.py`
  에 `is_private` 검사가 없어 전 세션을 export 한다). 노출 전에 결과 행의 `session_id` 를
  report.db 에서 조회해 비공개/삭제 세션 유래 행을 걸러야 한다.
