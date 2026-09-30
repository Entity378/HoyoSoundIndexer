# The result tree: rows, or one node per action for a character, with wem locations built on expansion.
# Numbers stay numbers in the items, so Qt sorts them itself, and a delegate per column formats them.

import os
import time

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem,
)

from src.audio import safe_file_stem
from src.characters import action_label, action_sort_key
from src.model import Kind, WemLocation
from src.gui.results import ResultRow
from src.gui.theme import BUCKET_COLORS, type_dot
from src.gui.widgets import format_count, format_duration, format_size

# Items carry their ResultRow or WemLocation under this role, action nodes nothing.
USER_ROLE = Qt.ItemDataRole.UserRole
DISPLAY_ROLE = Qt.ItemDataRole.DisplayRole
COLUMNS = ["Name / Source", "Type", "ID", "Language", "Wems", "Size", "Duration", "Tags"]
COL_NAME, COL_TYPE, COL_ID, COL_LANGUAGE, COL_WEMS, COL_SIZE, COL_DURATION, COL_TAGS = range(len(COLUMNS))
DEFAULT_WIDTHS = [500, 104, 116, 66, 56, 72, 76, 240]
# Qt compares 64-bit numbers as signed, so an id is stored minus 2^63 and shown plus it.
_ID_SHIFT = 1 << 63
# Quantities sit right-aligned and sort biggest first on the first click.
_QUANTITIES = (COL_WEMS, COL_SIZE, COL_DURATION)
_NAME_COLUMN_MIN = 280
_TAGS_COLUMN_MIN = 80
_TAG_TEXT_LIMIT = 160
# The child marking a row whose wem locations are not built yet.
_PLACEHOLDER = "..."
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _format_id(value):
    return str(value + _ID_SHIFT)


_FORMATTERS = {COL_ID: _format_id, COL_WEMS: format_count, COL_SIZE: format_size,
               COL_DURATION: format_duration}


# Qt sorts an empty cell above every number in a descending sort, so the blanks hold 0 or -1 instead.
def _set_numbers(item, item_id=None, wems=0, size=0, duration=-1):
    if item_id is not None:
        item.setData(COL_ID, DISPLAY_ROLE, item_id - _ID_SHIFT)
    item.setData(COL_WEMS, DISPLAY_ROLE, wems)
    item.setData(COL_SIZE, DISPLAY_ROLE, size)
    item.setData(COL_DURATION, DISPLAY_ROLE, duration)


class _NumberDelegate(QStyledItemDelegate):
    def __init__(self, formatter, align_right, parent):
        super().__init__(parent)
        self.formatter = formatter
        self.align_right = align_right

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        if self.align_right:
            option.displayAlignment = _RIGHT

    def displayText(self, value, locale):
        return self.formatter(value) if isinstance(value, int) else ""


