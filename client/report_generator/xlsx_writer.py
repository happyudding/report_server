"""단일 xlwings(Excel COM) 세션 xlsx 리포트 생성 — 진입점/오케스트레이터.

- 모든 시트(raw / summary / yield / cpk / fail_item / issue_table / distribution /
  histogram)를 하나의 xw.App 세션에서 생성·스타일링·저장한다(openpyxl 미사용).
- 셀 기입은 **범위 단위 일괄(bulk range)**, 스타일은 **Range 단위 COM** 적용으로
  셀 단위 왕복을 피한다. raw data 는 임시 CSV 를 Excel 네이티브 파싱으로 복사한다.
- distribution/histogram 차트는 같은 세션에서 그린다(차트 옵션 정밀 제어 목적).

이 모듈은 조립(write)만 담당하고, 실제 기능은 기능별 _xlsx_* 모듈로 분리되어 있다:
  _xlsx_style          스타일/레이아웃 상수 + Range 스타일 헬퍼
  _xlsx_profile        프로파일링/디버그 측정 인프라
  _xlsx_table_helpers  표 채움 공통 헬퍼
  _xlsx_sheets         summary/yield/cpk/fail_item/issue_table 시트 채움
  _xlsx_chart_common   차트 x축 범위 계산 + 제목 배너
  _xlsx_distribution_chart  distribution(ECDF) 차트 그리기
  _xlsx_histogram_chart     histogram 차트 그리기
  _xlsx_png_export     차트 PNG export/부착 + xlsx 파일 검증
  _xlsx_distribution_phase  distribution phase 오케스트레이션 + PNG 썸네일 부착

계산은 analyzer/_builders 에서 끝났고, 이 모듈은 출력만 담당한다. Excel/xlwings 가
없으면 전체 실패한다(openpyxl fallback 없음).
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import xlwings as xw

from ._xlsx_distribution_phase import (
    _apply_zoom_gridlines,
    _write_distribution_phase,
)
from ._xlsx_histogram_chart import _write_histogram_phase
from ._xlsx_png_export import _validate_embedded_images, _wait_for_xlsx_ready
from ._xlsx_profile import _CURRENT_PROFILE_CB, _flow_prof, _prof_report
from ._xlsx_sheets import (
    _fill_cpk,
    _fill_cpk_rows,
    _fill_fail_item,
    _fill_issue_table,
    _fill_summary,
    _fill_yield,
    _unique_sheet_name,
)
from ._xlsx_style import (
    ALL_SHEETS,
    _XL_CALC_AUTO,
    _XL_CALC_MANUAL,
)
from ._xlsx_table_helpers import (
    _finalize_sheet_layouts,
    _normalize_report_sheet_names,
    _report_sheet_display_name,
)

# distribution 차트 데이터 변환 헬퍼는 외부(테스트/프로파일) 호환을 위해 재노출
from ._xlsx_distribution_chart import sort_alldata, sort_data_to_percent  # noqa: F401


# ── write ────────────────────────────────────────────────────────────────────

def write(result, out_path, sheets=None, colors=None, progress_cb=None,
          raw_sheets=None, dist_progress_cb=None, attach_progress_cb=None,
          profile_cb=None) -> str:
    """AnalysisResult 를 xlsx 로 저장. 반환: 저장 경로(str).

    sheets: 출력할 시트명 리스트/집합 (None 이면 전체). 알 수 없는 이름은 무시.
    colors: distribution Legend(소스)별 '#RRGGBB' 색 리스트 (None 이면 Excel 기본색).
    progress_cb: 시트 1개 생성 후 progress_cb(done, total, name) 호출 (선택).
    raw_sheets: [(sheet명, df_honey 포맷 DataFrame), ...]. 주어지면 source(input
        file)별로 df_honey 적재 포맷 그대로의 시트를 맨 앞에 추가한다.

    단일 xlwings(Excel COM) 세션에서 모든 시트(raw/table/diff/distribution)를 생성·
    스타일링·저장한다. Excel/xlwings 가 없으면 전체 실패한다(openpyxl fallback 없음).
    """
    _CURRENT_PROFILE_CB.set(profile_cb)
    out_path = str(Path(out_path).resolve())
    sel = [s for s in ALL_SHEETS if (sheets is None or s in set(sheets))]
    if not sel:
        sel = ["summary"]

    table_writers = {
        "summary": _fill_summary,
        "yield": _fill_yield,
        "cpk": _fill_cpk,
        "fail_item": _fill_fail_item,
        "issue_table": _fill_issue_table,
    }
    want_dist = "distribution" in sel and bool(result.distributions)
    want_hist = "histogram" in sel and bool(result.distributions)

    # diff compare: a_only / b_only 의 CPK·Distribution 추가 시트 스펙 준비
    dc = getattr(result, "diff_classification", None)
    diff_cpk_specs = []   # [(시트명, cpk_rows)]
    diff_dist_specs = []  # [(시트명, dists, sources)]
    if dc:
        name_a, name_b = dc["name_a"], dc["name_b"]
        if "cpk" in sel:
            if result.cpk_rows_a_only:
                diff_cpk_specs.append((f"CPK_{name_a}", result.cpk_rows_a_only))
            if result.cpk_rows_b_only:
                diff_cpk_specs.append((f"CPK_{name_b}", result.cpk_rows_b_only))
        if "distribution" in sel:
            if result.distributions_a_only:
                diff_dist_specs.append((f"Distribution_{name_a}",
                                        result.distributions_a_only, [name_a],
                                        result.dist_source_data_a_only))
            if result.distributions_b_only:
                diff_dist_specs.append((f"Distribution_{name_b}",
                                        result.distributions_b_only, [name_b],
                                        result.dist_source_data_b_only))
    want_dist_phase = want_dist or bool(diff_dist_specs)

    table_sel = [nm for nm in ALL_SHEETS if nm in table_writers and nm in sel]
    total = len(table_sel) + (len(raw_sheets) if raw_sheets else 0) \
        + (1 if want_dist else 0) + (1 if want_hist else 0) \
        + len(diff_cpk_specs) + len(diff_dist_specs)
    done = 0
    tmpdirs = []

    with xw.App(visible=False, add_book=False) as app:
        app.display_alerts = False
        app.screen_updating = False
        try:
            app.api.Calculation = _XL_CALC_MANUAL
            app.api.EnableEvents = False
        except Exception:
            pass

        with _flow_prof("workbook_init"):
            wb = app.books.add()
            base = wb.sheets[0]   # 기본 빈 시트 — 저장 전 삭제
            for nm in table_sel:
                wb.sheets.add(_report_sheet_display_name(nm),
                              after=wb.sheets[wb.sheets.count - 1])

        # raw 를 맨 앞에 삽입하기 위한 앵커(첫 table 시트, 없으면 base)
        anchor = wb.sheets[1] if wb.sheets.count > 1 else base

        # table 시트 채움 (템플릿 순서 유지)
        for nm in table_sel:
            sheet_name = _report_sheet_display_name(nm)
            ws = wb.sheets[sheet_name]
            if nm == "issue_table":
                with _flow_prof(f"fill_{nm}"):
                    _fill_issue_table(ws, result, include_cpk=("cpk" in sel))
            else:
                with _flow_prof(f"fill_{nm}"):
                    table_writers[nm](ws, result)
            done += 1
            _progress(progress_cb, done, total, nm)

        # Compare Mode: goodlog 시트를 summary 와 yield 사이에 삽입 (차이가 있을 때만)
        if getattr(result, "goodlog_rows", None):
            _insert_goodlog_sheet(wb, result.goodlog_rows)

        # diff compare: a_only / b_only CPK 시트 (끝에 추가)
        for disp, cpk_rows in diff_cpk_specs:
            title = _unique_sheet_name(wb, disp)
            ws = wb.sheets.add(title, after=wb.sheets[wb.sheets.count - 1])
            with _flow_prof(f"fill_{title}"):
                _fill_cpk_rows(ws, cpk_rows)
            done += 1
            _progress(progress_cb, done, total, title)

        # Raw Data — source별 df_honey 포맷 시트를 앵커 앞(맨 앞)에 순서대로 추가
        raw_titles = []
        if raw_sheets:
            reserved = [_report_sheet_display_name("distribution")] if want_dist else []
            for name, df in raw_sheets:
                title = _unique_sheet_name(wb, name, reserved)
                with _flow_prof(f"fill_raw_data[{title}]"):
                    _copy_df_via_csv(app, wb, df, title, anchor)
                raw_titles.append(title)
                done += 1
                _progress(progress_cb, done, total, title)

        # 이름 정규화 + 제목 배너/중앙정렬 (distribution 시트 생성 전 — 기존 phase1 순서)
        with _flow_prof("normalize_sheet_names"):
            _normalize_report_sheet_names(wb)
        with _flow_prof("finalize_layouts"):
            _finalize_sheet_layouts(wb, skip_title_titles={t.lower() for t in raw_titles})

        # distribution 차트 + PNG 부착 (같은 세션, 앱/워크북 재오픈 없음)
        if want_dist_phase:
            try:
                with _flow_prof("distribution_xlwings_phase"):
                    tmpdirs.extend(_write_distribution_phase(
                        app, wb, result, colors,
                        attach_fail_item=("fail_item" in sel),
                        attach_issue_cpk=("cpk" in sel),
                        dist_progress_cb=dist_progress_cb,
                        attach_progress_cb=attach_progress_cb,
                        write_main=want_dist, extra_dist=diff_dist_specs))
                done += 1 + len(diff_dist_specs)
                _progress(progress_cb, done, total, "distribution")
            except Exception as exc:
                print(f"[xlsx_writer] distribution 차트 생략: {exc}")

        # histogram 차트 (distribution 뒤, 같은 세션). distribution 과 독립.
        if want_hist:
            try:
                with _flow_prof("histogram_xlwings_phase"):
                    tmpdirs.extend(_write_histogram_phase(
                        wb, result, colors, dist_progress_cb=dist_progress_cb))
                done += 1
                _progress(progress_cb, done, total, "histogram")
            except Exception as exc:
                print(f"[xlsx_writer] histogram 차트 생략: {exc}")

        # 모든 시트 Zoom/눈금선 (단일 세션 1회, distribution 포함)
        with _flow_prof("zoom_gridlines"):
            _apply_zoom_gridlines(app, wb, raw_titles)

        if wb.sheets.count > 1:
            base.delete()
        try:
            app.api.Calculation = _XL_CALC_AUTO
        except Exception:
            pass
        with _flow_prof("workbook_save"):
            wb.save(out_path)
        wb.close()

    # 저장 완료 후 파일 안정화 대기 + 임베드 이미지 무결성 검증
    _wait_for_xlsx_ready(out_path)
    try:
        _validate_embedded_images(out_path)
    finally:
        for td in tmpdirs:
            shutil.rmtree(td, ignore_errors=True)
        _prof_report()

    return out_path


def _copy_df_via_csv(app, wb, df, sheet_name, before_sheet):
    """df 를 임시 CSV 로 쓴 뒤 Excel 네이티브 파싱으로 열어 wb 의 before_sheet 앞으로 복사.

    셀 단위 기입 없이 시트를 통째로 가져온다(raw data 대량 기입 가속). 숫자 변환은
    Excel CSV 파싱이 담당(_coerce_number 대체). Raw Data 는 성능을 위해 별도 서식을
    적용하지 않는다.
    """
    # 헤더는 df.columns 로만 존재(row0=Units 불변)하므로 Serial 은 컬럼명으로 탐지
    serial_cols = [c for c in df.columns if str(c).strip() == "Serial"]
    if serial_cols:
        df = df.drop(columns=serial_cols)
    tmpdir = tempfile.mkdtemp(prefix="honey_raw_")
    csv_path = os.path.join(tmpdir, "df_temp.csv")
    df.to_csv(csv_path, index=False)   # row1=컬럼명, row2~=값 (기존 레이아웃 동일)
    before_names = {s.name for s in wb.sheets}
    wb_csv = app.books.open(csv_path)
    try:
        wb_csv.sheets[0].api.Copy(Before=before_sheet.api)
    finally:
        wb_csv.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
    new = [s for s in wb.sheets if s.name not in before_names]
    copied = new[0] if new else wb.sheets.active
    copied.name = sheet_name
    return copied


def _insert_goodlog_sheet(wb, goodlog_rows):
    """goodlog 시트를 summary 와 yield 사이에 생성·채움 (Compare Mode 전용).

    summary 시트가 있으면 그 뒤, 없으면 yield 시트 앞, 둘 다 없으면 맨 앞에 둔다.
    """
    from ._xlsx_goodlog import write_goodlog_sheet

    title = _unique_sheet_name(wb, "goodlog")
    summary_disp = _report_sheet_display_name("summary")
    yield_disp = _report_sheet_display_name("yield")
    existing = {s.name: s for s in wb.sheets}
    if summary_disp in existing:
        ws = wb.sheets.add(title, after=existing[summary_disp])
    elif yield_disp in existing:
        ws = wb.sheets.add(title, before=existing[yield_disp])
    else:
        ws = wb.sheets.add(title, before=wb.sheets[0])
    write_goodlog_sheet(ws, goodlog_rows)


def _progress(cb, done, total, name):
    if cb is None:
        return
    try:
        cb(done, total, name)
    except Exception:
        pass
