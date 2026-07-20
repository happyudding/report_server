# 16. VOC 게시판 UX 개편 설계 (보류)

> **상태: 보류 (2026-07-20 결정)**
> 아래 설계는 검토·확정까지 끝났으나 **구현하지 않았다.**
> - VOC **진입 버튼은 Confluence 외부 링크**로 되돌렸다 (Honey 도움말 메뉴 VOC, HELP
>   다이얼로그 VOC 버튼, 검색결과 페이지 헤더 📢 VOC).
> - 서버측 VOC 코드([routes_voc.py](../server/report/routes_voc.py) ·
>   [voc.html](../server/report/voc.html) · [voc_db.py](../server/database/voc_db.py) ·
>   [tests/test_voc.py](../tests/test_voc.py) · `config.REPORT_VOC_DB_PATH`)는 **무수정 보존**한다.
>   `/pe/report/voc` 직접 접속은 여전히 동작하지만 어디에서도 링크하지 않는다.
> - 재개할 때는 이 문서대로 구현하면 된다. 아래 "현재 구현" 은 보류 시점(커밋 ec77768) 상태다.

---

## 1. 배경 / 요구사항

현재 VOC 화면은 **등록 폼 + 본문 전문이 인라인으로 펼쳐진 목록** 한 화면뿐이다. 글이 쌓이면
스크롤이 길어지고, 검색·상태 추적·문의 이어가기(댓글)가 불가능하다. 유명 포탈 게시판 UX 로
개편한다.

사용자 확정 요구:

1. VOC 접속 시 **목록만** 표시. 상단에 **제목/번호 검색 칸** + **등록 버튼**.
2. 목록 컬럼: **번호 · 제목 · Status · 등록자** 순. 번호는 1부터 증가.
3. **Status = Open / Close.** 등록 시 무조건 Open, **관리자만** Close 로 변경.
4. 등록 버튼 → 등록 화면: **제목 칸 + 작성 칸(placeholder "본문 입력해주세요") + 등록 버튼**.
   기존 분류 select(버그/개선 제안/문의/기타)와 스크린샷 첨부(≤3장)는 **유지**.
5. 상세 화면: 제목·내용·Status·등록자 표시. **작성자 본인만 수정 버튼**.
6. 상세 하단 **댓글**: 입력창 + 등록 버튼 → 본문 밑에 댓글 표시.
7. 지정하지 않은 부분은 포탈 VOC/게시판 UX 벤치마킹.

**관리자 판별 방식(확정)**: 관리자 대시보드(`/pe/admin-<secret>/`) 로그인 쿠키 재사용.
사용자 단위 관리자 계정 목록은 만들지 않는다.

**번호 = DB `report_voc.id`.** 삭제 시 결번이 생기지만, 번호 검색의 기준이 흔들리지 않으려면
표시용 재번호(순번)를 쓰면 안 된다.

---

## 2. 현재 구현 (보류 시점)

| 항목 | 위치 | 요지 |
|---|---|---|
| 페이지 | [server/report/voc.html](../server/report/voc.html) | 492줄 인라인 CSS/JS 자기완결 단일 페이지. 상단 등록 폼 카드 + 아래 목록 카드(본문 전문·썸네일 인라인, 본인 글 삭제, 20건 페이지네이션). XSS-안전 렌더, 테마 토글(`localStorage report_theme` 공유), 토스트, 라이트박스 `<dialog>`, error_beacon.js |
| 라우트 | [server/report/routes_voc.py](../server/report/routes_voc.py) | `GET /pe/report/voc`(페이지) · `GET /api/voc`(목록, 익명 허용) · `POST /api/voc`(등록: CSRF+신원, multipart) · `GET /api/voc/<id>/screenshots/<image_id>` · `DELETE /api/voc/<id>`(작성자 본인) |
| DB | [server/database/voc_db.py](../server/database/voc_db.py) | 세션 DB 와 분리된 별도 SQLite(`config.REPORT_VOC_DB_PATH`). `open_conn()` 이 매번 `executescript(SCHEMA)` 멱등 생성 + WAL + `foreign_keys=ON`. `report_voc` / `report_voc_image`(메타만 — 실파일은 storage_gateway `voc_<id>` 네임스페이스) |
| 신원 | [server/auth_identity.py](../server/auth_identity.py) `current_user()` | HoneyUser UA 토큰(소문자 정규화), 없으면 `""` = 읽기 전용 |
| 관리자 | [server/admin_panel/routes.py](../server/admin_panel/routes.py) | 쿠키 게이트. `pe_admin_gate` = `sha256("pe-admin-gate\|" + REPORT_ADMIN_PASSWORD)`, **`path=/pe/admin-<secret>`** |

