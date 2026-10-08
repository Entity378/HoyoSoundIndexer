# The side filters; every change emits changed, and the window reads the state back from the rail.

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget,
)

from src.gui.results import TYPE_BUCKETS
from src.gui.theme import BUCKET_COLORS
from src.gui.widgets import FacetRow, rail_header

_RAIL_WIDTH = 198


class FilterRail(QFrame):
    changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("filterRail")
        self.setFixedWidth(_RAIL_WIDTH)
        self.active_bucket = "all"
        self.active_character = ""
        self.language_checks = {}
        self.character_rows = {}
        self._character_texts = {}
        column = QVBoxLayout(self)
        column.setContentsMargins(7, 7, 7, 7)
        column.setSpacing(3)

        column.addWidget(rail_header("TYPE"))
        self.type_rows = {}
        for key, label in TYPE_BUCKETS:
            row = FacetRow(key, label, BUCKET_COLORS[key])
            row.clicked.connect(self.select_bucket)
            column.addWidget(row)
            self.type_rows[key] = row
        self._paint_buckets()

        column.addSpacing(8)
        self.language_header = rail_header("LANGUAGE")
        column.addWidget(self.language_header)
        self.language_box = QVBoxLayout()
        self.language_box.setSpacing(3)
        column.addLayout(self.language_box)

        column.addSpacing(8)
        column.addWidget(rail_header("SHOW"))
        self.audio_only_check = QCheckBox("Only with audio")
        self.audio_only_check.stateChanged.connect(self._emit_changed)
        column.addWidget(self.audio_only_check)

        column.addSpacing(8)
        self.character_header = rail_header("CHARACTER")
        column.addWidget(self.character_header)
        self.character_filter = QLineEdit()
        self.character_filter.setObjectName("charFilter")
        self.character_filter.setPlaceholderText("filter characters")
        self.character_filter.textChanged.connect(self._paint_characters)
        column.addWidget(self.character_filter)
        self.character_scroll = QScrollArea()
        self.character_scroll.setWidgetResizable(True)
        self.character_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.character_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        self.character_box = QVBoxLayout(host)
        self.character_box.setContentsMargins(0, 0, 12, 0)
        self.character_box.setSpacing(3)
        self.character_box.addStretch(1)
        self.character_scroll.setWidget(host)
        column.addWidget(self.character_scroll, 1)
        self._show_characters(False)

    def wanted_languages(self):
        return {name for name, check in self.language_checks.items() if check.isChecked()}

    def audio_only(self):
        return self.audio_only_check.isChecked()

    def character_keys(self):
        return list(self.character_rows)

    def _emit_changed(self, *_args):
        self.changed.emit()

    def select_bucket(self, key):
        self.active_bucket = key
        self._paint_buckets()
        self.changed.emit()

    # Clicking the active character again goes back to all of them.
    def select_character(self, key):
        self.active_character = "" if key == self.active_character else key
        self._paint_characters()
        self.changed.emit()

    def refresh(self, model):
        self._refresh_buckets(model)
        self._refresh_languages(model)
        self._refresh_characters(model)

    def _refresh_buckets(self, model):
        counts = model.bucket_counts()
        for key, _label in TYPE_BUCKETS:
            total = len(model.rows) if key == "all" else counts.get(key, 0)
            self.type_rows[key].set_count(total, key == "all" or total > 0)
        if not self.type_rows[self.active_bucket].isEnabled():
            self.active_bucket = "all"
        self._paint_buckets()

    def _paint_buckets(self):
        for key, row in self.type_rows.items():
            row.set_active(key == self.active_bucket)

    def _refresh_languages(self, model):
        _clear_layout(self.language_box)
        self.language_checks = {}
        counts = model.language_counts()
        self.language_header.setVisible(bool(counts))
        for language, total in sorted(counts.items()):
            row = QHBoxLayout()
            row.setSpacing(6)
            check = QCheckBox(language)
            check.setChecked(True)
            check.stateChanged.connect(self._emit_changed)
            count = QLabel(f"{total:,}")
            count.setObjectName("facetCount")
            row.addWidget(check)
            row.addStretch(1)
            row.addWidget(count)
            self.language_box.addLayout(row)
            self.language_checks[language] = check

    def _refresh_characters(self, model):
        _clear_layout(self.character_box)
        self.character_rows = {}
        counts = model.character_counts()
        self._show_characters(bool(counts))
        if self.active_character not in counts:
            self.active_character = ""
        if not counts:
            return
        others, self._character_texts = model.character_spellings()
        rows = [("", "All", model.character_row_total())]
        for name, total in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            alias = ", ".join(sorted(others.get(name, ())))
            rows.append((name, f"{name} ({alias})" if alias else name, total))
        for key, text, total in rows:
            row = FacetRow(key, text, BUCKET_COLORS["vo"])
            row.setToolTip(text)
            row.set_count(total, True)
            row.clicked.connect(self.select_character)
            self.character_box.addWidget(row)
            self.character_rows[key] = row
        self.character_box.addStretch(1)
        self._paint_characters()

    # The scroll area stays visible either way, since it holds the rail's spare room.
    def _show_characters(self, visible):
        self.character_header.setVisible(visible)
        self.character_filter.setVisible(visible)

    def _paint_characters(self):
        wanted = self.character_filter.text().strip().lower()
        for key, row in self.character_rows.items():
            row.set_active(key == self.active_character)
            row.setVisible(not key or not wanted or wanted in self._character_texts.get(key, key.lower()))


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        child = item.layout()
        if child is not None:
            _clear_layout(child)
            child.deleteLater()
