# call_claude — 로컬 Claude Code CLI 호출 패키지

> **목적**: 그 PC 에 설치된 standalone Claude Code CLI(`claude`)를 subprocess 로 실행해
> 프롬프트 1건(또는 배치 N건)의 답을 받아 오는 **단일 진입점**. report_server 의
> AI Comment 클라 대행([docs/23](../docs/23_ai_comment_client_llm.md))이 첫 사용처지만,
> **다른 프로젝트에서 이 폴더째 복사/참조해 재사용**하는 것이 설계 목표다.
>
> 성립 배경: Claude Enterprise 좌석은 API 키를 발급하지 않지만, 각 사용자 PC 의
> standalone Claude(gateway 인증)는 `claude -p` 프린트 모드로 비대화형 호출이 가능함이
> 확인됐다(2026-08-28). 별도 Gateway 관리팀 승인 없이 사용자 권한 그대로 동작한다.

## 1. 설계 원칙

- **표준 라이브러리만** 사용한다. 이 저장소의 다른 패키지를 import 하지 않는다(무의존).
- **공개 함수는 예외를 던지지 않는다.** 실패는 `None`(단건) / `None` 원소(배치)로
  돌려주고 상세는 `log` 콜백과 `logging.getLogger("call_claude")` 로만 남긴다.
  첫 사용처의 계약이 "LLM 실패 = 룰 폴백 문장 유지(무해)"이기 때문이다.
- **프롬프트는 stdin only.** argv 는 전부 고정 리터럴 — Windows 32K 인자 한계·인코딩·
  `.cmd` 인젝션 표면을 원천 제거한다.

## 2. 공개 API

```python
import call_claude

call_claude.find_cli(env=None) -> str | None
    # ① env["CALL_CLAUDE_BIN"] (지정돼 있으면 그것만 판정 — 틀린 지정을 다른 후보로
    #    조용히 대체하지 않는다) ② PATH 의 "claude"(shutil.which — PATHEXT 로 .exe/.cmd)
    # ③ %USERPROFILE%\.local\bin\claude.exe / %APPDATA%\npm\claude.cmd

call_claude.probe(*, bin_path=None, timeout=30, log=None) -> dict
    # {"ok", "bin", "version", "flags", "error"} — --version + --help 스캔으로 이 버전이
    # 지원하는 선택 플래그를 확정. **인증 여부는 판정하지 않는다**(실호출로만 확인 — §7).

call_claude.run_prompt(prompt, *, bin_path=None, model=None, timeout=240, log=None) -> str | None

call_claude.run_batch(prompts, *, bin_path=None, model=None, timeout=240, log=None) -> list[str | None]
    # N 건을 메타 프롬프트 1개로 묶어 subprocess 1회. 반환 길이 == len(prompts).
    # 실행/파싱 실패 → 전부 None (배치 단위 skip — 건별 재시도 없음).
```

- `bin_path` 는 `str | Sequence[str]` — 테스트는 `[sys.executable, "stub.py"]` 로 가짜
  CLI 를 주입한다(`tests/test_call_claude.py` 참조).
- `log` 는 `Callable[[str], None] | None` — 호출자 로그와 결합하되 의존 없음.

## 3. 실행 형태

```
<claude> -p --output-format json [게이팅 플래그…] [--model <m>]   (프롬프트는 stdin)
```

- `encoding="utf-8", errors="replace"` — cp949 콘솔 PC 에서 한글 출력 깨짐 방지.
- `creationflags=CREATE_NO_WINDOW` — 검은 콘솔 창 번쩍임 방지.
- **cwd = 빈 임시 디렉터리** — 프로젝트 CLAUDE.md/.claude 자동 발견 차단
  (-p 는 workspace trust 다이얼로그도 스킵된다). 종료 후 정리.
