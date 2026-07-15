"""Progress bar helpers for Honey UI."""
import concurrent.futures
import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication


class ElapsedProgress:
    """Status progress bar that continuously renders elapsed time.

    mirror: 선택. 같은 진행 상태를 함께 표시할 두 번째 QProgressBar. dock 진행바가
    슬라이드 패널(입력/설정 창)에 가려 안 보이는 흐름에서, 패널 안 진행바로 같은
    range/value/format/show/hide 를 미러링해 사용자가 진행 상황을 바로 보게 한다.
    """

    def __init__(self, bar, label, status_cb=None, busy=True, minimum=0, maximum=100,
                 mirror=None):
        self.bar = bar
        self.mirror = mirror
        self.status_cb = status_cb
        self.busy = busy
        self.started = time.monotonic()
        self.label = label
        self.status = None
        self._last_secs = -1
        self._last_rendered = None
        self.token = int(self.bar.property("_honey_progress_token") or 0) + 1
        self.bar.setProperty("_honey_progress_token", self.token)
        for b in self._bars():
            b.setProperty("_honey_progress_token", self.token)
            b.setRange(0, 0) if busy and maximum == 0 else b.setRange(minimum, maximum)
            b.setValue(minimum)
            b.setFormat("")
            b.show()
        self.update(force=True)

    def _bars(self):
        return (self.bar, self.mirror) if self.mirror is not None else (self.bar,)

    def _elapsed(self):
        secs = int(time.monotonic() - self.started)
        return secs, f"{secs // 60:02d}:{secs % 60:02d}"

    def set(self, label=None, value=None, status=None, busy=None):
        if label is not None:
            self.label = label
        if busy is not None:
            self.busy = busy
        if value is not None:
            for b in self._bars():
                b.setValue(value)
        if status is not None:
            self.status = status
            if self.status_cb is not None:
                self.status_cb(status)
        self.update(force=True)

    def value(self):
        return self.bar.value()

    def maximum(self):
        return self.bar.maximum()

    def set_maximum(self, value):
        for b in self._bars():
            b.setMaximum(value)

    def update(self, force=False):
        secs, elapsed = self._elapsed()
        if not force and secs == self._last_secs:
            return
        suffix = " (진행중)" if self.busy else ""
        text = f"{self.label}  [{elapsed}]{suffix}"
        if force or text != self._last_rendered:
            for b in self._bars():
                b.setFormat(text)
            self._last_rendered = text
        self._last_secs = secs
        QApplication.processEvents()

    def success(self, text, value=None, hide_ms=5000):
        was_indeterminate = self.bar.minimum() == 0 and self.bar.maximum() == 0
        if value is None:
            value = 100 if was_indeterminate else self.bar.maximum()
        self.busy = False
        for b in self._bars():
            if was_indeterminate:
                b.setRange(0, 100)
            b.setValue(value)
            b.setFormat(text)
        self.label = text
        if self.status_cb is not None:
            self.status_cb(text)
        self._hide_later(hide_ms)
        QApplication.processEvents()

    def fail(self, text, hide_ms=8000):
        self.busy = False
        for b in self._bars():
            if b.minimum() == 0 and b.maximum() == 0:
                b.setRange(0, 100)
            b.setValue(0)
            b.setFormat(text)
        self.label = text
        if self.status_cb is not None:
            self.status_cb(text)
        self._hide_later(hide_ms)
        QApplication.processEvents()

    def _hide_later(self, ms):
        token = self.token

        def _hide_if_current():
            for b in self._bars():
                if int(b.property("_honey_progress_token") or 0) != token:
                    continue
                b.hide()
                b.setFormat("")

        QTimer.singleShot(ms, _hide_if_current)


def wait_for_future(future, progress, poll_cb=None, timeout=0.1):
    while True:
        if poll_cb is not None:
            poll_cb()
        progress.update()
        done, _ = concurrent.futures.wait([future], timeout=timeout)
        if done:
            if poll_cb is not None:
                poll_cb()
            return future.result()
