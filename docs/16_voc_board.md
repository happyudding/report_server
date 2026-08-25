# 16. VOC 게시판 (목록 · 상세 · 상태 · 댓글)

사용자 의견(VOC)을 서버에서 직접 접수·추적하는 게시판. 포탈 게시판 UX 를 따라
**목록 → 상세 → 댓글** 흐름이고, 처리 상태(Open/Close)로 접수 건을 추적한다.

> ⚠️ **현황: 기능은 등록돼 있으나 운영에서 사실상 쓰이지 않는다.** `voc.db` 는 그래서
> DB 백업 대상에서도 빠져 있다([server/README.md](../server/README.md) 백업 절).
> 코드·라우트는 그대로 살아 있으므로 아래 설명은 유효하다 — 다시 쓰기로 하면
> 백업 대상 편입부터 검토할 것.

진입: 검색결과 페이지 헤더 `📢 VOC` · Honey 도움말(&H) → VOC · 도움말 창 하단 VOC 버튼
(모두 `/pe/report/voc`). 구 Confluence 외부 링크를 대체했다.

세션 DB(report.db)와 **분리된 별도 SQLite**(`REPORT_VOC_DB_PATH`)를 쓰고, 스크린샷 실파일은
동결된 storage_gateway 의 note_image 공개 API 를 `voc_<id>` 네임스페이스로 재사용한다.

---

## 1. 화면 (SPA)

[voc.html](../server/report/voc.html) 한 장이 인라인 CSS/JS 로 자기완결한다(외부 참조는
error_beacon.js 뿐). **해시 라우팅**이라 뒤로가기·딥링크가 동작한다.

| 해시 | 화면 | 내용 |
|---|---|---|
| `#` | 목록 | 검색바(제목 또는 번호) + `✏️ VOC 등록` 버튼, 테이블 **번호 │ 분류 │ 제목(💬 댓글수) │ Status │ 등록자 │ 등록일**, 행 클릭 → 상세, 20건 페이지네이션 |
| `#new` | 등록 | (게스트면 이름 +) 분류 select + 제목 + 본문(placeholder "본문 입력해주세요") + 스크린샷 첨부(≤3장) + [등록][취소]. 성공 시 새 글 상세로 이동 |
| `#voc/<id>` | 상세 | 분류·Status badge·제목 / `#번호 · 등록자 · 등록일` / 본문 / 썸네일(클릭 확대) / 액션바 / 댓글 |
| `#voc/<id>/edit` | 수정 | 등록 폼 재사용(기존값 프리필). 스크린샷은 수정 대상 아님 |

- 목록 상태(`page`/`q`)를 메모리에 유지해 상세에서 뒤로가기 시 같은 페이지·검색으로 복귀한다.
- 목록 테이블은 `table-layout: fixed` — 번호 52 / 분류 82 / Status 68 / 등록자 96 / 등록일 88px
  로 고정하고 **제목이 남는 폭을 전부 갖는다**. 제목이 길면 말줄임하고 전체는 `title` 툴팁.
- Status badge: Open = 연녹 pill, Close = slate pill. 목록에서 Close 된 글은 제목이 muted.
- 상세 액션바: `← 목록` / 작성자에게만 `✏️ 수정`·`삭제` / 관리자에게만 `Close 처리`(또는 `다시 열기`).
- 댓글: 본문 아래 "댓글 N" → 작성순 목록(본인·관리자만 삭제 버튼) → 입력창 + `댓글 등록`.
- **일반 브라우저(게스트)**: Honey 신원이 없으면 등록 폼과 댓글 폼에 이름 칸이 뜬다(§3.2).
  게스트가 쓴 글·댓글에는 `guest` 배지가 붙어 Honey 계정 글과 구분된다. 이름은 다음 등록 때
  다시 치지 않도록 `localStorage voc_guest_name` 에 기억하지만, **소유권 증표는 아니다**.
