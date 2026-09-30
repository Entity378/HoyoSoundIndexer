import sys
import traceback

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.config import APP_NAME
from src.gui import test_hooks
from src.gui.main_window import MainWindow
from src.gui.theme import STYLESHEET, use_dark_titlebar


# PyQt aborts the application when an exception escapes a slot, unless a hook of our own is set.
def _install_exception_hook(app):
    # Each open box stays referenced until closed, or it would vanish at once.
    boxes = []

    def show(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        box = QMessageBox(QMessageBox.Icon.Critical, APP_NAME, f"Unexpected error: {exc_type.__name__}: {exc}",
                          parent=app.activeWindow())
        # Not modal, so a headless run never waits for a click.
        box.setModal(False)
        box.finished.connect(lambda _result: boxes.remove(box))
        boxes.append(box)
        box.show()

    sys.excepthook = show


def run_gui():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    _install_exception_hook(app)
    window = MainWindow()
    window.show()
    use_dark_titlebar(window)
    test_hooks.on_startup(app, window)
    sys.exit(app.exec())
