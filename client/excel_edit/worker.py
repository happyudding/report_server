"""ExcelEditWorker — run_excel_edit 를 백그라운드 스레드에서 돌리는 QThread 래퍼.

honey_main 이 status/done/failed 시그널을 UI(상태바·로그·새로고침)에 연결한다.
COM(Excel) 은 이 워커 스레드에서만 다루므로 여기서 CoInitialize 한다.
"""
from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal

from . import excel_session


class ExcelEditWorker(QThread):
    status = pyqtSignal(str, str)   # (state, message)
    done = pyqtSignal(bool, str)    # (changed, message)
    failed = pyqtSignal(str)        # error message

    def __init__(self, session_id, server_base, parent=None):
        super().__init__(parent)
        self._session_id = session_id
        self._server_base = server_base
        self._cancelled = False

    def cancel(self):
        """편집 취소 요청. 워커 스레드가 다음 폴링에서 Excel 을 강제 종료하고 끝낸다.
        (bool 플래그 세팅뿐이라 다른 스레드에서 호출 안전 — GIL 하 원자적.)"""
        self._cancelled = True

    def run(self):
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            result = excel_session.run_excel_edit(
                self._session_id, self._server_base,
                status_cb=lambda s, m: self.status.emit(s, m),
                should_cancel=lambda: self._cancelled)
            self.done.emit(bool(result.get("changed")), str(result.get("message") or ""))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