- 렌더는 전부 `createElement` + `textContent` (XSS-안전). 테마는 `localStorage report_theme` 를
  검색결과·세션 상세 페이지와 공유한다.

---

## 2. API — [routes_voc.py](../server/report/routes_voc.py)

blueprint 는 `report_bp` (`/pe/report`). CSRF 는 `report_bp.after_request` 가 발급하는
`report_csrf` 쿠키 ↔ `X-CSRF-Token` 헤더 double-submit (`_require_csrf`).

| 메서드 / URL | 가드 | 비고 |
|---|---|---|
| `GET /voc` | — | 페이지(gzip) |
| `GET /api/voc` | 익명 허용 | 최신순 + `limit`/`offset` + `q`(제목 부분일치, 숫자면 번호도). 본문·스크린샷 없는 lean 목록 + `comment_count`. 응답에 `user`/`is_admin` |
| `GET /api/voc/<id>` | 익명 허용 | 상세 — `voc`(status·is_guest 포함) + `screenshots` + `comments`(각 `can_delete`·`is_guest`) + `user`/`is_admin`/`can_edit`/`can_delete` |
| `POST /api/voc` | CSRF + 신원 **또는 `guest_name`** | multipart 등록. 분류 4종 / 제목 1~120자 / 내용 1~4000자 / PNG·JPEG ≤3장·장당 2MB(매직바이트 검사). 상태는 항상 `open` |
| `PATCH /api/voc/<id>` | CSRF + **작성자**(게스트는 등록한 브라우저) | JSON 수정(분류·제목·내용). 등록과 동일 검증. 스크린샷은 대상 외 |
| `POST /api/voc/<id>/status` | CSRF + **관리자** | `{"status":"open"\|"close"}` |
| `POST /api/voc/<id>/comments` | CSRF + 신원 **또는 `guest_name`** | `{"content": 1~1000자}`. **Close 된 글에도 허용** — 상태는 처리 표시이지 잠금이 아니다 |
| `DELETE /api/voc/<id>/comments/<cid>` | CSRF + **작성자 or 관리자** | 소속(voc_id) 확인 후 삭제 |
| `GET /api/voc/<id>/screenshots/<image_id>` | — | 소속 확인 후 서빙. `nosniff`, `private, max-age=86400` |
| `DELETE /api/voc/<id>` | CSRF + **작성자** | 하드 삭제 — 이미지·댓글 메타는 FK CASCADE, 실파일은 best-effort 정리 |

감사(메인 report.db `report_audit_log`, best-effort): `voc_create` / `voc_edit` / `voc_status` /
`voc_comment_create` / `voc_comment_delete` / `voc_delete`.

---

## 3. 신원

### 3.1 Honey 계정

[auth_identity.current_user()](../server/auth_identity.py) — Honey UA 의 `HoneyUser/<계정>`
토큰(또는 `AUTH_SSO_HEADER`). 이 신원이 있으면 그대로 작성자가 된다.

### 3.2 게스트 (일반 브라우저) — 이름 + 토큰 쿠키

Honey 신원이 없는 브라우저는 **이름을 직접 적어** 등록·댓글을 쓸 수 있다(`guest_name`,
1~20자). 다만 이름은 표시용이라 그것만으로 소유권을 주면 **아무나 같은 이름을 쳐서 남의 글을
고칠 수 있다.** 그래서 첫 게스트 쓰기에서 무작위 토큰(`secrets.token_hex(16)`)을 발급해
httponly 쿠키에 심고 글/댓글 행의 `guest_token` 에 저장한다:

```
report_voc_guest   path=/pe/report   httponly · SameSite=Lax · 180일
```

수정·삭제 권한은 `routes_voc._owns()` 가 판정한다:

- 행에 `guest_token` 이 있으면 → **쿠키 토큰이 일치하는 브라우저만** (`hmac.compare_digest`)
- 없으면(= Honey 계정 글) → `user_id == 현재 Honey 계정`

