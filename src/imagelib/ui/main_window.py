"""The Qt Widgets application window and its asynchronous coordinators."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from threading import Event

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRect,
    QThreadPool,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from imagelib.config import config
from imagelib.services import analyser, catalog
from imagelib.ui.models import CalendarDelegate, CalendarModel, ThumbnailDelegate, ThumbnailModel
from imagelib.ui.workers import FunctionTask, ImageAsset, ImageAssetTask, RootValidationTask, ScanTask


logger = logging.getLogger(__name__)


def _default_root() -> Path:
    value = config.get("scan", {}).get("active_root") or "~/Data/images"
    return Path(value).expanduser().resolve()


class CollectionView(QListView):
    image_clicked = Signal(object)

    def __init__(self, model: QAbstractItemModel, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self.setItemDelegate(ThumbnailDelegate(self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setSpacing(10)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.clicked.connect(self._clicked)

    def _clicked(self, index: QModelIndex) -> None:
        item = self.model().item(index)
        if item is not None:
            self.image_clicked.emit(item)

    def selected_image_ids(self) -> list[int]:
        return [item.id for item in (self.model().item(index) for index in self.selectedIndexes()) if item]


class FaceImageWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._faces = ()
        self._visualise = False
        self.setMinimumSize(260, 190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_asset(self, pixmap: QPixmap, faces=()) -> None:
        self._pixmap = pixmap
        self._faces = tuple(faces)
        self.update()

    def set_visualisation(self, enabled: bool) -> None:
        self._visualise = enabled
        self.update()

    def _display_rect(self) -> QRect:
        target = self._pixmap.scaled(
            self.rect().adjusted(8, 8, -8, -8).size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        return QRect(
            (self.width() - target.width()) // 2,
            (self.height() - target.height()) // 2,
            target.width(),
            target.height(),
        )

    def face_rects(self) -> tuple[QRect, ...]:
        if self._pixmap.isNull():
            return ()
        target = self._display_rect()
        scale_x = target.width() / self._pixmap.width()
        scale_y = target.height() / self._pixmap.height()
        return tuple(
            QRect(
                round(target.left() + face.x * scale_x),
                round(target.top() + face.y * scale_y),
                max(1, round(face.w * scale_x)),
                max(1, round(face.h * scale_y)),
            )
            for face in self._faces
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#111418"))
        if self._pixmap.isNull():
            painter.setPen(QColor("#9aa5b1"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Image unavailable")
            return
        target = self._display_rect()
        painter.drawPixmap(target, self._pixmap.scaled(
            target.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        ))
        if not self._visualise:
            return
        colours = (QColor("#ffe37a"), QColor("#67e8f9"), QColor("#f0a3ff"))
        for index, rectangle in enumerate(self.face_rects()):
            painter.setPen(colours[index % len(colours)])
            painter.drawRect(rectangle)


class DetailPanel(QFrame):
    def __init__(self, pool: QThreadPool, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self._serial = 0
        self._faces = ()
        self.title = QLabel("Select an image to see details")
        self.title.setObjectName("detailTitle")
        self.image = FaceImageWidget()
        self.face_scroll = QScrollArea()
        self.face_scroll.setWidgetResizable(True)
        self.face_scroll.setMinimumHeight(152)
        self.face_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.face_strip = QWidget()
        self.face_layout = QHBoxLayout(self.face_strip)
        self.face_layout.setContentsMargins(6, 6, 6, 6)
        self.face_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.face_strip.setMinimumHeight(140)
        self.face_strip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.face_scroll.setWidget(self.face_strip)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.image, 1)
        layout.addWidget(self.face_scroll)
        self.setMinimumHeight(380)

    def clear(self, message: str = "Select an image to see details") -> None:
        self._serial += 1
        self.title.setText(message)
        self.image.set_asset(QPixmap())
        self._clear_faces()

    def set_visualisation(self, enabled: bool) -> None:
        self.image.set_visualisation(enabled)

    def _clear_faces(self) -> None:
        while self.face_layout.count():
            child = self.face_layout.takeAt(0)
            if child.widget() is not None:
                child.widget().deleteLater()

    def show_detail(self, detail: catalog.ImageDetail | None) -> None:
        self._serial += 1
        serial = self._serial
        self._clear_faces()
        if detail is None:
            self.clear("The selected image is no longer in the catalogue")
            return
        timestamp = detail.taken_at or detail.modified_at
        date_text = timestamp.strftime("%Y-%m-%d %H:%M") if timestamp else "Unknown date"
        self.title.setText(f"{detail.relative_path}  ·  {date_text}  ·  {detail.status}")
        self._faces = detail.faces if detail.status == "analysed" else ()
        task = ImageAssetTask(detail.path, self._faces, detail.thumb_path, self)
        task.signals.result.connect(lambda asset, s=serial: self._asset_ready(s, asset))
        task.signals.error.connect(lambda message, s=serial: self._asset_error(s, message))
        self.pool.start(task)

    def _asset_ready(self, serial: int, asset: ImageAsset) -> None:
        if serial != self._serial:
            return
        pixmap = QPixmap.fromImage(asset.image) if not asset.image.isNull() else QPixmap()
        self.image.set_asset(pixmap, self._faces)
        for face, crop in zip(self._faces, asset.crops):
            card = QWidget()
            card.setMinimumWidth(120)
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(6, 6, 6, 6)
            preview = QLabel()
            preview.setFixedSize(96, 96)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            crop_pixmap = QPixmap.fromImage(crop) if not crop.isNull() else QPixmap()
            if not crop_pixmap.isNull():
                preview.setPixmap(crop_pixmap.scaled(preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            else:
                preview.setText("No crop")
            name = face.person_name or "Unknown / Unassigned"
            label = QLabel(name)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            card_layout.addWidget(preview)
            card_layout.addWidget(label)
            self.face_layout.addWidget(card)

    def _asset_error(self, serial: int, message: str) -> None:
        if serial == self._serial:
            logger.error("Could not load detail image: %s", message)
            self.image.set_asset(QPixmap())
            self.title.setText(f"Could not load image: {message}")


class BrowserView(QWidget):
    directory_changed = Signal(object)
    image_clicked = Signal(object)

    def __init__(self, pool: QThreadPool, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self.root = Path()
        self.directory = Path()
        self._all_items = []
        self.model = ThumbnailModel(self)
        self.view = CollectionView(self.model, self)
        self.view.image_clicked.connect(self.image_clicked)
        self.breadcrumbs = QWidget()
        self.breadcrumb_layout = QHBoxLayout(self.breadcrumbs)
        self.breadcrumb_layout.setContentsMargins(0, 0, 0, 0)
        self.breadcrumb_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.folder_row = QWidget()
        self.folder_layout = QHBoxLayout(self.folder_row)
        self.folder_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout = QVBoxLayout(self)
        layout.addWidget(self.breadcrumbs)
        layout.addWidget(self.folder_row)
        layout.addWidget(self.view, 1)

    def set_root(self, root: Path) -> None:
        self.root = root
        self._all_items = []
        self.model.set_items([])
        self.set_directory(Path())

    def set_catalog_items(self, items) -> None:
        self._all_items = list(items)
        self._render_folders()

    def set_directory(self, directory: Path) -> None:
        self.directory = directory
        while self.breadcrumb_layout.count():
            child = self.breadcrumb_layout.takeAt(0)
            if child.widget() is not None:
                child.widget().deleteLater()
        root_button = QPushButton(self.root.name or str(self.root))
        root_button.clicked.connect(lambda: self.directory_changed.emit(Path()))
        self.breadcrumb_layout.addWidget(root_button)
        for index, part in enumerate(directory.parts):
            self.breadcrumb_layout.addWidget(QLabel("›"))
            button = QPushButton(part)
            button.clicked.connect(lambda _checked=False, i=index: self.directory_changed.emit(Path(*directory.parts[: i + 1])))
            self.breadcrumb_layout.addWidget(button)
        self.breadcrumb_layout.addStretch(1)
        self._render_folders()

    def _render_folders(self) -> None:
        while self.folder_layout.count():
            child = self.folder_layout.takeAt(0)
            if child.widget() is not None:
                child.widget().deleteLater()
        prefix = self.directory.parts
        folders = set()
        for item in self._all_items:
            parts = Path(item.relative_path).parts
            if len(parts) > len(prefix) + 1 and parts[: len(prefix)] == prefix:
                folders.add(parts[len(prefix)])
        if folders:
            self.folder_layout.addWidget(QLabel("Folders:"))
            for folder in sorted(folders):
                button = QPushButton(folder)
                button.clicked.connect(lambda _checked=False, name=folder: self.directory_changed.emit(self.directory / name))
                self.folder_layout.addWidget(button)
        self.folder_layout.addStretch(1)

    def selected_image_ids(self) -> list[int]:
        return self.view.selected_image_ids()


class AnalysisCoordinator(QObject):
    status = Signal(str)
    progress = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)
    catalogue_changed = Signal()

    def __init__(self, pool: QThreadPool, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.started.connect(self._process_started)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.readyReadStandardError.connect(self._read_error_output)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._process_finished)
        self._generation = 0
        self._active = False
        self._root = Path()
        self._targets = []
        self._queue_index = 0
        self._pending: dict[str, tuple[analyser.AnalysisTarget, dict]] = {}
        self._responses: list[tuple[analyser.AnalysisTarget, dict]] = []
        self._starting_after_finish = False
        self._process_generation = None
        self._shutdown_requested = False

    def start(self, root: Path, image_ids: list[int] | None = None) -> None:
        self.cancel()
        self._generation += 1
        generation = self._generation
        self._active = True
        self._root = root
        self._targets = []
        self._pending = {}
        self._responses = []
        self._queue_index = 0
        self.status.emit("Selecting images for analysis…")
        task = FunctionTask(
            lambda: analyser.select_analysis_targets(root=root, image_ids=image_ids),
            self,
        )
        task.signals.result.connect(lambda targets, g=generation: self._targets_ready(g, targets))
        task.signals.error.connect(lambda message, g=generation: self._selection_error(g, message))
        self.pool.start(task)

    def cancel(self) -> None:
        self._generation += 1
        self._active = False
        self._targets = []
        self._pending.clear()
        self._responses.clear()
        self._starting_after_finish = False
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
        self.status.emit("Analysis cancelled")

    def _targets_ready(self, generation: int, targets) -> None:
        if not self._active or generation != self._generation:
            return
        self._targets = list(targets)
        if not self._targets:
            self._active = False
            self.status.emit("No eligible images to analyse")
            self.finished.emit(None)
            return
        self.status.emit(f"Analysing 0 of {len(self._targets)}…")
        self._start_process_when_available(generation)

    def _selection_error(self, generation: int, message: str) -> None:
        if self._active and generation == self._generation:
            logger.error("Analysis target selection failed for %s: %s", self._root, message)
            self._active = False
            self.failed.emit(f"Could not select analysis targets: {message}")

    def _start_process_when_available(self, generation: int) -> None:
        if not self._active or generation != self._generation:
            return
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._process_generation = generation
            self._starting_after_finish = False
            self.process.setProgram(sys.executable)
            self.process.setArguments(["-m", "imagelib.services.deepface_worker"])
            environment = QProcessEnvironment.systemEnvironment()
            self.process.setProcessEnvironment(environment)
            self.process.start()
        else:
            self._starting_after_finish = True
            self._process_generation = generation
            self.process.kill()

    def _process_started(self) -> None:
        if self._active and not self._starting_after_finish and self._process_generation == self._generation:
            self._send_next(self._generation)

    def _send_next(self, generation: int) -> None:
        if not self._active or generation != self._generation:
            return
        if self.process.state() != QProcess.ProcessState.Running:
            return
        if self._queue_index >= len(self._targets):
            self._persist_batch(generation)
            return
        target = self._targets[self._queue_index]
        request_id = f"{generation}:{self._queue_index}"
        self._pending[request_id] = (target, {})
        self._queue_index += 1
        request = {"op": "analyse", "path": target.path, "request_id": request_id}
        self.process.write((json.dumps(request) + "\n").encode())

    def _read_output(self) -> None:
        while self.process.canReadLine():
            raw = bytes(self.process.readLine()).strip()
            if not raw:
                continue
            try:
                response = json.loads(raw.decode())
                request_id = response.get("request_id")
                pending = self._pending.pop(request_id, None)
                if pending is None or not self._active:
                    if pending is None and self._active:
                        raise ValueError(f"unexpected DeepFace worker request_id: {request_id!r}")
                    continue
                target, _ = pending
                self._responses.append((target, response))
                self.progress.emit(len(self._responses), len(self._targets))
                self.status.emit(f"Analysing {len(self._responses)} of {len(self._targets)}…")
                self._send_next(self._generation)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as exc:
                logger.exception("Invalid DeepFace worker response")
                if self._active:
                    self._active = False
                    self.failed.emit(f"Invalid DeepFace worker response: {exc}")

    def _read_error_output(self) -> None:
        stderr = bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
        if stderr:
            logger.warning("DeepFace worker stderr: %s", stderr)

    def _persist_batch(self, generation: int) -> None:
        if not self._active or generation != self._generation or not self._responses:
            return
        self.status.emit("Saving analysis results and rebuilding people…")
        responses = list(self._responses)
        task = FunctionTask(lambda: analyser.persist_worker_batch(responses), self)
        task.signals.result.connect(lambda report, g=generation: self._batch_saved(g, report))
        task.signals.error.connect(lambda message, g=generation: self._persist_error(g, message))
        self._active = False
        self.pool.start(task)

    def _batch_saved(self, generation: int, report) -> None:
        if generation != self._generation:
            self.catalogue_changed.emit()
            return
        self.finished.emit(report)
        self.status.emit("Analysis complete")

    def _persist_error(self, generation: int, message: str) -> None:
        logger.error("Analysis persistence callback failed for %s: %s", self._root, message)
        if generation == self._generation:
            self.failed.emit(f"Could not save analysis results: {message}")
        else:
            self.catalogue_changed.emit()

    def _process_error(self, error) -> None:
        logger.error("DeepFace worker process error: %s", error)
        if self._starting_after_finish:
            return
        if (
            self._active
            and self._process_generation == self._generation
            and not self._shutdown_requested
        ):
            self._active = False
            self.failed.emit(f"DeepFace worker error: {error}")

    def _process_finished(self, _exit_code, _exit_status) -> None:
        if (
            self._starting_after_finish
            and self._active
            and self._process_generation == self._generation
            and not self._shutdown_requested
        ):
            self._starting_after_finish = False
            self._start_process_when_available(self._generation)
        elif (
            self._active
            and self._process_generation == self._generation
            and self._targets
        ):
            logger.error("DeepFace worker stopped before analysis completed")
            self._active = False
            self.failed.emit("DeepFace worker stopped before analysis completed")

    def shutdown(self) -> None:
        self._shutdown_requested = True
        self._active = False
        self._generation += 1
        if self.process.state() == QProcess.ProcessState.Running:
            self.process.write(b'{"op":"shutdown","request_id":"shutdown"}\n')
            QTimer.singleShot(1200, self.process.kill)
        elif self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("image-library")
        app_config = config.get("app", {})
        self.resize(int(app_config.get("window_width", 1280)), int(app_config.get("window_height", 800)))
        self.pool = QThreadPool(self)
        self._root = _default_root()
        self._root_generation = 0
        self._scan_cancel = Event()
        self._validation_serial = 0
        self._detail_serial = 0
        self._browser_serial = 0
        self._build_ui()
        self.analysis = AnalysisCoordinator(self.pool, self)
        self.analysis.status.connect(self._set_status)
        self.analysis.finished.connect(self._analysis_finished)
        self.analysis.failed.connect(self._analysis_failed)
        self.analysis.catalogue_changed.connect(lambda: self._refresh(self._root_generation))

    def _build_ui(self) -> None:
        self.root_input = QLineEdit(str(self._root))
        self.root_input.setPlaceholderText("Image library root")
        self.root_input.returnPressed.connect(self._confirm_root)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_root)
        root_row = QHBoxLayout()
        root_row.addWidget(self.root_input, 1)
        root_row.addWidget(choose)

        self.active_root_label = QLabel(f"Active root: {self._root}")
        self.active_root_label.setWordWrap(True)
        self.status_label = QLabel("Ready. Confirm a root to begin scanning.")
        self.status_label.setWordWrap(True)
        self.count_label = QLabel("Total 0 · Analysed 0 · Not analysed 0 · Errors 0")
        self.count_label.setWordWrap(True)
        self.calendar_button = QRadioButton("Calendar")
        self.browser_button = QRadioButton("Browser")
        self.browser_button.setChecked(True)
        self.calendar_button.toggled.connect(self._switch_view)
        self.visualise = QCheckBox("Show face rectangles on detail")
        self.visualise.toggled.connect(self._visualisation_changed)
        self.analyse_selected_button = QPushButton("Analyse selected")
        self.analyse_selected_button.clicked.connect(self._analyse_selected)
        self.analyse_all_button = QPushButton("Analyse all")
        self.analyse_all_button.clicked.connect(self._analyse_all)
        self.cancel_button = QPushButton("Cancel analysis")
        self.cancel_button.clicked.connect(self.analysis_cancelled)
        self.cancel_button.setEnabled(False)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(260)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.addWidget(QLabel("Library root"))
        sidebar_layout.addLayout(root_row)
        sidebar_layout.addWidget(self.active_root_label)
        sidebar_layout.addWidget(self.status_label)
        sidebar_layout.addWidget(self.count_label)
        sidebar_layout.addSpacing(12)
        sidebar_layout.addWidget(QLabel("View"))
        sidebar_layout.addWidget(self.browser_button)
        sidebar_layout.addWidget(self.calendar_button)
        sidebar_layout.addSpacing(12)
        sidebar_layout.addWidget(self.visualise)
        sidebar_layout.addWidget(self.analyse_selected_button)
        sidebar_layout.addWidget(self.analyse_all_button)
        sidebar_layout.addWidget(self.cancel_button)
        sidebar_layout.addStretch(1)

        self.browser = BrowserView(self.pool, self)
        self.browser.directory_changed.connect(self._browse_directory)
        self.browser.image_clicked.connect(self._image_clicked)
        self.calendar_model = CalendarModel(self)
        self.calendar = CollectionView(self.calendar_model, self)
        self.calendar.setUniformItemSizes(False)
        self.calendar.setItemDelegate(CalendarDelegate(self.calendar))
        self.calendar.image_clicked.connect(self._image_clicked)
        self.calendar_stack = QStackedWidget()
        self.calendar_stack.addWidget(self.browser)
        self.calendar_stack.addWidget(self.calendar)
        self.detail = DetailPanel(self.pool, self)
        central_splitter = QSplitter(Qt.Orientation.Vertical)
        central_splitter.addWidget(self.calendar_stack)
        central_splitter.addWidget(self.detail)
        central_splitter.setStretchFactor(0, 3)
        central_splitter.setStretchFactor(1, 2)

        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.addWidget(sidebar)
        content_layout.addWidget(central_splitter, 1)
        self.setCentralWidget(content)
        self.statusBar().showMessage("Ready")

    def _set_status(self, message: str) -> None:
        self.status_label.setText(message)
        self.statusBar().showMessage(message)

    def _choose_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose image root", str(self._root))
        if selected:
            self.root_input.setText(selected)
            self._confirm_root()

    def _confirm_root(self) -> None:
        root = Path(self.root_input.text().strip()).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        root = root.resolve()
        self._validation_serial += 1
        serial = self._validation_serial
        self._set_status("Checking root…")
        task = RootValidationTask(root, self)
        task.signals.result.connect(lambda result, s=serial: self._root_validated(s, result))
        task.signals.error.connect(lambda message, s=serial: self._root_validation_error(s, message))
        self.pool.start(task)

    def _root_validated(self, serial: int, result) -> None:
        if serial != self._validation_serial:
            return
        root, valid = result
        if not valid:
            self._set_status(f"Invalid or unreadable root: {root}")
            return
        self._root = root
        self.active_root_label.setText(f"Active root: {root}")
        self._root_generation += 1
        generation = self._root_generation
        self._scan_cancel.set()
        self.analysis.cancel()
        self._scan_cancel = Event()
        self.detail.clear()
        self._browser_serial += 1
        self.browser.set_root(root)
        self.calendar_model.set_groups({})
        self._set_status(f"Scanning {root}…")
        task = ScanTask(root, self._scan_cancel, self)
        task.signals.result.connect(lambda result, g=generation: self._scan_event(g, result))
        task.signals.error.connect(lambda message, g=generation: self._scan_error(g, message))
        self.pool.start(task)

    def _root_validation_error(self, serial: int, message: str) -> None:
        if serial == self._validation_serial:
            logger.error("Root validation callback failed: %s", message)
            self._set_status(f"Could not validate root: {message}")

    def _scan_event(self, generation: int, result) -> None:
        if generation != self._root_generation:
            return
        event, value = result
        if event == "progress":
            self._set_status(f"Scanning {Path(value).name}…")
        else:
            if value.error:
                self._set_status(value.error)
            elif value.cancelled:
                self._set_status("Scan cancelled; catalogue was not reconciled")
            else:
                self._set_status(
                    f"Scan complete: {value.indexed} indexed, {value.unchanged} unchanged, {value.errors} errors"
                )
            self._refresh(generation)

    def _scan_error(self, generation: int, message: str) -> None:
        if generation == self._root_generation:
            logger.error("Scan callback failed for %s: %s", self._root, message)
            self._set_status(f"Scan failed: {message}")

    def _refresh(self, generation: int) -> None:
        root = self._root
        directory = self.browser.directory
        browser_serial = self._browser_serial
        task = FunctionTask(
            lambda: (
                catalog.status_counts(root=root),
                catalog.browser_images(root=root, directory=directory),
                catalog.calendar_groups(root=root),
                catalog.browser_images(root=root),
            ),
            self,
        )
        task.signals.result.connect(
            lambda snapshot, g=generation, d=directory, b=browser_serial: self._catalog_ready(
                g, snapshot, d, b
            )
        )
        task.signals.error.connect(
            lambda message, g=generation, b=browser_serial: self._refresh_error(g, b, message)
        )
        self.pool.start(task)

    def _catalog_ready(self, generation: int, snapshot, directory: Path, browser_serial: int) -> None:
        if generation != self._root_generation:
            return
        counts, browser_items, groups, all_browser_items = snapshot
        total = sum(counts.values())
        not_analysed = counts.get("indexed", 0) + counts.get("pending", 0)
        self.count_label.setText(
            f"Total {total} · Analysed {counts.get('analysed', 0)} · Not analysed {not_analysed} · Errors {counts.get('error', 0)}"
        )
        self.browser.set_catalog_items(all_browser_items)
        if browser_serial == self._browser_serial and directory == self.browser.directory:
            self.browser.model.set_items(browser_items)
        self.calendar_model.set_groups(groups)
        if browser_serial == self._browser_serial and directory == self.browser.directory:
            self._load_thumbnails(self.browser.model, browser_items, generation)
        self._load_thumbnails(self.calendar_model, [entry.item for entry in self.calendar_model.entries if entry.item], generation)

    def _catalog_error(self, generation: int, message: str) -> None:
        if generation != self._root_generation:
            return
        self._set_status(f"Catalogue unavailable: {message}")
        logger.error("Catalogue callback failed for %s: %s", self._root, message)
        self.browser.model.set_items([])
        self.calendar_model.set_groups({})

    def _refresh_error(self, generation: int, browser_serial: int, message: str) -> None:
        logger.error("Catalogue refresh callback failed for %s: %s", self._root, message)
        if browser_serial == self._browser_serial:
            self._catalog_error(generation, message)

    def _load_thumbnails(self, model, items, generation: int) -> None:
        items = [item for item in items if item is not None]
        task = FunctionTask(
            lambda: [(item.id, QImage(item.thumb_path) if item.thumb_path else QImage()) for item in items],
            self,
        )
        task.signals.result.connect(lambda values, m=model, g=generation: self._thumbnails_ready(g, m, values))
        self.pool.start(task)

    def _thumbnails_ready(self, generation: int, model, values) -> None:
        if generation != self._root_generation:
            return
        for image_id, image in values:
            if not image.isNull():
                model.set_pixmap(image_id, QPixmap.fromImage(image))

    def _browse_directory(self, directory: Path) -> None:
        self.browser.set_directory(directory)
        self._browser_serial += 1
        browser_serial = self._browser_serial
        generation = self._root_generation
        root = self._root
        task = FunctionTask(lambda: catalog.browser_images(root=root, directory=directory), self)
        task.signals.result.connect(
            lambda items, g=generation, b=browser_serial, d=directory: self._browser_ready(
                g, b, d, items
            )
        )
        task.signals.error.connect(
            lambda message, g=generation, b=browser_serial: self._browser_error(g, b, message)
        )
        self.pool.start(task)

    def _browser_ready(self, generation: int, browser_serial: int, directory: Path, items) -> None:
        if (
            generation != self._root_generation
            or browser_serial != self._browser_serial
            or directory != self.browser.directory
        ):
            return
        self.browser.model.set_items(items)
        self._load_thumbnails(self.browser.model, items, generation)

    def _browser_error(self, generation: int, browser_serial: int, message: str) -> None:
        logger.error("Browser catalogue callback failed for %s: %s", self._root, message)
        if generation == self._root_generation and browser_serial == self._browser_serial:
            self._catalog_error(generation, message)

    def _switch_view(self, calendar_selected: bool) -> None:
        if calendar_selected:
            self.calendar_stack.setCurrentIndex(1)
        else:
            self.calendar_stack.setCurrentIndex(0)

    def _image_clicked(self, item) -> None:
        self._detail_serial += 1
        serial = self._detail_serial
        root = self._root
        task = FunctionTask(lambda: catalog.get_image_detail(item.id, root=root), self)
        task.signals.result.connect(lambda detail, s=serial: self._detail_ready(s, detail))
        task.signals.error.connect(lambda message, s=serial: self._detail_error(s, message))
        self.pool.start(task)

    def _detail_ready(self, serial: int, detail) -> None:
        if serial == self._detail_serial:
            self.detail.show_detail(detail)

    def _detail_error(self, serial: int, message: str) -> None:
        if serial == self._detail_serial:
            logger.error("Detail catalogue callback failed for image request: %s", message)
            self.detail.clear(f"Could not load details: {message}")

    def _visualisation_changed(self, enabled: bool) -> None:
        self.detail.set_visualisation(enabled)

    def _analyse_selected(self) -> None:
        view = self.calendar if self.calendar_stack.currentIndex() == 1 else self.browser.view
        image_ids = view.selected_image_ids()
        if not image_ids:
            self._set_status("Select one or more images first")
            return
        self.cancel_button.setEnabled(True)
        self.analysis.start(self._root, image_ids=image_ids)

    def _analyse_all(self) -> None:
        self.cancel_button.setEnabled(True)
        self.analysis.start(self._root)

    def analysis_cancelled(self) -> None:
        self.analysis.cancel()
        self.cancel_button.setEnabled(False)

    def _analysis_finished(self, _report) -> None:
        self.cancel_button.setEnabled(False)
        self._refresh(self._root_generation)

    def _analysis_failed(self, message: str) -> None:
        logger.error("Analysis failed: %s", message)
        self.cancel_button.setEnabled(False)
        self._set_status(message)
        self._refresh(self._root_generation)

    def closeEvent(self, event) -> None:
        self._scan_cancel.set()
        self.analysis.shutdown()
        self.pool.clear()
        event.accept()
