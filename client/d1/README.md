# client/d1 — D1 입력 제공자 경계

> `ENTRYPOINT / EXTERNAL_OWNER`. Honey 클라이언트가 분석 입력(csv/xlsx)을 **어디서
> 가져올지** 결정하는 경계. 기본 구현은 로컬 `d1_storage` 폴더를 검색한다.
> **외부 D1 담당자는 이 패키지만 교체하면 된다.**

## 1. 단일 진입점

Honey UI(`client/honey_main.py`)는 이 패키지의 공개 함수/다이얼로그만 사용하고,
입력 소스의 실체(로컬 폴더인지 서버 API인지)를 알지 못한다.

```
honey_main (UI)  ──▶  client/d1 (provider)  ──▶  LocalD1Provider (기본: 로컬 폴더 검색)
 결과 경로 목록만 사용      ↑ 여기까지만 의존        ↑ 외부 담당자가 교체
```

## 2. 공개 인터페이스 (`__init__.py`)

| 심볼 | 용도 |
|------|------|
| `get_provider()` | 활성 provider 반환. **외부 브랜치는 이 함수를 교체**해 server-backed provider 를 돌려준다 |
| `list_files(query="")` | 편의 진입점 — provider 를 ready 시키고 매칭 파일 경로 리스트 반환 |
| `D1BrowserDialog(parent=None, provider=None, ui_path=None)` | 검색/다중선택 다이얼로그(PyQt5). 선택 경로는 `selected_paths()` |
| `LocalD1Provider(storage_dir=None)` | 기본 provider. `list_files(query)`/`ensure_ready()` 제공 |

provider 계약(외부 구현이 지켜야 할 표면):
- `ensure_ready()` — 사용 전 준비(폴더 생성 등).
- `list_files(query="")` — 쿼리에 매칭되는 `Path` 리스트 반환.
- `storage_dir` 속성 — 다이얼로그 라벨/상대경로 표시에 사용.

## 3. 설정

```
HONEY_D1_STORAGE   입력 검색 폴더 경로. 미설정 시 기본값:
                   - exe(frozen): 실행 파일 폴더/d1_storage
                   - 스크립트: client/d1_storage
```
`client/config.py` 의 `D1_STORAGE_DIR` 가 이 값을 읽어 `LocalD1Provider` 기본값으로 쓴다.
`LocalD1Provider(storage_dir=...)` 로 직접 주입하면 config 없이도 동작한다.

## 4. 외부 D1 담당자 교체 가이드

1. **다른 로컬 경로**: `HONEY_D1_STORAGE` 환경변수만 설정.
2. **서버 백엔드로 교체**: `get_provider()` 가 server-backed provider 를 반환하도록
   이 패키지를 브랜치/교체. provider 계약(`ensure_ready`/`list_files`/`storage_dir`)만
   지키면 **Honey UI 코드는 무수정**.
3. `D1BrowserDialog` 의 검색 UX(검색어 → 결과 목록 → 다중선택)는 유지 권장.

## 5. 유지 계약

- Honey UI 는 provider 가 돌려준 **파일 경로 목록만** 사용한다. 반환 타입은 `Path`.
- 공개 심볼(`get_provider`/`list_files`/`D1BrowserDialog`/`LocalD1Provider`) 이름·시그니처
  변경 금지 — `honey_main.py` 의 계약이다.
