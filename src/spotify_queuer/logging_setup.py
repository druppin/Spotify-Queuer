"""Activity + error logs.

Activity log: rotating file + (via attach_handler) a live GUI log view.
Error log: file only, unhandled exceptions with full traceback.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOGGER_NAME = "spotify_queuer"
ACTIVITY_LOG_FILENAME = "activity.log"
ERROR_LOG_FILENAME = "error.log"

FORMATTER = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")


def setup_logging(log_dir: Path | str) -> logging.Logger:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    activity_handler = logging.handlers.RotatingFileHandler(
        log_dir / ACTIVITY_LOG_FILENAME, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    activity_handler.setLevel(logging.INFO)
    activity_handler.setFormatter(FORMATTER)
    logger.addHandler(activity_handler)

    error_handler = logging.FileHandler(log_dir / ERROR_LOG_FILENAME, encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(FORMATTER)
    logger.addHandler(error_handler)

    return logger


def attach_handler(handler: logging.Handler) -> None:
    """Attach an additional handler (e.g. the GUI's live log view) to the app logger."""
    logging.getLogger(LOGGER_NAME).addHandler(handler)


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
