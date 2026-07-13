# 14. 외부 담당자 영역 ↔ 이 프로젝트 병합 순서

> 외부 담당자(외부 소유) 버전의 `client/report_generator/` 와 `server/storage_gateway/` 를
> 나중에 **폴더째 교체**할 때, 이 프로젝트 전용 확장이 유실되지 않도록 하는 **병합 순서·계약
> 단일 참조점**. 교체 대상은 [CLAUDE.md §0 (외부)](../CLAUDE.md)·[INDEX §3.1](INDEX.md) 참조.

---

## 0. 왜 이 문서가 있나

`report_generator`·`storage_gateway` 는 외부에서 소유·교체된다. 그런데 이 프로젝트는 이 둘에
이 프로젝트 전용 기능을 **추가**했다(웨이퍼 bin map, web_report parquet/manifest 저장, S3 헬스/
백필 헬퍼). 교체 시점에 이 확장이 사라지지 않도록:

- **map** 은 교체 대상(`report_generator`) **밖**(`client/map_report/`)으로 이미 분리했다(A-1).
- **storage 확장 함수**는 `storage_gateway` 공개 API(facade)로 고정하고 계약을 문서화했다(A-2).
- **df_honey 의 DDI 진입점**은 교체로 딸려오므로 지금 신규 구현하지 않고, 이식 순서만
  못박는다(A-3).

---

## 1. 순서 불변식 (반드시 이 순서)

```
1) report_generator 폴더 교체 (외부 담당자 버전)
        │  외부 담당자 df_honey.from_ddi_paths / from_ddi_paths_by_dut 가 딸려옴
        ▼
2) honey_main MDDI 파이프라인 이식
        │  MDDI 분기가 from_ddi_paths* 를 요구 — 1) 이후에만 성립
        ▼
   (검증) MDDI/PDDI 리포트 생성 E2E
```

- **먼저** `report_generator` 를 교체하고, **그 다음** honey_main 의 MDDI 이식을 한다.
  둘은 **반드시 한 세트**로 진행한다(순서 뒤집기·분리 금지).
- 이유: 이 프로젝트 `client/report_generator/df_honey.py` 에는 `from_csv` 만 있고 외부 담당자가
  쓰는 `from_ddi_paths` / `from_ddi_paths_by_dut` 가 없다. honey_main 의 MDDI 분기가 이 진입점을
  요구하므로, 진입점이 딸려오는 report_generator 교체가 선행되어야 한다.
- **지금 이 프로젝트에서 `from_ddi_paths*` 를 새로 구현하지 말 것** (교체로 충족 — A-3).

---

## 2. A-1 결과 — map 을 report_generator 밖으로 분리 (완료)

교체 후에도 honey_main 이 **표준 `xlsx_writer.write()` 시그니처**만 쓰도록 map 을 들어냈다.

- 신규 패키지 [client/map_report/](../client/map_report/) 로 이동: `render_map_png`,
  `build_map_pngs`, `write_map_sheet`, `MAP_COORD_ERROR_MSG` + 신규 `attach_map_sheet`.
  내부에서 쓰던 `_report_sheet_display_name`(첫 글자 대문자화) 은 **인라인**(교체 대상 역참조
  제거).
- `report_generator` 는 이제 **map 코드 0**: `xlsx_writer.write()` 에서 `map_pngs=` 파라미터와
  Map 부착 블록 제거, `map_analyze.py` 삭제.
  - 확인: `grep -rn "map_analyze\|map_pngs\|write_map_sheet" client/report_generator/` → 비어야 함.
- 부착 시점 변경: honey_main 은 `xlsx_writer.write()` **반환 후** 별도 xlwings 세션으로
  [map_report.attach_map_sheet](../client/map_report/__init__.py) 를 호출해 Map 시트를 붙인다
  (write 와 같은 COM-init 워커 스레드). Excel 재오픈 1회 비용이 추가되지만, 교체 대비를 위한
  필연적 분리다.