두 갈래가 배타적이라 **게스트가 Honey 계정명을 사칭해 글을 써도 그 계정 주인은 손댈 수 없고,
반대로 게스트도 Honey 계정 글을 못 건드린다.** 화면에서는 `is_guest` 로 `guest` 배지를 붙여
구분한다(토큰 자체는 응답에 절대 싣지 않는다 — `_public_row` 가 걷어낸다).

한계는 명확하다: 브라우저 쿠키를 지우거나 다른 PC 로 가면 자기 글을 더는 못 고친다. 계정
없는 게시판에서 이보다 강한 보장은 로그인 없이는 불가능하다.

감사 로그의 `client_user` 는 게스트면 `guest:<이름>` 으로 남아 Honey 계정과 구분된다.

---

## 4. 관리자 판별 — admin 게이트 쿠키 재사용

사용자 단위 관리자 계정 목록은 두지 않는다. **관리자 대시보드(`/pe/admin-<secret>/`) 로그인
쿠키를 그대로 재사용**한다.

기존 `pe_admin_gate` 쿠키는 `path=/pe/admin-<secret>` 이라 `/pe/report/*` 요청에 실려오지
않는다. 그래서 [admin_panel/routes.py](../server/admin_panel/routes.py) `login()` 이 **별도
이름·경로의 두 번째 쿠키를 추가 발급**한다:

```
pe_admin_gate      path=/pe/admin-<secret>   (대시보드 게이트, 기존)
pe_admin_gate_voc  path=/pe/report           (VOC 상태 전환용, 추가)
```

둘 다 `httponly` · `SameSite=Lax` · 12시간. 토큰 값과 쿠키 이름의 정본은
[admin_panel/\_\_init\_\_.py](../server/admin_panel/__init__.py) 의 `gate_token()` /
`voc_gate_token()` / `GATE_COOKIE_VOC` — 무거운 `admin_panel.routes`(admin 서브모듈 8개
import) 를 routes_voc 가 끌어오지 않도록 경량 모듈에 뒀다. `routes_voc._is_admin()` 이
`hmac.compare_digest` 로 검증한다.

**두 쿠키의 토큰 값은 다르다.** VOC 쿠키는 `path=/pe/report` 라 web_report API·업로드 등
모든 요청에 실리므로, admin 토큰과 같은 값이면 평문 HTTP 구간에서 새어나갔을 때 그대로 admin
대시보드 쿠키로 재사용된다. 해시 라벨(`pe-admin-gate|` vs `pe-voc-admin-gate|`)을 분리해 한쪽
값에서 다른 쪽을 유도할 수 없게 했다.

같은 이름을 다른 path 로 재발급하지 않는 이유: 브라우저에서 쿠키 교체·중복 전송이 모호해진다.

**운영 주의**: 이 변경 배포 직후 기존 admin 로그인 세션은 새 쿠키가 없어 **재로그인 1회 필요**.

---

## 5. DB — [voc_db.py](../server/database/voc_db.py)

`config.REPORT_VOC_DB_PATH` 파일에 자체 커넥션(WAL, `foreign_keys=ON`)으로 연결하고,
스키마는 그 DB 경로의 첫 커넥션에서 `executescript` 로 멱등 생성한다(`core.get_conn` 미사용 —
eval_export 와 같은 별도 파일 패턴).

| 테이블 | 컬럼 |
|---|---|
| `report_voc` | `id` PK AUTOINCREMENT(= 화면의 **번호**) · `user_id`(Honey 계정 또는 게스트 이름) · `category` · `title` · `content` · `status`(`'open'`\|`'close'`, 기본 open) · `guest_token`(게스트 글만, NULL=Honey 계정 글) · `created_at` |
| `report_voc_image` | `id` · `voc_id`→CASCADE · `image_id` · `content_type` · `sort_order` · `created_at`, UNIQUE(voc_id, image_id). **메타만 — 바이트는 storage_gateway** |
| `report_voc_comment` | `id` · `voc_id`→CASCADE · `user_id` · `content` · `guest_token` · `created_at` |

