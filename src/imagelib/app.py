"""Application entry point for the PySide6 desktop UI."""

import logging

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from imagelib import __version__
from imagelib.diagnostics import diagnostic
from imagelib.ui.main_window import MainWindow


FOCUS_STYLE = """
QLineEdit:focus, QPushButton:focus, QToolButton:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid palette(highlight);
}
QToolButton#personCard:checked {
    border: 2px solid palette(highlight);
    background: palette(alternate-base);
}
QSplitter::handle {
    background: palette(mid);
}
"""


def _light_palette() -> QPalette:
    window = QColor("#f3f4f6")
    text = QColor("#1c1f24")
    highlight = QColor("#3d6b8a")
    muted = QColor("#5c6570")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, window)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f8f9"))
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, window)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Mid, QColor("#d5d8dc"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, muted)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, muted)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, muted)
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, muted)
    return palette


def main() -> int:
    diagnostic("Starting image-library UI")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = QApplication([])
    app.setApplicationName("image-library")
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setPalette(_light_palette())
    app.setStyleSheet(FOCUS_STYLE)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
