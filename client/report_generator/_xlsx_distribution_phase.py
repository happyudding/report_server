"""distribution phase 오케스트레이션 — 차트 시트 생성 + fail_item/issue_table PNG 부착.

이미 열린 app/wb 에 distribution 메인 시트(+diff a_only/b_only 시트)를 그리고, 생성된
COM 차트를 PNG 로 export 해 fail_item / issue_table 시트의 Distribution 칸에 썸네일로
부착한다(차트 원본 재생성 없이 재활용). 모든 시트 Zoom/눈금선 일괄 적용도 여기.
"""
from __future__ import annotations

import os
import tempfile

from ._xlsx_distribution_chart import _write_distribution
from ._xlsx_png_export import _attach_chart_picture, _notify_attach_progress
from ._xlsx_profile import (
    _CURRENT_DIST_STATS,
    _PNG_ATTACH_MODE,
    _PNG_SUBJECT_CACHE,
    _dist_emit_summary,
    _new_dist_stats,
    _prof,
    _prof_count,
    _profile_info_time,
)
from ._xlsx_sheets import _cpk_fail_subjects, _excel_safe_sheet_name
from ._xlsx_style import (
    _FAIL_ITEM_ROW_HEIGHT,
    _HEADER_ROW,
    _ISSUE_TABLE_ROW_HEIGHT,
    _START_COL,
)
from ._xlsx_table_helpers import _report_sheet_display_name


def _write_distribution_phase(app, wb, result, colors=None, attach_fail_item=False,
                              attach_issue_cpk=True,
                              dist_progress_cb=None, attach_progress_cb=None,
                              write_main=True, extra_dist=None):
    """이미 열린 app/wb 에 distribution 시트 + 차트 + PNG 썸네일을 추가한다.

    attach_fail_item=True 면 distribution 차트를 PNG 로 export 해 fail_item 시트에
    불량율 높은 순으로 1/3 크기 썸네일로 부착한다 (차트 원본 재생성 없이 재활용).

    write_main=False 면 공통 distribution 메인 시트는 건너뛴다 (common 분포가 없는
    diff 케이스). extra_dist=[(시트명, dists, sources), ...] 는 diff compare 의
    a_only/b_only distribution 시트를 추가로 그린다 (fail_item/issue 부착 없음).

    반환: 정리할 임시 PNG 디렉토리 리스트(호출자가 save 후 rmtree).
    """
    token = _CURRENT_DIST_STATS.set(_new_dist_stats())
    tmpdirs = []
    last_sheet = None
    png_cache = {} if (_PNG_SUBJECT_CACHE and _PNG_ATTACH_MODE != "copy_picture") else None
    try:
        if write_main:
            with _prof("clear"):
                names = [s.name for s in wb.sheets]
                dist_name = next((n for n in names if n.lower() == "distribution"), None)
                if dist_name:
                    sh = wb.sheets[dist_name]
                    for c in list(sh.charts):     # 템플릿/이전 차트 제거
                        try:
                            c.delete()
                        except Exception:
                            pass
                    sh.clear()
                else:
                    sh = wb.sheets.add(_report_sheet_display_name("distribution"),
                                       after=wb.sheets[len(wb.sheets) - 1])
            chart_map = _write_distribution(
                wb, sh, result, colors, dist_progress_cb=dist_progress_cb,
                profile_label="main")
            last_sheet = sh
            if attach_fail_item and chart_map:
                with _profile_info_time("distribution.attach_fail_item"):
                    tmpdir = _attach_fail_item_charts(
                        wb, result, chart_map, attach_progress_cb=attach_progress_cb,
                        png_cache=png_cache
                    )
                if tmpdir:
                    tmpdirs.append(tmpdir)
            if chart_map:
                with _profile_info_time("distribution.attach_issue_table"):
                    tmpdir = _attach_issue_table_charts(
                        wb, result, chart_map, include_cpk=attach_issue_cpk,
                        attach_progress_cb=attach_progress_cb, png_cache=png_cache
                    )
                if tmpdir:
                    tmpdirs.append(tmpdir)

        # diff compare: a_only / b_only distribution 시트 추가
        for raw_title, dists, sources, sdata in (extra_dist or []):
            if not dists:
                continue
            existing = {s.name.lower() for s in wb.sheets}
            sheet_title = _excel_safe_sheet_name(raw_title, existing)
            if sheet_title.lower() in existing:
                d_sh = wb.sheets[sheet_title]
                for c in list(d_sh.charts):
                    try:
                        c.delete()
                    except Exception:
                        pass
                d_sh.clear()
            else:
                d_sh = wb.sheets.add(sheet_title, after=wb.sheets[len(wb.sheets) - 1])
            _write_distribution(
                wb, d_sh, result, colors, dists=dists, sources=sources,
                source_data=sdata, title=sheet_title, profile_label=f"diff:{sheet_title}")
            last_sheet = d_sh

        if last_sheet is not None:
            try:
                last_sheet.activate()
            except Exception:
                pass
        return tmpdirs
    finally:
        _dist_emit_summary()
        _CURRENT_DIST_STATS.reset(token)


