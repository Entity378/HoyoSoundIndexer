# Background work runs on daemon threads, so a download still running at exit never blocks it.
# A QThread still running when destroyed would abort the process instead.

import threading

from PyQt6.QtCore import QObject, pyqtSignal

from src.pipeline import Cancelled


# work(progress, cancelled) runs in the background, then exactly one of the result signals follows.
class Task(QObject):
    progressed = pyqtSignal(int, int, str)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self._work = work
        self._cancel = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        try:
            result = self._work(self.progressed.emit, self._cancel.is_set)
        except Cancelled:
            self.cancelled.emit()
            return
        except Exception as e:
            self.failed.emit(str(e) or type(e).__name__)
            return
        if self._cancel.is_set():
            self.cancelled.emit()
        else:
            self.succeeded.emit(result)

    def cancel(self):
        self._cancel.set()

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def wait(self, seconds):
        if self._thread is not None:
            self._thread.join(seconds)
        return not self.is_running()


def running(task):
    return task is not None and task.is_running()
