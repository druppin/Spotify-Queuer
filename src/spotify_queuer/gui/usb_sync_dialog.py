"""Sync two USB sticks: pick a source and destination folder, scan for
differences, review the list, then copy. Additive/update-only -- never
deletes anything from the destination, even if it's gone from the source."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QVBoxLayout,
)

from ..usb_sync import apply_sync, plan_sync

logger = logging.getLogger("spotify_queuer.gui")


class _SyncWorker(QObject):
    """Runs apply_sync() off the GUI thread so a big/slow copy never makes
    the window look like it's stopped responding, and reports live progress
    (including mid-file) instead of going quiet until each file finishes."""

    progress = Signal(str, int, int)  # relative_path, bytes_copied, total_bytes
    file_done = Signal()
    finished = Signal(int)
    failed = Signal(str, int)  # error message, files copied before the failure

    def __init__(self, actions):
        super().__init__()
        self._actions = actions

    def run(self) -> None:
        done = 0

        def on_chunk(action, copied_bytes) -> None:
            self.progress.emit(str(action.relative_path), copied_bytes, action.size)

        def on_file_done(_action) -> None:
            nonlocal done
            done += 1
            self.file_done.emit()

        try:
            copied = apply_sync(self._actions, progress_cb=on_file_done, chunk_cb=on_chunk)
        except OSError as exc:
            self.failed.emit(str(exc), done)
            return
        self.finished.emit(copied)


def _folder_row(parent) -> tuple[QLineEdit, QHBoxLayout]:
    line = QLineEdit()
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


class UsbSyncDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sync USB Sticks")
        self.resize(560, 480)
        self._actions = []
        self._thread: QThread | None = None
        self._worker: _SyncWorker | None = None

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Source (checked for files to copy):"))
        self.source_edit, source_row = _folder_row(self)
        layout.addLayout(source_row)

        layout.addWidget(QLabel("Destination (receives new/changed files; nothing is ever deleted here):"))
        self.dest_edit, dest_row = _folder_row(self)
        layout.addLayout(dest_row)

        scan_btn = QPushButton("Scan for differences")
        scan_btn.clicked.connect(self._scan)
        layout.addWidget(scan_btn)

        self._summary_label = QLabel("")
        layout.addWidget(self._summary_label)

        self._list = QListWidget()
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox()
        self._copy_btn = buttons.addButton("Copy", QDialogButtonBox.AcceptRole)
        self._copy_btn.setEnabled(False)
        self._copy_btn.clicked.connect(self._copy)
        buttons.addButton(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _scan(self) -> None:
        source, dest = self.source_edit.text().strip(), self.dest_edit.text().strip()
        if not source or not dest:
            QMessageBox.warning(self, "Sync USB Sticks", "Pick both a source and a destination folder.")
            return
        try:
            self._actions = plan_sync(source, dest)
        except OSError as exc:
            QMessageBox.warning(self, "Sync USB Sticks", f"Failed to scan: {exc}")
            return

        self._list.clear()
        for action in self._actions:
            self._list.addItem(QListWidgetItem(f"[{action.reason}] {action.relative_path}"))

        total_bytes = sum(a.size for a in self._actions)
        self._summary_label.setText(
            f"{len(self._actions)} file(s) to copy ({total_bytes / 1_048_576:.1f} MB). "
            "Nothing on the destination will be deleted."
            if self._actions
            else "Destination is already up to date -- nothing to copy."
        )
        self._copy_btn.setEnabled(bool(self._actions))

    def _copy(self) -> None:
        if not self._actions:
            return
        self._copy_btn.setEnabled(False)
        total_files = len(self._actions)
        files_done = 0

        progress = QProgressDialog("Preparing to copy...", None, 0, 100, self)
        progress.setWindowTitle("Sync USB Sticks")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setCancelButton(None)  # mid-copy cancellation isn't supported
        progress.show()

        thread = QThread(self)
        worker = _SyncWorker(list(self._actions))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        def on_progress(relative_path: str, copied_bytes: int, total_bytes: int) -> None:
            pct = int(copied_bytes / total_bytes * 100) if total_bytes else 100
            progress.setValue(pct)
            progress.setLabelText(
                f"Copying file {files_done + 1} of {total_files}: {relative_path} "
                f"({copied_bytes / 1_048_576:.1f} / {total_bytes / 1_048_576:.1f} MB)"
            )

        def on_file_done() -> None:
            nonlocal files_done
            files_done += 1

        def cleanup() -> None:
            thread.quit()
            thread.wait()
            worker.deleteLater()
            thread.deleteLater()
            self._thread = None
            self._worker = None

        def on_finished(copied: int) -> None:
            progress.close()
            cleanup()
            QMessageBox.information(self, "Sync USB Sticks", f"Copied {copied} file(s).")
            self._actions = []
            self._list.clear()
            self._summary_label.setText("")
            self._copy_btn.setEnabled(False)

        def on_failed(message: str, done: int) -> None:
            logger.error("USB sync failed after %d file(s): %s", done, message)
            progress.close()
            cleanup()
            QMessageBox.warning(self, "Sync USB Sticks", f"Copy failed after {done} file(s): {message}")
            self._copy_btn.setEnabled(True)

        worker.progress.connect(on_progress)
        worker.file_done.connect(on_file_done)
        worker.finished.connect(on_finished)
        worker.failed.connect(on_failed)

        # Kept as attributes so the thread/worker aren't garbage-collected mid-run.
        self._thread = thread
        self._worker = worker
        thread.start()