def _apply_zoom_gridlines(app, wb, raw_gridline_sheets=None):
    """모든 시트 Zoom(fail_item/issue_table/distribution=80, 그 외 100) + 눈금선 숨김.
    raw 시트만 눈금선 표시 (단일 세션 1회 적용)."""
    zoom80 = {"fail_item", "issue_table", "distribution", "histogram"}
    raw_names = {str(n).lower() for n in (raw_gridline_sheets or [])}
    for s in wb.sheets:
        try:
            s.activate()
            app.api.ActiveWindow.DisplayGridlines = s.name.lower() in raw_names
            nm_key = s.name.lower().replace(" ", "_")
            app.api.ActiveWindow.Zoom = 80 if any(z in nm_key for z in zoom80) else 100
        except Exception:
            pass


def _attach_fail_item_charts(wb, result, chart_map, attach_progress_cb=None, png_cache=None):
    """fail_item 시트의 Distribution 열(각 bin 행)에 fail item 차트 PNG 삽입.

    한 bin 에 fail item 이 여럿일 수 있으므로, 해당 행 fail_subjects 전체를 불량율
    (portion %) 내림차순으로 Distribution 칸에서 오른쪽으로 나열한다 (fail_subjects 는
    이미 정렬됨). Distribution 은 마지막 열이라 우측 빈 공간으로 확장된다.
    """
    names = [s.name for s in wb.sheets]
    fi_name = next((n for n in names if n.lower() == "fail_item"), None)
    if fi_name is None:
        return None
    fi = wb.sheets[fi_name]

    # Distribution 열 = B(2) + Step + Bin + Item + (count+yield) × sources
    dist_col = _START_COL + 3 + 2 * len(result.sources)

    tmpdir = tempfile.mkdtemp(prefix="honey_fi_")
    seq = 0
    total = sum(
        1
        for r in result.fail_item_rows
        for fs in (r.get("fail_subjects") or [])
        if fs.get("subject") in chart_map
    )
    done = 0
    _notify_attach_progress(attach_progress_cb, "start", "fail_item", "", done, total)

    for i, r in enumerate(result.fail_item_rows):
        fail_subjects = r.get("fail_subjects") or []
        if not fail_subjects:
            continue
        row_excel = _HEADER_ROW + 1 + i
        try:
            cell = fi.range((row_excel, dist_col))
            base_left = cell.left
            top = cell.top
            w = cell.width
            h = cell.height
        except Exception:
            base_left, top = 700.0, 60.0 + i * _FAIL_ITEM_ROW_HEIGHT
            w, h = 200.0, float(_FAIL_ITEM_ROW_HEIGHT)
        k = 0   # 실제 부착된 차트 수 — 가로 위치 인덱스
        for fs in fail_subjects:
            subj = fs.get("subject")
            if not subj or subj not in chart_map:
                continue
            ch = chart_map[subj]   # COM Chart (Pass2 가 COM Chart 를 저장)
            left = base_left + k * w
            png = os.path.join(tmpdir, f"fi_{seq}.png")
            seq += 1
            if _attach_chart_picture(fi, ch, png, f"fi_chart_{seq}", left, top, w, h,
                                     "fail_item", subj, attach_progress_cb,
                                     png_cache=png_cache):
                _prof_count("pngs")
            done += 1
            _notify_attach_progress(
                attach_progress_cb, "progress", "fail_item", subj, done, total)
            k += 1

    _notify_attach_progress(attach_progress_cb, "done", "fail_item", "", done, total)
    return tmpdir if seq > 0 else None


