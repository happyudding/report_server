"""Excel 네이티브 차트 / 임베드 이미지 → PNG (Windows COM, 클라이언트 측 렌더).

세 가지 공개 함수:

export_chart_pngs(xlsx_path)
    모든 워크시트 ChartObjects + 차트 시트를 개별 PNG 로 반환.
    서버에서 격자 합성해 Distribution 탭에 표시하는 기존 방식 (하위호환 유지).

export_issue_table_pngs(xlsx_path, header_row=3)
    Issue Table 시트의 행별 임베드 PNG 추출.
    Method A: xlsx zip 직접 파싱 (COM 없이 동작, 우선).
    Method B: Excel COM ws.Shapes 순회 (폴백).
    반환: [{"row": int, "png": bytes}, ...] — row 는 0-based 데이터행 인덱스.

export_distribution_png(xlsx_path)
    Distribution 시트 전체를 단일 PNG 로 렌더링.
    ExportAsFixedFormat(PDF) → PyMuPDF(fitz) → PNG.
    다중 페이지는 수직 합성. 클립보드 미사용.
    반환: bytes | None.

pywin32 / Excel 미설치·실패 시 빈 값 반환(그레이스풀).
PyMuPDF(fitz) 미설치 시 export_distribution_png 는 None 반환.
"""
import io
import math
import os
import posixpath
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# XML 네임스페이스
_NS_WB  = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R   = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_XDR = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_NS_A   = "http://schemas.openxmlformats.org/drawingml/2006/main"


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────────

def is_available() -> bool:
    """win32com(Excel COM) 사용 가능 여부."""
    try:
        import win32com.client  # noqa: F401
        return True
    except Exception:
        return False


def _resolve_rel(base_zip_path: str, rel_target: str) -> str:
    """zip 내 상대 경로 → 절대 zip 경로 변환.

    예) base="xl/drawings/drawing2.xml", rel="../media/image1.png"
        → "xl/media/image1.png"
    """
    base_dir = posixpath.dirname(base_zip_path)
    raw = posixpath.join(base_dir, rel_target)
    parts = []
    for part in raw.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part and part != ".":
            parts.append(part)
    return "/".join(parts)


def _find_sheet_by_name(wb, name: str):
    """Excel COM 워크북에서 시트 이름으로 시트 반환 (대소문자 무시)."""
    name_lower = name.strip().lower()
    for ws in wb.Worksheets:
        try:
            if ws.Name.strip().lower() == name_lower:
                return ws
        except Exception:
            continue
    return None


def _open_excel(xlsx_path: str):
    """Excel.Application COM 객체 + Workbook 반환. 실패 시 (None, None)."""
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        wb = excel.Workbooks.Open(xlsx_path, ReadOnly=True, UpdateLinks=0)
        return excel, wb
    except Exception:
        return None, None


def _close_excel(excel, wb):
    try:
        if wb is not None:
            wb.Close(SaveChanges=False)
    except Exception:
        pass
    try:
        if excel is not None:
            excel.Quit()
    except Exception:
        pass
    try:
        import pythoncom
        pythoncom.CoUninitialize()
    except Exception:
        pass


# ── export_chart_pngs (기존, 하위호환) ───────────────────────────────────────

def export_chart_pngs(xlsx_path, progress_cb=None) -> list:
    """xlsx 의 모든 차트를 PNG bytes 리스트로 반환.

    순서: 워크시트 순회 → 시트 내 임베드 차트, 그다음 차트 시트.
    실패/미설치 시 [] 반환.

    progress_cb: callable(done: int, total: int) — 차트 1장 완료될 때마다 호출.
    """
    xlsx_path = str(Path(xlsx_path).resolve())
    pngs = []
    tmpdir = tempfile.mkdtemp(prefix="honey_charts_")
    excel, wb = _open_excel(xlsx_path)
    if excel is None:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return []

    seq = [0]
    done_count = [0]

    def _export(chart, total):
        out = os.path.join(tmpdir, f"{seq[0]}.png")
        seq[0] += 1
        chart.Export(out, "PNG")
        try:
            with open(out, "rb") as fh:
                data = fh.read()
        finally:
            try:
                os.remove(out)
            except OSError:
                pass
        if data[:8] == _PNG_MAGIC:
            pngs.append(data)
        done_count[0] += 1
        if progress_cb:
            try:
                progress_cb(done_count[0], total)
            except Exception:
                pass

    try:
        total = 0
        for ws in wb.Worksheets:
            try:
                total += int(ws.ChartObjects().Count)
            except Exception:
                pass
        try:
            total += int(wb.Charts.Count)
        except Exception:
            pass
        if total == 0:
            total = 1

        if progress_cb:
            try:
                progress_cb(0, total)
            except Exception:
                pass

        for ws in wb.Worksheets:
            try:
                cobjs = ws.ChartObjects()
                for i in range(1, int(cobjs.Count) + 1):
                    try:
                        _export(cobjs.Item(i).Chart, total)
                    except Exception:
                        done_count[0] += 1
                        if progress_cb:
                            try:
                                progress_cb(done_count[0], total)
                            except Exception:
                                pass
            except Exception:
                pass

        try:
            charts = wb.Charts
            for i in range(1, int(charts.Count) + 1):
                try:
                    _export(charts.Item(i), total)
                except Exception:
                    done_count[0] += 1
                    if progress_cb:
                        try:
                            progress_cb(done_count[0], total)
                        except Exception:
                            pass
        except Exception:
            pass
    except Exception:
        pass
    finally:
        _close_excel(excel, wb)
        shutil.rmtree(tmpdir, ignore_errors=True)

    return pngs


