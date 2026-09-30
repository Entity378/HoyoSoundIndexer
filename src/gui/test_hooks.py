# HSI_* environment switches drive the GUI headless: HSI_AUTOSCAN, HSI_SCREENSHOT, HSI_CHARACTER and more.
# Headless runs need QT_QPA_PLATFORM=offscreen and QT_QPA_FONTDIR=C:/Windows/Fonts, or text renders as boxes.

import os

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractItemView, QApplication

from src.model import WemLocation
from src.gui.results import TYPE_BUCKETS
from src.gui.results_tree import COL_NAME, USER_ROLE, ResultRow


def _screenshot_and_quit(window):
    window.grab().save(os.environ["HSI_SCREENSHOT"])
    QApplication.instance().quit()


def on_startup(app, window):
    if os.environ.get("HSI_TAB"):
        window.tabs.setCurrentIndex(int(os.environ["HSI_TAB"]))
    if os.environ.get("HSI_AUTOSCAN"):
        QTimer.singleShot(300, window.start_scan)
    elif os.environ.get("HSI_AUTOHARVEST"):
        window.tabs.setCurrentIndex(1)
        QTimer.singleShot(300, window.generate_tab.start_harvest)
    elif os.environ.get("HSI_SCREENSHOT"):
        QTimer.singleShot(1500, lambda: _screenshot_and_quit(window))


def after_harvest(window):
    if os.environ.get("HSI_SCREENSHOT") and os.environ.get("HSI_AUTOHARVEST"):
        QTimer.singleShot(800, lambda: _screenshot_and_quit(window))


def after_scan(window):
    if os.environ.get("HSI_CLICKALL"):
        _click_every_filter(window)
    if os.environ.get("HSI_SCREENSHOT"):
        _screenshot_after_scan(window)


# Every type bucket with every character, to catch combinations a single screenshot never reaches.
def _click_every_filter(window):
    failures = 0
    characters = ["", *sorted(window.rail.character_rows)]
    for bucket, _label in TYPE_BUCKETS:
        for character in characters:
            window.rail.active_bucket = bucket
            window.rail.active_character = character
            try:
                window.apply_filter()
            except Exception as e:
                failures += 1
                print(f"[clickall] {bucket} / {character or 'all'}: {e!r}", flush=True)
    print(f"[clickall] {len(TYPE_BUCKETS)} buckets x {len(window.rail.character_rows)} characters, "
          f"{failures} failures", flush=True)
    QApplication.instance().quit()


def _screenshot_after_scan(window):
    wanted = os.environ.get("HSI_CHARACTER")
    if wanted:
        window.rail.select_bucket("vo")
        window.rail.select_character(wanted)
        QTest.qWait(700)
        QTimer.singleShot(3000, lambda: _screenshot_and_quit(window))
        return
    typed = os.environ.get("HSI_FILTER")
    if typed:
        window.filter_edit.setFocus()
        QTest.keyClicks(window.filter_edit, typed)
        QTest.qWait(700)
        lookup = window.tree.lookup_item
        if lookup is not None and lookup.childCount():
            child = lookup.child(0)
            window.tree.setCurrentItem(child)
            if isinstance(child.data(COL_NAME, USER_ROLE), WemLocation):
                window.play_selected()
        QTimer.singleShot(4000, lambda: _screenshot_and_quit(window))
        return
    _play_first_wem(window)
    QTimer.singleShot(4000, lambda: _grab_after_checks(window))


def _play_first_wem(window):
    tree = window.tree
    for i in range(tree.topLevelItemCount()):
        row = tree.topLevelItem(i).data(COL_NAME, USER_ROLE)
        if isinstance(row, ResultRow) and row.match.wem_ids:
            window.filter_edit.setText(str(row.match.wem_ids[0]))
            window.apply_filter()
            # apply_filter rebuilds the tree, so the item read above is already destroyed.
            item = tree.topLevelItem(0)
            if item is None:
                return
            tree.expandItem(item)
            if item.childCount():
                child = item.child(0)
                tree.setCurrentItem(child)
                tree.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtTop)
                if isinstance(child.data(COL_NAME, USER_ROLE), WemLocation):
                    window.play_selected()
            return


def _grab_after_checks(window):
    if os.environ.get("HSI_COLTEST"):
        header = window.tree.header()
        before = window.tree.column_widths()
        header.resizeSection(1, before[1] + 120)
        print("widths before:", before, "after:", window.tree.column_widths(), flush=True)
    if os.environ.get("HSI_CLICKTEST"):
        slider = window.seek_bar.seek_slider
        QTest.mouseClick(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(int(slider.width() * 0.8), slider.height() // 2))
        QTest.qWait(400)
    _screenshot_and_quit(window)