def _attach_issue_table_charts(wb, result, chart_map, include_cpk=True,
                               attach_progress_cb=None, png_cache=None):
    """issue_table 시트의 Distribution 열(각 데이터 행)에 해당 subject 차트 PNG 삽입.

    fail_item 과 동일한 COM Export 방식. dist_col 계산만 issue_table header 기준으로 다름.
    header: ["Category","Step","Bin","TNO","Item","avg", {src}_yield×N, "Distribution", ...]
    → dist_col = _START_COL + 6 + len(sources)
    """
    names = [s.name for s in wb.sheets]
    it_name = next((n for n in names if n.lower() == "issue_table"), None)
    if it_name is None:
        return None
    it = wb.sheets[it_name]

    dist_col = _START_COL + 6 + len(result.sources)

    tmpdir = tempfile.mkdtemp(prefix="honey_it_")
    seq = 0
    cpk_subjects = _cpk_fail_subjects(result) if include_cpk else []
    total = sum(
        1
        for r in result.issue_yield_rows
        if r.get("item") in chart_map
    ) + sum(1 for subj, _cpk_val in cpk_subjects if subj in chart_map)
    done = 0
    _notify_attach_progress(attach_progress_cb, "start", "issue_table", "", done, total)

    for i, r in enumerate(result.issue_yield_rows):
        subj = r.get("item")
        if not subj or subj not in chart_map:
            continue
        ch = chart_map[subj]
        row_excel = _HEADER_ROW + 1 + i
        try:
            cell = it.range((row_excel, dist_col))
            left = cell.left
            top = cell.top
            w = cell.width
            h = cell.height
        except Exception:
            left, top = 700.0, 60.0 + i * _ISSUE_TABLE_ROW_HEIGHT
            w, h = 200.0, float(_ISSUE_TABLE_ROW_HEIGHT)
        png = os.path.join(tmpdir, f"it_{seq}.png")
        seq += 1
        if _attach_chart_picture(it, ch, png, f"it_chart_{seq}", left, top, w, h,
                                 "issue_table", subj, attach_progress_cb,
                                 png_cache=png_cache):
            _prof_count("pngs")
        done += 1
        _notify_attach_progress(
            attach_progress_cb, "progress", "issue_table", subj, done, total)

    if not include_cpk:
        _notify_attach_progress(attach_progress_cb, "done", "issue_table", "", done, total)
        return tmpdir if seq > 0 else None

    # CPK < 1.33 행 distribution 차트 부착 (+1: CPK 카테고리 서브헤더 행 보정)
    n_yield = len(result.issue_yield_rows)
    for j, (subj, _cpk_val) in enumerate(cpk_subjects):
        if subj not in chart_map:
            continue
        ch = chart_map[subj]
        row_excel = _HEADER_ROW + 1 + n_yield + 1 + j
        try:
            cell = it.range((row_excel, dist_col))
            left = cell.left
            top = cell.top
            w = cell.width
            h = cell.height
        except Exception:
            left = 700.0
            top = 60.0 + (n_yield + 1 + j) * _ISSUE_TABLE_ROW_HEIGHT
            w, h = 200.0, float(_ISSUE_TABLE_ROW_HEIGHT)
        png = os.path.join(tmpdir, f"it_{seq}.png")
        seq += 1
        if _attach_chart_picture(it, ch, png, f"it_chart_{seq}", left, top, w, h,
                                 "issue_table", subj, attach_progress_cb,
                                 png_cache=png_cache):
            _prof_count("pngs")
        done += 1
        _notify_attach_progress(
            attach_progress_cb, "progress", "issue_table", subj, done, total)

    _notify_attach_progress(attach_progress_cb, "done", "issue_table", "", done, total)
    return tmpdir if seq > 0 else None
