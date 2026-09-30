from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from src.config import APP_NAME, save_config
from src.games import blocks_folder
from src.harvest import default_harvest_prefixes, harvest_folder
from src.online.sources import source_for
from src.vocabulary import DEFAULT_HARVEST_PREFIXES, HARVEST_PREFIXES_BY_GAME
from src.gui.tasks import Task, running

_PREVIEW_NAMES = 5000
_PREVIEW_PREFIXES = 40
_PREVIEW_SOURCES = 3000
_LABEL_WIDTH = 80
_BUTTON_WIDTH = 96


class GenerateTab(QWidget):
    progressed = pyqtSignal(int, int, str)
    status = pyqtSignal(str)
    # The harvest ended either way, so the window hides its progress bar.
    finished = pyqtSignal()
    harvested = pyqtSignal(object)

    def __init__(self, config, current_game):
        super().__init__()
        self.config = config
        self.current_game = current_game
        self.result = None
        self.task = None
        # The blocks folder follows the game folder until the user picks one by hand.
        self.blocks_auto = True
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(_form_label("Blocks folder:"))
        self.blocks_edit = QLineEdit(config.get("harvest_folder", ""))
        self.blocks_edit.setPlaceholderText("derived from the game folder — override only if needed")
        row.addWidget(self.blocks_edit, 1)
        override_button = QPushButton("Override…")
        override_button.setFixedWidth(_BUTTON_WIDTH)
        override_button.clicked.connect(self.pick_blocks_folder)
        row.addWidget(override_button)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(_form_label("Prefixes:"))
        self.prefixes_edit = QLineEdit(config.get("prefixes") or default_harvest_prefixes(config.get("game")))
        row.addWidget(self.prefixes_edit, 1)
        self.harvest_button = QPushButton("Harvest")
        self.harvest_button.setFixedWidth(_BUTTON_WIDTH)
        self.harvest_button.clicked.connect(self.start_harvest)
        row.addWidget(self.harvest_button)
        layout.addLayout(row)

        columns = QHBoxLayout()
        columns.setSpacing(7)
        self.names_header, self.names_preview = _preview_column(
            columns, "Harvested event names", "harvested candidate names will appear here")
        self.voice_header, self.voice_preview = _preview_column(
            columns, "Harvested voice data", "voice prefixes and file names will appear here")
        layout.addLayout(columns, 1)

        row = QHBoxLayout()
        self.save_names_button = QPushButton("Save names TXT...")
        self.save_names_button.clicked.connect(self.save_names)
        self.save_names_button.setEnabled(False)
        row.addWidget(self.save_names_button)
        self.save_voice_button = QPushButton("Save voice data JSON...")
        self.save_voice_button.clicked.connect(self.save_voice)
        self.save_voice_button.setEnabled(False)
        row.addWidget(self.save_voice_button)
        row.addStretch(1)
        hint = QLabel("Harvested names feed Scan through the \"Client harvest\" box in the Search tab.")
        hint.setStyleSheet("color: gray")
        row.addWidget(hint)
        layout.addLayout(row)

    # Names and voices belong to the previous game, and the prefixes follow the game unless typed by hand.
    def reset_for_game(self, game):
        self.result = None
        self.names_preview.clear()
        self.voice_preview.clear()
        self.names_header.setText("Harvested event names")
        self.voice_header.setText("Harvested voice data")
        self.save_names_button.setEnabled(False)
        self.save_voice_button.setEnabled(False)
        defaults = {DEFAULT_HARVEST_PREFIXES} | set(HARVEST_PREFIXES_BY_GAME.values())
        if self.prefixes_edit.text().strip() in defaults:
            self.prefixes_edit.setText(default_harvest_prefixes(game))
        self.blocks_auto = True

    def follow_install(self, install_root, game):
        if not self.blocks_auto or not install_root:
            return
        folder = blocks_folder(install_root, game)
        if folder is not None:
            self.blocks_edit.setText(str(folder))

    # Better no blocks folder than another game's.
    def forget_blocks_folder(self):
        self.blocks_edit.clear()

    def pick_blocks_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Blocks folder", self.blocks_edit.text() or "")
        if path:
            self.blocks_auto = False
            self.blocks_edit.setText(path)

    def start_harvest(self):
        if running(self.task):
            self.task.cancel()
            return
        folder = self.blocks_edit.text().strip()
        if not folder or not Path(folder).is_dir():
            QMessageBox.warning(self, APP_NAME, "Select a valid folder")
            return
        prefixes = [p for p in self.prefixes_edit.text().split(",") if p.strip()]
        if not prefixes:
            QMessageBox.warning(self, APP_NAME, "Enter at least one prefix")
            return
        game = self.current_game()
        self.config.update({"harvest_folder": folder, "prefixes": self.prefixes_edit.text(), "game": game})
        save_config(self.config)
        self.harvest_button.setText("Cancel")
        task = Task(lambda progress, cancelled: harvest_folder(folder, prefixes, progress=progress,
                                                               cancel=cancelled, game=game))
        task.progressed.connect(self.progressed)
        task.succeeded.connect(lambda result: self._on_done(task, result))
        task.failed.connect(lambda message: self._on_stopped(task, message))
        task.cancelled.connect(lambda: self._on_stopped(task, "Harvest cancelled"))
        self.task = task.start()

    def _on_done(self, task, result):
        if task is not self.task:
            return
        self.harvest_button.setText("Harvest")
        self.finished.emit()
        self.result = result
        names, voice = result.names, result.voice
        self.status.emit(f"{len(names)} candidate names harvested"
                         + (f", {len(voice.sources)} voice names" if voice.sources else ""))
        self.names_header.setText(f"Harvested event names — {len(names):,}")
        self.voice_header.setText(f"Harvested voice data — {len(voice.sources):,}")
        self.save_names_button.setEnabled(bool(names))
        self.save_voice_button.setEnabled(bool(voice.sources))
        more = len(names) - _PREVIEW_NAMES
        self.names_preview.setPlainText("\n".join(names[:_PREVIEW_NAMES]) + (f"\n... {more} more" if more > 0 else ""))
        self.voice_preview.setPlainText("\n".join(self._voice_preview_lines(voice)))
        self.harvested.emit(result)

    # A cancelled or failed harvest keeps the previous result.
    def _on_stopped(self, task, message):
        if task is not self.task:
            return
        self.harvest_button.setText("Harvest")
        self.finished.emit()
        self.status.emit(message)

    # GI and HSR keep only partial voice data in the client, and ZZZ all of it, as its source lists none.
    def _voice_preview_lines(self, voice):
        source = source_for(self.current_game(), self.config) or {}
        if source.get("voice_subtree") or source.get("voice_files"):
            lines = ["This game keeps only partial voice data in the client — "
                     "use \"Online names\" in the Search tab.\n"]
        else:
            lines = ["The client carries the full voice paths: no online source needed.\n"]
        lines += [f"vo_prefixes ({len(voice.prefixes)}):"] + [f"  {x}" for x in voice.prefixes[:_PREVIEW_PREFIXES]]
        lines += ["", f"vo_sources ({len(voice.sources)}):"] + [f"  {x}" for x in voice.sources[:_PREVIEW_SOURCES]]
        return lines

    def cancel(self):
        if running(self.task):
            self.task.cancel()

    def save_names(self):
        if not self.result or not self.result.names:
            return
        out, _filter = QFileDialog.getSaveFileName(self, "Save harvested names", "harvested_names.txt",
                                                   "Text (*.txt)")
        if not out:
            return
        try:
            Path(out).write_text("\n".join(self.result.names) + "\n", encoding="utf-8")
            self.status.emit(f"Saved {len(self.result.names)} names to {Path(out).name}")
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Save failed: {e}")

    def save_voice(self):
        if not self.result:
            return
        out, _filter = QFileDialog.getSaveFileName(self, "Save voice data", "voice_data.json", "JSON (*.json)")
        if not out:
            return
        try:
            self.result.voice.save(out)
            self.status.emit(f"Saved voice data to {Path(out).name}")
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Save failed: {e}")


def _form_label(text):
    label = QLabel(text)
    label.setFixedWidth(_LABEL_WIDTH)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return label


def _preview_column(columns, title, placeholder):
    column = QVBoxLayout()
    column.setSpacing(3)
    header = QLabel(title)
    column.addWidget(header)
    preview = QPlainTextEdit()
    preview.setReadOnly(True)
    preview.setPlaceholderText(placeholder)
    column.addWidget(preview, 1)
    columns.addLayout(column, 1)
    return header, preview
