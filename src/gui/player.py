# A wem plays once vgmstream has converted it to wav on a background task.
# The wavs live in a temporary folder made on the first play and removed when the application quits.

import shutil
import tempfile

from PyQt6.QtCore import QCoreApplication, QObject, Qt, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QWidget

from src.audio import download_vgmstream, extract_wem_bytes, find_vgmstream, wem_to_wav
from src.gui.tasks import Task, running
from src.gui.widgets import ClickSlider, format_ms

_VOLUME_SLIDER_WIDTH = 110
_VOLUME_LABEL_WIDTH = 34


class Player(QObject):
    message = pyqtSignal(str)
    state_changed = pyqtSignal()
    # vgmstream was just downloaded, so the window plays whatever is selected by now.
    ready = pyqtSignal()

    def __init__(self, volume, parent=None):
        super().__init__(parent)
        self.media = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(volume / 100)
        self.media.setAudioOutput(self.output)
        self.media.playbackStateChanged.connect(lambda _state: self.state_changed.emit())
        self.vgmstream = find_vgmstream()
        self.temp_dir = None
        # However the application ends, the converted files go.
        QCoreApplication.instance().aboutToQuit.connect(self.shutdown)
        self._loaded = None
        self._pending = None
        self._sequence = 0
        self._convert_task = None
        self._download_task = None

    def is_playing(self):
        return self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def is_stopped(self):
        return self.media.playbackState() == QMediaPlayer.PlaybackState.StoppedState

    # With nothing new to load it toggles pause, and without vgmstream it downloads it first.
    def play(self, location):
        if not self.vgmstream:
            self._download_vgmstream()
            return
        if (location is None or location is self._loaded) and not self.is_stopped():
            if self.is_playing():
                self.media.pause()
            else:
                self.media.play()
            return
        if location is None or running(self._convert_task):
            return
        self.media.stop()
        self.media.setSource(QUrl())
        self.message.emit("Converting...")
        self._pending = location
        self._sequence += 1
        stem = f"hsi_{self._sequence}"
        if self.temp_dir is None:
            self.temp_dir = tempfile.mkdtemp(prefix="hoyosoundindexer_")
        out_dir = self.temp_dir
        self._convert_task = Task(
            lambda _progress, _cancelled: wem_to_wav(extract_wem_bytes(location), self.vgmstream,
                                                     out_dir, stem))
        self._convert_task.succeeded.connect(self._on_wav_ready)
        self._convert_task.failed.connect(lambda error: self.message.emit(f"Error: {error}"))
        self._convert_task.start()

    def _on_wav_ready(self, wav_path):
        self.message.emit("")
        self._loaded = self._pending
        self.media.setSource(QUrl.fromLocalFile(wav_path))
        self.media.play()

    def stop(self):
        self.media.stop()

    def set_volume(self, percent):
        self.output.setVolume(percent / 100)

    def _download_vgmstream(self):
        if running(self._download_task):
            return
        self.message.emit("Downloading vgmstream...")
        self._download_task = Task(lambda progress, _cancelled: download_vgmstream(progress=progress))
        self._download_task.progressed.connect(lambda _done, _total, text: self.message.emit(text))
        self._download_task.succeeded.connect(self._on_vgmstream_ready)
        self._download_task.failed.connect(
            lambda error: self.message.emit(f"vgmstream download failed: {error}"))
        self._download_task.start()

    def _on_vgmstream_ready(self, path):
        self.vgmstream = path
        self.message.emit("")
        self.ready.emit()

    # Safe to call twice, since both the window's close and the application's quit call it.
    def shutdown(self):
        self.media.stop()
        self.media.setSource(QUrl())
        if self.temp_dir is not None:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None


class SeekBar(QWidget):
    volume_changed = pyqtSignal(int)

    def __init__(self, player, volume):
        super().__init__()
        self.player = player
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.seek_slider = ClickSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(player.media.setPosition)
        self.seek_slider.clickedValue.connect(player.media.setPosition)
        row.addWidget(self.seek_slider, 1)
        self.time_label = QLabel("0:00 / 0:00")
        row.addWidget(self.time_label)
        row.addSpacing(16)
        row.addWidget(QLabel("Vol"))
        self.volume_slider = ClickSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setFixedWidth(_VOLUME_SLIDER_WIDTH)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(volume)
        self.volume_slider.valueChanged.connect(self._on_volume)
        row.addWidget(self.volume_slider)
        self.volume_label = QLabel(f"{volume}%")
        self.volume_label.setFixedWidth(_VOLUME_LABEL_WIDTH)
        row.addWidget(self.volume_label)
        player.media.positionChanged.connect(self._on_position)
        player.media.durationChanged.connect(self._on_duration)
        player.state_changed.connect(self._on_state)

    def _on_position(self, position):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(int(position))
        self.time_label.setText(f"{format_ms(position)} / {format_ms(self.player.media.duration())}")

    def _on_duration(self, duration):
        self.seek_slider.setRange(0, max(0, int(duration)))
        self.time_label.setText(f"{format_ms(self.player.media.position())} / {format_ms(duration)}")

    def _on_state(self):
        if self.player.is_stopped():
            self.seek_slider.setValue(0)
            self.time_label.setText(f"0:00 / {format_ms(self.player.media.duration())}")

    def _on_volume(self, value):
        self.player.set_volume(value)
        self.volume_label.setText(f"{value}%")
        self.volume_changed.emit(value)
