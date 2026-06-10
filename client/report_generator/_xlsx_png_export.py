"""차트 PNG export/부착 + 저장된 xlsx 파일 안정화·이미지 무결성 검증.

COM Chart 를 PNG 로 export 해 대상 시트에 그림으로 부착하는 경로(직접 export / 화면
영역으로 이동 후 export / CopyPicture 폴백)와, 저장 후 파일이 Excel 로 열리는지/임베드
이미지 relationship 이 온전한지 확인하는 검증 루틴을 모은다.
"""
from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path

from ._xlsx_distribution_chart import _CHART_H, _DIST_TITLE_PX
from ._xlsx_profile import (
    _PNG_ATTACH_MODE,
    _dist_add_time,
    _dist_count_png,
    _dist_time,
    _prof,
)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_EXPORT_MOVE_RETRIES = 2
_EXPORT_RETRY_SLEEP = 0.08
_EXCEL_QUIT_FILE_READY_RETRIES = 20
_EXCEL_QUIT_FILE_READY_SLEEP = 1.0
_EXCEL_QUIT_FILE_STABLE_SLEEP = 0.25


def _attach_chart_picture(sheet, chart, png_path, name, left, top, width, height,
                          sheet_name, subject, attach_progress_cb=None, png_cache=None):
    """Export a COM chart as PNG, then embed it as a picture on target sheet."""
    try:
        if _PNG_ATTACH_MODE == "copy_picture":
            _notify_attach_progress(attach_progress_cb, "copy_picture", sheet_name, subject)
            with _prof(f"{sheet_name}.copy_picture"):
                with _dist_time(f"png.{sheet_name}.copy_picture.total"):
                    _copy_chart_picture_to_sheet(
                        chart, sheet, name, left, top, width, height, sheet_name=sheet_name)
            _dist_count_png(sheet_name, "copy_picture")
            return True

        cached_png = png_cache.get(subject) if png_cache is not None else None
        if cached_png and os.path.exists(cached_png):
            with _prof(f"{sheet_name}.picadd"):
                with _dist_time(f"png.{sheet_name}.picture_add"):
                    _add_picture_from_file(sheet, cached_png, name, left, top, width, height)
            _dist_count_png(sheet_name, "cache", cached_png)
            return True

        export_t0 = time.perf_counter()
        with _prof(f"{sheet_name}.export"):
            if _PNG_ATTACH_MODE == "move_first_export":
                method = _export_chart_png_move_first(chart, png_path)
            else:
                method = _export_chart_png_stable(chart, png_path)
        export_elapsed = time.perf_counter() - export_t0
        if method:
            export_bucket = "export.direct" if method == "direct" else "export.moved"
            _dist_add_time(f"png.{sheet_name}.{export_bucket}", export_elapsed)
            with _prof(f"{sheet_name}.picadd"):
                with _dist_time(f"png.{sheet_name}.picture_add"):
                    _add_picture_from_file(sheet, png_path, name, left, top, width, height)
            if png_cache is not None:
                png_cache[subject] = png_path
            _dist_count_png(sheet_name, method, png_path)
            return True
        _dist_add_time(f"png.{sheet_name}.export.failed", export_elapsed)
        _notify_attach_progress(attach_progress_cb, "copy_picture", sheet_name, subject)
        with _prof(f"{sheet_name}.copy_picture"):
            with _dist_time(f"png.{sheet_name}.copy_picture.total"):
                _copy_chart_picture_to_sheet(
                    chart, sheet, name, left, top, width, height, sheet_name=sheet_name)
        _dist_count_png(sheet_name, "copy_picture")
        _log_chart_attach(f"{sheet_name}:{subject} used CopyPicture fallback")
        return True
    except Exception as exc:
        _dist_count_png(sheet_name, "failed")
        _log_chart_attach(f"{sheet_name}:{subject} attach failed: {exc!r}")
        return False


def _add_picture_from_file(sheet, png_path, name, left, top, width, height):
    sheet.pictures.add(
        png_path,
        link_to_file=False,
        save_with_document=True,
        name=name,
        left=left,
        top=top,
        width=width,
        height=height,
    )


