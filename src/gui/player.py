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


# The queue holds (wem id, location) pairs, the wems of one event, which previous and next walk through.
class Player(QObject):
    message = pyqtSignal(str)
    state_changed = pyqtSignal()
    queue_changed = pyqtSignal()

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
        self.queue = []
        self.position = -1
        self._requested = None
        self._loaded = None
        self._sequence = 0
        self._convert_task = None
        self._download_task = None

    def is_playing(self):
        return self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def is_stopped(self):
        return self.media.playbackState() == QMediaPlayer.PlaybackState.StoppedState

    # Playing or paused on that copy, or converting it.
    def is_current(self, location):
        return location is self._requested and (location is not self._loaded or not self.is_stopped())

    def play(self, queue, position):
        self.queue, self.position = queue, position
        self.queue_changed.emit()
        self._load(queue[position][1])

    def step(self, delta):
        position = self.position + delta
        if 0 <= position < len(self.queue):
            self.play(self.queue, position)

    # False with nothing loaded, so the caller can play the selection instead.
    def toggle(self):
        if self._loaded is None:
            return False
        if self.is_playing():
            self.media.pause()
        else:
            self.media.play()
        return True

    # A copy already converted starts over, and of several conversions only the latest request plays.
    def _load(self, location):
        self._requested = location
        self._sequence += 1
        sequence = self._sequence
        if not self.vgmstream:
            self._download_vgmstream()
            return
        if location is self._loaded:
            self.message.emit("")
            self.media.setPosition(0)
            self.media.play()
            return
        self.media.stop()
        self.media.setSource(QUrl())
        self._loaded = None
        self.message.emit("Converting...")
        if self.temp_dir is None:
            self.temp_dir = tempfile.mkdtemp(prefix="hoyosoundindexer_")
        out_dir = self.temp_dir
        vgmstream = self.vgmstream
        task = Task(lambda _progress, _cancelled: wem_to_wav(extract_wem_bytes(location), vgmstream, out_dir,
                                                             f"hsi_{sequence}"))
        task.succeeded.connect(lambda wav_path: self._on_wav_ready(sequence, location, wav_path))
        task.failed.connect(lambda error: self._on_wav_failed(sequence, error))
        # Held until the next conversion, or the finished task could be collected before its result arrives.
        self._convert_task = task.start()

    def _on_wav_ready(self, sequence, location, wav_path):
        if sequence != self._sequence:
            return
        self.message.emit("")
        self._loaded = location
        self.media.setSource(QUrl.fromLocalFile(wav_path))
        self.media.play()

    # The failed copy is no longer current, so Play tries it again.
    def _on_wav_failed(self, sequence, error):
        if sequence == self._sequence:
            self._requested = None
            self.message.emit(f"Error: {error}")

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

    # The wem asked for while downloading plays as soon as vgmstream is there.
    def _on_vgmstream_ready(self, path):
        self.vgmstream = path
        self.message.emit("")
        if self._requested is not None:
            self._load(self._requested)

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
