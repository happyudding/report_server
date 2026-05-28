"""Excel 네이티브 차트 → PNG (Windows COM, 클라이언트 측 렌더).

Honey 가 실행되는 PC 의 로컬 Excel 을 win32com 으로 구동해, 선택한 xlsx 의 모든
차트(워크시트 임베드 차트 + 차트 시트)를 PNG bytes 리스트로 반환한다. 서버는
헤드리스라 렌더링은 클라이언트가 담당한다.

pywin32/Excel 미설치·실패 시 빈 리스트를 반환(그레이스풀) → 호출 측은 xlsx 만 업로드.
"""
import os
import shutil
import tempfile
from pathlib import Path

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def is_available() -> bool:
    """win32com(Excel COM) 사용 가능 여부."""
    try:
        import win32com.client  # noqa: F401
        return True
    except Exception:
        return False


def export_chart_pngs(xlsx_path) -> list:
    """xlsx 의 모든 차트를 PNG bytes 리스트로 반환.

    순서: 워크시트 순회 → 시트 내 임베드 차트, 그다음 차트 시트.
    실패/미설치 시 [] 반환.
    """
    try:
        import pythoncom
        import win32com.client
    except Exception:
        return []

    xlsx_path = str(Path(xlsx_path).resolve())
    pngs = []
    tmpdir = tempfile.mkdtemp(prefix="honey_charts_")
    pythoncom.CoInitialize()
    excel = None
    wb = None
    seq = [0]

    def _export(chart):
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

    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        # ReadOnly + UpdateLinks=0: 사용자가 같은 파일을 열어둬도 락/프롬프트 회피
        wb = excel.Workbooks.Open(xlsx_path, ReadOnly=True, UpdateLinks=0)

        # 1) 워크시트 임베드 차트 (ChartObjects)
        for ws in wb.Worksheets:
            try:
                cobjs = ws.ChartObjects()
                for i in range(1, int(cobjs.Count) + 1):
                    try:
                        _export(cobjs.Item(i).Chart)
                    except Exception:
                        pass
            except Exception:
                pass

        # 2) 차트 시트 (Chart sheets)
        try:
            charts = wb.Charts
            for i in range(1, int(charts.Count) + 1):
                try:
                    _export(charts.Item(i))
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass
    finally:
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
            pythoncom.CoUninitialize()
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    return pngs