- 소비처 import: `honey_main.py`(`import map_report`), `excel_download/_charts.py`
  (`from map_report import render_map_png` — web_report Excel 다운로드의 wafer map 렌더).

**교체 시 주의**: report_generator 를 갈아끼워도 `client/map_report/` 는 건드리지 않는다.
honey_main 의 map 호출은 report_generator 계약에 의존하지 않는다(별도 세션 부착).

---

## 3. A-2 결과 — storage_gateway 공개 계약 고정 (완료)

교체본이 지켜야 할 계약은 [storage_gateway/README.md §2](../server/storage_gateway/README.md) 정본.
요지:

- **공개 함수 전체 + 예외 2종(`S3NotConfigured`, `S3ObjectCorrupted`)의 이름·시그니처를
  그대로 노출**해야 프로젝트 코드(라우트·업로드·admin·백필)가 무수정으로 동작한다.
- 이 프로젝트가 추가해 **보존 대상**인 함수: `save_webreport_sources` / `save_webreport_manifest` /
  `load_webreport_sources` / `load_webreport_manifest` / `delete_report_artifacts` /
  `save_issue_images` / `s3_available` / `s3_health` / `s3_object_exists` /
  `download_bytes_from_s3` / `make_distribution_combined_s3_key`.
- **facade 경계**: 프로젝트 코드는 내부 `_s3`·`_issue_images` 를 직접 import 하지 않는다.
  (admin_panel/sysinfo.py, tools/backfill_local_to_s3.py 의 직접 import 위반은 공개 API 경유로
  정리 완료.)
  - 확인: `grep -rn "storage_gateway\._s3\|storage_gateway import _s3\|import _issue_images" server/`
    → 비어야 함.
- **교체본 `_s3.py` 가 보존할 이 프로젝트 상수/env**: prefix 3종(`REPORT_S3_DIST_COMBINED_PREFIX`,
  `REPORT_S3_WEBREPORT_SOURCE_PREFIX`, `REPORT_S3_WEBREPORT_MANIFEST_PREFIX`) + 타임아웃 env
  (`REPORT_S3_CONNECT_TIMEOUT` 기본5, `REPORT_S3_READ_TIMEOUT` 기본30). 상세는 README §5.

---

## 4. A-3 — df_honey DDI 진입점 (교체로 충족, 신규 구현 금지)

- [client/report_generator/df_honey.py](../client/report_generator/df_honey.py) 는 **수정하지
  않는다**(외부·무수정 존). 현재 `from_csv` 만 있음.
- 외부 담당자가 쓰는 `from_ddi_paths` / `from_ddi_paths_by_dut` 는 report_generator 교체로
  자연히 딸려온다 → **이 프로젝트에서 미리 구현하지 말 것**.
- honey_main MDDI 파이프라인 이식은 §1 순서에 따라 **report_generator 교체 직후 한 세트**로.

---

## 5. 병합 체크리스트

- [ ] `report_generator/` 를 외부 담당자 버전으로 교체 (map 코드 없는 상태 유지 확인).
- [ ] `client/map_report/` 유지(교체로 삭제/덮어쓰지 않기).
- [ ] honey_main MDDI 분기 이식 — `from_ddi_paths*` 사용 (report_generator 교체 후).
- [ ] `storage_gateway/` 교체 시 [README §2](../server/storage_gateway/README.md) 공개 함수 +
      예외 전부 노출 확인, `_s3.py` 보존 상수/env 이식 확인.
- [ ] 검증: MDDI/PDDI 리포트 생성 → xlsx Map 시트(yield~cpk 사이) 부착 + web_report Excel
      다운로드 wafer map 렌더.
- [ ] 검증: `python tools/backfill_local_to_s3.py`(dry-run) 정상, `/pe/admin-pte/` S3 상태 카드 동작.
