"""Sync two USB sticks: pick a source and destination folder, scan for
differences, review the list, then copy. Additive/update-only -- never
deletes anything from the destination, even if it's gone from the source.

The review list is a per-track diff (status, both sides' size and date, and a
note on how they differ) that also shows files that only exist on the
destination, so it doubles as a way to spot tracks that need re-downloading.
It can be filtered and exported to CSV."""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..usb_sync import (
    TrackDiff,
    actions_from_diffs,
    apply_sync,
    compare_trees,
    format_bytes,
    format_mtime,
    write_diff_csv,
)

logger = logging.getLogger("spotify_queuer.gui")

_STATUS_LABELS = {"new": "New", "changed": "Changed", "dest_only": "Only on destination"}
# (label, filter): None = everything, "size" = size differs, else a set of statuses.
_FILTERS = [
    ("All differences", None),
    ("New (missing on destination)", {"new"}),
    ("Changed", {"changed"}),
    ("Size differs", "size"),
    ("Only on destination", {"dest_only"}),
]
_COLUMNS = ["Status", "File", "Source size", "Dest size", "Size diff", "Source modified", "Dest modified", "Note"]
_SORT_ROLE = Qt.UserRole + 1


class _SortableItem(QTableWidgetItem):
    """Sorts by the raw value (bytes, timestamp) stored under _SORT_ROLE
    instead of by its formatted display text; blanks sort first."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine, theirs = self.data(_SORT_ROLE), other.data(_SORT_ROLE)
        if mine is None or theirs is None:
            return mine is None and theirs is not None
        return mine < theirs


def _item(text: str, sort_value=None, sortable: bool = False) -> QTableWidgetItem:
    item = _SortableItem(text) if sortable else QTableWidgetItem(text)
    if sortable:
        item.setData(_SORT_ROLE, sort_value)
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


def _size_item(size: int | None) -> QTableWidgetItem:
    return _item("" if size is None else format_bytes(size), size, sortable=True)


def _delta_item(delta: int | None) -> QTableWidgetItem:
    if delta is None:
        text = ""
    else:
        sign = "+" if delta > 0 else "-" if delta < 0 else ""
        text = sign + format_bytes(abs(delta))
    return _item(text, delta, sortable=True)


def _passes_filter(diff: TrackDiff, criterion) -> bool:
    if criterion is None:
        return True
    if criterion == "size":
        return bool(diff.size_delta)
    return diff.status in criterion


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
        self.resize(1100, 600)
        self._actions = []
        self._diffs: list[TrackDiff] = []
        self._thread: QThread | None = None
        self._worker: _SyncWorker | None = None
        self._progress: QProgressDialog | None = None
        self._total_files = 0
        self._files_done = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

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
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Show:"))
        self._filter_combo = QComboBox()
        for label, _ in _FILTERS:
            self._filter_combo.addItem(label)
        self._filter_combo.currentIndexChanged.connect(self._populate_table)
        filter_row.addWidget(self._filter_combo)
        filter_row.addStretch(1)
        self._export_btn = QPushButton("Export CSV...")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._export_csv)
        filter_row.addWidget(self._export_btn)
        layout.addLayout(filter_row)

        self._table = QTableWidget(0, len(_COLUMNS))
        self._table.setHorizontalHeaderLabels(_COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        layout.addWidget(self._table, 1)

        buttons = QDialogButtonBox()
        self._copy_btn = buttons.addButton("Copy", QDialogButtonBox.AcceptRole)
        self._copy_btn.setProperty("primary", True)
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
            self._diffs = compare_trees(source, dest)
        except OSError as exc:
            QMessageBox.warning(self, "Sync USB Sticks", f"Failed to scan: {exc}")
            return
        self._actions = actions_from_diffs(self._diffs, source, dest)
        self._populate_table()

        if not self._diffs:
            self._summary_label.setText("Both folders match -- nothing to copy.")
        else:
            total_bytes = sum(a.size for a in self._actions)
            size_differs = sum(1 for d in self._diffs if d.size_delta)
            dest_only = sum(1 for d in self._diffs if d.status == "dest_only")
            copy_text = (
                f"{len(self._actions)} file(s) to copy ({total_bytes / 1_048_576:.1f} MB)."
                if self._actions
                else "Nothing to copy."
            )
            self._summary_label.setText(
                f"{copy_text} {size_differs} file(s) differ in size, {dest_only} only on the destination. "
                "Nothing on the destination will be deleted."
            )
        self._copy_btn.setEnabled(bool(self._actions))
        self._export_btn.setEnabled(bool(self._diffs))

    def _filtered_diffs(self) -> list[TrackDiff]:
        criterion = _FILTERS[self._filter_combo.currentIndex()][1]
        return [d for d in self._diffs if _passes_filter(d, criterion)]

    def _populate_table(self) -> None:
        rows = self._filtered_diffs()
        self._table.setSortingEnabled(False)  # otherwise rows re-sort mid-insert and cells land in the wrong row
        self._table.setRowCount(len(rows))
        muted = QBrush(QColor(128, 128, 128))
        for row, diff in enumerate(rows):
            cells = [
                _item(_STATUS_LABELS[diff.status]),
                _item(str(diff.relative_path)),
                _size_item(diff.source_size),
                _size_item(diff.dest_size),
                _delta_item(diff.size_delta),
                _item(format_mtime(diff.source_mtime), diff.source_mtime, sortable=True),
                _item(format_mtime(diff.dest_mtime), diff.dest_mtime, sortable=True),
                _item(diff.note),
            ]
            for col, cell in enumerate(cells):
                if diff.status == "dest_only":
                    cell.setForeground(muted)  # listed for review only; never copied
                self._table.setItem(row, col, cell)
        self._table.setSortingEnabled(True)

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export differences", "usb_sync_diff.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            write_diff_csv(self._filtered_diffs(), path)
        except OSError as exc:
            QMessageBox.warning(self, "Sync USB Sticks", f"Failed to export: {exc}")

    def _copy(self) -> None:
        if not self._actions:
            return
        self._copy_btn.setEnabled(False)
        self._total_files = len(self._actions)
        self._files_done = 0

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
        # Connected to methods (not local functions) so they run on the GUI
        # thread -- PySide runs plain functions on the emitting worker thread.
        worker.progress.connect(self._on_copy_progress)
        worker.file_done.connect(self._on_copy_file_done)
        worker.finished.connect(self._on_copy_finished)
        worker.failed.connect(self._on_copy_failed)

        # Kept as attributes so the thread/worker aren't garbage-collected mid-run.
        self._progress = progress
        self._thread = thread
        self._worker = worker
        thread.start()

    def _on_copy_progress(self, relative_path: str, copied_bytes: int, total_bytes: int) -> None:
        pct = int(copied_bytes / total_bytes * 100) if total_bytes else 100
        self._progress.setValue(pct)
        self._progress.setLabelText(
            f"Copying file {self._files_done + 1} of {self._total_files}: {relative_path} "
            f"({copied_bytes / 1_048_576:.1f} / {total_bytes / 1_048_576:.1f} MB)"
        )

    def _on_copy_file_done(self) -> None:
        self._files_done += 1

    def _finish_copy_run(self) -> None:
        self._progress.close()
        self._progress = None
        self._thread.quit()
        self._thread.wait()
        self._worker.deleteLater()
        self._thread.deleteLater()
        self._thread = None
        self._worker = None

    def _on_copy_finished(self, copied: int) -> None:
        self._finish_copy_run()
        QMessageBox.information(self, "Sync USB Sticks", f"Copied {copied} file(s).")
        self._actions = []
        self._diffs = []
        self._populate_table()
        self._summary_label.setText("")
        self._copy_btn.setEnabled(False)
        self._export_btn.setEnabled(False)

    def _on_copy_failed(self, message: str, done: int) -> None:
        logger.error("USB sync failed after %d file(s): %s", done, message)
        self._finish_copy_run()
        QMessageBox.warning(self, "Sync USB Sticks", f"Copy failed after {done} file(s): {message}")
        self._copy_btn.setEnabled(True)