def _export_chart_png_stable(chart, png_path):
    """Keep COM Chart.Export, but retry after moving off-screen charts into view."""
    if _export_chart_png_once(chart, png_path):
        return "direct"

    chart_object = _chart_object(chart)
    if chart_object is None:
        return None

    old_left = old_top = None
    try:
        old_left, old_top = chart_object.Left, chart_object.Top
        for attempt in range(1, _EXPORT_MOVE_RETRIES + 1):
            chart_object.Left = 0
            chart_object.Top = _DIST_TITLE_PX + (attempt - 1) * (_CHART_H + 6)
            time.sleep(_EXPORT_RETRY_SLEEP * attempt)
            if _export_chart_png_once(chart, png_path):
                return f"moved{attempt}"
    except Exception as exc:
        _log_chart_attach(f"Chart.Export move retry failed: {exc!r}")
    finally:
        if old_left is not None and old_top is not None:
            try:
                chart_object.Left = old_left
                chart_object.Top = old_top
            except Exception:
                pass
    return None


def _export_chart_png_move_first(chart, png_path):
    """Move the chart into Excel's renderable area before the first PNG export."""
    chart_object = _chart_object(chart)
    if chart_object is None:
        return None

    old_left = old_top = None
    try:
        old_left, old_top = chart_object.Left, chart_object.Top
        for attempt in range(1, _EXPORT_MOVE_RETRIES + 2):
            chart_object.Left = 0
            chart_object.Top = _DIST_TITLE_PX + (attempt - 1) * (_CHART_H + 6)
            time.sleep(_EXPORT_RETRY_SLEEP * attempt)
            if _export_chart_png_once(chart, png_path):
                return f"moved{attempt}"
    except Exception as exc:
        _log_chart_attach(f"Chart.Export move-first failed: {exc!r}")
    finally:
        if old_left is not None and old_top is not None:
            try:
                chart_object.Left = old_left
                chart_object.Top = old_top
            except Exception:
                pass
    return None


def _export_chart_png_once(chart, png_path):
    try:
        if os.path.exists(png_path):
            os.remove(png_path)
        chart.Export(png_path, "PNG")
    except Exception as exc:
        _log_chart_attach(f"Chart.Export failed: {exc!r}")
        return False
    return _is_valid_png(png_path)


def _is_valid_png(png_path):
    try:
        if os.path.getsize(png_path) <= len(_PNG_MAGIC):
            return False
        with open(png_path, "rb") as fh:
            return fh.read(len(_PNG_MAGIC)) == _PNG_MAGIC
    except OSError:
        return False


def _copy_chart_picture_to_sheet(chart, sheet, name, left, top, width, height, sheet_name=None):
    chart_object = _chart_object(chart)
    if chart_object is None:
        raise RuntimeError("chart object not found for CopyPicture fallback")
    prefix = f"png.{sheet_name}." if sheet_name else "png."
    with _dist_time(prefix + "copy_picture.copy"):
        chart_object.CopyPicture(Appearance=1, Format=-4147)
    with _dist_time(prefix + "copy_picture.paste"):
        sheet_api = sheet.api
        shapes = sheet_api.Shapes
        before = int(shapes.Count)
        try:
            sheet_api.Activate()
        except Exception:
            pass
        sheet_api.Paste()
        after = int(shapes.Count)
        if after <= before:
            raise RuntimeError("CopyPicture paste did not create a shape")
        shape = shapes.Item(after)
    with _dist_time(prefix + "copy_picture.position"):
        shape.Name = name
        shape.Left = float(left)
        shape.Top = float(top)
        shape.Width = float(width)
        shape.Height = float(height)
    return shape


def _notify_attach_progress(cb, event, sheet_name, subject, done=None, total=None):
    if cb is None:
        return
    try:
        cb(event, sheet_name, subject, done, total)
    except Exception:
        pass


def _chart_object(chart):
    try:
        return chart.Parent
    except Exception:
        return None


def _log_chart_attach(message):
    print(f"[xlsx_writer] chart attach: {message}", file=sys.stderr)


def _validate_embedded_images(xlsx_path):
    try:
        with zipfile.ZipFile(xlsx_path) as zf:
            names = set(zf.namelist())
            rel_names = [n for n in names if n.startswith("xl/drawings/_rels/")
                         and n.endswith(".rels")]
            for rel_name in rel_names:
                rel_xml = zf.read(rel_name).decode("utf-8", errors="replace")
                if 'Target="NULL"' in rel_xml or "Target='NULL'" in rel_xml:
                    raise RuntimeError(f"broken image relationship in {rel_name}: Target=NULL")
                for target in _image_rel_targets(rel_xml):
                    part = _resolve_xlsx_part(rel_name, target)
                    if part not in names:
                        raise RuntimeError(
                            f"broken image relationship in {rel_name}: missing {part}"
                        )
    except zipfile.BadZipFile:
        # DRM(NASCA) 암호화 파일은 zip 이 아니어서 내부 part 를 들여다볼 수 없다.
        # _wait_for_xlsx_ready 가 이미 Excel 로 열림을 확인했으므로 손상이 아니라
        # 암호화로 보고 임베드 이미지 검증을 건너뛴다(평문 xlsx 검증은 그대로).
        return


