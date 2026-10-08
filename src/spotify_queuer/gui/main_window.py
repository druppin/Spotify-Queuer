"""Main window.

Left sidebar: every Spotify playlist, cover art + checkbox, multi-select.
Checking a playlist appends it to the "Queue order" list below, which can be
dragged into any order -- that order (not the alphabetical playlist list) is
what "Queue Selected"/"Add After Current" actually send to Spotify.
Right side: downloads-watcher status/feed, a live activity log, and entry
points to Settings and the USB-sync tool.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QSize, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import logging_setup
from ..app import Application
from ..m3u_writer import sanitize_filename
from ..queue_engine import DeviceNotFoundError
from .bridge import EngineBridge, QtLogHandler
from .settings_dialog import SettingsDialog
from .usb_sync_dialog import UsbSyncDialog

logger = logging.getLogger("spotify_queuer.gui")

_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_MAX_LOG_RECORDS = 5000


class _QueueWorker(QObject):
    """Runs Application.queue_selected_playlists() off the GUI thread.

    Queuing makes one Spotify request per track when appending behind
    current playback, so a big playlist can take a while -- doing this
    synchronously on the GUI thread is what made the window look like it had
    stopped responding.
    """

    progress = Signal(str)
    finished = Signal(object)  # QueueResult
    failed = Signal(str)

    def __init__(self, app: Application, playlist_ids: list[str], interrupt_override: bool | None):
        super().__init__()
        self._app = app
        self._playlist_ids = playlist_ids
        self._interrupt_override = interrupt_override

    def run(self) -> None:
        try:
            result = self._app.queue_selected_playlists(
                self._playlist_ids,
                interrupt_playback=self._interrupt_override,
                on_progress=self.progress.emit,
            )
        except DeviceNotFoundError as exc:
            self.failed.emit(str(exc))
            return
        except Exception:
            logger.exception("Failed to queue playlists")
            self.failed.emit("Failed to queue playlists; see error log.")
            return
        self.finished.emit(result)


class MainWindow(QMainWindow):
    def __init__(self, application: Application):
        super().__init__()
        self.setWindowTitle("Spotify Queuer")
        self.resize(1000, 680)

        self._app = application
        self._row_by_playlist: dict[str, QListWidgetItem] = {}
        self._log_records: list[tuple[int, str, str]] = []
        self._suppress_check_signal = False
        self._queue_thread: QThread | None = None
        self._queue_worker: _QueueWorker | None = None

        self._network = QNetworkAccessManager(self)
        self._cover_cache_dir = self._app.config_path.parent / "_cache" / "covers"
        self._cover_cache_dir.mkdir(parents=True, exist_ok=True)
        self._pending_replies: list[QNetworkReply] = []

        self._bridge = EngineBridge()
        self._bridge.download_new_file.connect(self._on_download_new_file)
        self._bridge.download_error.connect(self._on_download_error)

        log_handler = QtLogHandler(self._bridge)
        log_handler.setLevel(logging.INFO)
        log_handler.setFormatter(logging_setup.FORMATTER)
        logging_setup.attach_handler(log_handler)
        self._bridge.log_message.connect(self._on_log_message)

        self._build_ui()

    # -- UI construction --
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)

        # -- left: playlist sidebar --
        left = QWidget()
        left.setMaximumWidth(340)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Your playlists"))

        self._playlist_list = QListWidget()
        self._playlist_list.setIconSize(QSize(48, 48))
        self._playlist_list.itemChanged.connect(self._on_playlist_item_changed)
        left_layout.addWidget(self._playlist_list, 2)

        refresh_btn = QPushButton("Refresh playlists")
        refresh_btn.clicked.connect(self._refresh_playlists)
        left_layout.addWidget(refresh_btn)

        left_layout.addWidget(QLabel("Queue order (drag to reorder)"))
        self._queue_order_list = QListWidget()
        self._queue_order_list.setDragDropMode(QAbstractItemView.InternalMove)
        left_layout.addWidget(self._queue_order_list, 1)

        self._skip_local_checkbox = QCheckBox("Skip tracks I already have locally")
        self._skip_local_checkbox.setChecked(self._app.config.queue.skip_local_tracks)
        self._skip_local_checkbox.toggled.connect(self._on_skip_local_toggled)
        left_layout.addWidget(self._skip_local_checkbox)

        self._interrupt_checkbox = QCheckBox("Interrupt current playback (1 request)")
        self._interrupt_checkbox.setChecked(self._app.config.queue.interrupt_playback)
        self._interrupt_checkbox.toggled.connect(self._on_interrupt_toggled)
        left_layout.addWidget(self._interrupt_checkbox)

        self._queue_btn = QPushButton("Queue Selected")
        self._queue_btn.setToolTip("Plays the queue order list now, per the checkbox above.")
        self._queue_btn.setDefault(True)
        self._queue_btn.clicked.connect(self._queue_selected)
        left_layout.addWidget(self._queue_btn)

        self._add_after_btn = QPushButton("Add After Current")
        self._add_after_btn.setToolTip("Always appends behind what's already playing -- never interrupts it.")
        self._add_after_btn.clicked.connect(lambda: self._queue_selected(interrupt_override=False))
        left_layout.addWidget(self._add_after_btn)

        outer.addWidget(left)

        # -- right: downloads status + log --
        right = QWidget()
        right_layout = QVBoxLayout(right)

        toolbar = QHBoxLayout()
        toolbar.addStretch(1)
        usb_sync_btn = QPushButton("Sync USB Sticks...")
        usb_sync_btn.clicked.connect(self._open_usb_sync)
        toolbar.addWidget(usb_sync_btn)
        settings_btn = QPushButton("Settings...")
        settings_btn.clicked.connect(self._open_settings)
        toolbar.addWidget(settings_btn)
        right_layout.addLayout(toolbar)

        self._download_banner = QLabel("")
        self._download_banner.setStyleSheet("background-color: #b00020; color: white; padding: 6px;")
        self._download_banner.setWordWrap(True)
        self._download_banner.hide()
        right_layout.addWidget(self._download_banner)

        splitter = QSplitter(Qt.Vertical)

        downloads_panel = QWidget()
        downloads_layout = QVBoxLayout(downloads_panel)
        downloads_layout.addWidget(QLabel("Recently downloaded (filed into today's playlist)"))
        self._downloads_view = QPlainTextEdit()
        self._downloads_view.setReadOnly(True)
        self._downloads_view.setMaximumBlockCount(2000)
        downloads_layout.addWidget(self._downloads_view)
        splitter.addWidget(downloads_panel)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_toolbar = QHBoxLayout()
        log_toolbar.addWidget(QLabel("Log level:"))
        self._log_level_combo = QComboBox()
        self._log_level_combo.addItems(_LOG_LEVELS)
        self._log_level_combo.setCurrentText("INFO")
        self._log_level_combo.currentTextChanged.connect(self._rerender_log_view)
        log_toolbar.addWidget(self._log_level_combo)
        log_toolbar.addStretch(1)
        log_layout.addLayout(log_toolbar)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(_MAX_LOG_RECORDS)
        log_layout.addWidget(self._log_view)
        splitter.addWidget(log_panel)
        splitter.setSizes([200, 300])

        right_layout.addWidget(splitter)
        outer.addWidget(right, 1)

    # -- lifecycle --
    def start_engine(self) -> None:
        self._app.start_download_watcher(
            on_new_file=lambda path: self._bridge.download_new_file.emit(str(path)),
            on_error=lambda message: self._bridge.download_error.emit(message),
        )
        self._refresh_playlists()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._app.stop()
        super().closeEvent(event)

    # -- playlist sidebar --
    def _refresh_playlists(self) -> None:
        try:
            playlists = self._app.spotify.list_user_playlists()
        except Exception:
            logger.exception("Failed to list playlists")
            QMessageBox.warning(self, "Spotify Queuer", "Failed to fetch your playlists; see error log.")
            return

        known_ids = {pl["id"] for pl in playlists}
        name_by_id = {pl["id"]: pl.get("name", pl["id"]) for pl in playlists}

        if self._queue_order_list.count() == 0:
            # First load: seed the queue order from what was selected last time.
            order = [pid for pid in self._app.config.queue.last_selected_playlist_ids if pid in known_ids]
        else:
            # A manual refresh: keep the order the user's already built, just
            # drop anything that no longer exists on the Spotify side.
            order = [
                self._queue_order_list.item(i).data(Qt.UserRole)
                for i in range(self._queue_order_list.count())
                if self._queue_order_list.item(i).data(Qt.UserRole) in known_ids
            ]

        self._suppress_check_signal = True
        self._playlist_list.clear()
        self._row_by_playlist.clear()
        self._queue_order_list.clear()

        for playlist_id in order:
            self._append_to_queue_order(playlist_id, name_by_id[playlist_id])
        selected_ids = set(order)

        for pl in playlists:
            item = QListWidgetItem(pl.get("name", pl["id"]))
            item.setData(Qt.UserRole, pl["id"])
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if pl["id"] in selected_ids else Qt.Unchecked)
            self._playlist_list.addItem(item)
            self._row_by_playlist[pl["id"]] = item
            self._fetch_cover(pl)
        self._suppress_check_signal = False

    def _on_playlist_item_changed(self, item: QListWidgetItem) -> None:
        if self._suppress_check_signal:
            return
        playlist_id = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self._append_to_queue_order(playlist_id, item.text())
        else:
            self._remove_from_queue_order(playlist_id)

    def _append_to_queue_order(self, playlist_id: str, name: str) -> None:
        for i in range(self._queue_order_list.count()):
            if self._queue_order_list.item(i).data(Qt.UserRole) == playlist_id:
                return  # already queued
        order_item = QListWidgetItem(name)
        order_item.setData(Qt.UserRole, playlist_id)
        self._queue_order_list.addItem(order_item)

    def _remove_from_queue_order(self, playlist_id: str) -> None:
        for i in range(self._queue_order_list.count()):
            if self._queue_order_list.item(i).data(Qt.UserRole) == playlist_id:
                self._queue_order_list.takeItem(i)
                return

    def _selected_playlist_ids(self) -> list[str]:
        """Queue order, top to bottom -- reflects drag-reordering, not the
        (alphabetical) order playlists happen to appear in the sidebar."""
        return [self._queue_order_list.item(i).data(Qt.UserRole) for i in range(self._queue_order_list.count())]

    def _queue_selected(self, interrupt_override: bool | None = None) -> None:
        playlist_ids = self._selected_playlist_ids()
        if not playlist_ids:
            QMessageBox.information(self, "Spotify Queuer", "Check at least one playlist first.")
            return

        self._queue_btn.setEnabled(False)
        self._add_after_btn.setEnabled(False)

        progress = QProgressDialog("Starting...", None, 0, 0, self)  # indeterminate -- track count isn't known yet
        progress.setWindowTitle("Spotify Queuer")
        progress.setMinimumDuration(0)
        progress.setCancelButton(None)  # mid-queue cancellation isn't supported
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.show()

        thread = QThread(self)
        worker = _QueueWorker(self._app, playlist_ids, interrupt_override)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(progress.setLabelText)

        def cleanup() -> None:
            thread.quit()
            thread.wait()
            worker.deleteLater()
            thread.deleteLater()
            self._queue_thread = None
            self._queue_worker = None
            self._queue_btn.setEnabled(True)
            self._add_after_btn.setEnabled(True)

        def on_finished(result) -> None:
            progress.close()
            cleanup()
            message = f"Queued {result.queued} track(s)."
            if result.skipped_local:
                message += f" ({result.skipped_local} already local, skipped.)"
            QMessageBox.information(self, "Spotify Queuer", message)

        def on_failed(message: str) -> None:
            progress.close()
            cleanup()
            QMessageBox.warning(self, "Spotify Queuer", message)

        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed)

        # Kept as attributes so the thread/worker aren't garbage-collected mid-run.
        self._queue_thread = thread
        self._queue_worker = worker
        thread.start()

    def _on_skip_local_toggled(self, checked: bool) -> None:
        self._app.config.queue.skip_local_tracks = checked
        self._app.save_config()

    def _on_interrupt_toggled(self, checked: bool) -> None:
        self._app.config.queue.interrupt_playback = checked
        self._app.save_config()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._app.config, self)
        if dialog.exec():
            dialog.apply_to(self._app.config)
            self._app.save_config()
            self._app.restart_download_watcher(
                on_new_file=lambda path: self._bridge.download_new_file.emit(str(path)),
                on_error=lambda message: self._bridge.download_error.emit(message),
            )

    def _open_usb_sync(self) -> None:
        UsbSyncDialog(self).exec()

    # -- cover art: cached locally, keyed by snapshot_id so a changed playlist re-fetches --
    def _fetch_cover(self, playlist: dict) -> None:
        images = playlist.get("images") or []
        cover_url = images[0]["url"] if images else None
        if not cover_url:
            return
        playlist_id = playlist["id"]
        cache_key = sanitize_filename(f"{playlist_id}_{playlist.get('snapshot_id', '')}")
        cache_path = self._cover_cache_dir / f"{cache_key}.jpg"
        if cache_path.exists():
            self._set_cover_icon(playlist_id, cache_path)
            return
        reply = self._network.get(QNetworkRequest(QUrl(cover_url)))
        self._pending_replies.append(reply)

        def handle_finished(reply=reply, playlist_id=playlist_id, cache_path=cache_path) -> None:
            self._pending_replies.remove(reply)
            if reply.error() == QNetworkReply.NoError:
                cache_path.write_bytes(bytes(reply.readAll()))
                self._set_cover_icon(playlist_id, cache_path)
            reply.deleteLater()

        reply.finished.connect(handle_finished)

    def _set_cover_icon(self, playlist_id: str, image_path: Path) -> None:
        item = self._row_by_playlist.get(playlist_id)
        if item is None:
            return
        pixmap = QPixmap(str(image_path))
        if pixmap.isNull():
            return
        pixmap = pixmap.scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        item.setIcon(QIcon(pixmap))

    # -- downloads panel --
    def _on_download_new_file(self, path: str) -> None:
        self._downloads_view.appendPlainText(path)
        # A successful file-in is good evidence the watcher is healthy again.
        self._download_banner.hide()

    def _on_download_error(self, message: str) -> None:
        self._download_banner.setText(f"Download watcher: {message}")
        self._download_banner.show()

    # -- log view --
    def _on_log_message(self, levelno: int, levelname: str, message: str) -> None:
        self._log_records.append((levelno, levelname, message))
        if len(self._log_records) > _MAX_LOG_RECORDS:
            self._log_records = self._log_records[-_MAX_LOG_RECORDS:]
        if self._passes_filter(levelno):
            self._log_view.appendPlainText(f"[{levelname}] {message}")

    def _passes_filter(self, levelno: int) -> bool:
        return levelno >= logging.getLevelName(self._log_level_combo.currentText())

    def _rerender_log_view(self, _new_level: str) -> None:
        self._log_view.clear()
        for levelno, levelname, message in self._log_records:
            if self._passes_filter(levelno):
                self._log_view.appendPlainText(f"[{levelname}] {message}")