# ── export_issue_table_pngs ───────────────────────────────────────────────────

def export_issue_table_pngs(xlsx_path, header_row: int = 3) -> list:
    """Issue Table 시트의 행별 임베드 PNG 추출.

    Method A(zip 직접 파싱) 우선, 0개이면 Method B(Excel COM) 폴백.
    반환: [{"row": int, "png": bytes}, ...] — row 는 0-based 데이터행 인덱스.
    """
    out = _issue_pngs_from_zip(str(xlsx_path), "issue_table", header_row)
    if out:
        return out
    return _issue_pngs_from_com(str(xlsx_path), "issue_table", header_row)


def _issue_pngs_from_zip(xlsx_path: str, sheet_name: str, header_row: int) -> list:
    """xlsx zip 에서 직접 임베드 PNG 추출. Excel COM 불필요."""
    out = []
    try:
        with zipfile.ZipFile(xlsx_path) as zf:
            names = set(zf.namelist())

            # 1. workbook.xml → sheet 이름 → rId
            wb_xml = ET.fromstring(zf.read("xl/workbook.xml"))
            sheet_rId = None
            for sh in wb_xml.findall(f".//{{{_NS_WB}}}sheet"):
                if sh.get("name", "").strip().lower() == sheet_name.strip().lower():
                    sheet_rId = sh.get(f"{{{_NS_R}}}id")
                    break
            if not sheet_rId:
                return out

            # 2. workbook.xml.rels → rId → sheet xml 경로
            wb_rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            sheet_xml_path = None
            for rel in wb_rels:
                if rel.get("Id") == sheet_rId:
                    target = rel.get("Target", "")
                    # Target 예: "worksheets/sheet3.xml"
                    sheet_xml_path = f"xl/{target.lstrip('/')}"
                    break
            if not sheet_xml_path or sheet_xml_path not in names:
                return out

            # 3. sheet xml.rels → drawing xml 경로
            # xl/worksheets/sheet3.xml → xl/worksheets/_rels/sheet3.xml.rels
            sheet_rels_path = (
                sheet_xml_path
                .replace("xl/worksheets/", "xl/worksheets/_rels/") + ".rels"
            )
            if sheet_rels_path not in names:
                return out

            sheet_rels = ET.fromstring(zf.read(sheet_rels_path))
            drawing_xml_path = None
            for rel in sheet_rels:
                target = rel.get("Target", "")
                typ = rel.get("Type", "")
                if "drawing" in typ.lower() or "drawing" in target.lower():
                    drawing_xml_path = _resolve_rel(sheet_xml_path, target)
                    break
            if not drawing_xml_path or drawing_xml_path not in names:
                return out

            # 4. drawing xml.rels → media 파일 경로 맵
            drawing_rels_path = (
                drawing_xml_path
                .replace("xl/drawings/", "xl/drawings/_rels/") + ".rels"
            )
            drawing_rels_map = {}
            if drawing_rels_path in names:
                for rel in ET.fromstring(zf.read(drawing_rels_path)):
                    drawing_rels_map[rel.get("Id")] = rel.get("Target", "")

            # 5. drawing xml 파싱 → anchor row + image rId → PNG bytes
            drawing_xml = ET.fromstring(zf.read(drawing_xml_path))

            for anchor_tag in (
                f"{{{_NS_XDR}}}twoCellAnchor",
                f"{{{_NS_XDR}}}oneCellAnchor",
            ):
                for anchor in drawing_xml.findall(anchor_tag):
                    # pic 타입만 (graphicFrame = 차트는 제외)
                    if anchor.find(f"{{{_NS_XDR}}}pic") is None:
                        continue

                    frm = anchor.find(f"{{{_NS_XDR}}}from")
                    if frm is None:
                        continue
                    row_el = frm.find(f"{{{_NS_XDR}}}row")
                    if row_el is None or not (row_el.text or "").strip():
                        continue

                    anchor_row = int(row_el.text)   # 0-based
                    ri = anchor_row - header_row     # 0-based 데이터행 인덱스
                    if ri < 0:
                        continue

                    blip = anchor.find(f".//{{{_NS_A}}}blip")
                    if blip is None:
                        continue
                    embed_id = blip.get(f"{{{_NS_R}}}embed")
                    if not embed_id or embed_id not in drawing_rels_map:
                        continue

                    media_path = _resolve_rel(drawing_xml_path, drawing_rels_map[embed_id])
                    if media_path not in names:
                        continue

                    data = zf.read(media_path)
                    if data and len(data) > 16:
                        out.append({"row": ri, "png": data})

    except Exception:
        pass

    return sorted(out, key=lambda x: x["row"])


