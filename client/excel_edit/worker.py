"""ExcelEditWorker — run_excel_edit 를 백그라운드 스레드에서 돌리는 QThread 래퍼.

honey_main 이 status/done/failed 시그널을 UI(상태바·로그·새로고침)에 연결한다.
COM(Excel) 은 이 워커 스레드에서만 다루므로 여기서 CoInitialize 한다.
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import QThread, pyqtSignal

from . import excel_session


class ExcelEditWorker(QThread):
    status = pyqtSignal(str, str)         # (state, message)
    # 반영 확인 요청 — payload 는 rawvalues.build_confirm_sections 의 구조화 dict
    # (메인스레드가 확인창을 띄우고 answer_confirm 로 응답). str 이 아니라 object 여야
    # dict 가 그대로 전달된다.
    confirm_request = pyqtSignal(object)
    done = pyqtSignal(bool, str)       # (changed, message)
    failed = pyqtSignal(str)           # error message

    def __init__(self, session_id, server_base, parent=None):
        super().__init__(parent)
        self._session_id = session_id
        self._server_base = server_base
        self._cancelled = False
        self._confirm_event = None
        self._confirm_result = False

    def cancel(self):
        """편집 취소 요청. 워커 스레드가 다음 폴링에서 Excel 을 강제 종료하고 끝낸다.
        (bool 플래그 세팅뿐이라 다른 스레드에서 호출 안전 — GIL 하 원자적.)"""
        self._cancelled = True
        if self._confirm_event is not None:
            self._confirm_event.set()   # 확인 대기 중이면 깨워서 거부로 끝낸다

    def answer_confirm(self, accepted):
        """메인스레드가 confirm_request 에 응답할 때 호출 — 워커의 대기를 푼다."""
        self._confirm_result = bool(accepted)
        if self._confirm_event is not None:
            self._confirm_event.set()

    def _confirm(self, payload):
        """워커 스레드에서 실행 — 시그널로 질의하고 응답을 기다린다.

        응답이 영영 안 올 수도 있으므로(창 닫힘 등) 취소 플래그를 주기적으로 확인해
        교착을 피한다. 취소되면 거부로 본다."""
        self._confirm_event = threading.Event()
        self._confirm_result = False
        self.confirm_request.emit(payload)
        while not self._confirm_event.wait(0.2):
            if self._cancelled:
                return False
        return False if self._cancelled else self._confirm_result

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
                should_cancel=lambda: self._cancelled,
                confirm_cb=self._confirm)
            self.done.emit(bool(result.get("changed")), str(result.get("message") or ""))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass
