"""App-wide dark theme (Spotify-like): near-black surfaces, light text, one
green accent reserved for primary actions. Widgets opt in to the accent and
heading styles with setProperty("primary", True) / setProperty("heading", True).

Fusion style underneath because the native Windows style ignores much of a
stylesheet; the palette covers what the stylesheet doesn't reach (message
boxes, progress dialogs, checkbox indicators)."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BACKGROUND = "#121212"
SURFACE = "#181818"
RAISED = "#282828"
HOVER = "#333333"
TEXT = "#ffffff"
MUTED = "#b3b3b3"
ACCENT = "#1db954"
ACCENT_HOVER = "#1ed760"

_STYLESHEET = f"""
QWidget {{ background: {BACKGROUND}; color: {TEXT}; font-family: "Segoe UI", sans-serif; font-size: 10pt; }}
QLabel {{ background: transparent; }}
QLabel[heading="true"] {{ font-size: 13pt; font-weight: bold; padding: 8px 0 4px 0; }}
QToolTip {{ background: {RAISED}; color: {TEXT}; border: none; padding: 4px; }}

QListWidget, QPlainTextEdit, QTableWidget, QLineEdit, QSpinBox, QComboBox {{
    background: {SURFACE}; border: 1px solid {RAISED}; border-radius: 6px;
}}
QListWidget {{ padding: 4px; outline: none; }}
QListWidget::item {{ padding: 6px; border-radius: 4px; }}
QListWidget::item:hover {{ background: {HOVER}; }}
QListWidget::item:selected {{ background: {RAISED}; color: {TEXT}; }}
QPlainTextEdit {{ padding: 6px; color: {MUTED}; }}
QLineEdit, QSpinBox, QComboBox {{ padding: 5px 8px; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {MUTED}; }}

QTableWidget {{ gridline-color: {RAISED}; }}
QTableWidget::item {{ padding: 4px; }}
QTableWidget::item:selected {{ background: {RAISED}; color: {TEXT}; }}
QHeaderView::section {{
    background: {SURFACE}; color: {MUTED}; border: none;
    border-bottom: 1px solid {RAISED}; padding: 6px; font-weight: bold;
}}

QPushButton {{
    background: transparent; color: {TEXT}; border: 1px solid {MUTED};
    border-radius: 16px; padding: 6px 16px; min-height: 20px;
}}
QPushButton:hover {{ border-color: {TEXT}; }}
QPushButton:pressed {{ background: {HOVER}; }}
QPushButton:disabled {{ color: #5a5a5a; border-color: #3e3e3e; }}
QPushButton[primary="true"] {{ background: {ACCENT}; color: #000000; border: none; font-weight: bold; }}
QPushButton[primary="true"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[primary="true"]:disabled {{ background: #1e5a34; color: #2b2b2b; }}

QCheckBox {{ background: transparent; spacing: 8px; padding: 2px 0; }}
/* Solid green when checked, outlined when not -- no image needed, and
   Fusion's default indicator is invisible on a dark surface. */
QCheckBox::indicator, QListWidget::indicator {{
    width: 14px; height: 14px; border: 2px solid {MUTED}; border-radius: 4px; background: transparent;
}}
QCheckBox::indicator:hover, QListWidget::indicator:hover {{ border-color: {TEXT}; }}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QAbstractSpinBox {{ qproperty-buttonSymbols: NoButtons; }}
QSplitter::handle {{ background: {BACKGROUND}; }}
QProgressBar {{ background: {RAISED}; border: none; border-radius: 4px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle {{ background: #4d4d4d; border-radius: 5px; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: #6b6b6b; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; width: 0; height: 0;
}}
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in (
        (QPalette.Window, BACKGROUND),
        (QPalette.WindowText, TEXT),
        (QPalette.Base, SURFACE),
        (QPalette.AlternateBase, RAISED),
        (QPalette.Text, TEXT),
        (QPalette.Button, RAISED),
        (QPalette.ButtonText, TEXT),
        (QPalette.Highlight, ACCENT),
        (QPalette.HighlightedText, "#000000"),
        (QPalette.ToolTipBase, RAISED),
        (QPalette.ToolTipText, TEXT),
        (QPalette.PlaceholderText, MUTED),
    ):
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor("#5a5a5a"))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5a5a5a"))
    app.setPalette(palette)
    app.setStyleSheet(_STYLESHEET)