def _issue_pngs_from_com(xlsx_path: str, sheet_name: str, header_row: int) -> list:
    """Excel COM ws.Shapes 순회로 Issue Table 임베드 이미지 추출 (zip 파싱 폴백)."""
    out = []
    try:
        from PIL import ImageGrab
    except ImportError:
        return out

    excel, wb = _open_excel(xlsx_path)
    if excel is None:
        return out

    try:
        ws = _find_sheet_by_name(wb, sheet_name)
        if ws is None:
            return out

        xl_screen = 1
        xl_bitmap = 2
        mso_picture = 13  # msoPicture

        for i in range(1, int(ws.Shapes.Count) + 1):
            try:
                shape = ws.Shapes.Item(i)
                if int(shape.Type) != mso_picture:
                    continue
                top_row = int(shape.TopLeftCell.Row)     # 1-based Excel row
                ri = top_row - (header_row + 1)          # 0-based 데이터행 인덱스
                if ri < 0:
                    continue
                shape.CopyPicture(Appearance=xl_screen, Format=xl_bitmap)
                img = ImageGrab.grabclipboard()
                if img is None:
                    continue
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                out.append({"row": ri, "png": buf.getvalue()})
            except Exception:
                continue
    except Exception:
        pass
    finally:
        _close_excel(excel, wb)

    return sorted(out, key=lambda x: x["row"])


# ── export_distribution_png ───────────────────────────────────────────────────

def export_distribution_png(xlsx_path) -> bytes | None:
    """Distribution 시트 전체를 단일 PNG 로 렌더링.

    ExportAsFixedFormat(PDF) → PyMuPDF(fitz) → PNG.
    다중 페이지는 수직 합성. 클립보드 미사용.
    fitz(PyMuPDF) 또는 win32com 미설치 시 None 반환.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None

    xlsx_path = str(Path(xlsx_path).resolve())
    tmpdir = tempfile.mkdtemp(prefix="honey_dist_")
    pdf_path = os.path.join(tmpdir, "distribution.pdf")

    excel, wb = _open_excel(xlsx_path)
    if excel is None:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None

    try:
        ws = _find_sheet_by_name(wb, "distribution")
        if ws is None:
            return None
        ws.ExportAsFixedFormat(
            Type=0,                     # xlTypePDF
            Filename=pdf_path,
            Quality=0,                  # xlQualityStandard
            IncludeDocProperties=False,
            IgnorePrintAreas=False,
            OpenAfterPublish=False,
        )
    except Exception:
        return None
    finally:
        _close_excel(excel, wb)

    if not os.path.exists(pdf_path):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None

    try:
        from PIL import Image

        doc = fitz.open(pdf_path)
        if doc.page_count == 0:
            return None

        mat = fitz.Matrix(1.5, 1.5)   # ~108 DPI

        if doc.page_count == 1:
            pix = doc[0].get_pixmap(matrix=mat)
            return pix.tobytes("png")

        # 다중 페이지 → 수직 합성
        imgs = []
        for i in range(doc.page_count):
            pix = doc[i].get_pixmap(matrix=mat)
            imgs.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))

        total_h = sum(img.height for img in imgs)
        max_w = max(img.width for img in imgs)
        combined = Image.new("RGB", (max_w, total_h), (255, 255, 255))
        y = 0
        for img in imgs:
            combined.paste(img, (0, y))
            y += img.height

        buf = io.BytesIO()
        combined.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    except Exception:
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
