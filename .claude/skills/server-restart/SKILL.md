---
name: server-restart
description: 코드·설정을 고친 뒤 서버 재기동이 필요한지 판단하고, 필요하면 terminate.bat → start.bat 순서로 안전하게 재기동. 사용자가 "서버 재시작해야 돼?", "재기동", "고쳤는데 반영이 안 돼", "서버 내렸다 올려줘" 라고 하거나, 파이썬 코드·server.env·cache_policy 를 수정한 직후에 사용.
---

# 서버 재기동 판단·수행

**운영 중인 서비스다.** 재기동은 진행 중인 업로드·리포트 빌드를 끊을 수 있으므로,
실제 실행 전에 반드시 사용자 승인을 받는다. 이 skill 의 주 역할은 판단과 안내다.

## 1. 재기동 **불필요** — 파일만 덮어쓰면 즉시 반영

- `server/report/report_view.html`, `server/report/report_analysis_index.html`
- `server/report/static/webreport/*.js` (그 외 static 자산도 동일)
- `server/landing/landing.html`
- `server/releases/announcement.txt` (업데이트 공지 원문)
- `DB/pe/report/product_info.db` — (mtime, size) 변화를 감지해 자동 재로딩
- `/pe/eval` 룰 편집(thresholds·signature) — 저장 즉시 반영, `rules_rev` 가 올라 캐시가 갈림

이 경우 "반영이 안 된다"는 신고는 재기동 문제가 아니라 **브라우저 캐시**이거나,
web_report payload 라면 **스키마 버전 미상향**이다(→ `webreport-change` skill).

## 2. 재기동 **필요**

- 모든 파이썬 코드 (`server/`, `web_report/`, `eval_analyzer/`)
- `server/env/server.env` — HOST/PORT/워커 수 등 환경변수 정본
- `web_report/cache_policy.py` 의 스키마 버전 상수
- `requirements.txt` 변경(의존성 설치 후)

## 3. 절차 — 반드시 이 순서

```
server\terminate.bat
server\start.bat
```

**terminate.bat 이 하는 일**(순서대로):

1. **watchdog 일시 정지** (`schtasks /Change /TN report-server-watchdog /DISABLE`).
   이걸 건너뛰면 서버를 내려둔 사이 5분 주기 watchdog 이 끼어들어 **옛 코드로 재기동**한다.
2. **drain** ([drain_wait.ps1](../../../server/drain_wait.ps1)) — `inflight`(진행 중 요청 수)가
   0 이 되는 순간을 노려 종료(최대 90초, `DRAIN_TIMEOUT_SEC`). 진행 중 요청 수가
   `DRAIN_STALL_SEC`(15초) 동안 **줄지 않으면** 멈춘 것으로 보고 곧바로 종료로 넘어가고,
   그때는 **종료 직전 스레드 덤프**를 `log\diagnose_terminate_*.txt` 에 남긴다 —
   서버를 내리면 안 끝나던 요청의 현행범 스택이 통째로 사라지기 때문이다(2026-08-19 사고).
   waitress 를 "신규 요청 차단" 상태로 만들 수는 없으므로 **완전한 drain 은 아니다** —
   요청이 없는 순간을 포착하는 것이다.
   > 급하면 `server\terminate.bat force` — drain 을 아예 건너뛴다(덤프는 그래도 남는다).
3. `kill_server_tree.ps1` 로 **트리째** 종료. 서버 프로세스 하나만 죽이면 안 된다 —
   web_report 컴퓨트 워커는 포트를 LISTEN 하지 않아 고아로 남고, 워커당 tables 캐시가
   최대 4GB 다.

**start.bat 이 하는 일**: `env/server.env` 를 읽어 기동하고, `.venv` 파이썬이 3.11 미만이면
`.venv_old` 로 밀어낸 뒤 재생성한다(3.10 venv 가 남아 콜드 빌드가 100% 실패한 사고 대응).
기동을 마치면 **watchdog 을 자동 재개**한다.

## 4. 주의

- **terminate.bat 만 돌리고 start.bat 을 안 쓸 거면** watchdog 을 수동으로 되살려야 한다:
  `schtasks /Change /TN report-server-watchdog /ENABLE`
- `HOST` 를 운영 IP 로 바꾸지 말 것 — 그 IP 를 가진 PC 외에서 기동이 실패한다. `0.0.0.0` 유지.
  (실사례: LISTEN 은 정상인데 watchdog 점검만 실패해 재기동 무한 반복)
- `start.bat` / `terminate.bat` 은 **CRLF + BOM 없는 UTF-8** 로 저장해야 한다
  (`.gitattributes` 가 강제). LF 면 cmd 가 죽고, BOM 이면 첫 줄 `@echo off` 를 못 읽는다.
- 스키마 버전을 올린 재기동이면 이어서 프리웜을 권한다(→ `webreport-change` skill 4단계).
- 포트가 안 잡히거나 watchdog 이 이상하면 진단 스크립트가 따로 있다:
  `server\diagnose_port.bat`, `server\diagnose_watchdog.ps1`.
