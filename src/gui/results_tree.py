# The result tree: rows, or one node per action for a character, with wem locations built on expansion.

import os
import time

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QTreeWidget, QTreeWidgetItem

from src.audio import safe_file_stem
from src.characters import action_label, action_sort_key
from src.model import Kind, WemLocation
from src.gui.results import MAX_ID_LOOKUPS, ResultRow
from src.gui.theme import BUCKET_COLORS, type_dot

# Items carry their ResultRow or WemLocation under this role, action nodes nothing.
USER_ROLE = Qt.ItemDataRole.UserRole
COLUMNS = ["Name / Source", "Type", "ID", "Language", "Size", "Tags"]
COL_NAME, COL_TYPE, COL_ID, COL_LANGUAGE, COL_SIZE, COL_TAGS = range(len(COLUMNS))
DEFAULT_WIDTHS = [560, 116, 116, 70, 62, 240]
_NAME_COLUMN_MIN = 280
_TAGS_COLUMN_MIN = 80
_TAG_TEXT_LIMIT = 160
_MAX_EXPANDED_WEMS = 500
# The child marking a row whose wem locations are not built yet.
_PLACEHOLDER = "..."


class ResultsTree(QTreeWidget):
    def __init__(self, saved_widths=None):
        super().__init__()
        self.model = None
        self.lookup_item = None
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
        self.viewport().installEventFilter(self)
        self.setFont(QFont("Segoe UI", 9))
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setRootIsDecorated(True)
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

    # Rebuilding beats hiding rows one by one: 0.1 s against 1.4 s on 111k rows.
    def show_selection(self, model, selection, character=""):
        started = time.time()
        self.model = model
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
        self.setUpdatesEnabled(True)
        if os.environ.get("HSI_TIMING"):
            print(f"[timing] tree with {len(items)} rows: {time.time() - started:.2f}s", flush=True)

    def _group_by_action(self, rows):
        groups, stats = {}, {}
        for row in rows:
            key = row.action
            node = groups.get(key)
            if node is None:
                node = QTreeWidgetItem([action_label(key) if key else "(other)", "", "", "", ""])
                bold = node.font(COL_NAME)
                bold.setBold(True)
                node.setFont(COL_NAME, bold)
                groups[key] = node
                stats[key] = [0, 0]
            node.addChild(self._row_item(row))
            stats[key][0] += 1
            stats[key][1] += len(row.match.wem_ids)
        for key, (count, wems) in stats.items():
            groups[key].setText(COL_TYPE, f"{count} rows")
            groups[key].setText(COL_SIZE, f"{wems} wems")
        return groups

    def _row_item(self, row):
        m = row.match
        item = QTreeWidgetItem([m.name, m.kind, str(m.hash_id), row.language,
                                f"{len(m.wem_ids)} wems" if m.wem_ids else ""])
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
            item = QTreeWidgetItem([f"(wem {lookup_id})", "WEM", str(lookup_id), "", f"{len(locations)} loc"])
            tag_text = self.model.tags.wem_text(lookup_id)
            self._set_tags(item, tag_text)
            for location in locations:
                item.addChild(self._location_item(location, lookup_id, tag_text))
            return item
        if lookup_id in self.model.wems_by_bank:
            wems = sorted(self.model.wems_by_bank[lookup_id])
            item = QTreeWidgetItem([f"(bnk {lookup_id})", "BNK", str(lookup_id), "", f"{len(wems)} wems"])
            for wem_id in wems[:MAX_ID_LOOKUPS]:
                tag_text = self.model.tags.wem_text(wem_id)
                for location in index.locations_of(wem_id):
                    item.addChild(self._location_item(location, wem_id, tag_text))
            return item
        return None

    def _location_item(self, location, wem_id, tag_text):
        item = QTreeWidgetItem([location.label(), "wem", str(wem_id), location.lang, f"{location.size:,}"])
        item.setData(COL_NAME, USER_ROLE, location)
        self._set_tags(item, tag_text)
        return item

    def _fill_children(self, item):
        started = time.time()
        row = item.data(COL_NAME, USER_ROLE)
        if not isinstance(row, ResultRow) or item.childCount() != 1 or item.child(0).text(COL_NAME) != _PLACEHOLDER:
            return
        item.takeChildren()
        wem_ids = row.match.wem_ids
        for wem_id in wem_ids[:_MAX_EXPANDED_WEMS]:
            tag_text = self.model.tags.wem_text(wem_id)
            locations = self.model.index.locations_of(wem_id)
            if not locations:
                child = QTreeWidgetItem(["(not found in pcks)", "wem", str(wem_id), "", ""])
                self._set_tags(child, tag_text)
                item.addChild(child)
                continue
            for location in locations:
                item.addChild(self._location_item(location, wem_id, tag_text))
        if len(wem_ids) > _MAX_EXPANDED_WEMS:
            item.addChild(QTreeWidgetItem([f"... {len(wem_ids) - _MAX_EXPANDED_WEMS} more wems", "", "", "", ""]))
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

    # Playback takes the first wem location of the selection, since the other rows may be events.
    def selected_location(self):
        for item in self.selectedItems():
            data = item.data(COL_NAME, USER_ROLE)
            if isinstance(data, WemLocation):
                return data
        return None

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
                add_location(item.text(COL_ID) or "wem", data)
            elif isinstance(data, ResultRow):
                add_row(data)
            else:
                for child, location in self._child_locations(item):
                    add_location(child.text(COL_ID) or "wem", location)
                for row in self._child_rows(item):
                    add_row(row)
        return jobs

    def selected_ids(self):
        return [item.text(COL_ID) for item in self.selectedItems()]

    def selected_wem_ids(self):
        out = []
        for item in self.selectedItems():
            data = item.data(COL_NAME, USER_ROLE)
            if isinstance(data, WemLocation):
                out.append(item.text(COL_ID))
            elif isinstance(data, ResultRow):
                out.extend(str(wem_id) for wem_id in data.match.wem_ids)
            else:
                if item.text(COL_TYPE) == "WEM":
                    out.append(item.text(COL_ID))
                out.extend(child.text(COL_ID) for child, _location in self._child_locations(item))
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
                out.append(item.text(COL_ID))
        return out

    def selected_names(self):
        out = []
        for item in self.selectedItems():
            data = item.data(COL_NAME, USER_ROLE)
            out.append(data.match.name if isinstance(data, ResultRow) else item.text(COL_NAME))
        return out

    def selected_rows_text(self):
        columns = range(self.columnCount())
        return ["\t".join(item.text(c) for c in columns) for item in self.selectedItems()]

    def tags_of_item(self, item):
        if self.model is None or item is None:
            return []
        data = item.data(COL_NAME, USER_ROLE)
        if isinstance(data, ResultRow):
            return self.model.tags.tags_of_match(data.match)
        if isinstance(data, WemLocation) and item.text(COL_ID).isdigit():
            return self.model.tags.tags_of_wem(int(item.text(COL_ID)))
        return []
