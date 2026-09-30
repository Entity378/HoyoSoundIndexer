import ctypes
import sys

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

WINDOW = "#353535"
BASE = "#2a2a2a"
ALTERNATE = "#303030"
BUTTON = "#3c3c3c"
BUTTON_HOVER = "#484848"
LINE = "#5a5a5a"
LINE_SOFT = "#444444"
TEXT = "#f0f0f0"
TEXT_DIM = "#9a9a9a"
TEXT_OFF = "#7f7f7f"
ACCENT = "#d13438"
SELECTION = "#4a4a4a"
HEADER = "#3a3a3a"

BUCKET_COLORS = {"vo": "#4ec9b0", "sfx": "#569cd6", "music": "#c586c0",
                 "bank": "#d7ba7d", "sync": "#9a9a9a", "all": "#d0d0d0"}


STYLESHEET = f"""
QWidget {{
    background: {WINDOW};
    color: {TEXT};
    font-family: "Segoe UI";
    font-size: 12px;
}}

QLineEdit, QPlainTextEdit, QTreeWidget {{
    background: {BASE};
    border: 1px solid {LINE};
    color: {TEXT};
    selection-background-color: {SELECTION};
}}
QLineEdit {{ padding: 3px 6px; }}
QLineEdit#searchEdit {{ border: 1px solid {ACCENT}; }}

QPushButton {{
    background: {BUTTON};
    border: 1px solid {LINE};
    color: {TEXT};
    padding: 4px 12px;
}}
QPushButton:hover {{ background: {BUTTON_HOVER}; }}
QPushButton:default {{ border-color: {ACCENT}; }}
QPushButton:disabled {{
    background: #333333;
    color: {TEXT_OFF};
    border-color: {LINE_SOFT};
}}

QFrame#sourceBox {{ background: #323232; border: 1px solid {LINE_SOFT}; }}
QFrame#sourceBox[on="true"] {{ background: {HEADER}; border: 1px solid {LINE}; }}
QFrame#sourceBox QWidget {{ background: transparent; }}
QFrame#filterRail {{ background: {WINDOW}; border: 1px solid {LINE_SOFT}; }}
QFrame#filterRail QWidget {{ background: transparent; }}
QFrame#filterRail QLineEdit#charFilter {{ background: {BASE}; }}

QLabel#railHeader {{ color: {TEXT_OFF}; font-size: 10px; }}
QLabel#sourceTag {{
    color: {TEXT_OFF};
    border: 1px solid {LINE_SOFT};
    padding: 0px 5px;
}}
QLabel#sourceValue, QLabel#facetCount {{ color: {TEXT_DIM}; }}

QTreeWidget {{ alternate-background-color: {ALTERNATE}; outline: 0; }}
QTreeWidget::item {{ padding: 2px 0px; border: 0px; }}
QTreeWidget::item:selected, QTreeWidget::item:selected:active {{ background: {SELECTION}; color: {TEXT}; }}

QFrame#facetRow {{ background: transparent; border: 1px solid transparent; }}
QFrame#facetRow:hover {{ background: {HEADER}; }}
QFrame#facetRow[on="true"] {{ background: {SELECTION}; border: 1px solid {LINE_SOFT}; }}
QFrame#facetRow[off="true"] QLabel {{ color: {TEXT_OFF}; }}

QHeaderView::section {{
    background: {HEADER};
    color: #d0d0d0;
    border: 0px;
    border-right: 1px solid {LINE_SOFT};
    border-bottom: 1px solid {LINE_SOFT};
    padding: 3px 6px;
}}

QTabBar::tab {{
    background: {ALTERNATE};
    border: 1px solid {LINE_SOFT};
    border-bottom: none;
    color: {TEXT_DIM};
    padding: 4px 14px;
}}
QTabBar::tab:selected {{ background: {WINDOW}; color: {TEXT}; }}
QTabWidget::pane {{ border: 1px solid {LINE_SOFT}; top: -1px; }}

QProgressBar {{ background: {BASE}; border: 1px solid {LINE}; }}
QProgressBar::chunk {{ background: {ACCENT}; }}
QStatusBar {{ background: #2f2f2f; }}
QStatusBar QLabel {{ background: transparent; }}
QStatusBar::item {{ border: 0px; }}

QSlider::groove:horizontal {{ height: 4px; background: {SELECTION}; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; }}
QSlider::handle:horizontal {{
    width: 9px;
    height: 12px;
    margin: -5px 0px;
    background: #c8c8c8;
}}

QCheckBox::indicator {{
    width: 13px;
    height: 13px;
    background: {BASE};
    border: 1px solid #8a8a8a;
}}
QCheckBox::indicator:hover {{ border: 1px solid #b4b4b4; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border: 1px solid #dcdcdc; }}
QCheckBox::indicator:disabled {{ background: #333333; border: 1px solid #565656; }}

QRadioButton::indicator {{
    width: 13px;
    height: 13px;
    border-radius: 7px;
    background: {BASE};
    border: 1px solid #8a8a8a;
}}
QRadioButton::indicator:hover {{ border: 1px solid #b4b4b4; }}
QRadioButton::indicator:checked {{
    border: 1px solid #dcdcdc;
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5, stop:0 {ACCENT}, stop:0.55 {ACCENT}, stop:0.6 {BASE}, stop:1 {BASE});
}}

QScrollBar:vertical {{ background: {BASE}; width: 12px; }}
QScrollBar:horizontal {{ background: {BASE}; height: 12px; }}
QScrollBar::handle {{
    background: #4f4f4f;
    min-height: 20px;
    min-width: 20px;
}}
QScrollBar::handle:hover {{ background: #5c5c5c; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; width: 0px; }}

QToolTip {{
    background: {HEADER};
    color: {TEXT};
    border: 1px solid {LINE};
}}
"""


def type_swatch(color):
    pixmap = QPixmap(9, 9)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.fillRect(1, 1, 7, 7, QColor(color))
    painter.end()
    return pixmap


def type_dot(color):
    return QIcon(type_swatch(color))


def media_icon(kind, color):
    size, ratio = 15, 2
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    if kind == "play":
        path = QPainterPath()
        path.moveTo(2.5, 1)
        path.lineTo(13.5, size / 2)
        path.lineTo(2.5, size - 1)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "pause":
        painter.drawRoundedRect(QRectF(3, 1, 4, 13), 1, 1)
        painter.drawRoundedRect(QRectF(9, 1, 4, 13), 1, 1)
    elif kind == "stop":
        painter.drawRoundedRect(QRectF(1, 1, 13, 13), 1, 1)
    elif kind in ("previous", "next"):
        if kind == "previous":
            painter.translate(size, 0)
            painter.scale(-1, 1)
        path = QPainterPath()
        path.moveTo(1.5, 1.5)
        path.lineTo(10.5, size / 2)
        path.lineTo(1.5, size - 1.5)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawRoundedRect(QRectF(10.5, 1.5, 3, 12), 0.8, 0.8)
    painter.end()
    return QIcon(pixmap)


# DWMWA_USE_IMMERSIVE_DARK_MODE has two attribute numbers across Windows builds, so both are set.
def use_dark_titlebar(widget):
    if sys.platform != "win32":
        return
    try:
        handle = int(widget.winId())
        flag = ctypes.c_int(1)
        for attribute in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, attribute, ctypes.byref(flag),
                                                       ctypes.sizeof(flag))
    except Exception:
        pass
