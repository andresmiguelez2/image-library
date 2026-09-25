"""Application entry point for the PySide6 desktop UI."""

import logging

from PySide6.QtWidgets import QApplication

from imagelib import __version__
from imagelib.ui.main_window import MainWindow


STYLE = """
QMainWindow, QWidget { background: #181b1f; color: #edf1f5; }
QLineEdit, QScrollArea, QListView { background: #20252b; border: 1px solid #3c4652; border-radius: 4px; color: #edf1f5; }
QPushButton { background: #2f3b49; border: 1px solid #536273; border-radius: 4px; padding: 6px 10px; }
QPushButton:hover { background: #405267; }
QPushButton:checked, QRadioButton:checked { color: #8ec5ff; }
QLabel#detailTitle { font-weight: bold; padding: 4px; }
QFrame { border: 1px solid #303943; }
QStatusBar { background: #20252b; }
"""


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = QApplication([])
    app.setApplicationName("image-library")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(STYLE)

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
