"""The Qt Widgets application window and its asynchronous coordinators."""

from __future__ import annotations

import json
import logging
import sys
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from threading import Event

from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QModelIndex,
    QObject,
    QPoint,
    QPointF,
    QProcess,
    QProcessEnvironment,
    QRect,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QIcon,
    QImage,
    QPainter,
    QPalette,
    QPen,
    QPolygon,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from imagelib.config import config
from imagelib.diagnostics import diagnostic, diagnostic_exception
from imagelib.services import analyser, catalog
from imagelib.ui.models import CalendarModel, ThumbnailDelegate, ThumbnailModel, status_colour
from imagelib.ui.workers import (
    FaceBounds,
    FunctionTask,
    ImageAsset,
    ImageAssetTask,
    RootValidationTask,
    ScanTask,
)



from imagelib.ui._widgets import (
    ButtonRowScroll,
    ElidedLabel,
    FaceNameLabel,
    WrappingLabel,
    _break_opportunities,
    _default_root,
    _elide_two_lines,
    _mute_label,
    _section_label,
    _thumb_metrics,
    _use_surface,
)
from imagelib.ui.coordinator import AnalysisCoordinator
from imagelib.ui.detail import DetailPanel, FaceImageWidget, FaceMatchReviewDialog
from imagelib.ui.views import (
    BrowserView,
    CalendarDayCell,
    CalendarView,
    ChronologicalDayGroup,
    ChronologicalView,
    CollectionView,
    PeopleView,
    _ChronologicalCanvas,
    _ChronologicalRow,
)

