"""Thread-safe bridge from background threads (the download watcher's
watchdog/rescan threads) to Qt signals.

Callbacks run on those background threads. Qt signals emitted there are
auto-queued to the GUI thread by Qt (cross-thread emit -> QueuedConnection)
as long as the receiving QObject lives on the GUI thread, which EngineBridge
does here.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal


class EngineBridge(QObject):
    log_message = Signal(int, str, str)  # levelno, levelname, formatted message
    download_new_file = Signal(str)  # path of a file just filed into today's playlist
    download_error = Signal(str)  # download watcher degraded/failed -- surface loudly


class QtLogHandler(logging.Handler):
    """logging.Handler that forwards records to the bridge's log_message signal,
    giving the GUI a live view of the same activity log stream written to disk."""

    def __init__(self, bridge: EngineBridge):
        super().__init__()
        self._bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        self._bridge.log_message.emit(record.levelno, record.levelname, msg)
