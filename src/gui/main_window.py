# The main window wires the pieces together and owns the workflows: scan, online update, exports, copies.

import os
import time
from pathlib import Path
from typing import NamedTuple

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QProgressBar, QPushButton, QRadioButton, QTabWidget, QVBoxLayout, QWidget,
)

from src.audio import export_wems, extract_wem_bytes
from src.config import APP_NAME, load_config, save_config
from src.durations import read_durations
from src.games import DEFAULT_GAME, GAME_ORDER, GAME_PROFILES, detect_game_from_path, locate_game_folder
from src.names_io import export_json, export_txt, read_names_file
from src.online.data import has_online_cache, load_online_data, read_online_cache
from src.online.sources import source_for, source_short_name
from src.pipeline import Cancelled, resolve_all_matches
from src.resolve_cache import load_saved_resolve, resolve_key, restore_resolve, save_resolve
from src.scan import scan_folder
from src.gui import test_hooks
from src.gui.filter_rail import FilterRail
from src.gui.generate_tab import GenerateTab
from src.gui.player import Player, SeekBar
from src.gui.results import ResultModel
from src.gui.results_tree import ResultsTree
from src.gui.tasks import Task, running
from src.gui.theme import media_icon
from src.gui.widgets import SourceBox

_WINDOW_SIZE = (1180, 760)
_DEFAULT_VOLUME = 80
_FILTER_DELAY_MS = 250
# Long enough that holding an arrow key converts only the row it stops on.
_AUTOPLAY_DELAY_MS = 180
_EXPORT_CONFIRM_FILES = 500
_CLOSE_WAIT_SECONDS = 3
_MENU_TAGS = 8
_FOLDER_PLACEHOLDER = "game root folder works too — pck files are found recursively (Persistent included)"
_SEARCH_PLACEHOLDER = "search by name, by tag (state/switch name or id) or by ID — event/bank/wem id"
_CRACK_TOOLTIP = ("Deep name cracking (a minute or two): tags, event families, context.\n"
                  "The result is saved per game and comes back in seconds on the next scans,\n"
                  "until the game files, the name sources or the tool change.")
_PLAY_TOOLTIP = "Play or pause the selection (Space).\nEnter or a double click plays it from the start."
_AUTOPLAY_TOOLTIP = "Play each row as it gets selected, so the arrow keys browse the sounds"


# restored is the date of the cracked names brought back from the cache, empty when resolved now.
class ScanOutcome(NamedTuple):
    index: object
    result: object
    model: object
    restored: str


