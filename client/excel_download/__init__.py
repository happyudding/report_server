"""Excel Download — 열려 있는 web_report 세션을 xlsx 로 저장 (클라이언트 연산).

서버는 기존 엔드포인트로 데이터만 내려주고(무수정), 시트 구성·차트 PNG 렌더·Excel
기입은 전부 클라이언트가 한다. 진입점은 run_excel_download() 하나 — honey_main 은
worker.ExcelDownloadWorker(QThread) 로 감싸 호출한다.

시트: Summary / Yield / CPK / Issue Table / Distribution / Histogram / Map Analysis
(+ Temperature 세션은 Issue Table 뒤에 "Issue Table Temp" — CT/HT 를 RT Limit 으로 전 항목
재판정한 이슈 표. Map 썸네일은 bin 이 아니라 **항목별 fail die** 강조다).
Distribution·Histogram 은 전체 항목(다운샘플링 금지 — 불변규칙 6)을 4열 그리드
청크 PNG 로 렌더해 세로로 이어 붙인다. 렌더는 ProcessPoolExecutor 병렬(실행시간
30초 목표), Excel 텍스트 시트 기입은 렌더와 동시에 진행한다.

Map Analysis 좌표 강조는 서버가 모르는 **브라우저 메모리 상태**(map_select.js
mapSelChips)라, honey_main 이 runJavaScript 로 읽어 ``chips`` 로 넘겨준다 — Map 시트
맵에는 색 원 마커, Distribution 시트 CDF 에는 (값, 누적%) 점으로 화면과 같게 그린다.
Histogram·Issue Table 미니셀은 웹에도 강조가 없어 chips 를 무시한다.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np

import chart_colors

from ._charts import (
    DIST_PALETTE,
    NCOLS,
    ROWS_PER_CHUNK,
    chunk_px_size,
    issue_cdf_pt_size,
    issue_map_pt_size,
    render_chunk_pair,
    render_issue_maps_job,
    render_map_png_job,
    render_single_cdf,
    render_temp_maps_job,
)
from ._fetch import fetch_distribution_bin1, fetch_report_data, fetch_temp_map
from ._map import build_bin_desc_map, build_global_bin_legend
from . import _extra
from . import _sheets

_CHUNK_CELLS = NCOLS * ROWS_PER_CHUNK

SHEET_ORDER = ["Summary", "Yield", "CPK", "Issue Table",
               "Distribution", "Histogram", "Map Analysis"]
# Temperature 세션에만 추가되는 시트 — CT/HT 를 RT Limit 으로 전 항목 재판정한 이슈 표
# (웹 "Issue Table Temp" 탭과 같은 시트). Issue Table 바로 뒤에 끼운다.
TEMP_SHEET = "Issue Table Temp"
# XlsxWriter 엔진에서만 만드는 web_report 파리티 시트
COMPARE_SHEET = "Compare"

# 기본 기입 엔진. 사용자 선택은 없앴고(2026-08-18) XlsxWriter 로만 만든다 —
# 이것이 실패할 때만 COM 경로로 자동 재시도한다(파일은 항상 생성).
DEFAULT_ENGINE = "xlsxwriter"

# 시간 예산(초) — 3분 SLA. 넘으면 남은 이미지를 건너뛰고(경고로 보고) 저장으로 간다.
BUDGET_SKIP_IMAGES_SEC = 150.0
BUDGET_FORCE_SAVE_SEC = 165.0


def _sheet_order(sheets, *, engine=DEFAULT_ENGINE, has_compare=False):
    """이 세션에 실제로 만들 시트 순서.

    Temp 행이 있을 때만 TEMP_SHEET 를 끼우고, Compare 는 XlsxWriter 엔진에서만 만든다
    (COM 경로는 동결이라 종전 시트 구성 그대로).
    """
    order = list(SHEET_ORDER)
    if (sheets or {}).get(TEMP_SHEET):
        order.insert(order.index("Issue Table") + 1, TEMP_SHEET)
    if engine != "xlsxwriter":
        return order
    if has_compare:
        anchor = TEMP_SHEET if TEMP_SHEET in order else "Issue Table"
        order.insert(order.index(anchor) + 1, COMPARE_SHEET)
    return order


class _Progress:
    """상세 진행 표시 — 단계별 가중치로 전체 %를 만든다(하단 진행바가 실제로 움직이게).

    status_cb(state, message) 는 종전 시그니처 그대로 두고, 선택적 progress_cb(percent,
    message) 를 하나 더 받는다(있는 쪽만 갱신).
    """

    WEIGHTS = (("download", 15), ("render", 40), ("attach", 30), ("save", 15))

    def __init__(self, status_cb=None, progress_cb=None):
        self.status_cb = status_cb
        self.progress_cb = progress_cb
        self.t0 = time.perf_counter()
        self._base = {}
        acc = 0
        for name, weight in self.WEIGHTS:
            self._base[name] = (acc, weight)
            acc += weight

    def elapsed(self):
        return time.perf_counter() - self.t0

    def _fmt(self, message):
        secs = int(self.elapsed())
        return f"[{secs // 60}:{secs % 60:02d}] {message}"

    def __call__(self, phase, message, done=None, total=None):
        text = message if not total else f"{message} ({done}/{total})"
        base, weight = self._base.get(phase, (0, 0))
        frac = (done / total) if (total and done is not None) else 0.0
        percent = int(base + weight * min(1.0, max(0.0, frac)))
        if self.status_cb:
            try:
                self.status_cb(phase, self._fmt(text))
            except Exception:
                pass
        if self.progress_cb:
            try:
                self.progress_cb(percent, self._fmt(text))
            except Exception:
                pass


def run_excel_download(session_id, server_base, out_path, status_cb=None,
                       bin1=False, chips=None, engine=None, progress_cb=None) -> dict:
    """세션 web_report 를 out_path(xlsx)로 저장.

    반환 {"out_path", "elapsed", "items", "engine", "warnings"}.

    ``engine``: 기본은 DEFAULT_ENGINE("xlsxwriter") — 호출부(Honey)는 지정하지 않는다.
    XlsxWriter 경로가 실패하면 **이미 받은 데이터와 렌더된 PNG 를 그대로 재사용해**
    COM 경로로 자동 재시도한다 — 어떤 경우에도 파일은 만들어진다(테스트만 "com" 지정).

    status_cb(state, message): 진행 통지 (state ∈ download/render/attach/save/done).
    progress_cb(percent, message): 선택 — 하단 진행바용 0~100 백분율.
    COM 경로를 쓸 때는 호출 스레드에서 CoInitialize 가 되어 있어야 한다 (worker.py 참조).

    ``bin1`` 이면 Distribution(CDF)·Histogram 시트를 양품(BIN==1) & 규격(LSL/USL) 이내
    die 만의 산포로 그린다(그 외 시트는 전체 die 기준 그대로).

    ``chips`` 는 브라우저 Map Analysis 에서 선택한 좌표 스냅샷(map_select.js
    honeyMapSelSnapshot). 주면 Map Analysis 시트 맵에 색 원 마커, Distribution 시트
    CDF 에 그 좌표의 (값, 누적%) 점을 화면과 같은 색으로 그린다.
    """
    engine = (engine or DEFAULT_ENGINE).lower()
    emit = _Progress(status_cb, progress_cb)

    # ── 1. 서버 데이터 수신 ──────────────────────────────────────────────────
    emit("download", "리포트 데이터 다운로드 중…")
    full, dist = fetch_report_data(server_base, session_id, bin1=bin1,
                                   status_cb=lambda m: emit("download", m))
    report = full["web_report"]
    session_url = f"{str(server_base).rstrip('/')}/pe/report/view/{session_id}"
    sheets = report.get("sheets") or {}
    source_names = [s.get("name") for s in (report.get("sources") or [])]
    colors = _source_colors(source_names, report.get("dist_colors"))
    chart_notes = full.get("chart_notes") or {}
    emit("download", f"데이터 수신 완료 ({emit.elapsed():.1f}s)", 1, 1)

    # ── 2. 차트 렌더 잡 구성 + 프로세스풀 시작 ──────────────────────────────
    tmpdir = tempfile.mkdtemp(prefix="honey_exceldl_")
    try:
        chunk_jobs, n_items, cell_of = _build_chunk_jobs(
            report, dist, dict(colors), tmpdir, chips, chart_notes)
        map_rows = sheets.get("Map Analysis") or []
        map_jobs, map_colors = _build_map_jobs(map_rows, tmpdir, chips)
        emit("render", f"차트 {n_items}항목 렌더 시작 "
                       f"({len(chunk_jobs)}청크 + 웨이퍼맵 {len(map_jobs)})")

        n_workers = max(1, min(16, os.cpu_count() or 4, len(chunk_jobs) + len(map_jobs)))
        pool = ProcessPoolExecutor(max_workers=n_workers)
        try:
            ctx = {
                "report": report, "sheets": sheets, "source_names": source_names,
                "colors": colors, "session_url": session_url, "map_rows": map_rows,
                "map_colors": map_colors, "cell_of": cell_of, "tmpdir": tmpdir,
                "server_base": server_base, "session_id": session_id, "bin1": bin1,
                "chunk_jobs": chunk_jobs, "map_jobs": map_jobs, "n_items": n_items,
                "pool": pool, "emit": emit,
                "chunk_futs": [pool.submit(render_chunk_pair, j) for j in chunk_jobs],
                "map_futs": [pool.submit(render_map_png_job, j) for j in map_jobs],
            }

            warnings = []
            used_engine = engine
            if engine == "xlsxwriter":
                try:
                    _write_with_xlsxwriter(ctx, out_path, warnings)
                except Exception as exc:
                    # 신규 엔진이 실패해도 사용자는 파일을 받아야 한다 — 이미 받은 데이터와
                    # 렌더된 PNG 를 그대로 재사용해 기존 COM 경로로 다시 만든다.
                    warnings.append(f"신규 엔진(XlsxWriter) 실패 → 기존 방식으로 재시도: {exc}")
                    emit("save", f"신규 엔진 실패 — 기존 방식으로 다시 만드는 중… ({exc})")
                    _write_with_com(ctx, out_path)
                    used_engine = "com"
            else:
                _write_with_com(ctx, out_path)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        # 부분 생성된 파일은 남기지 않는다
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            pass
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    elapsed = emit.elapsed()
    label = "XlsxWriter" if used_engine == "xlsxwriter" else "Excel"
    suffix = f" · 경고 {len(warnings)}건" if warnings else ""
    emit("done", f"Excel Download 완료 ({elapsed:.1f}s, {label}{suffix}): {out_path}", 1, 1)
    return {"out_path": out_path, "elapsed": elapsed, "items": n_items,
            "engine": used_engine, "warnings": warnings}


# ── 기입 단계 (엔진별) ───────────────────────────────────────────────────────

def _write_with_com(ctx, out_path):
    """기존 xlwings/Excel COM 경로 — `_sheets.py`(동결)를 그대로 쓴다. 폴백 겸 옵션."""
    import xlwings as xw
    from excel_edit.excel_session import _quit_app
    from report_generator._xlsx_png_export import (_validate_embedded_images,
                                                   _wait_for_xlsx_ready)
    from report_generator._xlsx_style import _XL_CALC_AUTO, _XL_CALC_MANUAL

    emit = ctx["emit"]
    sheets = ctx["sheets"]
    sheet_order = _sheet_order(sheets, engine="com")
    emit("attach", "Excel 시트 작성 중…")
    app = xw.App(visible=False, add_book=False)
    try:
        app.display_alerts = False
        app.screen_updating = False
        wb = app.books.add()
        app.api.Calculation = _XL_CALC_MANUAL       # 열린 workbook 이 있어야 설정 가능
        book = _ComBook(wb, sheet_order, ctx["session_url"])
        _fill_workbook(book, ctx, sheet_order)
        app.api.Calculation = _XL_CALC_AUTO
        if os.path.exists(out_path):
            os.remove(out_path)
        wb.save(out_path)
        wb.close()
    finally:
        _quit_app(app)
    _wait_for_xlsx_ready(out_path)
    _validate_embedded_images(out_path)


def _write_with_xlsxwriter(ctx, out_path, warnings):
    """XlsxWriter 경로 — Excel 없이 파일을 직접 만든다(기본 엔진)."""
    from . import _xlsx
    from ._charts import DPI

    sheets = ctx["sheets"]
    sheet_order = _sheet_order(sheets, engine="xlsxwriter",
                               has_compare=bool((ctx["report"] or {}).get("compare")))
    book = _xlsx.XlsxBook(out_path, ctx["session_url"], chart_dpi=DPI)
    try:
        book.add_sheets(sheet_order)
        _fill_workbook(book, ctx, sheet_order, warnings=warnings)
        book.close()
    except Exception:
        book.abort()
        raise


class _ComBook:
    """`_sheets.py`(COM) 를 XlsxBook 과 같은 메서드 이름으로 감싼 어댑터.

    두 엔진이 `_fill_workbook` 한 벌을 공유하기 위한 얇은 껍데기다 — `_sheets.py` 자체는
    손대지 않는다(동결). 파리티 보강 시트(Compare)와 색 인자는 COM 경로에 없으므로
    조용히 무시한다.
    """

    def __init__(self, wb, sheet_order, session_url):
        self.session_url = session_url
        self.ws = {}
        first = wb.sheets[0]
        first.name = sheet_order[0]
        self.ws[sheet_order[0]] = first
        for name in sheet_order[1:]:
            self.ws[name] = wb.sheets.add(name, after=wb.sheets[wb.sheets.count - 1])

    def has(self, name):
        return name in self.ws

    def write_sheet_title(self, name, text=None, **_kw):
        _sheets.write_sheet_title(self.ws[name], text or name)

    def add_session_link(self, name):
        _sheets.add_session_link(self.ws[name], self.session_url)

    def write_summary_sheet(self, yield_summary, fail_bin_rows, **_kw):
        _sheets.write_summary_sheet(self.ws["Summary"], yield_summary, fail_bin_rows)

    def write_yield_sheet(self, yield_rows, yield_bin_groups, source_names,
                          step_groups=None, step_summary=None, **_kw):
        _sheets.write_yield_sheet(self.ws["Yield"], yield_rows, yield_bin_groups,
                                  source_names, step_groups=step_groups,
                                  step_summary=step_summary)

    def write_cpk_sheet(self, cpk_rows):
        _sheets.write_cpk_sheet(self.ws["CPK"], cpk_rows)

    def write_issue_sheet(self, name, issue_rows, source_names, *, title=None):
        return _sheets.write_issue_sheet(self.ws[name], issue_rows, source_names,
                                         title=title or name)

    def write_compare_sheet(self, *_a, **_kw):
        pass                                    # COM 경로에는 없는 시트

    def write_sheet_error(self, name, exc):
        try:
            self.ws[name].range((3, 2)).value = f"⚠ 이 시트를 만들지 못했습니다: {exc}"
        except Exception:
            pass

    def write_source_legend(self, name, colors):
        _sheets.write_source_legend(self.ws[name], colors)

    def chart_anchor(self, name):
        return _sheets.chart_anchor(self.ws[name])

    def picture_stack_tops(self, heights_px, top0):
        return _sheets.picture_stack_tops(heights_px, top0)

    def add_picture_at(self, name, path, *, top, width_px, height_px, left):
        _sheets.add_picture_at(self.ws[name], path, top=top, width_px=width_px,
                               height_px=height_px, left=left)

    def add_picture_in_cell(self, name, path, row, col, w_pt, h_pt):
        _sheets.add_picture_in_cell(self.ws[name], path, row, col, w_pt, h_pt)

    def write_hidden_item_index(self, name, entries, tops, *, left, top):
        _sheets.write_hidden_item_index(self.ws[name], entries, tops, left=left, top=top)

    def add_map_grid(self, name, pngs, *, left, top):
        _sheets.add_map_grid(self.ws[name], pngs, left=left, top=top)

    def write_map_legend(self, name, legend_rows, desc_map, color_map, n_maps, *, left):
        _sheets.write_map_legend(self.ws[name], legend_rows, desc_map, color_map, n_maps,
                                 left=left)

    def close(self, *, activate="Summary"):
        if activate in self.ws:
            self.ws[activate].activate()


def _fill_workbook(book, ctx, sheet_order, *, warnings=None):
    """시트 기입 + 차트 부착 — 두 엔진이 공유하는 본체.

    시트마다 예외를 격리한다(`_safe`): 한 시트가 실패해도 그 시트에 사유만 남기고 나머지는
    정상적으로 만든다. 이미지 부착도 장별로 격리해 렌더 실패 1건이 전체를 막지 않는다.
    """
    warnings = warnings if warnings is not None else []
    emit = ctx["emit"]
    report, sheets = ctx["report"], ctx["sheets"]
    source_names, colors = ctx["source_names"], ctx["colors"]
    pool, tmpdir, cell_of = ctx["pool"], ctx["tmpdir"], ctx["cell_of"]
    map_rows, map_colors = ctx["map_rows"], ctx["map_colors"]
    mode = str(report.get("mode") or "Normal")

    def safe(label, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            warnings.append(f"{label}: {exc}")
            try:
                book.write_sheet_error(label, exc)
            except Exception:
                pass
            return None

    # ── 표 시트 ─────────────────────────────────────────────────────────────
    emit("render", "Summary·Yield·CPK 시트 작성 중…")
    safe("Summary", book.write_summary_sheet, report.get("yield_summary"),
         sheets.get("Fail Bin"), issue_rows=sheets.get("Issue Table"),
         temp_rows=sheets.get(TEMP_SHEET), summary_engr=report.get("summary_engr"),
         mode=mode)
    safe("Yield", book.write_yield_sheet, sheets.get("Yield"),
         report.get("yield_bin_groups"), source_names,
         step_groups=report.get("yield_step_groups"),
         step_summary=(report.get("yield_summary") or {}).get("by_step"),
         yield_summary=report.get("yield_summary"), yield_basis=report.get("yield_basis"))
    safe("CPK", book.write_cpk_sheet, sheets.get("CPK"))
    if COMPARE_SHEET in sheet_order:
        safe(COMPARE_SHEET, book.write_compare_sheet, COMPARE_SHEET,
             _extra.build_compare_tables(report.get("compare")))

    emit("render", "Issue Table 작성 중…")
    issue_layout = safe("Issue Table", book.write_issue_sheet, "Issue Table",
                        sheets.get("Issue Table"), source_names)
    issue_layout = issue_layout or _EMPTY_LAYOUT

    # Issue Table 썸네일 잡 — CPK 섹션만 Bin1(양품) ECDF (그 행의 cpk 가 Bin1 기준이라
    # 웹 미니셀 data-bin1 과 같은 데이터를 쓴다). 배치 수신 실패 시 전체 기준으로 폴백.
    cpk_subjects = [item for item, _r, section in issue_layout["rows"]
                    if section == "CPK" and item in cell_of]
    bin1_items = {} if ctx["bin1"] else fetch_distribution_bin1(
        ctx["server_base"], ctx["session_id"], cpk_subjects)
    issue_targets, issue_futs = [], []
    for item, excel_row, section in issue_layout["rows"]:
        cell = cell_of.get(item)
        if cell is None:
            continue
        if section == "CPK" and item in bin1_items:
            cell = _bin1_cell(cell, bin1_items[item])
        out = os.path.join(tmpdir, f"issue_{excel_row:04d}.png")
        issue_futs.append(pool.submit(render_single_cdf, {"cell": cell, "out_path": out}))
        issue_targets.append((excel_row, out))
    issue_map_jobs, issue_map_paths = _build_issue_map_jobs(
        map_rows, issue_layout["map_rows"], map_colors, tmpdir)
    issue_map_futs = [pool.submit(render_issue_maps_job, j) for j in issue_map_jobs]

    # ── Issue Table Temp (Temperature 세션만) ───────────────────────────────
    temp_layout, temp_targets, temp_futs, temp_map_paths = None, [], [], {}
    if TEMP_SHEET in sheet_order:
        temp_layout = safe(TEMP_SHEET, book.write_issue_sheet, TEMP_SHEET,
                           sheets.get(TEMP_SHEET), source_names, title=TEMP_SHEET)
        if temp_layout:
            for item, excel_row, _section in temp_layout["rows"]:
                cell = cell_of.get(item)
                if cell is None:
                    continue
                out = os.path.join(tmpdir, f"temp_{excel_row:04d}.png")
                temp_targets.append((excel_row, out))
                temp_futs.append(pool.submit(render_single_cdf,
                                             {"cell": cell, "out_path": out}))
            temp_map = fetch_temp_map(ctx["server_base"], ctx["session_id"])
            temp_jobs, temp_map_paths = _build_temp_map_jobs(
                map_rows, temp_layout["temp_rows"], temp_map, tmpdir)
            temp_futs.extend(pool.submit(render_temp_maps_job, j) for j in temp_jobs)

    # ── 차트 시트 ───────────────────────────────────────────────────────────
    for name in ("Distribution", "Histogram", "Map Analysis"):
        safe(name, book.write_sheet_title, name)
    safe("Distribution", book.write_source_legend, "Distribution", colors)
    safe("Histogram", book.write_source_legend, "Histogram", colors)

    chunk_jobs, chunk_futs = ctx["chunk_jobs"], ctx["chunk_futs"]
    dist_left, dist_top = book.chart_anchor("Distribution")
    hist_left, hist_top = book.chart_anchor("Histogram")
    sizes = [chunk_px_size(len(j["cells"])) for j in chunk_jobs]
    dist_tops = book.picture_stack_tops([h for _, h in sizes], dist_top)
    hist_tops = book.picture_stack_tops([h for _, h in sizes], hist_top)
    idx_of = {fut: i for i, fut in enumerate(chunk_futs)}
    done = 0
    skipped = 0
    for fut in as_completed(chunk_futs):
        i = idx_of[fut]
        done += 1
        emit("render", "산포·히스토그램 차트", done, len(chunk_futs))
        if emit.elapsed() > BUDGET_SKIP_IMAGES_SEC:
            skipped += 1
            continue
        try:
            cdf_path, hist_path = fut.result()
            w_px, h_px = sizes[i]
            book.add_picture_at("Distribution", cdf_path, top=dist_tops[i],
                                width_px=w_px, height_px=h_px, left=dist_left)
            book.add_picture_at("Histogram", hist_path, top=hist_tops[i],
                                width_px=w_px, height_px=h_px, left=hist_left)
        except Exception as exc:
            warnings.append(f"차트 청크 {i + 1} 부착 실패: {exc}")
    if skipped:
        warnings.append(f"시간 예산({BUDGET_SKIP_IMAGES_SEC:.0f}초) 초과로 차트 "
                        f"{skipped}청크를 건너뛰었습니다")

    index_entries = _build_item_index(chunk_jobs)
    safe("Distribution", book.write_hidden_item_index, "Distribution", index_entries,
         dist_tops, left=dist_left, top=dist_top)
    safe("Histogram", book.write_hidden_item_index, "Histogram", index_entries,
         hist_tops, left=hist_left, top=hist_top)

    # ── 웨이퍼맵 ────────────────────────────────────────────────────────────
    emit("attach", "웨이퍼 맵 부착 중…")
    map_pngs = []
    for job, fut in zip(ctx["map_jobs"], ctx["map_futs"]):
        try:
            map_pngs.append((job["title"], fut.result()))
        except Exception as exc:
            warnings.append(f"웨이퍼 맵 '{job.get('title')}' 렌더 실패: {exc}")
    map_left, map_top = book.chart_anchor("Map Analysis")
    safe("Map Analysis", book.add_map_grid, "Map Analysis", map_pngs,
         left=map_left, top=map_top)
    safe("Map Analysis", book.write_map_legend, "Map Analysis",
         build_global_bin_legend(map_rows), build_bin_desc_map(sheets.get("Yield")),
         map_colors, len(map_pngs), left=map_left)

    # ── Issue Table 썸네일 ──────────────────────────────────────────────────
    iw_pt, ih_pt = issue_cdf_pt_size()
    mw_pt, mh_pt = issue_map_pt_size()
    _attach_thumbs(book, "Issue Table", issue_targets, issue_futs, issue_layout,
                   iw_pt, ih_pt, emit, warnings, "Issue Table 썸네일")
    for fut in issue_map_futs:
        try:
            fut.result()
        except Exception as exc:
            warnings.append(f"Issue Table Map 렌더 실패: {exc}")
    for bin_value, excel_row in issue_layout["map_rows"]:
        png = issue_map_paths.get(str(bin_value))
        if png and os.path.exists(png):
            try:
                book.add_picture_in_cell("Issue Table", png, excel_row,
                                         issue_layout["map_col"], mw_pt, mh_pt)
            except Exception as exc:
                warnings.append(f"Issue Table Map 부착 실패(행 {excel_row}): {exc}")

    if temp_layout:
        for fut in temp_futs:
            try:
                fut.result()
            except Exception as exc:
                warnings.append(f"Temp 썸네일 렌더 실패: {exc}")
        for excel_row, out in temp_targets:
            if os.path.exists(out):
                try:
                    book.add_picture_in_cell(TEMP_SHEET, out, excel_row,
                                             temp_layout["dist_col"], iw_pt, ih_pt)
                except Exception as exc:
                    warnings.append(f"Temp 산포 부착 실패(행 {excel_row}): {exc}")
        for item, excel_row in temp_layout["temp_rows"]:
            png = temp_map_paths.get(item)
            if png and os.path.exists(png):
                try:
                    book.add_picture_in_cell(TEMP_SHEET, png, excel_row,
                                             temp_layout["map_col"], mw_pt, mh_pt)
                except Exception as exc:
                    warnings.append(f"Temp Map 부착 실패(행 {excel_row}): {exc}")

    # ── 마무리 ──────────────────────────────────────────────────────────────
    for name in sheet_order:
        try:
            book.add_session_link(name)
        except Exception:
            pass
    emit("save", "파일 저장·검증 중…")


_EMPTY_LAYOUT = {"rows": [], "map_rows": [], "temp_rows": [], "dist_col": 8, "map_col": 7}


def _attach_thumbs(book, sheet, targets, futs, layout, w_pt, h_pt, emit, warnings, label):
    """행별 CDF 썸네일 부착 — 장별 격리(1장 실패가 나머지를 막지 않는다)."""
    total = len(targets)
    for i, ((excel_row, out), fut) in enumerate(zip(targets, futs), 1):
        if i % 25 == 0 or i == total:
            emit("attach", label, i, total)
        try:
            fut.result()
            book.add_picture_in_cell(sheet, out, excel_row, layout["dist_col"], w_pt, h_pt)
        except Exception as exc:
            warnings.append(f"{label} 부착 실패(행 {excel_row}): {exc}")


# ── 잡 구성 헬퍼 ──────────────────────────────────────────────────────────────

def _source_colors(source_names, dist_colors):
    """[(source, hex)] — 웹과 동일 규칙: dist_colors[i] 지정 색, 없으면 기본 팔레트.

    10색 초과분 폴백은 모듈로 순환(색 중복) 대신 chart_colors 기본 48색 공식 연장 —
    report_view.html 의 distDefaultColor 와 동일해 웹↔Excel 색이 일치한다.
    """
    defaults = None
    out = []
    for i, name in enumerate(source_names):
        custom = (dist_colors or [])[i] if i < len(dist_colors or []) else None
        if not custom and i >= len(DIST_PALETTE):
            if defaults is None:
                defaults = chart_colors.generate_default_colors(
                    max(len(source_names), chart_colors.N_COLORS))
            out.append((name, defaults[i]))
        else:
            out.append((name, custom or DIST_PALETTE[i]))
    return out


def _ecdf_mean_std(x, y):
    """ECDF(x=오름차순 고유값, y=누적% 0..100) → (가중평균, 모표준편차).

    히스토그램 가우시안 곡선용 — 곡선이 **그 칸에 그린 분포**와 어긋나지 않도록 CPK 시트
    통계(Bin1·규격 클리핑 없음) 대신 여기서 직접 산출한다. 값 분포를 누적%로 온전히 담으므로
    가중평균=실제 평균, 가중분산=모분산. 단일 고유값이면 std=0(→ 축퇴 스파이크).
    데이터 없으면 (None, None).
    """
    x = np.asarray(x, dtype="float64")
    y = np.asarray(y, dtype="float64")
    if x.size == 0:
        return None, None
    w = np.empty_like(y)
    w[0] = y[0] / 100.0
    if y.size > 1:
        w[1:] = np.diff(y) / 100.0
    s = w.sum()
    if s <= 0:
        return None, None
    w = w / s
    mean = float((w * x).sum())
    std = float(np.sqrt((w * (x - mean) ** 2).sum()))
    return mean, std


def _bin1_cell(cell, info):
    """셀의 소스별 ECDF 를 Bin1 배치 응답 값으로 갈아끼운 얕은 복사본.

    미니 CDF 렌더는 (색, x, y) 와 셀의 lo/hi 만 쓰므로 나머지 필드(n/avg/std)는 그대로 둔다.
    Bin1 응답에 없는 소스는 양품 die 가 없다는 뜻이라 빈 배열 — 그 소스만 안 그려진다.
    """
    src_map = info.get("sources") or {}
    sources = []
    for s in cell["sources"]:
        d = src_map.get(s[0]) or {}
        sources.append((s[0], s[1],
                        np.asarray(d.get("x") or [], dtype="float32"),
                        np.asarray(d.get("y") or [], dtype="float32"),
                        s[4], s[5], s[6]))
    return {**cell, "sources": sources}


def _chips_for_subject(chips, subject):
    """선택 좌표 스냅샷 → 이 항목의 [{color, value, cum_pct}] (값 없는 chip 은 제외).

    웹 chipMarkersFor 와 같은 판정(값·누적% 가 모두 숫자인 chip 만 점으로 찍는다).
    """
    out = []
    for c in chips or []:
        it = ((c.get("items") or {}).get(subject)) or {}
        value, cum = it.get("value"), it.get("cum_pct")
        if isinstance(value, (int, float)) and isinstance(cum, (int, float)):
            out.append({"color": c.get("color") or "#e11d48",
                        "value": float(value), "cum_pct": float(cum)})
    return out


def _build_chunk_jobs(report, dist, color_of, tmpdir, chips=None, chart_notes=None):
    """distribution_index 순서(TSEQ)로 전 항목 셀을 만들어 32개씩 청크 잡으로 나눈다.

    dist items 에만 있고 index 에 없는 항목도 뒤에 붙인다 (데이터 누락 금지).
    히스토그램 가우시안 통계(avg/std)는 넘겨받은 ``dist`` 의 ECDF 에서 산출하므로
    전체 die / bin1(?bin1=1) 어느 응답이든 곡선이 그 칸의 분포와 같은 표본을 따른다.
    ``chips`` 를 주면 셀마다 그 항목의 선택 좌표 점을 실어 CDF 에 강조로 그린다
    (Histogram 은 웹에도 강조가 없어 셀의 chips 를 무시한다 — _charts._draw_hist_cell).
    반환: (jobs, n_items, cell_of{subject: cell}).
    """
    index_rows = report.get("distribution_index") or []
    items = (dist.get("items") or {})
    ordered = [r.get("subject") for r in index_rows]
    seen = set(ordered)
    ordered += [k for k in items.keys() if k not in seen]
    meta_of = {r.get("subject"): r for r in index_rows}

    cells = []
    for subject in ordered:
        info = items.get(subject) or {}
        meta = meta_of.get(subject) or {}
        sources = []
        for src_name, data in (info.get("sources") or {}).items():
            xs, ys = data.get("x") or [], data.get("y") or []
            # 가우시안 곡선 통계는 **그리는 ECDF 에서 직접** 산출한다 — CPK 시트 통계는
            # Bin1(규격 클리핑 없음) 기준이라 여기 분포(전체 die 또는 bin1·규격내)와
            # 표본이 다르다. n=None → 다중점은 곡선, 단일점은 std=0 으로 축퇴 스파이크.
            avg, std = _ecdf_mean_std(xs, ys)
            n = None
            # float32: 플롯(수백 px 폭) 정밀도로 충분 — 자식 프로세스 피클 전송량 절반
            sources.append((
                src_name,
                color_of.get(src_name, "#888888"),
                np.asarray(xs, dtype="float32"),
                np.asarray(ys, dtype="float32"),
                n,
                avg, std,
            ))
        cells.append({
            "title": subject,
            "test_num": meta.get("test_num"),
            "units": info.get("units") or meta.get("units"),
            "lo": info.get("lo", meta.get("lower_limit")),
            "hi": info.get("hi", meta.get("upper_limit")),
            "status": meta.get("status"),
            "cpk": meta.get("cpk"),
            "sources": sources,
            # Issue Table 미니셀도 이 cell 을 재사용하지만 미니 렌더는 chips 를 그리지
            # 않는다(웹 미니셀에도 강조가 없다) — 시트별 차이는 렌더 쪽에서 갈린다.
            "chips": _chips_for_subject(chips, subject),
            # 사용자가 웹 차트 위에 남긴 주석(도형·텍스트·코멘트). 대부분 None 이라
            # 자식 프로세스로 넘어가는 피클 크기에 사실상 영향이 없다.
            "note_cdf": (chart_notes or {}).get(f"cdf:{subject}"),
            "note_hist": (chart_notes or {}).get(f"hist:{subject}"),
        })

    jobs = []
    for ci in range(0, len(cells), _CHUNK_CELLS):
        idx = ci // _CHUNK_CELLS
        jobs.append({
            "cells": cells[ci:ci + _CHUNK_CELLS],
            "cdf_path": os.path.join(tmpdir, f"cdf_{idx:03d}.png"),
            "hist_path": os.path.join(tmpdir, f"hist_{idx:03d}.png"),
        })
    return jobs, len(cells), {c["title"]: c for c in cells}


def _build_item_index(chunk_jobs):
    """[(chunk_idx, cell_idx, subject)] — 숨김 항목 인덱스(Ctrl+F)용 셀 좌표 원장.

    차트 제목은 PNG 안에서 46자로 잘리지만 여기 텍스트는 전체 항목명이라 검색이 온전하다.
    """
    return [(ci, idx, cell["title"])
            for ci, job in enumerate(chunk_jobs)
            for idx, cell in enumerate(job["cells"])]


def _build_map_jobs(map_rows, tmpdir, chips=None):
    """Map Analysis 행 → 웹-파리티 wafer map 렌더 잡. 제목: source (step, yield %).

    좌표(dies)가 없는 행은 건너뛴다. bin→색은 전 맵 합산 기준 전역 색맵(웹과 동일 색)
    을 한 번만 만들어 모든 맵이 공유한다. 선택 좌표는 **source 만 대조**해 넘긴다 —
    웹 renderThumbMarkers 도 step 은 보지 않아 같은 source 의 step 맵마다 마커가 찍힌다.
    반환: (jobs, color_map).
    """
    from ._map import build_bin_color_map

    color_map, _order = build_bin_color_map(map_rows)
    jobs = []
    for i, row in enumerate(map_rows):
        dies = row.get("dies") or []
        if not dies:
            continue
        pass_pct = next((b.get("pct") for b in (row.get("bin_counts") or [])
                         if b.get("is_pass")), None)
        parts = [str(row.get("source") or f"map_{i}")]
        if row.get("step"):
            parts.append(str(row["step"]))
        title = " ".join(parts)
        if pass_pct is not None:
            title += f" (yield {pass_pct}%)"
        jobs.append({
            "out_path": os.path.join(tmpdir, f"map_{i:02d}.png"),
            "title": title,
            "dies": dies,
            "color_map": color_map,
            "chips": [c for c in (chips or [])
                      if str(c.get("source") or "") == str(row.get("source") or "")],
        })
    return jobs, color_map


def _build_issue_map_jobs(map_rows, targets, color_map, tmpdir):
    """Issue Table Map 셀 렌더 잡 — bin 별 썸네일을 **같은 맵을 쓰는 것끼리 묶어** 만든다.

    맵 선택은 웹 renderMiniMapCell 과 동일: step 분리 맵이면 그 bin 의 fail 이 실제로
    등장하는 step 맵을(fail 은 자기 step 맵에만 그려진다), 아니면 첫 맵을 쓴다.
    die 목록을 bin 마다 자식 프로세스로 피클 전송하지 않도록 맵 단위 1잡으로 묶는다.
    반환: (jobs, {bin: out_path}).
    """
    rows = [m for m in (map_rows or []) if m.get("dies")]
    if not rows or not targets:
        return [], {}
    stepwise = rows[0].get("step") is not None
    by_map = {}
    path_of = {}
    for bin_value, _excel_row in targets:
        b = str(bin_value)
        if b in path_of:
            continue
        idx = 0
        if stepwise:
            idx = next((i for i, m in enumerate(rows)
                        if any(str(bc.get("bin")) == b
                               for bc in (m.get("bin_counts") or []))), 0)
        out = os.path.join(tmpdir, f"issuemap_{len(path_of):03d}.png")
        path_of[b] = out
        by_map.setdefault(idx, []).append((b, out))
    jobs = [{"dies": rows[i]["dies"], "color_map": color_map, "targets": t}
            for i, t in by_map.items()]
    return jobs, path_of


def _build_temp_map_jobs(map_rows, targets, temp_map, tmpdir):
    """Issue Table Temp Map 셀 렌더 잡 — 항목별 fail die 강조 썸네일.

    웹 renderMiniTempCell 과 동일하게 **그 항목이 fail 난 첫 CT/HT 소스**의 맵 1장을 쓰고,
    같은 소스를 쓰는 항목끼리 묶어 die 목록 피클 전송을 1회로 줄인다.
    temp_map 은 {source: {item: [die 인덱스, ...]}} (_fetch.fetch_temp_map).
    반환: (jobs, {item: out_path}). temp_map 이 비면 ([], {}) — Map 열은 빈 칸이 된다.
    """
    rows = [m for m in (map_rows or []) if m.get("dies")]
    if not rows or not targets or not temp_map:
        return [], {}
    # STEP 분리 세션은 소스당 맵이 여러 장이지만 dies 길이·순서가 같으므로 첫 장을 쓴다.
    map_of = {}
    for m in rows:
        map_of.setdefault(m.get("source"), m)
    by_source = {}
    path_of = {}
    for item, _excel_row in targets:
        if item in path_of:
            continue
        src = next((s for s in temp_map if item in (temp_map.get(s) or {})
                    and s in map_of), None)
        if src is None:
            continue
        out = os.path.join(tmpdir, f"tempmap_{len(path_of):03d}.png")
        path_of[item] = out
        by_source.setdefault(src, []).append((item, temp_map[src][item], out))
    jobs = [{"dies": map_of[src]["dies"], "targets": t} for src, t in by_source.items()]
    return jobs, path_of
