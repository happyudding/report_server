"""ExcelDownloadWorker — run_excel_download 를 백그라운드 스레드에서 돌리는 QThread 래퍼.

honey_main 이 status/done/failed 시그널을 UI(상태바·로그)에 연결한다.
COM(Excel/xlwings) 은 이 워커 스레드에서만 다루므로 여기서 CoInitialize 한다
(excel_edit/worker.py 와 동일 패턴).
"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal


class ExcelDownloadWorker(QThread):
    status = pyqtSignal(str, str)   # (state, message)
    done = pyqtSignal(str, float)   # (out_path, elapsed_sec)
    failed = pyqtSignal(str)        # error message

    def __init__(self, session_id, server_base, out_path, bin1=False, parent=None):
        super().__init__(parent)
        self._session_id = session_id
        self._server_base = server_base
        self._out_path = out_path
        self._bin1 = bin1

    def run(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            from . import run_excel_download
            result = run_excel_download(
                self._session_id, self._server_base, self._out_path,
                status_cb=lambda s, m: self.status.emit(s, m),
                bin1=self._bin1)
            self.done.emit(str(result.get("out_path") or self._out_path),
                           float(result.get("elapsed") or 0.0))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