class ResultsTree(QTreeWidget):
    # Enter, Space and Ctrl+Left or Ctrl+Right, which the window turns into playback.
    play_requested = pyqtSignal()
    toggle_requested = pyqtSignal()
    step_requested = pyqtSignal(int)

    def __init__(self, saved_widths=None):
        super().__init__()
        self.model = None
        self.lookup_item = None
        self._shown = None
        self._type_icons = {}
        self._fitting_columns = False
        self.setHeaderLabels(COLUMNS)
        header = self.header()
        # Interactive everywhere: a Stretch section would absorb the space but refuse the user's drag.
        # So the fitting is done by hand in _on_section_resized.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setCascadingSectionResizes(False)
        header.setMinimumSectionSize(40)
        saved = saved_widths or []
        for i, width in enumerate(DEFAULT_WIDTHS):
            self.setColumnWidth(i, saved[i] if i < len(saved) and saved[i] > 20 else width)
        header.sectionResized.connect(self._on_section_resized)
        # Sorted by hand: with setSortingEnabled every row added, as on an expansion, re-sorts the whole list.
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSortIndicatorClearable(True)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.sortIndicatorChanged.connect(self._on_sort_changed)
        head = self.headerItem()
        for column, formatter in _FORMATTERS.items():
            quantity = column in _QUANTITIES
            self.setItemDelegateForColumn(column, _NumberDelegate(formatter, quantity, self))
            if quantity:
                head.setTextAlignment(column, _RIGHT)
                head.setData(column, Qt.ItemDataRole.InitialSortOrderRole, Qt.SortOrder.DescendingOrder.value)
        self.viewport().installEventFilter(self)
        self.setFont(QFont("Segoe UI", 9))
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setRootIsDecorated(True)
        # A double click plays the row, and the arrow still expands it.
        self.setExpandsOnDoubleClick(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.itemExpanded.connect(self._fill_children)

    def column_widths(self):
        return [self.columnWidth(i) for i in range(self.columnCount())]

    def eventFilter(self, obj, event):
        if obj is self.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_name_column()
        return super().eventFilter(obj, event)

    # A dragged border follows the mouse and the tags column absorbs the difference, down to its minimum.
    # When the table already overflows, narrowing a column reduces the overflow instead.
    def _on_section_resized(self, section, old, new):
        if self._fitting_columns:
            return
        last = self.columnCount() - 1
        if section == last:
            return
        self._fitting_columns = True
        try:
            tags = self.columnWidth(last)
            leftover = self.viewport().width() - sum(self.columnWidth(i) for i in range(last))
            wanted = min(tags - (new - old), max(tags, leftover))
            self.setColumnWidth(last, max(_TAGS_COLUMN_MIN, wanted))
        finally:
            self._fitting_columns = False

    def _fit_name_column(self):
        if self._fitting_columns:
            return
        self._fitting_columns = True
        try:
            others = sum(self.columnWidth(i) for i in range(1, self.columnCount()))
            self.setColumnWidth(COL_NAME, max(_NAME_COLUMN_MIN, self.viewport().width() - others))
        finally:
            self._fitting_columns = False

    # Space, Enter and Ctrl+arrows would otherwise select, activate and collapse.
    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if modifiers == Qt.KeyboardModifier.NoModifier:
            if key == Qt.Key.Key_Space:
                self.toggle_requested.emit()
                return
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.is_playable(self.playback_item()):
                self.play_requested.emit()
                return
        elif modifiers == Qt.KeyboardModifier.ControlModifier and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            self.step_requested.emit(-1 if key == Qt.Key.Key_Left else 1)
            return
        super().keyPressEvent(event)

    # Rebuilding beats hiding rows one by one: 0.1 s against 1.4 s on 111k rows.
    def show_selection(self, model, selection, character=""):
        started = time.time()
        self.model = model
        self._shown = (model, selection, character)
        self.setUpdatesEnabled(False)
        self.lookup_item = None
        self.clear()
        items = []
        for lookup_id in selection.lookups:
            item = self._lookup_item(lookup_id)
            if item is None:
                continue
            items.append(item)
            if self.lookup_item is None:
                self.lookup_item = item
        rows = selection.sync_rows + selection.rows
        if character:
            tagged = model.tagged_row(character, rows)
            if tagged is not None:
                rows = rows + [tagged]
            groups = self._group_by_action(rows)
            items.extend(groups[key] for key in sorted(groups, key=action_sort_key))
        else:
            items.extend(self._row_item(row) for row in rows)
            groups = {}
        self.addTopLevelItems(items)
        for node in groups.values():
            node.setExpanded(True)
        if self.lookup_item is not None:
            self.lookup_item.setExpanded(True)
        self._sort()
        self.setUpdatesEnabled(True)
        if os.environ.get("HSI_TIMING"):
            print(f"[timing] tree with {len(items)} rows: {time.time() - started:.2f}s", flush=True)

    # The whole tree, or the children of one item, by the column the header shows.
    def _sort(self, item=None):
        header = self.header()
        column = header.sortIndicatorSection()
        if column < 0:
            return
        if item is None:
            self.sortItems(column, header.sortIndicatorOrder())
        else:
            item.sortChildren(column, header.sortIndicatorOrder())

    # The third click on a header clears the sort, and the list goes back to the resolve order.
    def _on_sort_changed(self, column, _order):
        if column >= 0:
            self._sort()
        elif self._shown is not None:
            self.show_selection(*self._shown)

    def _group_by_action(self, rows):
        groups, totals = {}, {}
        for row in rows:
            key = row.action
            node = groups.get(key)
            if node is None:
                node = QTreeWidgetItem([action_label(key) if key else "(other)"])
                bold = node.font(COL_NAME)
                bold.setBold(True)
                node.setFont(COL_NAME, bold)
                groups[key] = node
                totals[key] = [0, 0, 0, -1]
            node.addChild(self._row_item(row))
            total = totals[key]
            total[0] += 1
            total[1] += len(row.match.wem_ids)
            total[2] += row.size
            total[3] = max(total[3], row.duration)
        for key, (count, wems, size, duration) in totals.items():
            groups[key].setText(COL_TYPE, f"{count} rows")
            _set_numbers(groups[key], None, wems, size, duration)
        return groups

    def _row_item(self, row):
        m = row.match
        item = QTreeWidgetItem([m.name, m.kind, "", row.language])
        _set_numbers(item, m.hash_id, len(m.wem_ids), row.size, row.duration)
        item.setIcon(COL_NAME, self._type_icon(row.bucket))
        item.setData(COL_NAME, USER_ROLE, row)
        self._set_tags(item, row.tag_text)
        if m.wem_ids:
            item.addChild(QTreeWidgetItem([_PLACEHOLDER]))
        return item

    def _lookup_item(self, lookup_id):
        index = self.model.index
        locations = index.locations_of(lookup_id)
        if locations:
            item = QTreeWidgetItem([f"(wem {lookup_id})", "WEM"])
            _set_numbers(item, lookup_id, 1, locations[0].size, index.wem_durations.get(lookup_id, -1))
            tag_text = self.model.tags.wem_text(lookup_id)
            self._set_tags(item, tag_text)
            for location in locations:
                item.addChild(self._location_item(location, lookup_id, tag_text))
            return item
        if lookup_id in self.model.wems_by_bank:
            wems = sorted(self.model.wems_by_bank[lookup_id])
            item = QTreeWidgetItem([f"(bnk {lookup_id})", "BNK"])
            _set_numbers(item, lookup_id, len(wems))
            for wem_id in wems:
                tag_text = self.model.tags.wem_text(wem_id)
                for location in index.locations_of(wem_id):
                    item.addChild(self._location_item(location, wem_id, tag_text))
            return item
        return None

    def _location_item(self, location, wem_id, tag_text):
        item = QTreeWidgetItem([location.label(), "wem", "", location.lang])
        _set_numbers(item, wem_id, 0, location.size, self.model.index.wem_durations.get(wem_id, -1))
        item.setData(COL_NAME, USER_ROLE, location)
        self._set_tags(item, tag_text)
        return item

    # Every wem gets its rows, since the biggest row, 28k wems on ZZZ, builds in 0.4 s.
    def _fill_children(self, item):
        started = time.time()
        row = item.data(COL_NAME, USER_ROLE)
        if not isinstance(row, ResultRow) or item.childCount() != 1 or item.child(0).text(COL_NAME) != _PLACEHOLDER:
            return
        item.takeChildren()
        wem_ids = row.match.wem_ids
        for wem_id in wem_ids:
            tag_text = self.model.tags.wem_text(wem_id)
            locations = self.model.index.locations_of(wem_id)
            if not locations:
                child = QTreeWidgetItem(["(not found in pcks)", "wem"])
                _set_numbers(child, wem_id)
                self._set_tags(child, tag_text)
                item.addChild(child)
                continue
            for location in locations:
                item.addChild(self._location_item(location, wem_id, tag_text))
        self._sort(item)
        if os.environ.get("HSI_TIMING"):
            print(f"[timing] expanding {len(wem_ids)} wems: {time.time() - started:.3f}s", flush=True)

    def _set_tags(self, item, text):
        if not text:
            return
        if len(text) > _TAG_TEXT_LIMIT:
            item.setText(COL_TAGS, text[:_TAG_TEXT_LIMIT - 1] + "…")
            item.setToolTip(COL_TAGS, text.replace("; ", "\n"))
        else:
            item.setText(COL_TAGS, text)

    def _type_icon(self, bucket):
        icon = self._type_icons.get(bucket)
        if icon is None:
            icon = type_dot(BUCKET_COLORS.get(bucket, BUCKET_COLORS["sync"]))
            self._type_icons[bucket] = icon
        return icon

    @staticmethod
    def item_id(item):
        value = item.data(COL_ID, DISPLAY_ROLE)
        return value + _ID_SHIFT if isinstance(value, int) else None

    def _id_text(self, item):
        item_id = self.item_id(item)
        return "" if item_id is None else str(item_id)

    def cell_text(self, item, column):
        formatter = _FORMATTERS.get(column)
        if formatter is None:
            return item.text(column)
        value = item.data(column, DISPLAY_ROLE)
        return formatter(value) if isinstance(value, int) else ""

    # The current item when selected, else the first selected one.
    def playback_item(self):
        item = self.currentItem()
        if item is not None and item.isSelected():
            return item
        selected = self.selectedItems()
        return selected[0] if selected else None

    def is_playable(self, item):
        if item is None or self.model is None:
            return False
        data = item.data(COL_NAME, USER_ROLE)
        if isinstance(data, WemLocation):
            return True
        if isinstance(data, ResultRow):
            index = self.model.index
            return any(wem_id in index.wem_locations or wem_id in index.external_locations
                       for wem_id in data.match.wem_ids)
        return item.text(COL_TYPE) in ("WEM", "BNK") and item.childCount() > 0

    # (wem id, location) per wem, the biggest copy of each, and the position to start from.
    # A row starts from its biggest wem, a wem row from its own copy, which may be a bank's short stub.
    def playback_queue(self, item):
        if not self.is_playable(item):
            return [], -1
        data = item.data(COL_NAME, USER_ROLE)
        if isinstance(data, ResultRow):
            queue = self._queue_of(data.match.wem_ids)
            return queue, _biggest(queue)
        if isinstance(data, WemLocation):
            wem_id = self.item_id(item)
            parent = item.parent()
            owner = parent.data(COL_NAME, USER_ROLE) if parent is not None else None
            if isinstance(owner, ResultRow):
                queue = self._queue_of(owner.match.wem_ids)
            else:
                queue = self._queue_of(self._child_wem_ids(parent)) if parent is not None else []
            for position, (queued_id, _location) in enumerate(queue):
                if queued_id == wem_id:
                    queue[position] = (wem_id, data)
                    return queue, position
            return [(wem_id, data)], 0
        queue = self._queue_of(self._child_wem_ids(item))
        return queue, _biggest(queue)

    def _queue_of(self, wem_ids):
        queue = []
        for wem_id in wem_ids:
            locations = self.model.index.locations_of(wem_id)
            if locations:
                queue.append((wem_id, locations[0]))
        return queue

    def _child_wem_ids(self, item):
        return list(dict.fromkeys(self.item_id(child) for child, _location in self._child_locations(item)))

    # A row never expanded has no location children built yet.
    @staticmethod
    def _child_locations(item):
        for i in range(item.childCount()):
            child = item.child(i)
            data = child.data(COL_NAME, USER_ROLE)
            if isinstance(data, WemLocation):
                yield child, data

    @staticmethod
    def _child_rows(item):
        for i in range(item.childCount()):
            data = item.child(i).data(COL_NAME, USER_ROLE)
            if isinstance(data, ResultRow):
                yield data

    # A cheap upper bound for the button label that never touches the index.
    def selected_export_count(self):
        total = 0
        for item in self.selectedItems():
            data = item.data(COL_NAME, USER_ROLE)
            if isinstance(data, WemLocation):
                total += 1
            elif isinstance(data, ResultRow):
                total += len(data.match.wem_ids)
            else:
                total += sum(1 for _ in self._child_locations(item))
                total += sum(len(row.match.wem_ids) for row in self._child_rows(item))
        return total

    # A wem row exports that exact location, a result row the biggest location of each of its wems.
    def selected_export_jobs(self):
        if self.model is None:
            return []
        index = self.model.index
        jobs, seen = [], set()

        def add_location(stem, location):
            key = (stem, location.file_path, location.offset, location.size)
            if key not in seen:
                seen.add(key)
                jobs.append((stem, location))

        def add_row(row):
            prefix = f"{safe_file_stem(row.match.name)}_" if row.match.name else ""
            for wem_id in row.match.wem_ids:
                locations = index.locations_of(wem_id)
                if locations:
                    add_location(f"{prefix}{wem_id}", locations[0])

        for item in self.selectedItems():
            data = item.data(COL_NAME, USER_ROLE)
            if isinstance(data, WemLocation):
                add_location(self._id_text(item) or "wem", data)
            elif isinstance(data, ResultRow):
                add_row(data)
            else:
                for child, location in self._child_locations(item):
                    add_location(self._id_text(child) or "wem", location)
                for row in self._child_rows(item):
                    add_row(row)
        return jobs

    def selected_ids(self):
        return [self._id_text(item) for item in self.selectedItems()]

    def selected_wem_ids(self):
        out = []
        for item in self.selectedItems():
            data = item.data(COL_NAME, USER_ROLE)
            if isinstance(data, WemLocation):
                out.append(self._id_text(item))
            elif isinstance(data, ResultRow):
                out.extend(str(wem_id) for wem_id in data.match.wem_ids)
            else:
                if item.text(COL_TYPE) == "WEM":
                    out.append(self._id_text(item))
                out.extend(self._id_text(child) for child, _location in self._child_locations(item))
        return out

    def selected_bank_ids(self):
        out = []
        for item in self.selectedItems():
            data = item.data(COL_NAME, USER_ROLE)
            if isinstance(data, WemLocation):
                out.append(str(data.bnk_id or ""))
            elif isinstance(data, ResultRow):
                if data.match.kind == Kind.BANK:
                    out.append(str(data.match.hash_id))
                if self.model is not None:
                    for wem_id in data.match.wem_ids:
                        out.extend(str(location.bnk_id) for location in self.model.index.locations_of(wem_id)
                                   if location.bnk_id)
            elif item.text(COL_TYPE) == "BNK":
                out.append(self._id_text(item))
        return out

    def selected_names(self):
        out = []
        for item in self.selectedItems():
            data = item.data(COL_NAME, USER_ROLE)
            out.append(data.match.name if isinstance(data, ResultRow) else item.text(COL_NAME))
        return out

    def selected_rows_text(self):
        columns = range(self.columnCount())
        return ["\t".join(self.cell_text(item, c) for c in columns) for item in self.selectedItems()]

    def tags_of_item(self, item):
        if self.model is None or item is None:
            return []
        data = item.data(COL_NAME, USER_ROLE)
        if isinstance(data, ResultRow):
            return self.model.tags.tags_of_match(data.match)
        wem_id = self.item_id(item)
        if isinstance(data, WemLocation) and wem_id is not None:
            return self.model.tags.tags_of_wem(wem_id)
        return []


def _biggest(queue):
    if not queue:
        return -1
    return max(range(len(queue)), key=lambda position: queue[position][1].size)
