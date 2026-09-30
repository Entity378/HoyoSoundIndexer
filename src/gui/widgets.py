from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QSlider, QStyle, QVBoxLayout,
)

from src.gui.theme import type_swatch


# Re-applies the stylesheet after a dynamic property changed, for the [on="true"] selectors.
def repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def format_ms(ms):
    seconds = max(0, int(ms)) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"


def format_count(count):
    return f"{count:,}" if count > 0 else ""


# Binary units, the way Windows Explorer counts them.
def format_size(size):
    if size <= 0:
        return ""
    if size < 1024:
        return f"{size} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"


# Tenths of a second, since most sound effects last less than one.
def format_duration(ms):
    if ms < 0:
        return ""
    minutes, tenths = divmod(ms // 100, 600)
    return f"{minutes}:{tenths // 10:02d}.{tenths % 10}"


def rail_header(text):
    label = QLabel(text)
    label.setObjectName("railHeader")
    return label


# A click on the track jumps to that point, and dragging then continues as usual.
class ClickSlider(QSlider):
    clickedValue = pyqtSignal(int)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > self.minimum():
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), int(event.position().x()), self.width())
            self.setValue(value)
            self.clickedValue.emit(value)
        super().mousePressEvent(event)


class FacetRow(QFrame):
    clicked = pyqtSignal(str)

    def __init__(self, key, text, color):
        super().__init__()
        self.key = key
        self.setObjectName("facetRow")
        self.setProperty("on", False)
        self.setProperty("off", False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 3, 6, 3)
        row.setSpacing(7)
        dot = QLabel()
        dot.setPixmap(type_swatch(color))
        dot.setFixedWidth(9)
        self.count_label = QLabel("")
        self.count_label.setObjectName("facetCount")
        row.addWidget(dot)
        row.addWidget(QLabel(text))
        row.addStretch(1)
        row.addWidget(self.count_label)

    def mouseReleaseEvent(self, event):
        if self.isEnabled():
            self.clicked.emit(self.key)

    def set_active(self, active):
        self.setProperty("on", bool(active))
        repolish(self)

    def set_count(self, total, usable):
        self.count_label.setText(f"{total:,}" if total else "")
        self.setEnabled(usable)
        self.setProperty("off", not usable)
        repolish(self)


class SourceBox(QFrame):
    def __init__(self, title, tag_text="", button_text=None):
        super().__init__()
        self.setObjectName("sourceBox")
        self.setProperty("on", False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 5, 8, 6)
        outer.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.check = QCheckBox(title)
        self.tag = QLabel(tag_text)
        self.tag.setObjectName("sourceTag")
        top.addWidget(self.check)
        top.addStretch(1)
        top.addWidget(self.tag)
        outer.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.value = QLabel("—")
        self.value.setObjectName("sourceValue")
        bottom.addWidget(self.value, 1)
        self.button = None
        if button_text:
            self.button = QPushButton(button_text)
            self.button.setMaximumHeight(22)
            bottom.addWidget(self.button)
        outer.addLayout(bottom)
        self.check.toggled.connect(self._paint)

    def _paint(self, on):
        self.setProperty("on", bool(on))
        repolish(self)