def _wait_for_xlsx_ready(xlsx_path):
    last_exc = None
    last_size = None
    for attempt in range(1, _EXCEL_QUIT_FILE_READY_RETRIES + 1):
        try:
            last_size = _stable_file_size(xlsx_path)
            with zipfile.ZipFile(xlsx_path) as zf:
                names = zf.namelist()
                if "[Content_Types].xml" not in names:
                    raise zipfile.BadZipFile("missing [Content_Types].xml")
            return
        except (FileNotFoundError, zipfile.BadZipFile, PermissionError, OSError) as exc:
            last_exc = exc
            if attempt >= _EXCEL_QUIT_FILE_READY_RETRIES:
                break
            time.sleep(_EXCEL_QUIT_FILE_READY_SLEEP)

    # zip 으로 안 열리면 DRM(NASCA) 암호화 파일일 수 있다. 암호화 파일은 zip 이
    # 아니므로 Excel(DispatchEx)이 열 수 있으면 준비 완료로 간주한다(zip 재검증 안 함).
    # 대용량 파일은 암호화 완료까지 시간이 걸리므로 재오픈을 재시도 루프로 감싼다.
    excel_exc = None
    for attempt in range(1, _EXCEL_QUIT_FILE_READY_RETRIES + 1):
        try:
            _retry_open_xlsx_with_excel(xlsx_path)
            return
        except Exception as exc:
            excel_exc = exc
            if attempt >= _EXCEL_QUIT_FILE_READY_RETRIES:
                break
            time.sleep(_EXCEL_QUIT_FILE_READY_SLEEP)

    last_msg = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "none"
    raise RuntimeError(
        "xlsx file is not ready after Excel quit: "
        f"{xlsx_path} (size={last_size}, last_error={last_msg}, "
        f"excel_retry_error={type(excel_exc).__name__}: {excel_exc})"
    ) from excel_exc


def _stable_file_size(xlsx_path):
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(xlsx_path)
    size1 = os.path.getsize(xlsx_path)
    if size1 <= 0:
        raise OSError(f"xlsx file is empty: {xlsx_path}")
    time.sleep(_EXCEL_QUIT_FILE_STABLE_SLEEP)
    size2 = os.path.getsize(xlsx_path)
    if size1 != size2:
        raise OSError(f"xlsx file size is still changing: {size1} -> {size2}")
    return size2


def _retry_open_xlsx_with_excel(xlsx_path):
    """저장된 xlsx 를 Excel COM(DispatchEx)으로 열어 본다.

    DRM(NASCA)이 걸린 파일은 Excel COM 으로만 열 수 있으므로 xlwings 대신
    win32com DispatchEx 를 쓴다(upload_prepare._extract_via_excel_com,
    chart_export._open_excel 와 동일 패턴). 열기에 실패하면 예외를 전파한다.
    """
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    excel = None
    wb = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        wb = excel.Workbooks.Open(str(Path(xlsx_path).resolve()),
                                  UpdateLinks=0, ReadOnly=True)
    finally:
        try:
            if wb is not None:
                wb.Close(SaveChanges=False)
        finally:
            try:
                if excel is not None:
                    excel.Quit()
            finally:
                pythoncom.CoUninitialize()


def _is_package_integrity_error(exc):
    msg = str(exc)
    return ("broken image relationship" in msg
            or "invalid xlsx package" in msg)


def _image_rel_targets(rel_xml):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(rel_xml)
    for rel in root:
        typ = rel.attrib.get("Type", "")
        if typ.endswith("/image"):
            yield rel.attrib.get("Target", "")


def _resolve_xlsx_part(rel_name, target):
    base = Path(rel_name).parent.parent
    part = (base / target).as_posix()
    while "/../" in part:
        left, right = part.split("/../", 1)
        part = left.rsplit("/", 1)[0] + "/" + right
    return part.lstrip("/")