logger = logging.getLogger(__name__)

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
        self._catalog_serial = 0
        self._people_query_serial = 0
        self._people_filter_ids: tuple[int, ...] = ()
        self._people_filter_mode = "any"
        self._people_return_widget: QWidget | None = None
        self._splitters_sized = False
        self._selected_image_id: int | None = None
        self._face_label_active = False
        self._proposal_query_serial = 0
        self._proposal_dialog: FaceMatchReviewDialog | None = None
        self._proposal_queue: list[catalog.FaceMatchProposal] = []
        self._proposal_deferred: set[tuple[int, int]] = set()
        self._build_ui()
        self.analysis = AnalysisCoordinator(self.pool, self)
        self.analysis.status.connect(self._set_status)
        self.analysis.finished.connect(self._analysis_finished)
        self.analysis.failed.connect(self._analysis_failed)
        self.analysis.catalogue_changed.connect(self._analysis_catalogue_changed)
        self._check_pending_proposals()

    def _build_ui(self) -> None:
        self.root_input = QLineEdit(str(self._root))
        self.root_input.setPlaceholderText("Image library root")
        self.root_input.setMinimumWidth(0)
        self.root_input.returnPressed.connect(self._confirm_root)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_root)
        root_row = QHBoxLayout()
        root_row.addWidget(self.root_input, 1)
        root_row.addWidget(choose)

        self.active_root_label = WrappingLabel(f"Active root: {self._root}")
        self.total_label = QLabel("Total 0")
        self.analysed_label = QLabel("Analysed 0")
        self.not_analysed_label = QLabel("Not analysed 0")
        self.errors_label = QLabel("Errors 0")
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)
        view_switch = QWidget()
        self.browser_button = QRadioButton("Browser", view_switch)
        self.calendar_button = QRadioButton("Calendar", view_switch)
        self.chronological_button = QRadioButton("Chronological", view_switch)
        self.browser_button.setAccessibleName("Browser view")
        self.browser_button.setToolTip("Browse images by folder")
        self.calendar_button.setAccessibleName("Calendar view")
        self.calendar_button.setToolTip("Browse images in a monthly calendar")
        self.chronological_button.setAccessibleName("Chronological view")
        self.chronological_button.setToolTip("Browse images by date, oldest first")
        self.browser_button.setChecked(True)
        self.browser_button.toggled.connect(
            lambda checked: self._switch_view(0, checked)
        )
        self.calendar_button.toggled.connect(
            lambda checked: self._switch_view(1, checked)
        )
        self.chronological_button.toggled.connect(
            lambda checked: self._switch_view(2, checked)
        )
        view_layout = QHBoxLayout(view_switch)
        view_layout.setContentsMargins(4, 0, 4, 0)
        view_layout.setSpacing(8)
        view_layout.addWidget(self.browser_button)
        view_layout.addWidget(self.calendar_button)
        view_layout.addWidget(self.chronological_button)
        view_switch.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.people_button = QPushButton("People")
        self.people_button.setAccessibleName("Open people search")
        self.people_button.clicked.connect(self._open_people_picker)
        self.visualise = QCheckBox("Show face rectangles on detail")
        self.visualise.toggled.connect(self._visualisation_changed)
        self.analyse_selected_button = QPushButton("Analyse selected")
        self.analyse_selected_button.clicked.connect(self._analyse_selected)
        self.analyse_all_button = QPushButton("Analyse all")
        self.analyse_all_button.clicked.connect(self._analyse_all)
        self.cancel_button = QPushButton("Cancel analysis")
        self.cancel_button.clicked.connect(self.analysis_cancelled)
        self.cancel_button.setEnabled(False)

        toolbar = QToolBar()
        toolbar.setObjectName("mainToolBar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        toolbar.addWidget(view_switch)
        toolbar.addWidget(self.people_button)
        toolbar.addSeparator()
        toolbar.addWidget(self.analyse_selected_button)
        toolbar.addWidget(self.analyse_all_button)
        toolbar.addWidget(self.cancel_button)
        self.addToolBar(toolbar)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(240)
        sidebar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        _use_surface(sidebar, QPalette.ColorRole.Window)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.addWidget(_section_label("Library"))
        sidebar_layout.addLayout(root_row)
        sidebar_layout.addWidget(self.active_root_label)
        sidebar_layout.addSpacing(16)
        sidebar_layout.addWidget(_section_label("Catalogue"))
        sidebar_layout.addWidget(self.total_label)
        sidebar_layout.addWidget(self.analysed_label)
        sidebar_layout.addWidget(self.not_analysed_label)
        sidebar_layout.addWidget(self.errors_label)
        self.people_filter_label = QLabel()
        self.people_filter_label.setObjectName("peopleFilterIndicator")
        self.people_filter_label.setWordWrap(True)
        self.people_filter_label.setAccessibleName("Active people filter")
        self.edit_people_filter_button = QPushButton("Edit people filter…")
        self.edit_people_filter_button.clicked.connect(self._open_people_picker)
        self.clear_people_filter_button = QPushButton("Clear people filter")
        self.clear_people_filter_button.clicked.connect(self._clear_people_filter)
        self.people_filter_label.setVisible(False)
        self.edit_people_filter_button.setVisible(False)
        self.clear_people_filter_button.setVisible(False)
        sidebar_layout.addWidget(self.people_filter_label)
        sidebar_layout.addWidget(self.edit_people_filter_button)
        sidebar_layout.addWidget(self.clear_people_filter_button)
        sidebar_layout.addSpacing(8)
        sidebar_layout.addWidget(self.visualise)
        sidebar_layout.addStretch(1)

        self.browser = BrowserView(self.pool, self)
        self.browser.directory_changed.connect(self._browse_directory)
        self.browser.image_clicked.connect(self._image_clicked)
        self.calendar_model = CalendarModel(self)
        self.calendar = CalendarView(self.calendar_model, self)
        self.calendar.image_clicked.connect(self._image_clicked)
        self.chronological = ChronologicalView(self.calendar_model, self)
        self.chronological.image_clicked.connect(self._image_clicked)
        self.calendar_stack = QStackedWidget()
        self.calendar_stack.addWidget(self.browser)
        self.calendar_stack.addWidget(self.calendar)
        self.calendar_stack.addWidget(self.chronological)
        self.people_view = PeopleView(self)
        self.people_view.apply_requested.connect(self._apply_people_filter)
        self.people_view.back_requested.connect(self._cancel_people_picker)
        self.calendar_stack.addWidget(self.people_view)
        self.calendar_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        _use_surface(self.calendar_stack, QPalette.ColorRole.Base)
        self.detail = DetailPanel(self.pool, self)
        self.detail.label_face_requested.connect(self._request_face_label)
        central_splitter = QSplitter(Qt.Orientation.Vertical)
        central_splitter.addWidget(self.calendar_stack)
        central_splitter.addWidget(self.detail)
        central_splitter.setStretchFactor(0, 3)
        central_splitter.setStretchFactor(1, 2)
        central_splitter.setCollapsible(0, False)
        central_splitter.setCollapsible(1, False)
        central_splitter.setHandleWidth(8)
        central_splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        _use_surface(central_splitter, QPalette.ColorRole.Base)

        self.content_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.content_splitter.addWidget(sidebar)
        self.content_splitter.addWidget(central_splitter)
        self.content_splitter.setStretchFactor(0, 0)
        self.content_splitter.setStretchFactor(1, 1)
        self.content_splitter.setCollapsible(0, False)
        self.content_splitter.setCollapsible(1, False)
        self.content_splitter.setHandleWidth(8)
        self.content_splitter.setSizes([280, 1000])
        self.setCentralWidget(self.content_splitter)
        self.statusBar().showMessage("Ready. Confirm a root to begin scanning.")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._splitters_sized:
            self._splitters_sized = True
            sidebar_width = 280
            self.content_splitter.setSizes([sidebar_width, max(1, self.width() - sidebar_width)])

    def _set_status(self, message: str) -> None:
        bar = self.statusBar()
        bar.showMessage(message)
        bar.setToolTip(message)
        self.status_label.setText(message)

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
        self._people_query_serial += 1
        self._people_filter_ids = ()
        self._people_filter_mode = "any"
        self.people_view.reset()
        self._selected_image_id = None
        self._detail_serial += 1
        self._face_label_active = False
        self._proposal_query_serial += 1
        self._proposal_queue.clear()
        self._proposal_deferred.clear()
        previous_dialog = self._proposal_dialog
        self._proposal_dialog = None
        if previous_dialog is not None:
            previous_dialog.set_busy(False)
            previous_dialog.close()
        self._scan_cancel.set()
        self.analysis.cancel()
        self._scan_cancel = Event()
        self.detail.clear()
        self._browser_serial += 1
        self.browser.set_root(root)
        self._update_people_filter_indicator()
        if self.calendar_stack.currentWidget() is self.people_view:
            self._restore_photo_view()
        self.calendar_model.set_groups({})
        self._set_status(f"Scanning {root}…")
        self._check_pending_proposals()
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
                self._check_pending_proposals()
            self._refresh(generation)

    def _scan_error(self, generation: int, message: str) -> None:
        if generation == self._root_generation:
            logger.error("Scan callback failed for %s: %s", self._root, message)
            self._set_status(f"Scan failed: {message}")

    def _open_people_picker(self) -> None:
        if self.calendar_stack.currentWidget() is not self.people_view:
            self._people_return_widget = self.calendar_stack.currentWidget()
        self.browser_button.setEnabled(False)
        self.calendar_button.setEnabled(False)
        self.people_view.begin_edit(self._people_filter_ids, self._people_filter_mode)
        self.people_view.set_loading()
        self.calendar_stack.setCurrentWidget(self.people_view)
        self._people_query_serial += 1
        serial = self._people_query_serial
        generation = self._root_generation
        root = self._root
        task = FunctionTask(lambda: catalog.list_person_groups(root=root), self)
        task.signals.result.connect(
            lambda groups, s=serial, g=generation: self._people_groups_ready(s, g, groups)
        )
        task.signals.error.connect(
            lambda message, s=serial, g=generation: self._people_groups_error(s, g, message)
        )
        self.pool.start(task)

    def _people_groups_ready(self, serial: int, generation: int, groups) -> None:
        if serial != self._people_query_serial or generation != self._root_generation:
            return
        groups = list(groups)
        self.people_view.set_groups(groups)
        for group in groups:
            task = ImageAssetTask(
                group.representative_image_path,
                (
                    FaceBounds(
                        group.representative_face_x,
                        group.representative_face_y,
                        group.representative_face_w,
                        group.representative_face_h,
                    ),
                ),
                group.representative_thumbnail_path,
                self,
            )
            task.signals.result.connect(
                lambda asset, s=serial, g=generation, p=group.person_id: self._person_crop_ready(
                    s, g, p, asset
                )
            )
            task.signals.error.connect(
                lambda _message, s=serial, g=generation, p=group.person_id: self._person_crop_failed(
                    s, g, p
                )
            )
            self.pool.start(task)

    def _people_groups_error(self, serial: int, generation: int, message: str) -> None:
        if serial == self._people_query_serial and generation == self._root_generation:
            logger.error("Could not load people groups for %s: %s", self._root, message)
            self.people_view.set_error(message)

    def _person_crop_ready(
        self, serial: int, generation: int, person_id: int, asset: ImageAsset
    ) -> None:
        if serial != self._people_query_serial or generation != self._root_generation:
            return
        image = asset.crops[0] if asset.crops and not asset.crops[0].isNull() else asset.image
        if not image.isNull():
            self.people_view.set_crop(person_id, QPixmap.fromImage(image))

    def _person_crop_failed(self, serial: int, generation: int, person_id: int) -> None:
        if serial == self._people_query_serial and generation == self._root_generation:
            card = self.people_view.cards.get(person_id)
            if card is not None:
                card.setToolTip(f"{card.toolTip()}\nRepresentative face unavailable")

    def _apply_people_filter(self, person_ids, mode: str) -> None:
        selected = tuple(dict.fromkeys(int(person_id) for person_id in person_ids))
        if not selected:
            return
        self._people_filter_ids = selected
        self._people_filter_mode = mode if mode in {"any", "all"} else "any"
        self._people_query_serial += 1
        self._restore_photo_view()
        self._update_people_filter_indicator()
        self._clear_selected_detail()
        self._browser_serial += 1
        self._refresh(self._root_generation)

    def _cancel_people_picker(self) -> None:
        self._people_query_serial += 1
        self._restore_photo_view()

    def _restore_photo_view(self) -> None:
        target = self._people_return_widget
        if target not in (self.browser, self.calendar):
            target = self.browser
        self.calendar_stack.setCurrentWidget(target)
        self.browser_button.blockSignals(True)
        self.calendar_button.blockSignals(True)
        self.browser_button.setChecked(target is self.browser)
        self.calendar_button.setChecked(target is self.calendar)
        self.browser_button.blockSignals(False)
        self.calendar_button.blockSignals(False)
        self.browser_button.setEnabled(True)
        self.calendar_button.setEnabled(True)

    def _update_people_filter_indicator(self) -> None:
        active = bool(self._people_filter_ids)
        if active:
            match = "Any" if self._people_filter_mode == "any" else "All"
            message = f"People filter: {len(self._people_filter_ids)} selected · {match} match"
            self.people_filter_label.setText(message)
            self.people_filter_label.setToolTip(message)
        self.people_filter_label.setVisible(active)
        self.edit_people_filter_button.setVisible(active)
        self.clear_people_filter_button.setVisible(active)

    def _clear_selected_detail(self) -> None:
        self._selected_image_id = None
        self._detail_serial += 1
        self.detail.clear()

    def _clear_people_filter(self) -> None:
        self._people_filter_ids = ()
        self._people_filter_mode = "any"
        self._people_query_serial += 1
        if self.calendar_stack.currentWidget() is self.people_view:
            self._restore_photo_view()
        self._update_people_filter_indicator()
        self._clear_selected_detail()
        self._browser_serial += 1
        self._refresh(self._root_generation)

    def _refresh(self, generation: int) -> None:
        root = self._root
        directory = self.browser.directory
        browser_serial = self._browser_serial
        self._catalog_serial += 1
        catalog_serial = self._catalog_serial
        persons = self._people_filter_ids
        person_match = self._people_filter_mode
        task = FunctionTask(
            lambda: (
                catalog.status_counts(root=root),
                catalog.browser_images(
                    root=root, directory=directory, persons=persons or None, person_match=person_match
                ),
                catalog.calendar_groups(root=root, persons=persons or None, person_match=person_match),
                catalog.browser_images(root=root, persons=persons or None, person_match=person_match),
            ),
            self,
        )
        task.signals.result.connect(
            lambda snapshot, g=generation, d=directory, b=browser_serial, c=catalog_serial: self._catalog_ready(
                g, snapshot, d, b, c
            )
        )
        task.signals.error.connect(
            lambda message, g=generation, b=browser_serial, c=catalog_serial: self._refresh_error(
                g, b, message, c
            )
        )
        self.pool.start(task)

    def _catalog_ready(
        self, generation: int, snapshot, directory: Path, browser_serial: int,
        catalog_serial: int | None = None,
    ) -> None:
        if generation != self._root_generation or (
            catalog_serial is not None and catalog_serial != self._catalog_serial
        ):
            return
        counts, browser_items, groups, all_browser_items = snapshot
        total = sum(counts.values())
        not_analysed = counts.get("indexed", 0) + counts.get("pending", 0)
        self.total_label.setText(f"Total {total}")
        self.analysed_label.setText(f"Analysed {counts.get('analysed', 0)}")
        self.not_analysed_label.setText(f"Not analysed {not_analysed}")
        self.errors_label.setText(f"Errors {counts.get('error', 0)}")
        self.browser.set_catalog_items(all_browser_items)
        if browser_serial == self._browser_serial and directory == self.browser.directory:
            self.browser.model.set_items(browser_items)
        self.calendar_model.set_groups(groups)
        if browser_serial == self._browser_serial and directory == self.browser.directory:
            self._load_thumbnails(self.browser.model, browser_items, generation)
        self._load_thumbnails(self.calendar_model, self.calendar_model.items, generation)
        if (
            self._selected_image_id is not None
            and self._people_filter_ids
            and self._selected_image_id not in {item.id for item in all_browser_items}
        ):
            self._clear_selected_detail()

    def _catalog_error(self, generation: int, message: str) -> None:
        if generation != self._root_generation:
            return
        self._set_status(f"Catalogue unavailable: {message}")
        logger.error("Catalogue callback failed for %s: %s", self._root, message)
        self.browser.model.set_items([])
        self.calendar_model.set_groups({})

    def _refresh_error(
        self, generation: int, browser_serial: int, message: str,
        catalog_serial: int | None = None,
    ) -> None:
        logger.error("Catalogue refresh callback failed for %s: %s", self._root, message)
        if browser_serial == self._browser_serial and (
            catalog_serial is None or catalog_serial == self._catalog_serial
        ):
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
        self._set_thumbnail_batch(generation, model, iter(values))

    def _set_thumbnail_batch(self, generation: int, model, images) -> None:
        if generation != self._root_generation:
            return
        for _ in range(32):
            try:
                image_id, image = next(images)
            except StopIteration:
                return
            if not image.isNull():
                model.set_pixmap(image_id, QPixmap.fromImage(image))
        QTimer.singleShot(
            0,
            lambda g=generation, m=model, values=images: self._set_thumbnail_batch(
                g, m, values
            ),
        )

    def _browse_directory(self, directory: Path) -> None:
        self.browser.set_directory(directory)
        self._browser_serial += 1
        browser_serial = self._browser_serial
        generation = self._root_generation
        root = self._root
        persons = self._people_filter_ids
        person_match = self._people_filter_mode
        task = FunctionTask(
            lambda: catalog.browser_images(
                root=root, directory=directory, persons=persons or None, person_match=person_match
            ),
            self,
        )
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

    def _switch_view(self, index: int, checked: bool = True) -> None:
        if checked:
            self.calendar_stack.setCurrentIndex(index)

    def _image_clicked(self, item) -> None:
        self._selected_image_id = item.id
        self._load_detail(item.id)

    def _load_detail(self, image_id: int) -> None:
        self._detail_serial += 1
        serial = self._detail_serial
        generation = self._root_generation
        root = self._root
        task = FunctionTask(lambda: catalog.get_image_detail(image_id, root=root), self)
        task.signals.result.connect(
            lambda detail, s=serial, g=generation: self._detail_ready(s, detail, g)
        )
        task.signals.error.connect(
            lambda message, s=serial, g=generation: self._detail_error(s, message, g)
        )
        self.pool.start(task)

    def _detail_ready(self, serial: int, detail, generation: int | None = None) -> None:
        if serial == self._detail_serial and (
            generation is None or generation == self._root_generation
        ):
            self.detail.show_detail(detail)

    def _detail_error(self, serial: int, message: str, generation: int | None = None) -> None:
        if serial == self._detail_serial and (
            generation is None or generation == self._root_generation
        ):
            logger.error("Detail catalogue callback failed for image request: %s", message)
            self.detail.clear(f"Could not load details: {message}")

    def _reload_selected_detail(self) -> None:
        if self._selected_image_id is not None:
            self._load_detail(self._selected_image_id)

    def _request_face_label(self, face_id: int) -> None:
        if self._face_label_active:
            return
        generation = self._root_generation
        root = self._root
        name, accepted = QInputDialog.getText(
            self,
            "Label face group",
            "Name this person:",
            QLineEdit.EchoMode.Normal,
        )
        if not accepted:
            return
        name = name.strip()
        if not name:
            message = "Person name cannot be empty."
            self.detail.set_action_message(message)
            self._set_status(message)
            return
        self._face_label_active = True
        self.detail.set_action_message(f"Labelling face group as {name}…")
        task = FunctionTask(
            lambda: analyser.label_face_group(face_id, name, root=root), self
        )
        task.signals.result.connect(
            lambda result, g=generation, n=name: self._face_labelled(g, n, result)
        )
        task.signals.error.connect(
            lambda message, g=generation: self._face_label_error(g, message)
        )
        self.pool.start(task)

    def _face_labelled(self, generation: int, name: str, result) -> None:
        if generation != self._root_generation:
            return
        self._face_label_active = False
        self.detail.set_action_message(
            f"Labelled {result.face_count} face(s) as {result.person_name}."
        )
        self._set_status(f"Labelled {result.face_count} face(s) as {result.person_name}")
        self._refresh(generation)
        self._reload_selected_detail()
        self._check_pending_proposals()

    def _face_label_error(self, generation: int, message: str) -> None:
        if generation != self._root_generation:
            return
        self._face_label_active = False
        visible = f"Could not label face group: {message}"
        logger.error(visible)
        self.detail.set_action_message(visible)
        self._set_status(visible)

    @staticmethod
    def _proposal_key(proposal: catalog.FaceMatchProposal) -> tuple[int, int]:
        return proposal.face_id, proposal.target_person_id

    def _check_pending_proposals(self) -> None:
        self._proposal_query_serial += 1
        serial = self._proposal_query_serial
        generation = self._root_generation
        root = self._root
        task = FunctionTask(
            lambda: catalog.list_pending_face_match_proposals(root=root), self
        )
        task.signals.result.connect(
            lambda proposals, s=serial, g=generation: self._pending_proposals_ready(
                s, g, proposals
            )
        )
        task.signals.error.connect(
            lambda message, s=serial, g=generation: self._pending_proposals_error(
                s, g, message
            )
        )
        self.pool.start(task)

    def _pending_proposals_ready(self, serial: int, generation: int, proposals) -> None:
        if serial != self._proposal_query_serial or generation != self._root_generation:
            return
        queued = {self._proposal_key(item) for item in self._proposal_queue}
        active_key = (
            self._proposal_key(self._proposal_dialog.proposal)
            if self._proposal_dialog is not None
            else None
        )
        for proposal in proposals:
            key = self._proposal_key(proposal)
            if key in self._proposal_deferred or key == active_key or key in queued:
                continue
            self._proposal_queue.append(proposal)
            queued.add(key)
        self._show_next_proposal()

    def _pending_proposals_error(self, serial: int, generation: int, message: str) -> None:
        if serial != self._proposal_query_serial or generation != self._root_generation:
            return
        logger.error("Could not list pending face-match proposals: %s", message)
        self._set_status(f"Could not load pending face-match proposals: {message}")

    def _show_next_proposal(self) -> None:
        if self._proposal_dialog is not None:
            return
        while self._proposal_queue:
            proposal = self._proposal_queue.pop(0)
            key = self._proposal_key(proposal)
            if key in self._proposal_deferred:
                continue
            dialog = FaceMatchReviewDialog(proposal, self)
            self._proposal_dialog = dialog
            dialog.merge_requested.connect(
                lambda d=dialog: self._resolve_face_proposal(d, accept=True)
            )
            dialog.reject_requested.connect(
                lambda d=dialog: self._resolve_face_proposal(d, accept=False)
            )
            dialog.finished.connect(
                lambda _result, d=dialog, k=key: self._proposal_dialog_finished(d, k)
            )
            generation = self._root_generation
            self._load_proposal_sample(
                dialog,
                candidate=True,
                path=proposal.sample_image_path,
                thumbnail=proposal.sample_thumbnail_path,
                bounds=FaceBounds(
                    proposal.face_x, proposal.face_y, proposal.face_w, proposal.face_h
                ),
                generation=generation,
            )
            if proposal.target_sample_image_path:
                self._load_proposal_sample(
                    dialog,
                    candidate=False,
                    path=proposal.target_sample_image_path,
                    thumbnail=proposal.target_sample_thumbnail_path,
                    bounds=FaceBounds(
                        proposal.target_face_x or 0,
                        proposal.target_face_y or 0,
                        proposal.target_face_w or 0,
                        proposal.target_face_h or 0,
                    ),
                    generation=generation,
                )
            else:
                dialog.set_sample(False, QPixmap())
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return

    def _load_proposal_sample(
        self,
        dialog: FaceMatchReviewDialog,
        *,
        candidate: bool,
        path: str,
        thumbnail: str | None,
        bounds: FaceBounds,
        generation: int,
    ) -> None:
        task = ImageAssetTask(path, (bounds,), thumbnail, self)
        task.signals.result.connect(
            lambda asset, d=dialog, c=candidate, g=generation: self._proposal_sample_ready(
                d, c, g, asset
            )
        )
        task.signals.error.connect(
            lambda _message, d=dialog, c=candidate, g=generation: self._proposal_sample_failed(
                d, c, g
            )
        )
        self.pool.start(task)

    def _proposal_sample_ready(
        self, dialog: FaceMatchReviewDialog, candidate: bool, generation: int, asset: ImageAsset
    ) -> None:
        if dialog is not self._proposal_dialog or generation != self._root_generation:
            return
        image = asset.crops[0] if asset.crops and not asset.crops[0].isNull() else asset.image
        dialog.set_sample(candidate, QPixmap.fromImage(image) if not image.isNull() else QPixmap())

    def _proposal_sample_failed(
        self, dialog: FaceMatchReviewDialog, candidate: bool, generation: int
    ) -> None:
        if dialog is self._proposal_dialog and generation == self._root_generation:
            dialog.set_sample(candidate, QPixmap())

    def _proposal_dialog_finished(
        self, dialog: FaceMatchReviewDialog, key: tuple[int, int]
    ) -> None:
        if dialog is not self._proposal_dialog:
            return
        self._proposal_dialog = None
        self._proposal_deferred.add(key)
        self._show_next_proposal()

    def _resolve_face_proposal(self, dialog: FaceMatchReviewDialog, *, accept: bool) -> None:
        if dialog is not self._proposal_dialog or dialog._busy:
            return
        proposal = dialog.proposal
        generation = self._root_generation
        root = self._root
        dialog.set_busy(True)
        function: Callable[
            [], analyser.GroupLabelResult | analyser.FaceMatchResolutionResult
        ]
        if accept:
            function = lambda: analyser.accept_face_match_proposal(
                proposal.face_id, proposal.target_person_id, root=root
            )
        else:
            function = lambda: analyser.reject_face_match_proposal(
                proposal.face_id, proposal.target_person_id, root=root
            )
        task = FunctionTask(function, self)
        task.signals.result.connect(
            lambda result, d=dialog, g=generation, a=accept: self._proposal_resolved(
                d, g, a, result
            )
        )
        task.signals.error.connect(
            lambda message, d=dialog, g=generation, a=accept: self._proposal_resolution_error(
                d, g, a, message
            )
        )
        self.pool.start(task)

    def _proposal_resolved(
        self, dialog: FaceMatchReviewDialog, generation: int, accepted: bool, result
    ) -> None:
        if generation != self._root_generation:
            return
        self._proposal_queue.clear()
        self._proposal_query_serial += 1
        self._refresh(generation)
        self._reload_selected_detail()
        if accepted:
            self._set_status(
                f"Merged {result.face_count} face(s) into {result.person_name}"
            )
        else:
            self._set_status(
                f"Rejected the match with {dialog.proposal.target_person_name} "
                f"for {result.group_face_count} face(s)"
            )
        if dialog is self._proposal_dialog:
            dialog.set_busy(False)
            dialog.accept()
        self._check_pending_proposals()

    def _proposal_resolution_error(
        self, dialog: FaceMatchReviewDialog, generation: int, accepted: bool, message: str
    ) -> None:
        if generation != self._root_generation:
            return
        action = "merge" if accepted else "reject"
        visible = f"Could not {action} face match: {message}"
        logger.error(visible)
        self._set_status(visible)
        if dialog is self._proposal_dialog:
            dialog.set_message(visible)
            dialog.set_busy(False)

    def _visualisation_changed(self, enabled: bool) -> None:
        self.detail.set_visualisation(enabled)

    def _analyse_selected(self) -> None:
        active_view = self.calendar_stack.currentWidget()
        view = self.browser.view if active_view is self.browser else active_view
        image_ids = view.selected_image_ids()
        logger.info(f'Analysing images {image_ids}')
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
        self._check_pending_proposals()

    def _analysis_catalogue_changed(self) -> None:
        self._refresh(self._root_generation)
        self._check_pending_proposals()

    def _analysis_failed(self, message: str) -> None:
        logger.error("Analysis failed: %s", message)
        self.cancel_button.setEnabled(False)
        self._set_status(message)
        self._refresh(self._root_generation)

    def closeEvent(self, event) -> None:
        self._scan_cancel.set()
        dialog = self._proposal_dialog
        self._proposal_dialog = None
        if dialog is not None:
            dialog.set_busy(False)
            dialog.close()
        self.analysis.shutdown()
        self.pool.clear()
        event.accept()
