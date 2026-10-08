"""Entry point: python -m spotify_queuer [config_path]"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .app import Application, default_config_path
from .gui.main_window import MainWindow


def main() -> int:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_config_path()

    qt_app = QApplication(sys.argv)

    application = Application(config_path)
    window = MainWindow(application)
    window.show()
    window.start_engine()

    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
