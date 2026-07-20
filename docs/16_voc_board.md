# 16. VOC 게시판 (목록 · 상세 · 상태 · 댓글)

사용자 의견(VOC)을 서버에서 직접 접수·추적하는 게시판. 포탈 게시판 UX 를 따라
**목록 → 상세 → 댓글** 흐름이고, 처리 상태(Open/Close)로 접수 건을 추적한다.

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
| `#new` | 등록 | 분류 select + 제목 + 본문(placeholder "본문 입력해주세요") + 스크린샷 첨부(≤3장) + [등록][취소]. 성공 시 새 글 상세로 이동 |
| `#voc/<id>` | 상세 | 분류·Status badge·제목 / `#번호 · 등록자 · 등록일` / 본문 / 썸네일(클릭 확대) / 액션바 / 댓글 |
| `#voc/<id>/edit` | 수정 | 등록 폼 재사용(기존값 프리필). 스크린샷은 수정 대상 아님 |

- 목록 상태(`page`/`q`)를 메모리에 유지해 상세에서 뒤로가기 시 같은 페이지·검색으로 복귀한다.
- Status badge: Open = 연녹 pill, Close = slate pill. 목록에서 Close 된 글은 제목이 muted.
- 상세 액션바: `← 목록` / 작성자에게만 `✏️ 수정`·`삭제` / 관리자에게만 `Close 처리`(또는 `다시 열기`).
- 댓글: 본문 아래 "댓글 N" → 작성순 목록(본인·관리자만 삭제 버튼) → 입력창 + `댓글 등록`.
- 신원 없는 일반 브라우저는 **조회 전용** — 등록 버튼·폼·댓글 입력이 disabled 되고 안내가 뜬다.
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
| `GET /api/voc/<id>` | 익명 허용 | 상세 — `voc`(status 포함) + `screenshots` + `comments`(각 `can_delete`) + `user`/`is_admin`/`can_edit`/`can_delete` |
| `POST /api/voc` | CSRF + 신원 | multipart 등록. 분류 4종 / 제목 1~120자 / 내용 1~4000자 / PNG·JPEG ≤3장·장당 2MB(매직바이트 검사). 상태는 항상 `open` |
| `PATCH /api/voc/<id>` | CSRF + **작성자** | JSON 수정(분류·제목·내용). 등록과 동일 검증. 스크린샷은 대상 외 |
| `POST /api/voc/<id>/status` | CSRF + **관리자** | `{"status":"open"\|"close"}` |
| `POST /api/voc/<id>/comments` | CSRF + 신원 | `{"content": 1~1000자}`. **Close 된 글에도 허용** — 상태는 처리 표시이지 잠금이 아니다 |
| `DELETE /api/voc/<id>/comments/<cid>` | CSRF + **작성자 or 관리자** | 소속(voc_id) 확인 후 삭제 |
| `GET /api/voc/<id>/screenshots/<image_id>` | — | 소속 확인 후 서빙. `nosniff`, `private, max-age=86400` |
| `DELETE /api/voc/<id>` | CSRF + **작성자** | 하드 삭제 — 이미지·댓글 메타는 FK CASCADE, 실파일은 best-effort 정리 |

감사(메인 report.db `report_audit_log`, best-effort): `voc_create` / `voc_edit` / `voc_status` /
`voc_comment_create` / `voc_comment_delete` / `voc_delete`.

**신원**: [auth_identity.current_user()](../server/auth_identity.py) — Honey UA 의
`HoneyUser/<계정>` 토큰(또는 `AUTH_SSO_HEADER`). 없으면 `""` = 읽기 전용.

---

## 3. 관리자 판별 — admin 게이트 쿠키 재사용

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

## 4. DB — [voc_db.py](../server/database/voc_db.py)

`config.REPORT_VOC_DB_PATH` 파일에 자체 커넥션(WAL, `foreign_keys=ON`)으로 연결하고,
스키마는 커넥션 오픈마다 `executescript` 로 멱등 생성한다(`core.get_conn` 미사용 —
eval_export 와 같은 별도 파일 패턴).

| 테이블 | 컬럼 |
|---|---|
| `report_voc` | `id` PK AUTOINCREMENT(= 화면의 **번호**) · `user_id` · `category` · `title` · `content` · `status`(`'open'`\|`'close'`, 기본 open) · `created_at` |
| `report_voc_image` | `id` · `voc_id`→CASCADE · `image_id` · `content_type` · `sort_order` · `created_at`, UNIQUE(voc_id, image_id). **메타만 — 바이트는 storage_gateway** |
| `report_voc_comment` | `id` · `voc_id`→CASCADE · `user_id` · `content` · `created_at` |

- 번호는 DB id 그대로다. 삭제하면 결번이 생기지만, 번호 검색의 기준이 흔들리지 않도록
  표시용 재번호는 **쓰지 않는다.**
- `_migrate()` — status 컬럼이 없던 구 voc.db 를 `PRAGMA table_info` 검사 후 ALTER 로 멱등
  보정한다(기존 행은 `'open'`). Flask 는 waitress 단일 프로세스라 경합은 스레드 수준뿐이고,
  겹치면 뒤늦은 쪽이 받는 duplicate column 오류만 무시한다.
- 검색(`_search_where`) — `\`·`%`·`_` 를 이스케이프하고 `LIKE ? ESCAPE '\'`. 숫자(18자리 이하)면
  `id = ? OR title LIKE ?`. 18자리 초과는 SQLite INTEGER 범위 밖이라 제목 검색만 한다.

---

## 5. 한계

- **관리자 일반 브라우저는 HoneyUser 신원이 없어 댓글 작성 불가** (Close 전환·댓글 삭제는 가능).
  Honey 내장 브라우저에서 admin 로그인하면 신원과 admin 쿠키를 동시에 가져 겸용된다.
- 스크린샷은 등록 시에만 첨부한다 — 수정 화면에서 교체 불가(삭제 후 재등록).

---

## 6. 검증

`python tests/test_voc.py` — 시나리오 (a)~(m) 자체 실행 assert:
스키마 멱등 / 등록·서빙·로컬 실존 / 입력 거부 / 권한 / 이미지 격리 / 페이지네이션·lean 목록 /
감사 / XSS 원문 무변조 / 검색(제목·번호·LIKE 이스케이프) / 수정 권한·검증 / 상태 전환
(비관리자 403 → 관리자 close·open) / 댓글(순서·권한·관리자 삭제·CASCADE) / 구 스키마 마이그레이션.

관리자 쿠키는 `client.set_cookie(GATE_COOKIE_VOC, voc_gate_token(), path="/pe/report")` 로
시뮬레이션한다(무거운 admin_panel.routes 등록 불필요).

브라우저 수동 확인 포인트: 익명(조회 전용) / Honey UA 로 등록→상세→수정→댓글→삭제 /
admin 로그인 후 Close·다시 열기·타인 댓글 삭제 / 해시 뒤로가기·딥링크 / 다크 테마.
