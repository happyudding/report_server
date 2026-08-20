# 21 · Input File Information (세션 상세 ℹ 모달) + STDF 메타 요청 스펙

> "세션 안에서 내 input Data 정보를 볼 방법이 없다"(2026-08-20 요청)에 대한 답.
> 세션 상세 우상단 **ℹ** 버튼 → source 별 입력 파일·STDF 헤더를 가로로 넓은 표로 보여준다.
> 관련: 파이프라인 [10](10_web_report_pipeline.md) · 조회/접근제어 [02](02_server_query_edit.md)
>
> **§4 는 외부 담당자(honey_parse 소유자)에게 그대로 전달할 요청 스펙이다.**

## 1. 무엇을 보여주나

| 구분 | 항목 | 출처 | 현재 상태 |
|------|------|------|-----------|
| source | Source(legend) · Role(RT/CT/HT) · 그룹(Before/After, Group N) | manifest + 세션 옵션 | ✅ 동작 |
| 파일 | Input File(파일명) · 경로 · 크기 · **생성 날짜** · 수정 날짜 | Honey 가 `os.stat` 으로 수집 | ✅ **신규 업로드부터** |
| STDF | **LOT ID · Wafer No · Test 시각 · Test Time** · 수량/Good · Part Type · Job · Tester · Operator | honey_parse 가 넘겨줘야 함 | ⏳ **미수급** (§4 요청 대기) |

**열 구성은 데이터가 정한다** — 전 source 가 비어 있는 열은 화면에서 **통째로 사라진다**.
그래서 STDF 를 못 받는 지금은 그 열들이 아예 안 보이고 안내문만 뜨며, 담당자가 값을 넣어
보내기 시작하면 **코드 수정 없이** 열이 나타난다. 빈 칸을 15개 늘어놓지 않기 위한 규칙이다.

**기존 세션은 파일 정보가 없다**(manifest 에 그 키가 아예 없다). 이때 응답은
`has_file_info:false` 로 **200** 이고, 화면이 "이 세션은 기능 추가 전에 업로드되어 …" 라고
이유를 밝힌다. 없는 정보를 에러로 만들지 않는다 — 소급은 불가능하다(원본 파일이 그
사용자 PC 에 있고 서버는 parquet 만 갖고 있다).

## 2. 데이터 경로

```
[Honey] honey_main._build_webreport_parquets
          └ source_file_info(md)          ← honey_ui/source_name_dialog.py (조회 규칙 정본)
              ├ source_input_paths(md)    → file_path / input_files
              ├ os.stat(대표 파일)         → file_size / file_created / file_modified
              └ source_stdf_meta(md)      → stdf {} (현재 거의 항상 빈 dict)
                    ↓ manifest["sources"][i] 에 병합
══════ 업로드(POST /pe/report/upload_webreport) — manifest 는 그대로 저장 ══════
[서버] web_report/service.py input_info()
          ├ cache.load_manifest_cached()  ← parquet 을 **디코드하지 않는다**
          ├ compare.resolve_group_names() ← Before/After (리포트 본문과 같은 함수)
          └ metrics.temperature_roles()   ← RT/CT/HT (payload 태깅과 같은 함수)
                    ↓ GET /pe/report/session/<sid>/web_report/input_info
[브라우저] static/webreport/input_info.js — 빈 열 제거 → 그룹 소제목 → 표
```

**성능**: manifest(작은 JSON, 이미 캐시) 하나만 읽으므로 대형 세션에서도 즉시 응답한다.
parquet 다운로드·디코드 경로를 여기에 끌어들이지 말 것 — 모달 하나 열자고 콜드 빌드를
유발하게 된다.

