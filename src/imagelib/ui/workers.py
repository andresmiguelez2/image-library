"""Small Qt worker primitives used by the desktop UI."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QRunnable, QRect, Signal
from PySide6.QtGui import QImage


logger = logging.getLogger(__name__)


class TaskSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class FunctionTask(QRunnable):
    def __init__(self, function, parent=None) -> None:
        super().__init__()
        self.function = function
        self.signals = TaskSignals(parent)
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            self.signals.result.emit(self.function())
        except Exception as exc:
            logger.exception("UI background task failed (%s)", getattr(self.function, "__name__", "callable"))
            self.signals.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.signals.finished.emit()


class RootValidationTask(QRunnable):
    def __init__(self, root: Path, parent=None) -> None:
        super().__init__()
        self.root = root
        self.signals = TaskSignals(parent)

    def run(self) -> None:
        try:
            valid = self.root.is_dir()
            if valid:
                import os

                valid = os.access(self.root, os.R_OK | os.X_OK)
            self.signals.result.emit((self.root, bool(valid)))
        except OSError as exc:
            logger.exception("Could not validate image root %s", self.root)
            self.signals.result.emit((self.root, False))


class ScanTask(QRunnable):
    def __init__(self, root: Path, cancel: Event, parent=None) -> None:
        super().__init__()
        self.root = root
        self.cancel = cancel
        self.signals = TaskSignals(parent)

    def run(self) -> None:
        try:
            from imagelib.services.scanner import scan_root

            report = scan_root(
                self.root,
                progress=lambda path: self.signals.result.emit(("progress", path)),
                cancel=self.cancel,
            )
            self.signals.result.emit(("complete", report))
        except Exception as exc:
            logger.exception("Image scan failed for %s", self.root)
            self.signals.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.signals.finished.emit()


@dataclass(frozen=True)
class ImageAsset:
    image: QImage
    crops: tuple[QImage, ...]


class ImageAssetTask(QRunnable):
    def __init__(self, path: str, faces: tuple, fallback_path: str | None = None, parent=None) -> None:
        super().__init__()
        self.path = path
        self.faces = faces
        self.fallback_path = fallback_path
        self.signals = TaskSignals(parent)

    def run(self) -> None:
        try:
            image = QImage(self.path)
            if image.isNull() and self.fallback_path:
                image = QImage(self.fallback_path)
            crops = []
            if not image.isNull():
                for face in self.faces:
                    rectangle = image.rect().intersected(
                        QRect(int(face.x), int(face.y), int(face.w), int(face.h))
                    )
                    crops.append(image.copy(rectangle) if not rectangle.isEmpty() else QImage())
            self.signals.result.emit(ImageAsset(image, tuple(crops)))
        except Exception as exc:
            logger.exception("Could not load image asset %s", self.path)
            self.signals.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.signals.finished.emit()
