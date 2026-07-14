"""Excel Download — 열려 있는 web_report 세션을 xlsx 로 저장 (클라이언트 연산).

서버는 기존 엔드포인트로 데이터만 내려주고(무수정), 시트 구성·차트 PNG 렌더·Excel
기입은 전부 클라이언트가 한다. 진입점은 run_excel_download() 하나 — honey_main 은
worker.ExcelDownloadWorker(QThread) 로 감싸 호출한다.

시트: Summary / Yield / CPK / Issue Table / Distribution / Histogram / Map Analysis.
Distribution·Histogram 은 전체 항목(다운샘플링 금지 — 불변규칙 6)을 4열 그리드
청크 PNG 로 렌더해 세로로 이어 붙인다. 렌더는 ProcessPoolExecutor 병렬(실행시간
30초 목표), Excel 텍스트 시트 기입은 렌더와 동시에 진행한다.
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
    issue_cdf_px_size,
    render_chunk_pair,
    render_map_png_job,
    render_single_cdf,
)
from ._fetch import fetch_report_data
from . import _sheets

_CHUNK_CELLS = NCOLS * ROWS_PER_CHUNK

SHEET_ORDER = ["Summary", "Yield", "CPK", "Issue Table",
               "Distribution", "Histogram", "Map Analysis"]


def run_excel_download(session_id, server_base, out_path, status_cb=None) -> dict:
    """세션 web_report 를 out_path(xlsx)로 저장. 반환 {"out_path", "elapsed", "items"}.

    status_cb(state, message): 진행 통지 (state ∈ download/render/excel/save/done).
    호출 스레드에서 COM 초기화(CoInitialize)가 되어 있어야 한다 (worker.py 참조).
    """
    import xlwings as xw
    from excel_edit.excel_session import _quit_app
    from report_generator._xlsx_png_export import (_validate_embedded_images,
                                                   _wait_for_xlsx_ready)
    from report_generator._xlsx_style import _XL_CALC_AUTO, _XL_CALC_MANUAL

    def _emit(state, message):
        if status_cb:
            try:
                status_cb(state, message)
            except Exception:
                pass

    t0 = time.perf_counter()

    # ── 1. 서버 데이터 수신 (두 GET 동시) ────────────────────────────────────
    _emit("download", "리포트 데이터 다운로드 중...")
    full, dist = fetch_report_data(server_base, session_id)
    report = full["web_report"]
    session_url = f"{str(server_base).rstrip('/')}/pe/report/view/{session_id}"
    sheets = report.get("sheets") or {}
    source_names = [s.get("name") for s in (report.get("sources") or [])]
    colors = _source_colors(source_names, report.get("dist_colors"))
    t_dl = time.perf_counter()
    _emit("render", f"데이터 수신 완료 ({t_dl - t0:.1f}s) — 차트 렌더링 시작...")

    # ── 2. 차트 렌더 잡 구성 + 프로세스풀 시작 ──────────────────────────────
    tmpdir = tempfile.mkdtemp(prefix="honey_exceldl_")
    try:
        chunk_jobs, n_items, cell_of = _build_chunk_jobs(report, dist, dict(colors), tmpdir)
        product_type = (full.get("session") or {}).get("product_type", "")
        map_jobs = _build_map_jobs(sheets.get("Map Analysis") or [], tmpdir, product_type)
        _emit("render", f"차트 잡 구성 완료 ({time.perf_counter() - t_dl:.1f}s, "
                        f"{len(chunk_jobs)}청크 + map {len(map_jobs)})")

        n_workers = max(1, min(16, os.cpu_count() or 4, len(chunk_jobs) + len(map_jobs)))
        pool = ProcessPoolExecutor(max_workers=n_workers)
        try:
            chunk_futs = [pool.submit(render_chunk_pair, j) for j in chunk_jobs]
            map_futs = [pool.submit(render_map_png_job, j) for j in map_jobs]

            # ── 3. 렌더와 동시에 Excel 텍스트 시트 기입 ─────────────────────
            _emit("excel", f"Excel 시트 작성 중... (차트 {n_items}항목 x2 병렬 렌더)")
            app = xw.App(visible=False, add_book=False)
            try:
                app.display_alerts = False
                app.screen_updating = False

                wb = app.books.add()
                # Calculation 은 열린 workbook 이 있어야 설정 가능 (Excel COM 제약)
                app.api.Calculation = _XL_CALC_MANUAL
                ws = {}
                first = wb.sheets[0]
                first.name = SHEET_ORDER[0]
                ws[SHEET_ORDER[0]] = first
                for name in SHEET_ORDER[1:]:
                    ws[name] = wb.sheets.add(name, after=wb.sheets[wb.sheets.count - 1])

                _sheets.write_summary_sheet(ws["Summary"], report.get("yield_summary"),
                                            sheets.get("Fail Bin"))
                _sheets.write_yield_sheet(ws["Yield"], sheets.get("Yield"),
                                          report.get("yield_bin_groups"), source_names)
                _sheets.write_cpk_sheet(ws["CPK"], sheets.get("CPK"))
                issue_layout = _sheets.write_issue_sheet(
                    ws["Issue Table"], sheets.get("Issue Table"), source_names)
                # Issue Table 행별 CDF PNG 잡(분포 데이터가 있는 항목 행만) — 청크와 병렬 렌더.
                issue_targets, issue_jobs = [], []
                for item, excel_row in issue_layout["rows"]:
                    cell = cell_of.get(item)
                    if cell is None:
                        continue
                    out = os.path.join(tmpdir, f"issue_{excel_row:04d}.png")
                    issue_jobs.append({"cell": cell, "out_path": out})
                    issue_targets.append((excel_row, out))
                issue_futs = [pool.submit(render_single_cdf, j) for j in issue_jobs]
                t_text = time.perf_counter()
                _emit("excel", f"텍스트 시트 완료 ({t_text - t_dl:.1f}s) — 차트 대기/부착...")

                # ── 4. PNG 를 완료되는 순서대로 즉시 부착 (렌더 꼬리와 겹침) ──
                _sheets.write_source_legend(ws["Distribution"], colors)
                _sheets.write_source_legend(ws["Histogram"], colors)
                sizes = [chunk_px_size(len(j["cells"])) for j in chunk_jobs]
                tops = _sheets.picture_stack_tops([h for _, h in sizes])
                idx_of = {fut: i for i, fut in enumerate(chunk_futs)}
                for fut in as_completed(chunk_futs):
                    i = idx_of[fut]
                    cdf_path, hist_path = fut.result()
                    w_px, h_px = sizes[i]
                    _sheets.add_picture_at(ws["Distribution"], cdf_path,
                                           top=tops[i], width_px=w_px, height_px=h_px)
                    _sheets.add_picture_at(ws["Histogram"], hist_path,
                                           top=tops[i], width_px=w_px, height_px=h_px)

                map_pngs = []
                for job, fut in zip(map_jobs, map_futs):
                    map_pngs.append((job["title"], fut.result()))
                _sheets.add_map_grid(ws["Map Analysis"], map_pngs)

                # Issue Table 행별 CDF PNG 부착 (오름차순 — 행 높이 확대가 아래 행 top 에 반영)
                iw_px, ih_px = issue_cdf_px_size()
                for (excel_row, out), fut in zip(issue_targets, issue_futs):
                    fut.result()
                    _sheets.add_picture_in_cell(ws["Issue Table"], out, excel_row,
                                                issue_layout["dist_col"], iw_px, ih_px)

                # 모든 시트 상단에 세션 웹뷰 링크 삽입
                for name in SHEET_ORDER:
                    _sheets.add_session_link(ws[name], session_url)

                t_render = time.perf_counter()
                _emit("save", f"차트 부착 완료 ({t_render - t_text:.1f}s) — 저장 중...")

                # ── 5. 저장 + 무결성 검증 ───────────────────────────────────
                ws["Summary"].activate()
                app.api.Calculation = _XL_CALC_AUTO
                if os.path.exists(out_path):
                    os.remove(out_path)
                wb.save(out_path)
                wb.close()
            finally:
                _quit_app(app)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        _wait_for_xlsx_ready(out_path)
        _validate_embedded_images(out_path)
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

    elapsed = time.perf_counter() - t0
    _emit("done", f"Excel Download 완료 ({elapsed:.1f}s): {out_path}")
    return {"out_path": out_path, "elapsed": elapsed, "items": n_items}


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


def _build_chunk_jobs(report, dist, color_of, tmpdir):
    """distribution_index 순서(TSEQ)로 전 항목 셀을 만들어 32개씩 청크 잡으로 나눈다.

    dist items 에만 있고 index 에 없는 항목도 뒤에 붙인다 (데이터 누락 금지).
    반환: (jobs, n_items, cell_of{subject: cell}).
    """
    index_rows = report.get("distribution_index") or []
    items = (dist.get("items") or {})
    n_of = {}     # (subject, source) -> n  (정규분포 축퇴 판정용)
    stat_of = {}  # (subject, source) -> (avg, std)  (정규분포 곡선용)
    for r in report.get("sheets", {}).get("CPK") or []:
        key = (r.get("subject"), r.get("source"))
        if r.get("n") is not None:
            n_of[key] = r.get("n")
        stat_of[key] = (r.get("average"), r.get("stdev"))

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
            avg, std = stat_of.get((subject, src_name), (None, None))
            # float32: 플롯(수백 px 폭) 정밀도로 충분 — 자식 프로세스 피클 전송량 절반
            sources.append((
                src_name,
                color_of.get(src_name, "#888888"),
                np.asarray(data.get("x") or [], dtype="float32"),
                np.asarray(data.get("y") or [], dtype="float32"),
                n_of.get((subject, src_name)),
                avg, std,
            ))
        cells.append({
            "title": subject,
            "test_num": meta.get("test_num"),
            "units": info.get("units") or meta.get("units"),
            "lo": info.get("lo", meta.get("lower_limit")),
            "hi": info.get("hi", meta.get("upper_limit")),
            "status": meta.get("status"),
            "sources": sources,
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


def _build_map_jobs(map_rows, tmpdir, product_type=""):
    """Map Analysis 행 → 웹-파리티 wafer map 렌더 잡. 제목: source (step, yield %).

    좌표(dies)가 없는 행은 건너뛴다. bin→색은 전 소스 합산 기준 전역 색맵(웹과 동일 색)
    을 한 번만 만들어 모든 맵이 공유한다. frame(고정 웨이퍼 틀)·product_type 도 전달.
    """
    from ._map import build_bin_color_map

    color_map, order = build_bin_color_map(map_rows)
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
        present = {str(d.get("bin")) for d in dies}
        jobs.append({
            "out_path": os.path.join(tmpdir, f"map_{i:02d}.png"),
            "title": title,
            "dies": dies,
            "frame": {"x_min": row.get("x_min"), "x_max": row.get("x_max"),
                      "y_min": row.get("y_min"), "y_max": row.get("y_max")},
            "color_map": color_map,
            "bin_order": [b for b in order if b in present],
            "product_type": product_type,
        })
    return jobs