- 번호는 DB id 그대로다. 삭제하면 결번이 생기지만, 번호 검색의 기준이 흔들리지 않도록
  표시용 재번호는 **쓰지 않는다.**
- `guest_token` 은 **응답에 절대 싣지 않는다.** 목록(`list_voc`)은 SQL 에서
  `guest_token IS NOT NULL AS is_guest` 로 바꿔 내보내고, 토큰을 담아 돌려주는
  `get_voc`/`list_comments`/`get_comment` 는 권한 판정용이라 라우트의 `_public_row()` 를
  거쳐야 직렬화된다.
- `_migrate()` — 뒤늦게 추가된 컬럼(`status`, `guest_token` ×2)이 없는 구 voc.db 를
  `_ADDED_COLUMNS` 표대로 `PRAGMA table_info` 검사 후 ALTER 로 멱등 보정한다(기존 행은
  status `'open'`, guest_token NULL). Flask 는 waitress 단일 프로세스라 경합은 스레드
  수준뿐이고, 겹치면 뒤늦은 쪽이 받는 duplicate column 오류만 무시한다.
- 검색(`_search_where`) — `\`·`%`·`_` 를 이스케이프하고 `LIKE ? ESCAPE '\'`. 숫자(18자리 이하)면
  `id = ? OR title LIKE ?`. 18자리 초과는 SQLite INTEGER 범위 밖이라 제목 검색만 한다.

---

## 6. 한계

- 게스트는 **글을 쓴 브라우저에서만** 수정·삭제할 수 있다. 쿠키를 지우거나 다른 PC 로 가면
  자기 글도 못 고친다(계정 없는 게시판의 구조적 한계 — 관리자에게 요청해야 한다).
- 게스트 이름은 중복·사칭이 가능하다. `guest` 배지로 Honey 계정 글과 구분되지만, 게스트끼리
  같은 이름을 쓰는 것은 막지 않는다(권한은 이름이 아니라 토큰이 정한다).
- 스크린샷은 등록 시에만 첨부한다 — 수정 화면에서 교체 불가(삭제 후 재등록).

---

## 7. 검증

`python tests/test_voc.py` — 시나리오 (a)~(n) 자체 실행 assert:
스키마 멱등 / 등록·서빙·로컬 실존 / 입력 거부 / 권한 / 이미지 격리 / 페이지네이션·lean 목록 /
감사 / XSS 원문 무변조 / 검색(제목·번호·LIKE 이스케이프) / 수정 권한·검증 / 상태 전환
(비관리자 403 → 관리자 close·open) / 댓글(순서·권한·관리자 삭제·CASCADE) /
구 스키마 마이그레이션(gen0=최초, gen1=게시판 개편 직후 두 세대) /
게스트(이름 검증·토큰 쿠키 발급·다른 브라우저 403·Honey 계정 상호 격리·토큰 미노출).

- 관리자 쿠키는 `client.set_cookie(GATE_COOKIE_VOC, voc_gate_token(), path="/pe/report")` 로
  시뮬레이션한다(무거운 admin_panel.routes 등록 불필요).
- 게스트는 **별도 test_client** 로 검증한다 — 같은 클라이언트를 쓰면 게스트 쿠키가 남아
  "익명" 단언이 오염된다.

브라우저 수동 확인 포인트: 일반 브라우저에서 이름 입력 등록→수정→댓글(같은 브라우저에서만
수정·삭제 가능한지) / Honey UA 로 등록→상세→수정→댓글→삭제 / admin 로그인 후 Close·다시
열기·타인 댓글 삭제 / 긴 제목의 말줄임·툴팁 / 해시 뒤로가기·딥링크 / 다크 테마.
