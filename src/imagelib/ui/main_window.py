"""Main window shell: thumbnail grid + filter panel + detail views.

Stub: real widgets (grid, filter panel, face browser) land in a later pass.
"""

from PySide6.QtWidgets import QMainWindow, QLabel

from imagelib.config import config


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("image-library")
        self.resize(config["app"]["window_width"], config["app"]["window_height"])
        self.setCentralWidget(QLabel("Thumbnail grid + filters (TBD)"))