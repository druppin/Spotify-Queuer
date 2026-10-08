"""Settings panel: folder pickers for music_root, m3u_output_dir and the
downloads watch dir; numeric fields for the rescan interval, rate limit and
fuzzy-match threshold; a device-name field."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..config import AppConfig


def _folder_row(initial: str, parent) -> tuple[QLineEdit, QHBoxLayout]:
    line = QLineEdit(initial)
    browse = QPushButton("Browse...")

    def pick() -> None:
        path = QFileDialog.getExistingDirectory(parent, "Select folder", line.text() or ".")
        if path:
            line.setText(path)

    browse.clicked.connect(pick)
    row = QHBoxLayout()
    row.addWidget(line)
    row.addWidget(browse)
    return line, row


def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    return spin


class SettingsDialog(QDialog):
    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)

        form = QFormLayout()

        self.music_root_edit, music_root_row = _folder_row(config.paths.music_root, self)
        form.addRow("Music root (for 'skip local' check)", music_root_row)

        self.m3u_output_edit, m3u_output_row = _folder_row(config.paths.m3u_output_dir, self)
        form.addRow("M3U output dir", m3u_output_row)

        self.watch_dir_edit, watch_dir_row = _folder_row(config.download_watch.watch_dir, self)
        form.addRow("Downloads folder to watch", watch_dir_row)

        self.rescan_spin = _spin(5, 86400, config.download_watch.fallback_rescan_seconds)
        form.addRow("Downloads fallback rescan (s)", self.rescan_spin)

        self.device_name_edit = QLineEdit(config.spotify.device_name)
        form.addRow("Spotify device name", self.device_name_edit)

        self.fuzzy_threshold_spin = _spin(0, 100, config.matching.fuzzy_threshold)
        form.addRow("Local-match fuzzy threshold", self.fuzzy_threshold_spin)

        self.max_requests_spin = _spin(1, 10000, config.rate_limit.max_requests)
        form.addRow("Max requests", self.max_requests_spin)

        self.window_seconds_spin = _spin(1, 86400, config.rate_limit.window_seconds)
        form.addRow("Rate limit window (s)", self.window_seconds_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def apply_to(self, config: AppConfig) -> None:
        config.paths.music_root = self.music_root_edit.text()
        config.paths.m3u_output_dir = self.m3u_output_edit.text()
        config.download_watch.watch_dir = self.watch_dir_edit.text()
        config.download_watch.fallback_rescan_seconds = self.rescan_spin.value()
        config.spotify.device_name = self.device_name_edit.text()
        config.matching.fuzzy_threshold = self.fuzzy_threshold_spin.value()
        config.rate_limit.max_requests = self.max_requests_spin.value()
        config.rate_limit.window_seconds = self.window_seconds_spin.value()