**그룹 배치는 사본을 만들지 않는다** (CLAUDE.md 규칙 #13). Compare 의 폴백
(`after=[0]/before=[1]`)과 Temperature 의 역할 폴백(`member_roles` 없으면 members 순서로
CT→HT)이 각각 **한 곳에만** 있어야, 모달과 리포트가 같은 source 를 같은 그룹으로 그린다.
등가는 [tests/test_input_info.py](../tests/test_input_info.py) (c)(d)(e) 가 고정한다.

## 3. manifest sources 계약 (현행)

```jsonc
"sources": [{
  "index": 0,
  "name": "602XX2_3",                       // legend (필수 — 종전부터)
  "file_name": "602XX2_3_final.std",        // 대표 입력 파일명 (필수 — 종전부터)

  // ↓ 2026-08-20 추가. **모르면 키 자체를 만들지 않는다** (빈 문자열 금지 —
  //   "값이 없다" 와 "값이 빈 문자열이다" 를 서버가 구분할 수 없게 된다)
  "file_path":     "D:\\lot\\602XX2\\602XX2_3_final.std",
  "input_files":   ["...", "..."],          // 병합 입력이 2개 이상일 때만
  "file_size":     5242880,                 // bytes (int)
  "file_created":  "2026-08-01 09:30:00",   // Windows st_ctime = 생성 시각
  "file_modified": "2026-08-01 10:05:00",
  "stdf":          { /* §4 */ }
}]
```

호환: 위 키는 전부 **선택**이다. 없으면 서버가 빈 값으로 정규화하고 화면이 '-' 로 그린다.
구 Honey 가 올린 세션도 그대로 열린다.

---

## 4. 외부 담당자 요청 스펙 — STDF 헤더 메타 (⏳ 회신 대기)

**요청 내용**: `honey_parse.file_to_df` 가 돌려주는 각 source 에 STDF 헤더 값을 함께
실어 주세요. DataFrame 계약(7-meta honeyform)은 **그대로 두고**, 옆에 메타만 붙이면 됩니다.

### 4-1. 넣는 방법 (둘 중 아무거나 — 클라가 양쪽 다 읽습니다)

```python
# 방법 A (권장): report_meta 에 dict 하나
md.report_meta.stdf = {"lot_id": "602XX2", "wafer_id": "3", "start_t": 1785000660, ...}

# 방법 B: report_meta 의 개별 속성
md.report_meta.lot_id   = "602XX2"
md.report_meta.wafer_id = "3"
```

`stdf` / `stdf_meta` / `header_meta` 중 아무 이름이나 됩니다. **채워 주시면 Honey·서버·웹
코드는 수정 없이 값이 흐릅니다** — 조회가
[client/honey_ui/source_name_dialog.py](../client/honey_ui/source_name_dialog.py)
`_STDF_FIELDS` 표 한 곳에 모여 있고, 그 표가 아래 이름 후보를 이미 다 받아들입니다.

### 4-2. 필드 표

| 우리 키 | STDF 레코드 | 받아들이는 이름(아무거나) | 타입 | 화면 표기 | 우선순위 |
|---------|-------------|--------------------------|------|-----------|----------|
| `lot_id` | MIR.LOT_ID | `lot_id` `lotid` `LOT_ID` | str | LOT ID | ★ 필수 |
| `wafer_id` | WIR.WAFER_ID | `wafer_id` `waferid` `wafer_no` `WAFER_ID` | str | Wafer No | ★ 필수 |
| `start_time` | MIR.START_T | `start_time` `start_t` `START_T` | **epoch 초 또는 문자열** | Test 시작 | ★ 필수 |
| `finish_time` | MRR.FINISH_T | `finish_time` `finish_t` `FINISH_T` | 〃 | Test 종료 | ★ 필수 |
| `test_time_sec` | (계산) | `test_time_sec` `test_time` `elapsed_sec` | 초(number) | Test Time | ☆ 없으면 종료−시작으로 계산 |
| `sublot_id` | MIR.SBLOT_ID | `sublot_id` `sblot_id` `SBLOT_ID` | str | Sub LOT | ☆ 있으면 |
| `part_type` | MIR.PART_TYP | `part_type` `part_typ` `PART_TYP` | str | Part Type | ☆ |
| `job_name` | MIR.JOB_NAM | `job_name` `job_nam` `JOB_NAM` | str | Job | ☆ |
| `node_name` | MIR.NODE_NAM | `node_name` `node_nam` `NODE_NAM` | str | Tester | ☆ |
| `tester_type` | MIR.TSTR_TYP | `tester_type` `tstr_typ` `TSTR_TYP` | str | Tester Type | ☆ |
| `oper_name` | MIR.OPER_NAM | `oper_name` `oper_nam` `OPER_NAM` | str | Operator | ☆ |
| `setup_time` | MIR.SETUP_T | `setup_time` `setup_t` `SETUP_T` | epoch/문자열 | (미표시, 보관) | ☆ |
| `part_count` | WRR.PART_CNT | `part_count` `part_cnt` `PART_CNT` | int | Test 수량 | ☆ |
| `good_count` | WRR.GOOD_CNT | `good_count` `good_cnt` `GOOD_CNT` | int | Good | ☆ |

**시각 값**은 STDF 원형 그대로 **epoch 초(int)** 로 주셔도 됩니다 — 클라가
`YYYY-MM-DD HH:MM:SS` 로 정규화합니다. 이미 문자열이면 그대로 씁니다.

### 4-3. 주의

- **source 1개 = STDF 파일 1개가 아닐 수 있습니다.** 여러 입력을 1 source 로 병합하는
  product_type(MDDI/PDDI)에서는 **대표 1개 기준**으로 채워 주시면 됩니다(화면도 대표
  파일 하나를 보여주고 나머지는 "외 N개" 로 표시합니다).
- **모르는 값은 넣지 말아 주세요.** 키를 비워 두면 그 열이 화면에서 사라집니다.
  빈 문자열·`"N/A"`·`0` 을 넣으면 "값이 있다" 로 취급되어 빈 열이 남습니다.
- DataFrame(7-meta honeyform) 계약은 **변경 없습니다**. 반환 df 개수 = source 개수 규칙도
  그대로입니다(CLAUDE.md 불변 규칙 #9).

### 4-4. 회신 후 우리가 할 일

없습니다 — 값이 흘러들어오면 신규 업로드 세션부터 자동으로 열이 생깁니다. 확인은 세션
상세 ℹ 버튼 → 표에 LOT ID/Wafer No 열이 보이는지. (이미 올라간 세션에는 소급되지 않습니다.)

---

## 5. 세션 이름(Session name) 수정 — 같은 화면의 이웃 기능

같은 요청 흐름에서 추가됐다(2026-08-20). 검색결과 목록의 이름 칸(`report_session.file_name`)
을 **세션 상세 상단바에서 그 자리에 클릭해** 고친다.

| | `PATCH /session/<sid>/meta` (기존) | `PATCH /session/<sid>/name` (신규) |
|---|---|---|
| 대상 | 이름 + Family/Product/LOT/Process/STEP | **이름만** |
| 인증 | `X-Honey-Agent: 1` 필수 (master 는 CSRF 로 대체) | **CSRF** (헤더 불필요) |
| 어디서 | Honey 편집창 (웹은 master 만) | **웹 브라우저 포함** 어디서나 |
| 여파 | product_info 재lookup(14컬럼 갱신/비움), eval 전송 대상 변화 | 없음 — 표시 전용 |

갈라 둔 이유는 **위험이 다르기 때문**이다. 이름은 `analysis_key`·산출물·기준정보 어디에도
쓰이지 않아 웹에 열어도 잃을 게 없지만, Product/LOT 은 기준정보와 eval 을 함께 움직인다.

⚠️ **`update_session_meta` 를 이름 수정에 쓰지 말 것.** 그 함수는 기준정보 14컬럼을
**항상** 덮어쓰므로(product_info 미지정이면 전부 NULL) 이름만 고치려다 Wafer Size/Gross
Die 가 날아간다. 전용 함수 `sessions.rename_session` 을 쓴다 —
[tests/test_session_meta.py](../tests/test_session_meta.py) (k) 가 이 보존을 고정한다.
`update_session` 화이트리스트에 `file_name` 을 추가하는 것도 금지다(그 주석 참조).

캐시: 무거운 report payload 캐시는 세션 이름을 키에 쓰지 않아 **재계산이 없다**.
`/full` 응답 gzip 캐시만 extras digest 로 자연 무효화된다.

## 6. 파일

| 역할 | 파일 |
|------|------|
| 클라 조회 규칙 (정본) | [client/honey_ui/source_name_dialog.py](../client/honey_ui/source_name_dialog.py) `source_file_info` / `_STDF_FIELDS` |
| 클라 manifest 조립 | [client/honey_main.py](../client/honey_main.py) `_build_webreport_parquets` |
| 서버 조회 | [web_report/service.py](../web_report/service.py) `input_info` |
| 라우트 | [server/report/routes_webreport.py](../server/report/routes_webreport.py) `web_report_input_info` |
| 세션 이름 수정 | [server/report/routes_session.py](../server/report/routes_session.py) `update_session_name_route` + [server/database/sessions.py](../server/database/sessions.py) `rename_session` |
| 화면 | [report_view.html](../server/report/report_view.html)(ℹ 버튼·모달·CSS) + [static/webreport/input_info.js](../server/report/static/webreport/input_info.js) + [tabs_topbar.js](../server/report/static/webreport/tabs_topbar.js)`renderMeta` + [edit_mode.js](../server/report/static/webreport/edit_mode.js)`openSessionNameEdit` |
| 테스트 | [tests/test_input_info.py](../tests/test_input_info.py)(계약 8) · [tests/test_input_info_js.py](../tests/test_input_info_js.py)(headless Edge) · [tests/test_session_meta.py](../tests/test_session_meta.py)(이름 라우트 (j)~(n)) |