# Runs in the scan task, which also reads the names file there, once: an export runs to 200 MB.
def _scan_and_resolve(game, folder, names_path, extra_names, online, harvested_voice, crack, progress,
                      cancelled):
    names, export = [], None
    if names_path:
        try:
            names, export = read_names_file(names_path)
        except Exception as e:
            raise RuntimeError(f"Cannot read names file: {e}") from e
    names = list(dict.fromkeys(names + extra_names))
    index = scan_folder(folder, progress=progress, cancel=cancelled)
    if cancelled():
        raise Cancelled()
    index.wem_durations = read_durations(index, game, progress=progress, cancel=cancelled)
    if cancelled():
        raise Cancelled()
    saved = key = None
    if crack:
        progress(0, 1, "Looking for saved cracked names...")
        key = resolve_key(index, names, names_path, online, harvested_voice)
        saved = load_saved_resolve(game, key)
    if saved is not None:
        result = restore_resolve(index, saved, folder, progress=progress, cancel=cancelled)
    else:
        result = resolve_all_matches(index, names, folder, export=export, online=online,
                                     harvested_voice=harvested_voice, progress=progress, cancel=cancelled,
                                     crack=crack)
        if crack:
            progress(0, 1, "Saving the cracked names...")
            save_resolve(game, key, result, index)
    progress(0, 1, "Preparing the result list...")
    return ScanOutcome(index, result, ResultModel(index, result.matches), saved.saved_on() if saved else "")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(*_WINDOW_SIZE)
        self.config = load_config()
        self.index = None
        self.model = None
        self.matches = []
        self.unmatched = []
        self.scan_root = ""
        self.names_path = self.config.get("names", "")
        # The Generate tab's HarvestResult and the current game's OnlineData.
        self.harvest = None
        self.online = None
        self.scan_task = None
        self.online_task = None
        self.export_task = None
        self._suppress_folder_detect = False
        self.filter_timer = QTimer(self)
        self.filter_timer.setSingleShot(True)
        self.filter_timer.setInterval(_FILTER_DELAY_MS)
        self.filter_timer.timeout.connect(self.apply_filter)
        volume = int(self.config.get("volume", _DEFAULT_VOLUME))
        self.player = Player(volume, self)
        self.player.state_changed.connect(self._on_playback_state)
        self.player.queue_changed.connect(self._on_queue_changed)
        self.autoplay_timer = QTimer(self)
        self.autoplay_timer.setSingleShot(True)
        self.autoplay_timer.setInterval(_AUTOPLAY_DELAY_MS)
        self.autoplay_timer.timeout.connect(self._autoplay)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        search = QWidget()
        self.tabs.addTab(search, "Search")
        layout = QVBoxLayout(search)
        layout.addLayout(self._build_game_row())
        layout.addLayout(self._build_sources_row())
        layout.addLayout(self._build_body(), 1)
        self.seek_bar = SeekBar(self.player, volume)
        self.seek_bar.volume_changed.connect(self._on_volume_changed)
        layout.addWidget(self.seek_bar)
        layout.addLayout(self._build_buttons_row())
        self._build_status_bar()
        self.player.message.connect(self.message_label.setText)
        if not self.player.vgmstream:
            self.message_label.setText("vgmstream missing: it downloads on first play")

        self.generate_tab = GenerateTab(self.config, self.current_game)
        self.generate_tab.progressed.connect(self.on_progress)
        self.generate_tab.status.connect(self.status_label.setText)
        self.generate_tab.finished.connect(self._on_harvest_finished)
        self.generate_tab.harvested.connect(self._on_harvested)
        self.tabs.addTab(self.generate_tab, "Generate names")

        game = self.config.get("game")
        if game not in GAME_PROFILES:
            game = detect_game_from_path(self.folder_edit.text()) or DEFAULT_GAME
        self.game = game
        self.game_buttons[game].setChecked(True)
        self.harvest_box.value.setText("nothing harvested yet")
        self.generate_tab.restore_saved(game)
        self.file_box.value.setText(Path(self.names_path).name or "no file loaded")
        self.file_box.check.setChecked(bool(self.names_path))
        self._sync_game_ui()
        self.generate_tab.follow_install(self.folder_edit.text().strip(), game)

    def _build_game_row(self):
        row = QHBoxLayout()
        self.game_buttons = {}
        self.game_group = QButtonGroup(self)
        for game in GAME_ORDER:
            profile = GAME_PROFILES[game]
            radio = QRadioButton(profile.label)
            radio.setToolTip(f"{game}: .blk decryption {profile.blk_format}")
            radio.clicked.connect(lambda _checked=False, g=game: self._on_game_clicked(g))
            self.game_group.addButton(radio)
            self.game_buttons[game] = radio
            row.addWidget(radio)
        row.addSpacing(10)
        self.folder_edit = QLineEdit(self.config.get("folder", ""))
        self.folder_edit.setPlaceholderText(_FOLDER_PLACEHOLDER)
        self.folder_edit.textChanged.connect(self._on_folder_changed)
        row.addWidget(self.folder_edit, 1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self.pick_folder)
        row.addWidget(browse_button)
        self.crack_check = QCheckBox("Crack")
        self.crack_check.setChecked(bool(self.config.get("crack", True)))
        self.crack_check.setToolTip(_CRACK_TOOLTIP)
        row.addWidget(self.crack_check)
        self.scan_button = QPushButton("Scan")
        self.scan_button.setDefault(True)
        self.scan_button.clicked.connect(self.start_scan)
        row.addWidget(self.scan_button)
        return row

    def _build_sources_row(self):
        row = QHBoxLayout()
        row.setSpacing(7)
        self.harvest_box = SourceBox("Client harvest")
        self.harvest_box.check.setChecked(True)
        self.harvest_box.check.setToolTip("Use the names harvested in the \"Generate names\" tab")
        row.addWidget(self.harvest_box, 1)
        self.online_box = SourceBox("Online names", "", "Update")
        self.online_box.check.setChecked(True)
        self.online_box.button.clicked.connect(self.start_online_fetch)
        row.addWidget(self.online_box, 1)
        self.file_box = SourceBox("Names file", "txt / json", "Browse...")
        self.file_box.button.clicked.connect(self.pick_names)
        row.addWidget(self.file_box, 1)
        return row

    def _build_body(self):
        body = QHBoxLayout()
        body.setSpacing(7)
        self.rail = FilterRail()
        self.rail.changed.connect(self.apply_filter)
        body.addWidget(self.rail)
        right = QVBoxLayout()
        right.setSpacing(5)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("searchEdit")
        self.filter_edit.setPlaceholderText(_SEARCH_PLACEHOLDER)
        self.filter_edit.textChanged.connect(self.schedule_filter)
        search_row.addWidget(self.filter_edit, 1)
        right.addLayout(search_row)
        self.tree = ResultsTree(self.config.get("column_widths_v4"))
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.play_requested.connect(lambda: self.play_selected(restart=True))
        self.tree.toggle_requested.connect(self.toggle_playback)
        self.tree.step_requested.connect(self.player.step)
        self.tree.customContextMenuRequested.connect(self.on_tree_menu)
        copy_action = QAction("Copy IDs", self.tree)
        copy_action.setShortcut(QKeySequence.StandardKey.Copy)
        copy_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        copy_action.triggered.connect(self.copy_ids)
        self.tree.addAction(copy_action)
        right.addWidget(self.tree, 1)
        body.addLayout(right, 1)
        return body

    def _build_buttons_row(self):
        row = QHBoxLayout()
        color = self.palette().buttonText().color()
        self.icon_play = media_icon("play", color)
        self.icon_pause = media_icon("pause", color)
        self.icon_stop = media_icon("stop", color)
        self.play_button = QPushButton("Play")
        self.play_button.setIcon(self.icon_play)
        self.play_button.setToolTip(_PLAY_TOOLTIP)
        self.play_button.clicked.connect(lambda: self.play_selected())
        self.play_button.setEnabled(False)
        row.addWidget(self.play_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setIcon(self.icon_stop)
        self.stop_button.clicked.connect(self.player.stop)
        self.stop_button.setEnabled(False)
        row.addWidget(self.stop_button)
        self.previous_button = QPushButton()
        self.previous_button.setIcon(media_icon("previous", color))
        self.previous_button.setToolTip("Previous wem of the event (Ctrl+Left)")
        self.previous_button.clicked.connect(lambda: self.player.step(-1))
        self.previous_button.setEnabled(False)
        row.addWidget(self.previous_button)
        self.next_button = QPushButton()
        self.next_button.setIcon(media_icon("next", color))
        self.next_button.setToolTip("Next wem of the event (Ctrl+Right)")
        self.next_button.clicked.connect(lambda: self.player.step(1))
        self.next_button.setEnabled(False)
        row.addWidget(self.next_button)
        self.queue_label = QLabel("")
        row.addWidget(self.queue_label)
        row.addSpacing(6)
        self.autoplay_check = QCheckBox("Play on select")
        self.autoplay_check.setToolTip(_AUTOPLAY_TOOLTIP)
        self.autoplay_check.setChecked(bool(self.config.get("autoplay", False)))
        row.addWidget(self.autoplay_check)
        row.addSpacing(12)
        self.export_button = QPushButton("Export WEM...")
        self.export_button.setToolTip("Export the selected wems (ctrl/shift click selects more than one;\n"
                                      "selecting an event exports every wem it uses)")
        self.export_button.clicked.connect(self.export_selected)
        self.export_button.setEnabled(False)
        row.addWidget(self.export_button)
        self.copy_button = QPushButton("Copy IDs")
        self.copy_button.setToolTip("Copy the ID of the selected rows (Ctrl+C).\n"
                                    "Right click on the tree for the other copy options.")
        self.copy_button.clicked.connect(self.copy_ids)
        self.copy_button.setEnabled(False)
        row.addWidget(self.copy_button)
        self.export_names_button = QPushButton("Export names...")
        self.export_names_button.clicked.connect(self.export_names)
        self.export_names_button.setEnabled(False)
        row.addWidget(self.export_names_button)
        row.addStretch(1)
        self.message_label = QLabel("")
        row.addWidget(self.message_label)
        return row

    def _build_status_bar(self):
        bar = self.statusBar()
        self.status_label = QLabel("Ready")
        bar.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(12)
        self.progress.setFixedWidth(170)
        self.progress.setVisible(False)
        bar.addPermanentWidget(self.progress)
        self.detail_label = QLabel("")
        bar.addPermanentWidget(self.detail_label)

    def on_progress(self, done, total, message):
        self.progress.setVisible(True)
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.status_label.setText(message)

    def current_game(self):
        return self.game

    # A click on the game already selected changes nothing, where it used to throw the harvest away.
    def _on_game_clicked(self, game):
        if game == self.game:
            return
        folders = dict(self.config.get("folders") or {})
        current = self.folder_edit.text().strip()
        if current:
            folders[self.game] = current
        self.config["folders"] = folders
        target = folders.get(game) or locate_game_folder(game, near=current)
        self._set_game(game)
        # No install found: better an empty field than another game's path scanned as this game.
        self._suppress_folder_detect = True
        self.folder_edit.setText(target)
        self._suppress_folder_detect = False
        label = GAME_PROFILES[game].label
        self.folder_edit.setPlaceholderText(_FOLDER_PLACEHOLDER if target else f"select the {label} folder")
        if target:
            self.generate_tab.follow_install(target, game)
        else:
            self.generate_tab.forget_blocks_folder()

    # A path of another game switches to it, keeping the path as typed.
    def _on_folder_changed(self, text):
        if self._suppress_folder_detect:
            return
        detected = detect_game_from_path(text)
        if detected and detected != self.game:
            self._set_game(detected)
            self.game_buttons[detected].setChecked(True)
        self.generate_tab.follow_install(text.strip(), self.game)

    # Names and voices belong to the previous game, and keeping them would mix two games.
    def _set_game(self, game):
        self.game = game
        self.config["game"] = game
        self.harvest = None
        self.online = None
        self.harvest_box.value.setText("nothing harvested yet")
        self.generate_tab.reset_for_game(game)
        self._sync_game_ui()

    def _sync_game_ui(self):
        game = self.game
        profile = GAME_PROFILES[game]
        self.harvest_box.tag.setText(".blk " + profile.blk_format)
        has_online = source_for(game, self.config) is not None
        self.online_box.check.setEnabled(has_online)
        self.online_box.button.setEnabled(has_online and not running(self.online_task))
        if has_online:
            self.online_box.tag.setText(source_short_name(game))
            if self.online is not None:
                self.online_box.value.setText(self._online_summary())
            elif has_online_cache(game):
                self.online_box.value.setText("cached — loaded on Scan, Update to refresh")
            else:
                self.online_box.value.setText("not fetched yet — press Update")
        else:
            self.online_box.tag.setText("—")
            self.online_box.check.setChecked(False)
            self.online_box.value.setText("not needed — names are in the client")
        detail = ".blk " + profile.blk_format
        if has_online:
            detail += " · " + source_short_name(game)
        self.detail_label.setText(detail)

    def pick_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Game folder", self.folder_edit.text() or "")
        if path:
            self.folder_edit.setText(path)

    def pick_names(self):
        path, _filter = QFileDialog.getOpenFileName(self, "Names file", self.names_path or "",
                                                    "Names (*.txt *.json);;All files (*)")
        if path:
            self.names_path = path
            self.file_box.value.setText(Path(path).name)
            self.file_box.check.setChecked(True)

    def _on_harvested(self, result):
        self.harvest = result
        when = f" · {result.harvested}" if result.harvested else ""
        self.harvest_box.value.setText(f"{len(result.names):,} names{when}")
        self.harvest_box.check.setChecked(bool(result.names))

    def _on_harvest_finished(self):
        self.progress.setVisible(False)
        test_hooks.after_harvest(self)

    # An old cache has no roster, and saying so is how the user knows to press Update.
    def _online_summary(self):
        data = self.online
        avatars = len(data.avatar_names)
        note = f" · {avatars:,} avatar names" if avatars else " · no avatar names (Update)"
        return f"{len(data.voice_paths):,} voice · {len(data.id_names):,} labels" + note

    def _load_online_cache(self):
        self.online = read_online_cache(self.game)
        self._sync_game_ui()
        return self.online is not None

    def start_online_fetch(self):
        if running(self.online_task):
            return
        game = self.game
        if source_for(game, self.config) is None:
            return
        self.online_box.button.setEnabled(False)
        self.online_box.value.setText("downloading...")
        config = dict(self.config)
        task = Task(lambda progress, _cancelled: load_online_data(game, config=config, force=True,
                                                                  progress=progress))
        task.progressed.connect(self.on_progress)
        task.succeeded.connect(lambda data: self._on_online_ready(task, data))
        task.failed.connect(lambda message: self._on_online_failed(task, message))
        self.online_task = task.start()

    # A fresh download scans right away unless a scan is running.
    # One landing after a game switch has only refreshed that game's cache.
    def _on_online_ready(self, task, data):
        if task is not self.online_task:
            return
        self.progress.setVisible(False)
        self.online_box.button.setEnabled(True)
        if data.game != self.game:
            self._sync_game_ui()
            return
        self.online = data
        self.online_box.check.setChecked(True)
        self.online_box.value.setText(self._online_summary())
        folder = self.folder_edit.text().strip()
        if folder and Path(folder).is_dir() and not running(self.scan_task):
            self.start_scan()

    def _on_online_failed(self, task, message):
        if task is not self.online_task:
            return
        self.progress.setVisible(False)
        self.online_box.button.setEnabled(True)
        self.online_box.value.setText("download failed")
        QMessageBox.warning(self, APP_NAME, f"Could not fetch names:\n{message}")

    # Starts a scan with the ticked sources, or cancels the running one.
    def start_scan(self):
        if running(self.scan_task):
            self.scan_task.cancel()
            return
        folder = self.folder_edit.text().strip()
        # An empty field is not a folder, although Path("") is the current directory.
        if not folder or not Path(folder).is_dir():
            QMessageBox.warning(self, APP_NAME, "Select a valid folder")
            return
        names_path = self.names_path if self.file_box.check.isChecked() else ""
        if names_path and not Path(names_path).is_file():
            QMessageBox.warning(self, APP_NAME, "The selected names file no longer exists")
            return
        harvest = self.harvest if self.harvest_box.check.isChecked() else None
        online = None
        if self.online_box.check.isChecked():
            if self.online is None:
                self._load_online_cache()
            online = self.online
        if not names_path and harvest is None and online is None:
            QMessageBox.warning(self, APP_NAME,
                                "No name source: pick a names file, run a harvest, or fetch the online names")
            return
        # The names file first, then the harvest, then the online names: on a tie the first one wins.
        extra_names = (list(harvest.names) if harvest else []) + (list(online.names) if online else [])
        harvested_voice = harvest.voice if harvest is not None else None
        crack = self.crack_check.isChecked()

        folders = dict(self.config.get("folders") or {})
        folders[self.game] = folder
        self.config.update({"names": self.names_path, "folder": folder, "game": self.game,
                            "folders": folders, "crack": crack})
        save_config(self.config)
        self.scan_root = folder
        self.tree.clear()
        self.export_names_button.setEnabled(False)
        self.scan_button.setText("Cancel")
        game = self.game
        task = Task(lambda progress, cancelled: _scan_and_resolve(
            game, folder, names_path, extra_names, online, harvested_voice, crack, progress, cancelled))
        task.progressed.connect(self.on_progress)
        task.succeeded.connect(lambda outcome: self._on_scan_done(task, outcome))
        task.failed.connect(lambda message: self._on_scan_stopped(task, message, failed=True))
        task.cancelled.connect(lambda: self._on_scan_stopped(task, "Scan cancelled"))
        self.scan_task = task.start()

    def _on_scan_stopped(self, task, message, failed=False):
        if task is not self.scan_task:
            return
        self.progress.setVisible(False)
        self.scan_button.setText("Scan")
        self.status_label.setText(message)
        if failed:
            QMessageBox.warning(self, APP_NAME, message)

    # A superseded task's late result is dropped.
    def _on_scan_done(self, task, outcome):
        if task is not self.scan_task:
            return
        self.scan_button.setText("Scan")
        self.progress.setVisible(False)
        self.index, result, self.model = outcome.index, outcome.result, outcome.model
        self.matches, self.unmatched = result.matches, result.unmatched
        stats = self.index.stats
        restored = f" — cracked names restored from {outcome.restored}" if outcome.restored else ""
        self.status_label.setText(
            f"{stats['pck']:,} pck, {stats['objects']:,} objects, {stats['wem_ids']:,} wems — "
            f"matches: {len(self.matches):,}, unmatched: {len(self.unmatched):,}{restored}")
        self.export_names_button.setEnabled(bool(self.matches))
        self.rail.refresh(self.model)
        self.apply_filter()
        self._sync_game_ui()
        test_hooks.after_scan(self)

    def schedule_filter(self):
        self.filter_timer.start()

    def apply_filter(self):
        if self.model is None:
            return
        character = self.rail.active_character
        selection = self.model.filter(self.filter_edit.text(), self.rail.active_bucket, character,
                                      self.rail.wanted_languages(), self.rail.audio_only())
        self.tree.show_selection(self.model, selection, character)
        if len(selection.rows) != len(self.model.rows):
            self.status_label.setText(f"{len(selection.rows):,} of {len(self.model.rows):,} results shown")

    # Play toggles pause on the sound already playing, and Enter or a double click start it over.
    def play_selected(self, restart=False):
        started = time.time() if os.environ.get("HSI_TIMING") else None
        queue, position = self.tree.playback_queue(self.tree.playback_item())
        if position < 0 or (not restart and self.player.is_current(queue[position][1])):
            self.player.toggle()
        else:
            self.player.play(queue, position)
        if started is not None:
            print(f"[timing] play_selected (GUI thread): {time.time() - started:.3f}s", flush=True)

    def toggle_playback(self):
        if not self.player.toggle():
            self.play_selected()

    # A node without audio of its own, like a character's action, folds and unfolds instead.
    def _on_double_click(self, item, _column):
        if self.tree.is_playable(item):
            self.play_selected(restart=True)
        else:
            item.setExpanded(not item.isExpanded())

    def _on_selection_changed(self):
        self.update_buttons()
        if self.autoplay_check.isChecked():
            self.autoplay_timer.start()

    def _autoplay(self):
        queue, position = self.tree.playback_queue(self.tree.playback_item())
        if position >= 0 and not self.player.is_current(queue[position][1]):
            self.player.play(queue, position)

    def _on_queue_changed(self):
        player = self.player
        count = len(player.queue)
        self.queue_label.setText(f"{player.position + 1} / {count}" if count > 1 else "")
        self.previous_button.setEnabled(player.position > 0)
        self.next_button.setEnabled(0 <= player.position < count - 1)

    def _on_playback_state(self):
        if self.player.is_playing():
            self.play_button.setText("Pause")
            self.play_button.setIcon(self.icon_pause)
        else:
            self.play_button.setText("Play")
            self.play_button.setIcon(self.icon_play)
        self.stop_button.setEnabled(not self.player.is_stopped())
        self.update_buttons()

    def _on_volume_changed(self, value):
        self.config["volume"] = value

    def update_buttons(self):
        count = self.tree.selected_export_count()
        playable = self.tree.is_playable(self.tree.playback_item())
        self.play_button.setEnabled(playable or not self.player.is_stopped())
        self.export_button.setEnabled(count > 0 and not running(self.export_task))
        self.export_button.setText("Export WEM..." if count < 2 else f"Export {count:,} WEM...")
        self.copy_button.setEnabled(bool(self.tree.selectedItems()))

    # One wem opens a save dialog, more than one a folder written in the background.
    def export_selected(self):
        if running(self.export_task):
            return
        jobs = self.tree.selected_export_jobs()
        if not jobs:
            return
        if len(jobs) == 1:
            self._export_one(*jobs[0])
            return
        if len(jobs) > _EXPORT_CONFIRM_FILES:
            answer = QMessageBox.question(self, APP_NAME, f"Export {len(jobs):,} wem files?",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                return
        folder = QFileDialog.getExistingDirectory(
            self, f"Export {len(jobs):,} WEM files to folder",
            self.config.get("export_dir", "") or self.folder_edit.text().strip())
        if not folder:
            return
        self.config["export_dir"] = folder
        self.export_button.setEnabled(False)
        self.progress.setRange(0, len(jobs))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        task = Task(lambda progress, cancelled: export_wems(jobs, folder, progress, cancelled))
        task.progressed.connect(self._on_export_progress)
        task.succeeded.connect(lambda counts: self._on_export_done(task, counts, folder))
        task.failed.connect(lambda message: self._on_export_failed(task, message))
        task.cancelled.connect(lambda: self._on_export_failed(task, "cancelled"))
        self.export_task = task.start()

    def _export_one(self, stem, location):
        out, _filter = QFileDialog.getSaveFileName(self, "Export WEM", f"{stem}.wem", "WEM (*.wem)")
        if not out:
            return
        try:
            Path(out).write_bytes(extract_wem_bytes(location))
            self.message_label.setText(f"Exported {Path(out).name}")
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Export failed: {e}")

    def _on_export_progress(self, done, total, message):
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        self.message_label.setText(message)

    def _on_export_done(self, task, counts, folder):
        if task is not self.export_task:
            return
        written, failed = counts
        self.progress.setVisible(False)
        tail = f", {failed:,} failed" if failed else ""
        self.message_label.setText(f"Exported {written:,} wems to {Path(folder).name}{tail}")
        self.update_buttons()

    def _on_export_failed(self, task, message):
        if task is not self.export_task:
            return
        self.progress.setVisible(False)
        self.update_buttons()
        QMessageBox.warning(self, APP_NAME, f"Export failed: {message}")

    def export_names(self):
        if not self.matches:
            return
        out, selected_filter = QFileDialog.getSaveFileName(self, "Export names", "event_names.json",
                                                           "JSON (*.json);;Text (*.txt)")
        if not out:
            return
        extension = Path(out).suffix.lower()
        if not extension:
            out += ".txt" if selected_filter.startswith("Text") else ".json"
            extension = Path(out).suffix.lower()
        try:
            if extension == ".txt":
                count = export_txt(self.matches, out)
                self.message_label.setText(f"Exported {count} event names to {Path(out).name}")
            else:
                count = export_json(self.matches, self.unmatched, self.index, self.scan_root, self.names_path, out)
                self.message_label.setText(f"Exported {count} events to {Path(out).name}")
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Export failed: {e}")

    def _copy_lines(self, values, label):
        lines = list(dict.fromkeys(value for value in values if value))
        if not lines:
            self.message_label.setText(f"No {label} in the selection")
            return
        QApplication.clipboard().setText("\n".join(lines))
        plural = "" if len(lines) == 1 else "s"
        self.message_label.setText(f"Copied {len(lines):,} {label}{plural}")

    def copy_ids(self):
        self._copy_lines(self.tree.selected_ids(), "ID")

    def copy_wem_ids(self):
        self._copy_lines(self.tree.selected_wem_ids(), "wem id")

    def copy_bank_ids(self):
        self._copy_lines(self.tree.selected_bank_ids(), "bnk id")

    def copy_names(self):
        self._copy_lines(self.tree.selected_names(), "name")

    def copy_rows(self):
        self._copy_lines(self.tree.selected_rows_text(), "row")

    def on_tree_menu(self, position):
        item = self.tree.itemAt(position)
        if item is not None and not item.isSelected():
            self.tree.setCurrentItem(item)
        if not self.tree.selectedItems():
            return
        count = self.tree.selected_export_count()
        menu = QMenu(self)
        play = menu.addAction("Play")
        play.setEnabled(self.tree.is_playable(self.tree.playback_item()))
        play.triggered.connect(lambda: self.play_selected(restart=True))
        export = menu.addAction("Export WEM..." if count < 2 else f"Export {count:,} WEM...")
        export.setEnabled(self.export_button.isEnabled())
        export.triggered.connect(self.export_selected)
        menu.addSeparator()
        copy = menu.addAction("Copy ID")
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(self.copy_ids)
        menu.addAction("Copy WEM IDs").triggered.connect(self.copy_wem_ids)
        menu.addAction("Copy BNK IDs").triggered.connect(self.copy_bank_ids)
        menu.addAction("Copy name").triggered.connect(self.copy_names)
        menu.addAction("Copy row (tab separated)").triggered.connect(self.copy_rows)
        # One entry per tag of the row puts its id in the search box, bringing up the rows sharing it.
        tags = self.tree.tags_of_item(item)
        if tags:
            menu.addSeparator()
            for group_id, value_id in tags[:_MENU_TAGS]:
                action = menu.addAction(f"Search tag {self.model.tags.render([(group_id, value_id)])}")
                action.triggered.connect(
                    lambda _checked=False, sync_id=value_id or group_id: self.filter_edit.setText(str(sync_id)))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    # Saves what the next start opens with, stops the background work and removes the converted audio.
    def closeEvent(self, event):
        folder = self.folder_edit.text().strip()
        folders = dict(self.config.get("folders") or {})
        if folder:
            folders[self.game] = folder
        self.config.update({"column_widths_v4": self.tree.column_widths(), "game": self.game,
                            "folder": folder, "folders": folders, "names": self.names_path,
                            "crack": self.crack_check.isChecked(),
                            "autoplay": self.autoplay_check.isChecked()})
        save_config(self.config)
        for task in (self.scan_task, self.export_task, self.generate_tab.task):
            if running(task):
                task.cancel()
                task.wait(_CLOSE_WAIT_SECONDS)
        self.player.shutdown()
        super().closeEvent(event)
