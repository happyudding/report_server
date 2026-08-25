# 서버 아키텍처 핸드오프 문서

> ⚠️ **외부 전달용 스냅샷(2026-07-16 기준)이다.** 이후 서버는 계속 바뀌었으므로
> (테이블 27개, 라우트·캐시 계층 확장) **현행 사실 확인은 [docs/INDEX.md](docs/INDEX.md) 와
> [server/README.md](server/README.md) 로 하고**, 이 문서는 "서버를 처음 만드는 사람에게
> 설명하는 개념 설명서" 로만 읽는다. 개념(세션 단위 관리·동시 편집·캐시 계층)은 지금도
> 유효하고, 구체적인 숫자·경로는 낡았을 수 있다.

> **누구를 위한 문서인가**
> 처음으로 "웹에서 문서를 세션 단위로 관리하고, 여러 명이 편집하는 서버"를 만들려는 분.
> 서버 구축이 처음이어도 읽을 수 있게 **용어를 풀어서** 썼습니다.
> 이 문서는 현재 운영 중인 `report_server` 의 **서버·세션 관리 부분만** 뽑아 정리한 것입니다.
> (Honey 클라이언트, web_report 리포트 렌더링 같은 "우리 회사 전용 도메인 로직"은 뺐습니다.
> 여러분 프로젝트엔 그대로 안 쓰이니까요.)

> **함께 보는 파일**: `SERVER_ARCHITECTURE_HANDOFF.html` — 같은 내용을 **그림·흐름도**로 본 버전.
> 큰 그림이 먼저 필요하면 HTML 을 먼저 열어보고, 세부 코드는 이 `.md` 로 확인하세요.

---

## 목차