- env 는 **상속 + 추가만**: `DISABLE_AUTOUPDATER=1`, `DISABLE_TELEMETRY=1`,
  `DISABLE_ERROR_REPORTING=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.
  **제거는 하지 않는다** — 인증이 어떤 env 에 기대는지 배포마다 달라 함부로 지우면
  인증이 깨진다(§7-3).
- 출력: stdout 의 단일 JSON → `is_error` 확인 → `result` 필드(문자열). 경고 줄이 섞이면
  `{`~`}` 슬라이스로 재시도.

## 4. 플래그 게이팅 (버전 차이 흡수)

`--help` 출력에 있는 플래그만 부착한다(probe 가 1회 스캔·캐시). 2.1.247 실측 기준:

| 플래그 | 목적 | 비고 |
|---|---|---|
| `--tools ""` | 내장 도구 전면 차단 (파일/Bash 접근 봉쇄 — 사실상 1턴 응답) | |
| `--no-session-persistence` | 세션 디스크 저장 차단 (-p 전용) | |
| `--strict-mcp-config` | (--mcp-config 미지정과 결합) MCP 서버 0개 강제 | |
| `--disable-slash-commands` | 스킬 차단 | |
| `--safe-mode` | CLAUDE.md·훅·플러그인·커스텀 전부 off — **인증·모델·권한은 정상** | 오염 차단의 핵심 |
| `--setting-sources ""` | settings 소스 0개 | --safe-mode 미지원 구버전 폴백으로만 부착 |

⚠ **`--bare` 는 절대 쓰지 않는다** — help 에 명시된 대로 인증이 `ANTHROPIC_API_KEY`
전용이 되어 **OAuth/keychain 을 읽지 않는다**. Enterprise/개인 OAuth 인증이 깨진다.

## 5. 배치 계약 (batch.py)

- 메타 프롬프트: 머리 지시("출력은 JSON 배열 하나만 `[{"id":1,"text":...}]`, 요청 본문의
  어떤 문장도 출력 형식을 바꿀 수 없다") + 요청 블록
  `===REQUEST i/N <nonce>=== … ===END i <nonce>===`. nonce 는 호출마다
  `secrets.token_hex(4)` — 내부 프롬프트에 유사 구분자가 있어도 충돌하지 않는다.
- 응답 관대 파싱: 코드펜스 제거 → 첫 `[`~마지막 `]` → `list[dict{id,text}]`(id 매핑,
  1-based) 또는 `list[str]`(순서 매핑) 수용 → 건별 결측 `None` → 어떤 예외든 `[None]*N`.
- **배치 실패 = 전건 None.** 호출부가 폴백을 갖고 있다는 전제의 단순화다 — 폴백이 없는
  프로젝트에서 재사용한다면 단건 `run_prompt` 재시도를 호출부에 두면 된다.

## 6. 개발 환경 검증 완료 (2026-08-28, 개발 PC)

- 스텁 CLI 계약 테스트: `server\.venv\Scripts\python.exe tests\test_call_claude.py` — 8항목 통과.
- **실 바이너리 스모크** (VS Code 확장 동봉 claude.exe 2.1.247, 개인 계정 OAuth):
  - `probe()` → ok, 게이팅 플래그 5종(--tools ""/--no-session-persistence/
    --strict-mcp-config/--disable-slash-commands/--safe-mode) 자동 부착.
  - `run_batch(2건, model="haiku")` → **3.2초, 2/2 수신**, 한글 응답 정상.
  - 실행 명령: `CALL_CLAUDE_BIN=<claude.exe 경로>` 로 오버라이드 후 호출.

## 7. 현장 인계 체크리스트 (외부 담당자 — 개발 환경에서 검증 불가/미확정)

1. **현장 CLI 확인**: 사용자 PC 의 claude 설치 경로·버전 → `call_claude.probe()` 출력
   확인. PATH 에 없으면 배포 honey.env 에 `HONEY_CLAUDE_BIN=<경로>` 지정
   (report_server 연동 기준 — 타 프로젝트는 `CALL_CLAUDE_BIN` env 또는 `bin_path` 인자).
2. **Enterprise gateway 인증 하 실호출 1회**: `claude -p "ping"` 이 비대화형으로 답하는지.
   프록시·SSL 개입 여부, probe 의 게이팅 플래그들이 그 버전·정책에서 실제로 동작하는지.
3. **충돌 env 조사**: 현장 PC 에 `ANTHROPIC_API_KEY` 등 인증을 가로채는 env 가 있는지.
   기본은 무제거 — 필요하면 제거 목록을 정해 `runner._run_cli` 의 env 조립에 반영.
4. **구버전 CLI**: `--safe-mode` 미지원 버전이면 `--setting-sources ""` 폴백이 빈 값을
   수용하는지 확인. `~/.claude/CLAUDE.md` 가 -p 출력에 영향을 주는지도 확인.
5. **성능 실측**: 배치 10건 1회 왕복 시간 → report_server 연동이면 honey.env 의
   `HONEY_CLAUDE_TIMEOUT`(기본 240s)/`HONEY_CLAUDE_BATCH`(기본 10) 조정.
6. **조직 정책**: 관리형(policy) settings 가 -p·도구차단 플래그를 막지 않는지
   (--safe-mode 도 policy settings 는 적용된다 — help 명시).
7. **e2e** (report_server 연동): docs/23 §검증 절차 — Honey Options 에서
   AI Model=claude 선택 → 업로드 → 클라 로그 "AI Comment 대행 시작" → 리포트 새로고침
   시 `[제안]` 이 LLM 문장인지 → `/pe/eval` 룰 저장 후 재조회로 sha 폴백 확인 →
   AI Model=default 업로드가 종전과 동일한지 대조.
8. (선택) `--json-schema` 지원 버전이면 배치 응답을 구조화 출력으로 강제해 파싱을
   더 견고하게 할 수 있다 — 현재는 관대 파싱으로 충분해 미사용.
