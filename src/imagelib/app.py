"""Application entry point for the PySide6 desktop UI."""

from PySide6.QtWidgets import QApplication, QLabel

from imagelib import __version__


def main() -> int:
    app = QApplication([])
    app.setApplicationName("image-library")
    app.setApplicationVersion(__version__)

    window = QLabel(f"image-library {__version__} — UI lands in a later pass.")
    window.resize(640, 400)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())