- [0. 5분 요약 — 이것만 알아도 절반은 안다](#0-5분-요약--이것만-알아도-절반은-안다)
- [1. 큰 그림 — 요청 하나가 들어와서 나가기까지](#1-큰-그림--요청-하나가-들어와서-나가기까지)
- [2. 세션 관리 (핵심 1)](#2-세션-관리-핵심-1)
- [3. 2인 동시 편집 — 지금 방식과 더 나아가는 법 (핵심 관심사)](#3-2인-동시-편집--지금-방식과-더-나아가는-법-핵심-관심사)
- [4. 서버 운영 (핵심 2)](#4-서버-운영-핵심-2)
- [5. 기타 공통 노하우 (핵심 3)](#5-기타-공통-노하우-핵심-3)
- [6. 그대로 훔쳐 써도 되는 패턴 체크리스트](#6-그대로-훔쳐-써도-되는-패턴-체크리스트)
- [7. 파일 위치 색인표](#7-파일-위치-색인표)

---

## 0. 5분 요약 — 이것만 알아도 절반은 안다

이 서버가 하는 일은 딱 세 가지입니다.

1. **문서(세션)를 저장한다** — 누가, 언제, 무슨 문서를 올렸는지 DB 에 기록.
2. **찾아서 보여준다** — 목록에서 검색하고, 하나를 클릭하면 상세를 보여줌.
3. **고칠 수 있게 한다** — 권한 있는 사람이 내용을 편집하고 저장.

이걸 떠받치는 기술 스택은 **일부러 단순하게** 골랐습니다.

| 부품 | 무엇 | 왜 골랐나 |
|------|------|-----------|
| **Flask** | 파이썬 웹 프레임워크(요청을 받아 함수로 연결) | 가볍고 배우기 쉬움 |
| **waitress** | 실제 서비스용 웹 서버(WSGI 서버) | 설치 하나로 끝, 윈도우에서 잘 돎 |
| **SQLite** | 파일 하나로 되는 DB | 서버 따로 안 띄워도 됨. 사내 소수 인원엔 충분 |
| **(ORM 없음)** | SQL 을 직접 씀 | 마법이 없어 동작을 눈으로 볼 수 있음 |

> 💡 **용어 풀이**
> - **WSGI**: 파이썬 웹앱(Flask)을 진짜 서버 프로그램(waitress)에 꽂는 표준 규격. 콘센트 모양이라고 생각하세요.
> - **ORM**: 파이썬 객체 ↔ DB 테이블을 자동 변환해 주는 도구(예: SQLAlchemy). 편하지만 내부에서 무슨 SQL 이 나가는지 감춰집니다. 이 프로젝트는 **일부러 안 쓰고** SQL 을 직접 씁니다.

**이 문서에서 꼭 챙길 3가지 노하우:**

- 🗂 **세션 관리**: 테이블 설계, 안전한 검색/수정, 권한 체크 (→ [2장](#2-세션-관리-핵심-1))
- 👥 **동시 편집**: 지금은 "항목 단위로 나눠 저장 + 마지막 저장이 이김" 방식. 진짜 실시간 편집으로 키우는 법까지 (→ [3장](#3-2인-동시-편집--지금-방식과-더-나아가는-법-핵심-관심사))
- 🛠 **서버 운영**: 죽지 않게 돌리는 법, 자동 백업, 자동 청소, 관리 페이지 (→ [4장](#4-서버-운영-핵심-2))

---

## 1. 큰 그림 — 요청 하나가 들어와서 나가기까지

브라우저가 "이 세션 보여줘" 하고 요청하면, 서버 안에서 이런 순서로 흘러갑니다.

```
[브라우저]
   │  GET /pe/report/session/abc123/full
   ▼
[waitress]  ── 실제 서버. 요청을 스레드(작업자) 하나에 배정
   ▼
[Flask app]  ── 어느 함수가 이 URL 을 처리할지 찾음
   ▼
[Blueprint]  ── 라우트 묶음. report / honey / admin 3덩어리
   ▼
[보안 가드]  ── "너 이거 볼 권한 있어?" 확인 (신원 → 비공개 여부)
   ▼
[report_db (창구)]  ── DB 접근은 전부 이 창구 하나로
   ▼
[SQLite + 파일]  ── 실제 데이터
   ▼
[gzip 압축 응답]  ── 결과를 압축해서 브라우저로
```

> 💡 **Blueprint** = "관련 있는 URL 라우트를 한 바구니에 담은 것". Flask 기능입니다.
> 이 프로젝트는 `report`(문서/세션), `honey`(클라이언트 배포 — 무시해도 됨), `admin`(관리 페이지) 3바구니.

코드를 **책임별로 층**을 나눠 놨습니다. 새 프로젝트에서도 이 층 구분을 따라 하면 코드가 안 엉킵니다.

| 층 | 파일 | 하는 일 |
|----|------|---------|
| 진입점 | `server/wsgi.py` | 서버 켜기(waitress 기동), 로그 설정 |
| 조립 | `server/plugin.py` | Blueprint 등록 + DB 초기화 + 스케줄러 켜기 |
| 라우트 | `server/report/routes_*.py` | URL → 함수. "무엇을 할지" |
| 보안 | `server/report/security.py` | 권한 가드, CSRF, 입력 검증 |
| 신원 | `server/auth_identity.py` | "지금 요청한 사람이 누구지?" |
| DB 창구 | `server/database/report_db.py` | DB 접근 단일 창구(facade) |
| DB 구현 | `server/database/*.py` | 실제 SQL. 세션/감사/사용자 등 책임별 분리 |
| 운영 | `server/ops.py`, `report_cleanup.py`, `db_backup.py`, `admin_panel/` | 헬스체크, 자동청소, 백업, 관리 |

> 💡 **facade(파사드) 패턴** = "안쪽 복잡함을 감춘 창구 하나". 호출하는 쪽은 `report_db.get_session(...)`
> 처럼 창구만 부르고, 실제 SQL 이 어느 파일에 있는지는 신경 안 씁니다. 나중에 내부를 뜯어고쳐도
> 창구 모양만 그대로면 호출부는 안 바뀝니다. → [`server/database/report_db.py`](server/database/report_db.py)

---

## 2. 세션 관리 (핵심 1)

### 2.1 "세션"이란 무엇이고, 테이블은 어떻게 생겼나

여기서 **세션 = 업로드된 문서 1건 + 그에 딸린 메타정보**입니다. (로그인 세션 아님! 헷갈리기 쉬움)

DB 의 중심은 `report_session` 테이블 하나이고, 그 주위에 **부속 테이블**들이 붙습니다.

```
                    ┌─────────────────────┐
                    │   report_session    │  ← 세션 1건 = 이 테이블의 행 1개
                    │  (session_id 로 식별)│
                    └──────────┬──────────┘
        ┌──────────────┬───────┼────────┬──────────────┐
        ▼              ▼       ▼        ▼              ▼
 report_analysis  report_    report_  report_       report_
   _summary       object_    audit_   session_      webreport
 (요약 수치)       info       log      editor        _edit
                 (파일 위치) (감사)   (편집 위임)   (편집 내용)
```

`report_session` 의 핵심 컬럼만 뽑으면 (정본은 [`server/database/core.py`](server/database/core.py) 의 `SCHEMA`):

```sql
CREATE TABLE IF NOT EXISTS report_session (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT UNIQUE NOT NULL,   -- 바깥에 노출하는 세션 식별자
    analysis_key  TEXT,                   -- "같은 데이터"를 가리키는 지문(해시). 뒤에 설명
    file_name     TEXT NOT NULL,
    status        TEXT DEFAULT 'pending', -- 'pending' → 'done'/'reused'. 처리 단계
    created_at    INTEGER NOT NULL,       -- 유닉스 시간(정수). 문자열 날짜보다 다루기 쉬움
    product_type  TEXT,                   -- 검색 필터용 분류 컬럼들
    product       TEXT,
    lot_id        TEXT,
    uploaded_by   TEXT,                   -- 누가 올렸나 (권한 판단의 핵심)
    is_private    INTEGER DEFAULT 0,      -- 1이면 비공개(올린 사람만 봄)
    is_important  INTEGER DEFAULT 0,      -- 1이면 자동청소에서 제외
    source        TEXT DEFAULT 'xlsx_upload'
);
CREATE INDEX IF NOT EXISTS idx_report_session_status_created
    ON report_session(status, created_at DESC);   -- 목록 정렬을 빠르게
```

> 💡 **꼭 챙길 것 3개**
> 1. **시간은 정수(유닉스 초)로 저장** — `int(time.time())`. 타임존·포맷 지옥을 피합니다.
> 2. **status 컬럼으로 처리 단계를 표현** — 반쯤 만들다 죽은 세션을 나중에 걸러낼 수 있음.
> 3. **검색에 쓰는 컬럼엔 INDEX** — 안 걸면 데이터 쌓일수록 목록이 느려집니다.

### 2.2 ORM 없이 스키마 만들고 "진화"시키기 (마이그레이션)

ORM 을 안 쓰면 "DB 구조를 나중에 바꾸는 일(마이그레이션)"을 손으로 해야 합니다. 겁먹을 필요 없습니다.
이 프로젝트의 방법은 아주 실용적입니다.

**① 스키마는 `CREATE TABLE IF NOT EXISTS` 로** — 이미 있으면 무시, 없으면 생성. 서버 켤 때마다 안전하게 실행.

**② 컬럼 추가는 "있는지 확인하고 없으면 ALTER"** ([`core.py`](server/database/core.py) `_migrate`):

```python
# 기존 DB 에 컬럼이 없으면 하나씩 추가한다 (있으면 건너뜀).
sess_cols = {r[1] for r in conn.execute("PRAGMA table_info(report_session)")}
for col in ("family_product", "uploaded_by", "client_host", ...):
    if col not in sess_cols:
        conn.execute(f"ALTER TABLE report_session ADD COLUMN {col} TEXT")
```

> 💡 `PRAGMA table_info(테이블)` 은 "이 테이블에 어떤 컬럼이 있나" 를 알려주는 SQLite 명령.
> 이걸로 현재 상태를 확인하고 → 부족한 것만 추가하니, 서버를 몇 번 켜도 안전(**멱등**)합니다.
> **멱등** = 같은 걸 여러 번 실행해도 결과가 똑같음.

**③ 컬럼 이름 바꾸기/PK 변경처럼 ALTER 로 안 되는 건 "새 테이블 만들고 복사"**:

```python
# report_object_info 의 기본키 구조를 바꿔야 할 때: 새로 만들고 → 데이터 옮기고 → 옛 것 삭제
conn.execute("ALTER TABLE report_object_info RENAME TO _report_object_info_old")
conn.execute("CREATE TABLE report_object_info ( ...새 구조... )")
conn.execute("INSERT INTO report_object_info SELECT ... FROM _report_object_info_old")
conn.execute("DROP TABLE _report_object_info_old")
```

이 세 패턴이면 소규모 프로젝트의 마이그레이션은 거의 다 됩니다. (규모가 커지면 Alembic 같은 도구를 고려)

### 2.3 만들기·수정·삭제 — 안전하게 짜는 법

**만들 때: SQL 인젝션을 막는 "화이트리스트 + 파라미터 바인딩"** ([`sessions.py`](server/database/sessions.py) `create_session`)

문자열을 SQL 에 직접 이어붙이면 공격당합니다(SQL 인젝션). 이 프로젝트는 **컬럼 이름은 코드 상수에서만**
오고, **값은 항상 `?` 로 바인딩**합니다.

```python
# 컬럼 목록은 코드에 박힌 상수(_PRODUCT_INFO_COLUMNS)에서만 고름 → 이름 위조 불가
extra_cols = [c for c in _PRODUCT_INFO_COLUMNS if info.get(c)]
# 값은 전부 ? 로 바인딩 → 사용자 입력이 SQL 로 해석될 여지 없음
conn.execute(f"INSERT INTO report_session (..., {', '.join(extra_cols)}, ...) "
             f"VALUES (?, ?, ..., ?)", (..., *[info[c] for c in extra_cols], ...))
```

> ⚠️ **절대 하면 안 되는 것**: `f"... WHERE name = '{user_input}'"` 처럼 값을 문자열에 끼워넣기.
> **항상** `WHERE name = ?` + 값은 튜플로 넘기세요.

**수정할 때: 바꿔도 되는 컬럼만 화이트리스트로** ([`sessions.py`](server/database/sessions.py) `update_session`)

```python
_SESSION_UPDATABLE = {"status", "is_important", "is_private", ...}  # 이 목록에 있는 것만 수정 허용
def update_session(session_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _SESSION_UPDATABLE}  # 나머지는 버림
    ...
```

이렇게 하면 실수로(또는 악의로) `uploaded_by` 같은 걸 바꾸는 요청이 와도 무시됩니다.

**삭제할 때: "마지막으로 이 데이터를 쓰는 세션일 때만" 진짜 파일을 지운다**

같은 데이터를 두 번 올리면 `analysis_key`(데이터 지문)가 같아서 산출물(파일)을 공유합니다.
그래서 세션을 지울 때, **다른 세션이 아직 그 파일을 쓰고 있으면 파일은 남겨둡니다.**
([`routes_session.py`](server/report/routes_session.py) `delete_session_route`)

```python
akey = session.get("analysis_key")
# 나를 뺀 나머지 중에 같은 analysis_key 를 쓰는 세션이 0개일 때만 = 내가 마지막 사용자
if akey and report_db.count_sessions_for_analysis_key(akey, exclude_session_id=session_id) == 0:
    storage_gateway.delete_report_artifacts(akey, ...)  # 그제서야 파일 삭제
    report_db.delete_analysis_rows(akey)                # 관련 DB 행도 삭제
report_db.delete_session(session_id)                    # 세션 행은 항상 삭제
```

> 💡 이걸 **"참조 카운팅(누가 아직 쓰나 세기)"** 이라고 합니다. 공유 자원을 지울 때의 기본기.

### 2.4 검색 리스트 — 필터 + 페이지네이션 + 비공개 숨김

목록 화면(`GET /pe/report/api/history`)은 세 가지를 동시에 합니다.

**① 여러 조건으로 필터** — 조건이 있는 것만 `WHERE` 에 추가하는 빌더를 만들어 재사용
([`sessions.py`](server/database/sessions.py) `_history_where`):

```python
conditions = ["s.status IN ('done', 'reused')"]   # 기본: 완성된 세션만
params = []
if product_type:                                   # 넘어온 필터만 조건에 추가
    conditions.append("s.product_type = ?"); params.append(product_type)
if lot_id:
    conditions.append("s.lot_id LIKE ?"); params.append(f"%{lot_id}%")
# ... 목록(get_history)과 개수세기(count_history)가 이 빌더를 똑같이 씀 → 필터 불일치 버그 방지
```

**② 서버 페이지네이션** — 한 번에 다 주지 않고 `limit`/`offset` 으로 잘라서. 총 개수(`total`)도 함께.

```python
rows  = report_db.get_history(**filters, limit=limit, offset=offset, viewer=viewer)
total = report_db.count_history(**filters, viewer=viewer)   # 페이지 UI 에 "몇 개 중 몇 개" 표시용
```

**③ 비공개 세션은 SQL 단계에서 숨김** — 화면에서 가리는 게 아니라 **아예 조회되지 않게**.
`viewer`(지금 보는 사람)를 넘겨서, 공개이거나 / 내가 올렸거나 / 편집 위임받았을 때만 보이게:

```python
if viewer:  # 로그인 사용자가 있을 때
    conditions.append(
        "(COALESCE(s.is_private,0)=0"                          # 공개면 OK
        " OR s.uploaded_by = ?"                                # 내가 올렸으면 OK
        " OR EXISTS(SELECT 1 FROM report_session_editor e "    # 편집 위임받았으면 OK
        "           WHERE e.session_id=s.session_id AND e.editor_user=?))")
    params.extend([viewer, viewer])
else:       # 로그인 안 했으면 공개만
    conditions.append("COALESCE(s.is_private,0)=0")
```

**④ 정렬은 "안정적으로"** — `ORDER BY is_important DESC, created_at DESC, session_id`.
마지막에 `session_id` 를 넣는 이유: 시간이 같은 행들이 있어도 순서가 항상 똑같아, 페이지를
넘길 때 같은 행이 중복되거나 빠지지 않습니다.

### 2.5 세션 하나 안에서 할 수 있는 액션들

세션 상세 페이지에서 일어나는 요청들입니다. **가드(guard)** = 권한 체크(3장에서 설명). URL 앞에 `/pe/report` 생략.

| URL | 메서드 | 가드 | 하는 일 |
|-----|--------|------|---------|
| `/session/<sid>` | GET | 비공개 가드 | 세션 메타 조회 |
| `/session/<sid>/full` | GET | 비공개 가드 | 상세 복원에 필요한 전부(요약·파일·주석 등) |
| `/session/<sid>` | DELETE | **업로더만** | 세션 삭제(+마지막이면 파일 정리) |
| `/session/<sid>/private` | POST | **업로더만** | 비공개 on/off |
| `/session/<sid>/important` | POST | 편집자 | 내 개인 "중요" 표시(자동청소 제외) |
| `/session/<sid>/editors` | GET/POST/DELETE | **업로더만** | 편집 권한 위임 관리 |
| `/session/<sid>/my_access` | GET | 없음 | "나는 이 세션에 무슨 권한이 있나" |
| `/annotation`, `/api/favorites` | POST | CSRF | 주석/즐겨찾기 |

> 💡 **설계 포인트**: `my_access`(내 권한)는 **사람마다 답이 달라서** 세션 본문(`/full`)과 분리했습니다.
> `/full` 응답은 모두에게 똑같아 캐시(저장해두고 재사용)할 수 있는데, "내 권한"을 거기 섞으면 캐시가
> 깨집니다. **모두에게 같은 응답 / 사람마다 다른 응답을 나누는 것** — 캐싱의 기본기입니다.

---

## 3. 2인 동시 편집 — 지금 방식과 더 나아가는 법 (핵심 관심사)

여러분 프로젝트의 핵심 요구사항이죠. **먼저 결론부터:**

> 현재 서버는 **"항목(칸) 단위로 나눠 저장 + 마지막 저장이 이김"** 방식입니다.
> 이건 진짜 실시간 협업(구글 독스처럼)은 **아닙니다.** 하지만 소수 인원에겐 충분히 실용적이고,
> 여기서 조금만 더 얹으면 "준실시간"까지 갈 수 있습니다. 아래에서 정직하게 설명합니다.

### 3.1 지금 방식 — 항목 단위 upsert + rev 토큰

편집 내용을 통째로 한 덩어리로 저장하지 않고, **작은 항목(칸) 단위**로 쪼개 저장합니다.
테이블 `report_webreport_edit` 의 기본키가 `(session_id, kind, item_key)` 라는 게 핵심입니다.

```
편집 저장 테이블: report_webreport_edit
┌────────────┬───────────────┬───────────┬─────────┐
│ session_id │ kind          │ item_key  │ value   │  ← (앞 3개)가 기본키 = 칸 하나를 정확히 지목
├────────────┼───────────────┼───────────┼─────────┤
│ abc123     │ issue_comment │ row_5     │ "확인함"│
│ abc123     │ trim_override │ item_12   │ "0.35"  │
└────────────┴───────────────┴───────────┴─────────┘
```

저장 함수는 이렇게 생겼습니다 ([`server/database/webreport_edits.py`](server/database/webreport_edits.py) `apply_webreport_edits`):

```python
def apply_webreport_edits(session_id, changes, updated_by=None):
    # changes: [(kind, item_key, value), ...]  — value 가 None 이면 삭제
    with get_conn() as conn:                       # 여러 칸 변경을 한 트랜잭션으로 묶음
        for kind, item_key, value in changes:
            conn.execute(
                "INSERT INTO report_webreport_edit (session_id, kind, item_key, value, ...) "
                "VALUES (?, ?, ?, ?, ...) "
                "ON CONFLICT(session_id, kind, item_key) DO UPDATE SET value=excluded.value, ...",
                (session_id, kind, item_key, str(value), ...))   # ← 있으면 수정, 없으면 추가 = upsert
        conn.execute(                               # 저장할 때마다 rev(버전번호)를 1 올림
            "INSERT INTO report_webreport_edit_rev (session_id, rev) VALUES (?, 1) "
            "ON CONFLICT(session_id) DO UPDATE SET rev=rev+1", (session_id,))
        # 새 rev 를 돌려줌
```

두 가지 개념이 있습니다.

- **upsert** (있으면 Update, 없으면 inSERT): `ON CONFLICT ... DO UPDATE`. 같은 칸을 또 저장하면 덮어씀.
- **rev 토큰**: 세션이 편집될 때마다 1씩 오르는 **버전 번호**. "이 세션 내용이 바뀌었다"를 알리는 신호등.
  읽기 응답을 캐시할 때 이 rev 를 캐시 키에 넣어서, 편집이 일어나면(rev 가 바뀌면) 옛 캐시를 자동으로 버립니다.

### 3.2 그래서 2명이 동시에 편집하면 실제로 어떻게 되나

```
경우 A: 두 사람이 "서로 다른 칸"을 편집  →  ✅ 완전히 무충돌
   사용자A: row_5 코멘트 수정  ┐
   사용자B: item_12 값 수정    ┘  기본키가 달라 서로 다른 행 → 둘 다 그대로 저장됨

경우 B: 두 사람이 "같은 칸"을 편집  →  ⚠️ 나중에 저장한 사람이 이김 (last-write-wins)
   사용자A: row_5 = "확인함"  (10:00:00 저장)
   사용자B: row_5 = "재검토"  (10:00:03 저장)  →  최종값 "재검토". A의 저장은 조용히 사라짐
```

- **다른 칸**을 만지면 서로 안 부딪힙니다. 실무에서 편집은 대개 각자 다른 부분을 만지므로, 이 단순한
  방식만으로도 상당히 잘 굴러갑니다.
- **같은 칸**을 동시에 만지면 **마지막 저장이 이깁니다.** 그리고 A는 자기 게 사라진 걸 **모릅니다**(경고 없음).
- 편집할 사람이 여럿이면 **편집 권한 위임**(`report_session_editor`)으로 업로더가 편집자를 추가합니다.
- 상대가 편집하면 **rev 가 바뀌므로**, 내 화면이 다시 조회할 때 "어, 최신이 아니네"를 감지할 수 있습니다.

### 3.3 한계 (정직하게)

| 한계 | 무슨 뜻 | 언제 문제 되나 |
|------|---------|----------------|
| 충돌 감지 없음 | 같은 칸을 덮어써도 아무도 모름 | 두 명이 같은 칸을 자주 만질 때 |
| 실시간 아님 | 상대 편집이 내 화면에 자동으로 안 뜸(새로고침/재조회해야) | "지금 남이 뭐 치는지" 봐야 할 때 |
| 자유 문서엔 부적합 | "칸(item_key)"으로 딱 쪼갤 수 있는 데이터에만 잘 맞음 | 긴 글 본문을 여럿이 자유롭게 고칠 때 |

### 3.4 여러분 프로젝트에서 더 나아가는 법 (권장 순서대로)

지금 구조를 **버리지 말고 얹으세요.** 이미 `rev`(버전번호)가 있다는 게 큰 자산입니다.

**(A) 낙관적 락(optimistic lock) — 충돌을 "감지"하게 만들기 · 난이도 ★☆☆**

칸(또는 문서)마다 `version` 번호를 두고, 저장할 때 "내가 읽었던 version 이 그대로일 때만 써라"라고 조건을 겁니다.

```sql
-- 클라이언트는 편집 시작 시 받은 version 을 저장 요청에 같이 보냄
UPDATE edit SET value = ?, version = version + 1
 WHERE session_id = ? AND item_key = ? AND version = ?   -- ← 이 마지막 조건이 핵심
```

바뀐 행 수(`rowcount`)가 0이면 = 그 사이 누가 먼저 고쳤다는 뜻 → 서버가 **409 Conflict** 를 돌려주고,
클라이언트는 "다른 사람이 먼저 수정했어요. 최신 내용을 보고 다시 저장하세요" 라고 안내(머지 UI).
**이거 하나만 추가해도 "조용히 사라지는 편집"이 없어집니다.** 가장 가성비 좋은 첫 단계.

**(B) rev 브로드캐스트 — "준실시간"으로 만들기 · 난이도 ★★☆**

이미 세션마다 `rev` 가 있으니, 그 변화를 다른 사람에게 알리기만 하면 됩니다.

- **폴링(가장 쉬움)**: 클라이언트가 3~5초마다 `GET .../rev` 로 현재 rev 를 물어봄. 내가 아는 rev 와
  다르면 → 다시 조회해서 화면 갱신. 서버는 rev 숫자 하나만 돌려주면 되니 매우 가벼움.
- **SSE(Server-Sent Events)**: 서버가 "rev 바뀌었어!"를 밀어주는 단방향 통신. 폴링보다 즉각적.
- **WebSocket**: 양방향. 커서 위치까지 공유하고 싶을 때. 다만 인프라가 무거워짐.

> 사내 소수 인원이면 **(A) 낙관적 락 + (B) 폴링** 조합이면 충분히 좋습니다. 구현도 며칠이면 됩니다.

**(C) soft lock — "지금 누가 편집 중" 표시 · 난이도 ★☆☆**

이 프로젝트엔 이미 `report_analysis_lock` 이라는 **TTL(수명) 붙은 잠금** 패턴이 있습니다
([`core.py`](server/database/core.py) `try_acquire_analysis_lock`). 이걸 편집에 응용하면:

```
사용자A가 편집 시작 → "abc123 은 A가 편집 중(60초)" 락 기록
사용자B가 들어옴   → 락 확인 → "지금 A가 편집 중입니다" 배지 표시 (막지는 않음)
A가 계속 편집     → 락 시간 갱신(하트비트).  A가 나가면 TTL 지나 자동 해제
```

강제로 막는 게 아니라 **"누가 만지고 있어요" 를 보여주기만** 해도 실무 충돌이 확 줄어듭니다.

**(D) 진짜 실시간 협업(구글 독스급)이 꼭 필요하면 · 난이도 ★★★**

같은 문단을 여러 명이 **동시에 자유롭게** 타이핑해야 한다면 그때는 **CRDT**(예: Yjs 라이브러리)를
얹습니다. 다만 이건 별도 편집 서버 계층이 필요하고 복잡도가 크게 오릅니다. **대부분의 사내 도구엔
과합니다.** "칸 단위 데이터 + 낙관적 락"으로 해결되는지 먼저 따져보세요.

> 🎯 **한 줄 권고**: 지금의 **항목단위 upsert + rev** 를 유지하고 → **(A) 낙관적 락**을 먼저 넣고
> → 필요하면 **(B) 폴링**으로 준실시간을 얹으세요. CRDT 는 정말 필요할 때만.

---

## 4. 서버 운영 (핵심 2)

"코드는 됐고, 이걸 어떻게 **안 죽고 계속 돌게** 하나"에 대한 노하우입니다.

### 4.1 서버 켜기 (`server/wsgi.py`)

**① 개발용 서버로 서비스하지 말 것.** Flask 기본 서버(`app.run()`)는 개발용이라 동시 요청에 약합니다.
실제 운영은 **waitress** 로:

```python
from waitress import serve
serve(app, host=host, port=port, threads=8,          # 동시에 처리할 요청 수
      max_request_body_size=body_limit)
```

**② 업로드 용량 상한을 양쪽에 맞춰라.** Flask 와 waitress **둘 다** 상한이 있어서, 하나만 크게 잡으면
다른 쪽에서 막힙니다. 같은 환경변수 하나로 둘을 맞춥니다:

```python
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH_MB", "2048")) * 1024 * 1024
serve(app, ..., max_request_body_size=app.config["MAX_CONTENT_LENGTH"])  # 같은 값으로 정합
```

**③ 로그를 화면과 파일에 동시에 남겨라(tee) + 인코딩 사고 방지.** 윈도우 콘솔은 한글 인코딩(cp949)이라
특수문자(예: — em dash)를 만나면 **서버가 통째로 죽는** 사고가 납니다. stdout 을 UTF-8 로 강제하세요:

```python
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # 못 쓰는 글자는 대체, 죽지 않게
    except Exception:
        pass
```

> ⚠️ 실제로 이 프로젝트에서 **em dash 하나로 서버가 크래시**한 적이 있습니다. 로그는 사소해 보여도 킬러입니다.

**④ 쿠키 서명 키(SECRET_KEY)는 파일에 저장해 재사용.** 서버를 재시작해도 로그인/쿠키가 안 풀리게,
키를 매번 새로 만들지 말고 파일에 1회 생성해 보관합니다.

### 4.2 조립과 헬스체크 (`server/plugin.py`, `server/ops.py`)

`register_report_server(app)` 하나가 **Blueprint 등록 + DB 초기화 + 스케줄러 켜기**를 다 합니다.
새 프로젝트에서도 "조립 함수 하나"로 모으면 진입점이 깔끔합니다.

**헬스체크 엔드포인트를 꼭 두세요** ([`ops.py`](server/ops.py)). 모니터링 도구가 "서버 살아있니?"를
물어볼 가벼운 URL:

```python
@app.get("/healthz")
def healthz():
    try:
        with report_db.get_conn() as conn:
            conn.execute("SELECT 1").fetchone()   # DB 까지 진짜 되는지 확인
        return jsonify({"ok": True}), 200
    except Exception:
        return jsonify({"ok": False}), 503        # DB 죽었으면 503
```

**전역 에러 핸들러로 내부 사정을 숨기세요.** 처리 안 된 예외의 스택 트레이스가 사용자에게 노출되면
보안 위험 + 흉함. 로그엔 남기고 사용자에겐 뭉뚱그린 메시지만:

```python
@app.errorhandler(Exception)
def _unhandled(e):
    if isinstance(e, HTTPException):   # abort(404) 같은 의도된 응답은 그대로 통과
        return e
    _log.exception("unhandled exception")            # 상세는 서버 로그에만
    return jsonify({"error": "internal server error"}), 500   # 사용자에겐 이것만
```

### 4.3 SQLite 를 안정적으로 굴리는 법

SQLite 는 파일 하나라 편하지만, 몇 가지 **함정**이 있습니다. 다 챙겨져 있으니 그대로 따라 하세요.

**① WAL 모드 + PRAGMA 설정** ([`core.py`](server/database/core.py)):

```python
conn.execute("PRAGMA journal_mode = WAL")     # 읽기와 쓰기가 서로 안 막힘 (동시성 ↑)
conn.execute("PRAGMA synchronous = NORMAL")   # 안전성 약간 낮추고 속도 ↑ (WAL 과 궁합 좋음)
conn.execute("PRAGMA busy_timeout = 5000")    # 잠겨있으면 5초 기다렸다 재시도 (즉시 에러 방지)
```

> 💡 **WAL** = Write-Ahead Logging. 쉽게 말해 "읽는 사람과 쓰는 사람이 서로 안 기다리게" 해주는 모드.
> SQLite 로 동시 접속을 받는다면 **거의 필수**입니다.

> ⚠️ **함정**: `PRAGMA synchronous`, `temp_store` 같은 설정은 **커넥션(연결)마다** 다시 걸어야 합니다.
> DB 파일에 한 번 설정한다고 유지되는 게 아니에요(WAL 모드만 파일에 영속). 그래서 이 프로젝트는
> **커넥션을 여는 `get_conn()` 안에서 매번** 이 PRAGMA 들을 겁니다.

**② DB 접근은 컨텍스트 매니저로 열고 닫기** — 커밋/닫기를 깜빡하지 않게:

```python
@contextmanager
def get_conn():
    conn = sqlite3.connect(REPORT_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row          # 결과를 dict 처럼 컬럼명으로 접근
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()      # 블록이 정상 종료되면 자동 커밋
    finally:
        conn.close()       # 무슨 일이 있어도 닫기
# 사용: with get_conn() as conn: conn.execute(...)
```

**③ 백업은 "파일 복사" 말고 "온라인 백업 API" 로** ([`db_backup.py`](server/db_backup.py)):

WAL 모드에서 DB 파일을 그냥 `copy` 하면 **깨진 백업**이 나올 수 있습니다(-wal 파일 미반영). 반드시
SQLite 의 백업 API 를 쓰고, 백업본 무결성 검사까지 합니다:

```python
src = sqlite3.connect(DB_PATH)
dst = sqlite3.connect(backup_path)
with dst:
    src.backup(dst)                                  # ← 온라인 백업(쓰는 중에도 안전)
row = dst.execute("PRAGMA integrity_check").fetchone()   # 백업본이 멀쩡한지 검사
ok = row and row[0] == "ok"
# 깨진 백업(ok 아님)은 .bad 로 이름 바꿔 격리 → 정상 백업이 실수로 지워지지 않게
```

그리고 백업 사이클마다 `PRAGMA wal_checkpoint(TRUNCATE)` 로 -wal 파일이 무한정 커지는 걸 막습니다.
(VACUUM 은 오래 잠그므로 자동으로 돌리지 않고 필요할 때 수동으로.)

### 4.4 백그라운드 자동화 (백업·청소·모니터링)

세 가지 자동 작업(**백업 / 오래된 것 청소 / 리소스 측정**)이 전부 **같은 패턴**으로 돕니다:
**데몬 스레드 + 중복 실행 방지 플래그.**

```python
_started = False
def start_backup_scheduler():
    global _started
    if _started: return          # 실수로 두 번 켜도 하나만 돎
    _started = True
    def _loop():
        time.sleep(60)           # 서버 기동과 안 겹치게 살짝 늦게 시작
        while True:
            try:
                run_backup()
            except Exception:
                _log.exception("crashed")   # 한 번 실패해도 루프는 계속
            time.sleep(interval)
    threading.Thread(target=_loop, daemon=True).start()   # daemon = 메인 죽으면 같이 죽음
```

> 💡 **daemon 스레드** = 메인 프로그램이 끝나면 알아서 같이 종료되는 백그라운드 스레드.
> 별도 스케줄러 서버(cron 등) 없이, 서버 프로세스 안에서 주기 작업을 돌리는 가장 간단한 방법.

**청소(cleanup)에서 꼭 배울 것: DRYRUN 안전판** ([`report_cleanup.py`](server/report_cleanup.py))

"오래된 세션 삭제" 같은 **되돌릴 수 없는 작업**은 기본값을 **"진짜로는 안 지우고 대상만 로그에 남김"**
(`REPORT_CLEANUP_DRYRUN=True`)으로 둡니다. 로그로 "이런 걸 지울 뻔했다"를 며칠 지켜보고, 확신이
서면 그때 `=0` 으로 켭니다.

```python
if dry_run:
    _log.info("[cleanup:dry-run] would delete session=%s ...", sid)   # 지우는 척만
    return False
# 진짜 삭제는 dry_run 이 꺼졌을 때만
```

> ⚠️ **위험한 자동 삭제엔 항상 DRYRUN 기본값을.** "실수로 다 지웠어요"를 막는 가장 싼 보험입니다.
> 참고로 이 프로젝트는 데이터 유실이 무서워서 "오래된 세션 삭제" 자체를 접고, 대신 오래된 파일만
> S3(외부 저장소)로 옮기는 방식으로 바꿨습니다 — **"지우기보다 옮기기"** 도 좋은 원칙입니다.

### 4.5 관리자 대시보드 (`server/admin_panel/`)

운영자용 페이지(`/pe/admin-pte/`). 세션 목록/삭제, 백업 실행, 감사 로그, 서버 리소스 그래프 등.
여기서 배울 보안 패턴 두 가지:

**① 비밀 URL + 쿠키 게이트** — 관리 페이지 주소 자체를 추측 못 하게 하고, 비밀번호가 맞으면 쿠키 발급:

```python
# URL 경로에 비밀 조각을 넣음: /pe/admin-<secret>/
app.register_blueprint(admin_panel_bp, url_prefix=f"/pe/admin-{secret}")

# 매 요청 쿠키 확인 (쿠키엔 비밀번호 원문 대신 sha256 토큰 저장)
if hmac.compare_digest(request.cookies.get(_AUTH_COOKIE, ""), _expected_token()):
    return None    # 통과
```

**② 관리 API 변경요청엔 커스텀 헤더 요구** — 남의 사이트가 몰래 관리 API 를 못 부르게(CSRF 방어):

```python
if request.method not in ("GET","HEAD","OPTIONS") and request.headers.get("X-Admin-Request") != "1":
    abort(403)   # 브라우저 폼은 커스텀 헤더를 못 붙임 → 교차출처 공격 차단
```

**리소스 모니터링은 요청 경로에 부담 안 주게** ([`metrics.py`](server/admin_panel/metrics.py)):
10초마다 데몬 스레드 1개가 CPU/메모리를 재서 24시간치 링버퍼(고정 크기 큐)에 쌓습니다. 요청이 들어올
때마다 하는 일은 **정수 하나 증감(in-flight 카운터)** 뿐 — 측정 때문에 서비스가 느려지면 안 되니까요.

---

## 5. 기타 공통 노하우 (핵심 3)

### 5.1 "지금 요청한 사람이 누구지?" — 신원 확인 (`server/auth_identity.py`)

이 서버는 **로그인 세션이 없습니다.** 대신 요청마다 **누구인지를 그때그때 알아냅니다(stateless).**
현재는 클라이언트가 User-Agent(브라우저 신분증 문자열)에 `HoneyUser/계정` 을 심어 보내고, 서버가 그걸 읽습니다.

**핵심 노하우 — provider(신원 공급자) 체인으로 나중에 SSO 로 갈아탈 길을 열어둠:**

```python
def current_user():
    # 순서대로 시도: ① 역프록시 SSO 헤더  → ② User-Agent 토큰
    return _from_sso_header() or _from_honey_ua()

AUTH_SSO_HEADER = os.getenv("AUTH_SSO_HEADER", "")   # 이 env 를 켜면 SSO 헤더가 우선
```

> 💡 지금은 UA 방식이지만, 나중에 회사에 **SSO**(한 번 로그인하면 여러 서비스 통용)가 생기면 환경변수
> 하나(`AUTH_SSO_HEADER`)만 켜면 그쪽으로 전환됩니다. **코드는 안 바꿔도 됨.** 이렇게 "나중 변경 지점"을
> provider 체인으로 미리 열어두는 게 좋은 설계입니다.
>
> ⚠️ 단, "요청 헤더를 신뢰"하려면 **앞단 역프록시가 외부에서 들어온 같은 헤더를 반드시 제거**해야 합니다.
> 안 그러면 아무나 헤더를 위조해 남 행세를 합니다.

### 5.2 권한 가드 3종 (`server/report/security.py`)

권한 체크를 **작은 함수 3개**로 나눠, 라우트마다 골라 씁니다.

```python
_uploader_guard(session)  # 삭제·비공개·권한부여   → "올린 본인"만
_editor_guard(session)    # 내용 편집              → 올린 사람 또는 위임받은 편집자
_private_guard(session)   # 비공개 세션 "조회"     → 권한 없으면 404 (존재 자체를 숨김)
```

> 💡 **미묘하지만 중요**: 편집 거부는 `403`(권한 없음)이지만, 비공개 세션 조회 거부는 **`404`(없음)**
> 입니다. 403 을 주면 "여기 세션이 있긴 있구나"가 들통납니다. 비공개는 **존재 자체를 숨겨야** 하므로
> "그런 거 없는데요(404)"로 답합니다. 목록에서 SQL 로 숨기는 것(2.4)과 같은 철학.

### 5.3 CSRF 방어 — double-submit 쿠키

**CSRF** = 로그인한 사용자를 속여, 악성 사이트가 사용자 몰래 우리 서버로 요청을 보내는 공격.
이 프로젝트는 **쿠키 두 번 대조(double-submit)** 로 막습니다 ([`security.py`](server/report/security.py)):

```
1) 페이지를 GET 할 때 서버가 랜덤 토큰을 쿠키로 발급 (JS 가 읽을 수 있게)
2) 변경요청(POST/DELETE 등) 시, JS 가 그 토큰을 X-CSRF-Token 헤더로 되돌려 보냄
3) 서버는 쿠키의 토큰 == 헤더의 토큰 인지 확인. 다르면 403
```

악성 사이트는 **우리 도메인의 쿠키를 읽지도, 커스텀 헤더를 위조하지도 못하므로**(브라우저 동일출처
정책) 이 방식으로 막힙니다. 로그인 세션이 없어도 되는 가벼운 방어라 이 프로젝트에 잘 맞습니다.

### 5.4 감사 로그 (`server/database/audit.py`)

"누가 언제 뭘 올리고/고치고/지웠나"를 별도 테이블에 남깁니다. 두 가지 포인트:

- **삭제돼도 기록은 남게**: 세션을 지우면 세션 행은 사라지지만, 감사 로그엔 그때의 메타(파일명·제품 등)
  **스냅샷**을 함께 저장해 나중에도 "무엇을 지웠는지" 알 수 있습니다.
- **best-effort(실패해도 본 작업을 안 깨뜨림)**: 로그 기록이 실패해도 정작 중요한 삭제/편집은 진행됩니다.

```python
def _audit(action, session=None, ...):
    try:
        report_db.log_audit(action, ...)   # 기록 시도
    except Exception:
        pass   # ← 로그 실패가 실제 작업을 막지 않게 조용히 넘어감
```

> 💡 **best-effort 원칙**: "있으면 좋지만 없어도 서비스는 돌아가야 하는 부수작업"(로그·캐시무효화·썸네일)은
> `try/except pass` 로 감싸 **본 작업을 절대 안 막게** 합니다. 이 프로젝트 전반에 깔린 패턴입니다.

### 5.5 응답을 빠르게 — gzip + ETag + 캐시

- **gzip 압축**: 큰 JSON/HTML 응답은 압축해서 보냄(전송량 ↓). `Accept-Encoding: gzip` 확인 후.
- **ETag + 304**: 응답에 "내용 지문(ETag)"을 붙여, 브라우저가 다음에 "이 지문 그대로면 안 보내도 돼"
  라고 물으면 `304 Not Modified` 만 돌려줌(본문 재전송 생략).
- **캐시 무효화는 토큰으로**: 편집이 일어나면 3장의 `rev` 가 바뀌고, 캐시 키에 rev 가 들어있어
  **자동으로** 옛 캐시가 버려집니다. "언제 캐시를 지울까"를 고민할 필요가 없어집니다.

### 5.6 입력 검증 — 화이트리스트로 (`security.py`)

들어오는 값은 **정규식 화이트리스트**로 모양부터 검사합니다. 통과 못 하면 즉시 400.

```python
_ANALYSIS_KEY_RE = re.compile(r"^[0-9a-f]{64}$")     # 64자리 16진수만
_SESSION_ID_RE   = re.compile(r"^[A-Za-z0-9_-]{1,80}$")  # 영숫자/_/- 80자 이내만
```

파일 서빙도 화이트리스트로 **경로 탈출 공격**(`../../비밀파일`)을 막습니다 — 허용된 파일명만 내보냄.

### 5.7 설정은 환경변수로 — 함정 기본값 주의 (`server/config.py`)

모든 설정은 환경변수(`os.getenv`)로 빼서, 코드 수정 없이 운영값을 바꿉니다. **행동을 크게 바꾸는
기본값 몇 개만** 기억하세요:

| 환경변수 | 기본값 | 주의 |
|----------|--------|------|
| `HOST` | `127.0.0.1` | 이 PC 에서만 접속됨. 다른 PC 에서 접속하려면 `0.0.0.0` |
| `REPORT_CLEANUP_DRYRUN` | `1`(참) | **기본은 진짜 삭제 안 함.** 켜려면 `0` |
| `WAITRESS_THREADS` | `8` | 동시 처리 요청 수 |
| `MAX_CONTENT_LENGTH_MB` | `2048` | 업로드 상한(MB) |

---

## 6. 그대로 훔쳐 써도 되는 패턴 체크리스트

새 프로젝트를 시작할 때 이 목록을 그대로 따라가면 탄탄한 기반이 됩니다.

- [ ] **층 나누기**: 진입점 / 조립 / 라우트 / 보안 / 신원 / DB창구(facade) / 운영. 섞지 말 것.
- [ ] **DB 창구(facade) 하나**로 모든 DB 접근을 통과 → 나중에 내부 교체 자유.
- [ ] **SQLite면 WAL + PRAGMA(커넥션마다) + 온라인 백업(backup API)**. 파일 복사 백업 금지.
- [ ] **마이그레이션**: `CREATE IF NOT EXISTS` + `PRAGMA table_info` 확인 후 `ALTER ADD`.
- [ ] **SQL 은 항상 파라미터 바인딩(`?`)**, 컬럼명은 코드 상수 화이트리스트.
- [ ] **수정 가능한 필드는 화이트리스트**로 제한.
- [ ] **신원은 stateless + provider 체인**으로 나중에 SSO 전환 여지 남기기.
- [ ] **권한 가드는 작은 함수로 분리**, 조회 거부는 404(존재 은닉), 편집 거부는 403.
- [ ] **CSRF**: double-submit 쿠키(브라우저) / 커스텀 헤더(관리 API).
- [ ] **감사 로그 + best-effort**: 로그·캐시 등 부수작업은 실패해도 본 작업 안 막기.
- [ ] **백그라운드 작업**: daemon 스레드 + `_started` 가드 + 위험작업엔 **DRYRUN 기본값**.
- [ ] **동시 편집**: 항목단위 upsert + rev 토큰 → 필요하면 낙관적 락(409) → 폴링/SSE 준실시간.
- [ ] **응답 성능**: gzip + ETag(304) + 캐시키에 무효화 토큰(rev) 포함.
- [ ] **헬스체크(`/healthz`) + 전역 에러 핸들러**로 내부 노출 차단.
- [ ] **설정은 env**, 위험 기본값(DRYRUN·HOST)은 안전한 쪽으로.

---

## 7. 파일 위치 색인표

| 알고 싶은 것 | 파일 |
|--------------|------|
| 서버 켜기(waitress·로그·인코딩) | [`server/wsgi.py`](server/wsgi.py) |
| 조립(Blueprint·DB init·스케줄러) | [`server/plugin.py`](server/plugin.py) |
| 헬스체크·전역 에러 핸들러 | [`server/ops.py`](server/ops.py) |
| 설정(환경변수 전부) | [`server/config.py`](server/config.py) |
| DB 스키마(정본)·마이그레이션·커넥션·락 | [`server/database/core.py`](server/database/core.py) |
| DB 창구(facade) | [`server/database/report_db.py`](server/database/report_db.py) |
| 세션 CRUD·검색 히스토리 | [`server/database/sessions.py`](server/database/sessions.py) |
| 편집 저장(upsert·rev 토큰) | [`server/database/webreport_edits.py`](server/database/webreport_edits.py) |
| 감사 로그 | [`server/database/audit.py`](server/database/audit.py) |
| 즐겨찾기·편집 위임·방문자 | [`server/database/users.py`](server/database/users.py) |
| 세션 조회/삭제/권한 라우트 | [`server/report/routes_session.py`](server/report/routes_session.py) |
| 검색 히스토리·주석·즐겨찾기 라우트 | [`server/report/routes_misc.py`](server/report/routes_misc.py) |
| 권한 가드·CSRF·입력검증 | [`server/report/security.py`](server/report/security.py) |
| 신원 확인(SSO-ready) | [`server/auth_identity.py`](server/auth_identity.py) |
| 자동 청소·티어링 스케줄러 | [`server/report_cleanup.py`](server/report_cleanup.py) |
| DB 자동 백업 | [`server/db_backup.py`](server/db_backup.py) |
| 관리 대시보드 진입·게이트 | [`server/admin_panel/__init__.py`](server/admin_panel/__init__.py) · [`routes.py`](server/admin_panel/routes.py) |
| 리소스 모니터링 샘플러 | [`server/admin_panel/metrics.py`](server/admin_panel/metrics.py) |

---

*이 문서는 `report_server` 의 서버·세션 관리 부분을 신규 프로젝트 벤치마킹용으로 정리한 것입니다.
각 코드 스니펫은 위 실제 파일에서 발췌했으며, 정확한 최신 구현은 해당 파일을 직접 확인하세요.*
