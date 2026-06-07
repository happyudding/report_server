"""Progress bar helpers for Honey UI."""
import concurrent.futures
import time

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication


class ElapsedProgress:
    """Status progress bar that continuously renders elapsed time."""

    def __init__(self, bar, label, status_cb=None, busy=True, minimum=0, maximum=100):
        self.bar = bar
        self.status_cb = status_cb
        self.busy = busy
        self.started = time.monotonic()
        self.label = label
        self.status = None
        self._last_secs = -1
        self._last_rendered = None
        self.token = int(self.bar.property("_honey_progress_token") or 0) + 1
        self.bar.setProperty("_honey_progress_token", self.token)
        self.bar.setRange(0, 0) if busy and maximum == 0 else self.bar.setRange(minimum, maximum)
        self.bar.setValue(minimum)
        self.bar.setFormat("")
        self.bar.show()
        self.update(force=True)

    def _elapsed(self):
        secs = int(time.monotonic() - self.started)
        return secs, f"{secs // 60:02d}:{secs % 60:02d}"

    def set(self, label=None, value=None, status=None, busy=None):
        if label is not None:
            self.label = label
        if busy is not None:
            self.busy = busy
        if value is not None:
            self.bar.setValue(value)
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
        self.bar.setMaximum(value)

    def update(self, force=False):
        secs, elapsed = self._elapsed()
        if not force and secs == self._last_secs:
            return
        suffix = " (진행중)" if self.busy else ""
        text = f"{self.label}  [{elapsed}]{suffix}"
        if force or text != self._last_rendered:
            self.bar.setFormat(text)
            self._last_rendered = text
        self._last_secs = secs
        QApplication.processEvents()

    def success(self, text, value=None, hide_ms=5000):
        was_indeterminate = self.bar.minimum() == 0 and self.bar.maximum() == 0
        if value is None:
            value = 100 if was_indeterminate else self.bar.maximum()
        if was_indeterminate:
            self.bar.setRange(0, 100)
        self.busy = False
        self.bar.setValue(value)
        self.label = text
        self.bar.setFormat(text)
        if self.status_cb is not None:
            self.status_cb(text)
        self._hide_later(hide_ms)
        QApplication.processEvents()

    def fail(self, text, hide_ms=8000):
        if self.bar.minimum() == 0 and self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
        self.busy = False
        self.bar.setValue(0)
        self.label = text
        self.bar.setFormat(text)
        if self.status_cb is not None:
            self.status_cb(text)
        self._hide_later(hide_ms)
        QApplication.processEvents()

    def _hide_later(self, ms):
        token = self.token

        def _hide_if_current():
            if int(self.bar.property("_honey_progress_token") or 0) != token:
                return
            self.bar.hide()
            self.bar.setFormat("")

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