없는 것: 상세 페이지 · 수정 · 댓글 · Status · 검색.

---

## 3. 설계 (재개 시 이대로 구현)

### 3.1 DB — [voc_db.py](../server/database/voc_db.py)

`report_voc` 에 컬럼 추가, 댓글 테이블 신설 (규칙 #2 — `report_` prefix 유지):

```sql
-- report_voc CREATE 문에 추가
status     TEXT    NOT NULL DEFAULT 'open',   -- 'open' | 'close' (관리자만 close)

CREATE TABLE IF NOT EXISTS report_voc_comment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    voc_id     INTEGER NOT NULL REFERENCES report_voc(id) ON DELETE CASCADE,
    user_id    TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_voc_comment_voc
    ON report_voc_comment(voc_id, id);
```

기존 voc.db 마이그레이션 — `open_conn()` 의 `executescript(SCHEMA)` 직후:

```python
def _migrate(conn):
    """구 voc.db 멱등 마이그레이션 — status 컬럼 추가."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(report_voc)")}
    if "status" not in cols:
        try:
            conn.execute("ALTER TABLE report_voc ADD COLUMN status TEXT NOT NULL DEFAULT 'open'")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
```

모듈 플래그를 두지 않는 이유: 이 파일은 이미 "커넥션 오픈마다 스키마 보장" 철학이고,
플래그는 "ALTER 후 트랜잭션 rollback 됐는데 플래그는 True" 엣지를 만든다. Flask 는 waitress
**단일 프로세스**(스레드 8)이고 컴퓨트 워커는 voc_db 를 import 하지 않으므로, 드문 스레드
레이스는 duplicate column 무시만으로 충분히 멱등하다.

함수 변경/추가:

- `list_voc(limit=20, offset=0, q=None)` — **lean 화**: `id, user_id, category, title, status,
  created_at` + `comment_count` 서브쿼리. `content`·이미지 조인 제거(목록은 본문 안 씀).
  검색 `q`: `\`·`%`·`_` 이스케이프 후 `title LIKE ? ESCAPE '\'`, `q.isdigit()` 이면
  `(id = ? OR title LIKE ?)`. `total` 도 같은 WHERE 로 카운트(검색 결과 페이지네이션 정합).
- `get_voc` 에 `status` 포함.
- 신규: `list_voc_images(voc_id)` · `update_voc(voc_id, category, title, content)` ·
  `set_voc_status(voc_id, status)` · `add_comment(voc_id, user_id, content)` ·
  `list_comments(voc_id)`(id 오름차순) · `get_comment(voc_id, comment_id)` ·
  `delete_comment(comment_id)`.

### 3.2 관리자 판별 — admin 게이트 쿠키 재사용

문제: 기존 `pe_admin_gate` 쿠키는 `path=/pe/admin-<secret>` 이라 `/pe/report/*` 요청에 실려오지
않는다.

해결: **login 이 동일 토큰의 두 번째 쿠키를 `path=/pe/report` 로 추가 발급**한다.

- [admin_panel/\_\_init\_\_.py](../server/admin_panel/__init__.py) (경량 모듈 — config 만 import)에
  공유 심볼을 둔다: `GATE_COOKIE_VOC = "pe_admin_gate_voc"` + `gate_token()`.
  `admin_panel/routes.py` 의 `_expected_token()` 은 이를 위임 호출(공식 중복 제거).
  → routes_voc 가 무거운 `admin_panel.routes`(서브모듈 8개 import)를 끌어오지 않게 하려는 배치.
  순환 import 는 없음을 확인했다(`report/__init__.py`·`database/__init__.py` 모두 빈 파일,
  `admin_panel/__init__.py` 는 routes 를 register 시점 지연 import).
- `login()` 에 추가:
  ```python
  resp.set_cookie(GATE_COOKIE_VOC, gate_token(), max_age=12 * 3600,
                  httponly=True, samesite="Lax", secure=request.is_secure,
                  path="/pe/report")
  ```
- 쿠키 이름을 반드시 분리할 것 — 같은 이름을 다른 path 로 재발급하면 브라우저 쿠키 교체/중복
  전송이 모호해진다.
- 보안 평가: httponly 라 `/pe/report` 페이지 JS 가 읽을 수 없고, 그 경로의 어떤 응답도 쿠키를
  반사하거나 감사 로그에 기록하지 않으며, status 변경 API 는 `_require_csrf()` 를 병행한다.
- 운영 주의: **배포 직후 기존 admin 세션은 새 쿠키가 없어 재로그인 1회 필요.**

### 3.3 라우트 — [routes_voc.py](../server/report/routes_voc.py)

추가: `import hmac`, `from admin_panel import GATE_COOKIE_VOC, gate_token`,
`_COMMENT_MAX = 1000`, `_STATUSES = ("open", "close")`,

```python
def _is_admin():
    """admin 대시보드 게이트 쿠키(path=/pe/report 사본) 검증."""
    return hmac.compare_digest(request.cookies.get(GATE_COOKIE_VOC, ""), gate_token())
```

기존 `voc_page` / `voc_create` / `voc_screenshot` / `voc_delete` 는 유지
(create 는 DB DEFAULT 로 status='open', delete 는 작성자 본인만).

| 메서드 / URL | 가드 | 동작 |
|---|---|---|
| `GET /api/voc` (개편) | 익명 허용 | `q`(strip, ≤120자) 추가. lean items + `comment_count`. 응답에 `is_admin` 추가, `content`/`screenshots`/`can_delete` 제거(상세로 이동) |
| `GET /api/voc/<int:id>` (신설) | 익명 허용 | 상세: voc(status 포함) + `screenshots` + `comments`(각 `can_delete` = 본인 or admin) + `user`/`is_admin`/`can_edit`/`can_delete`. 없으면 404 |
| `PATCH /api/voc/<int:id>` (신설) | CSRF + 신원(401) + 작성자(403) | JSON `{category,title,content}` — create 와 동일 검증(`_CATEGORIES`/1~120자/1~4000자). 스크린샷은 수정 대상 제외. 감사 `voc_edit` |
| `POST /api/voc/<int:id>/status` (신설) | CSRF + `_is_admin()`(403) | JSON `{"status":"open"\|"close"}`, `_STATUSES` 외 400, 글 없으면 404. 감사 `voc_status` (uid 없으면 `"admin-panel"`) |
| `POST /api/voc/<int:id>/comments` (신설) | CSRF + 신원(401) | content 1~1000자. **Close 글에도 허용**(Close 는 처리 상태 표시이지 잠금이 아님). 201 `{ok,id}`. 감사 `voc_comment_create` |
| `DELETE /api/voc/<int:id>/comments/<int:cid>` (신설) | CSRF + (댓글 작성자 or admin) | `get_comment(voc_id, cid)` 로 소속 확인, 없으면 404. 감사 `voc_comment_delete` |

감사는 기존 `_audit_voc()` 를 그대로 재사용한다 (`log_audit` 은 action 문자열을 검증하지 않음).

### 3.4 프론트 — [voc.html](../server/report/voc.html) 해시 라우팅 SPA

보존 자산: 테마 부트스트랩·토글(`report_theme`), `toast()`, `csrfToken()`, 라이트박스,
스크린샷 미리보기 로직, error_beacon.js, **XSS-안전 렌더(`createElement`+`textContent` 만)**,
디자인 토큰(`#4a90e2` 버튼, `.card`, `.cat-badge` 4색, 다크 override 추가 전용 패턴).

라우팅: `#`(목록) / `#new`(등록) / `#voc/<id>`(상세) / `#voc/<id>/edit`(수정).
`hashchange` 리스너 + 카드 3개(`#viewList`/`#viewForm`/`#viewDetail`) hidden 토글.
상태 `{page, q, user, isAdmin}` 를 메모리에 유지 → 상세에서 뒤로가기 시 같은 페이지·검색 복귀.

- **목록 뷰** — 툴바(검색 input placeholder `"제목 또는 번호 검색"` + Enter/🔍 버튼 │ 우측
  `✏️ VOC 등록` 버튼) + 익명 안내(기존 문구 재사용). 테이블 컬럼:
  번호(60px) │ 분류(`.cat-badge`) │ 제목(+`💬 N`, N>0 일 때만) │ Status(pill) │ 등록자 │ 등록일.
  행 클릭 → 상세. 페이지네이션 기존 로직 유지.
  검색 결과 없음: `"<q>" 검색 결과가 없습니다.` + 전체 보기 버튼.
- **등록/수정 뷰 (겸용 폼)** — 분류 select + 제목 + 본문 textarea(**placeholder "본문
  입력해주세요"**) + 글자수 + 스크린샷 첨부(수정 모드에서는 hidden + "스크린샷은 수정할 수
  없습니다" 안내) + [등록/저장] [취소]. 등록 성공(201) → `#voc/<id>` 상세 이동(GitHub Issues 방식).
  수정은 기존값 프리필 후 저장 시 상세 복귀.
- **상세 뷰** — `.cat-badge` + status badge + 제목, meta(`#번호 · 등록자 · 등록일`),
  본문(pre-wrap), 썸네일/라이트박스, 액션바([← 목록] │ 작성자 [수정][삭제] │
  관리자 [Close 처리]/[다시 열기]), 댓글 섹션(h3 "댓글 N" → 목록(작성자 굵게 + 시각 muted 14px
  + 본문 pre-wrap, 본인/admin 만 `.del-btn` 소형 삭제) → 하단 textarea(maxlength 1000) +
  [댓글 등록], 익명은 disabled + 안내, 빈 상태 "아직 댓글이 없습니다 — 첫 댓글을 남겨보세요.").
- 없는 id 딥링크 → 404 → toast `"삭제되었거나 없는 VOC 입니다."` + 목록 복귀.

UX 디테일 (벤치마킹 + 기존 토큰 정합):

| 항목 | 확정안 |
|---|---|
| Status badge | pill(`border-radius:999px`). **Open** 연녹 `#dcfce7`/`#15803d`/테두리 `#86efac`, **Close** slate `#f1f5f9`/`#64748b`/`#cbd5e1`. 다크: Open `#123420`/`#4ade80`, Close `#262a33`/`#8b94a3` |
| 목록 테이블 | `border-collapse:collapse`, th 15px `#64748b` + 2px 하단 보더, 행 `cursor:pointer` + hover `#f8fafc`(다크 `#262a33`), 보더 `#e5e7eb`(다크 `#2a2e37` — 기존 `.voc-item` 색 그대로) |
| Close 된 글 | 목록에서 제목만 muted(`#94a3b8`) + badge 로 구분 (취소선은 과함) |
| 날짜 | 목록은 짧게 `toLocaleDateString("ko-KR")`, 상세·댓글은 기존 `fmtTime` 풀 포맷 |
| 등록 버튼 | `.submit-btn` 파랑 토큰, 익명이면 disabled + title 안내 |

### 3.5 테스트 — [tests/test_voc.py](../tests/test_voc.py)

**주의: 목록 lean 화로 기존 테스트가 깨진다.** 현재 시나리오 b·d·e·h 는 목록 응답에
`content`/`screenshots`/`can_delete` 가 있다고 가정한다 → 상세 API 기반으로 이식(`_detail()` 헬퍼).

신규 시나리오: 신규 글 `status=='open'` + lean 필드/comment_count / 검색(제목 LIKE · 번호 ·
`%` 이스케이프) / PATCH 권한 매트릭스(본인 ok · 타인 403 · 익명 401 · CSRF 403 · 검증 400) /
status(비관리자 403 → `client.set_cookie("pe_admin_gate_voc", gate_token(), path="/pe/report")`
후 close→open) / 댓글(등록·순서·1001자 400·익명 401·타인 삭제 403·본인/관리자 삭제·글 삭제
CASCADE) / 마이그레이션(구 스키마 DB 선생성 + 행 삽입 → `open_conn()` → status 컬럼·기존 행 'open').

---

## 4. 구현 순서 / 검증

1. `voc_db.py` → 인라인 python 으로 함수 단위 + 구 스키마 마이그레이션 확인
   (`REPORT_VOC_DB_PATH` 를 임시 경로로)
2. `admin_panel/__init__.py` + `routes.py` → 서버 기동, login 응답 `Set-Cookie` 2개(경로 각각)
   확인 + 기존 admin 패널 회귀
3. `routes_voc.py` → curl 권한 매트릭스 스모크
4. `voc.html` → 브라우저 수동 검증(라이트/다크)
5. `tests/test_voc.py` → 단독 실행 전체 통과

API 스모크 (PowerShell + `curl.exe`):

```powershell
# CSRF 쿠키 획득
curl.exe -s -i http://127.0.0.1:8000/pe/report/voc | Select-String "Set-Cookie"
# 등록 (Honey 신원 = UA 토큰)
curl.exe -s -A "Mozilla/5.0 HoneyUser/tester" -b "report_csrf=<T>" -H "X-CSRF-Token: <T>" `
  -F "category=버그" -F "title=스모크" -F "content=본문" http://127.0.0.1:8000/pe/report/api/voc
# 검색 / 상세
curl.exe -s "http://127.0.0.1:8000/pe/report/api/voc?q=스모크"
curl.exe -s http://127.0.0.1:8000/pe/report/api/voc/1
# admin 로그인 → Set-Cookie 2개(pe_admin_gate path=/pe/admin-pte, pe_admin_gate_voc path=/pe/report)
curl.exe -s -i -H "Content-Type: application/json" -d "{\"password\":\"0023\"}" `
  http://127.0.0.1:8000/pe/admin-pte/login | Select-String "Set-Cookie"
# status: admin 쿠키 없이 403 → 있으면 200
curl.exe -s -b "report_csrf=<T>; pe_admin_gate_voc=<TOKEN>" -H "X-CSRF-Token: <T>" `
  -H "Content-Type: application/json" -d "{\"status\":\"close\"}" `
  http://127.0.0.1:8000/pe/report/api/voc/1/status
```

기존 voc.db: 사본 백업 → 기동 후 API 1회 호출 →
`PRAGMA table_info(report_voc)` 로 status 컬럼 + 기존 행 `'open'` 확인.

---

## 5. 한계 (재개 시 고려)

- **관리자 일반 브라우저는 HoneyUser 신원이 없어 댓글 작성 불가** (Close·댓글 삭제는 가능).
  Honey 내장 브라우저에서 admin 로그인하면 신원+admin 쿠키를 동시 보유해 겸용 가능.
- 스크린샷은 수정 화면에서 변경 불가(등록 시에만 첨부).
- 배포 직후 기존 admin 로그인 세션은 재로그인 1회 필요.
