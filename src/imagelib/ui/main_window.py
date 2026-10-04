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

logger = logging.getLogger(__name__)


def _use_surface(widget: QWidget, role: QPalette.ColorRole) -> None:
    widget.setAutoFillBackground(True)
    widget.setBackgroundRole(role)


def _mute_label(label: QLabel) -> None:
    palette = label.palette()
    palette.setColor(QPalette.ColorRole.WindowText, palette.color(QPalette.ColorRole.PlaceholderText))
    label.setPalette(palette)


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def _break_opportunities(text: str) -> str:
    return (
        text.replace("\u200b", "")
        .replace("/", "/\u200b")
        .replace("\\", "\\\u200b")
        .replace("-", "-\u200b")
        .replace("_", "_\u200b")
    )


def _thumb_metrics(column_width: int) -> tuple[int, int]:
    margins = 8
    spacing = 3
    usable = max(0, column_width - margins)
    button = max(1, (usable - spacing * 2) // 3)
    icon = min(48, max(1, button - 6))
    if icon >= button:
        icon = max(1, button - 1)
    return button, icon


def _elide_two_lines(text: str, width: int, metrics: QFontMetrics) -> str:
    if width <= 0 or metrics.horizontalAdvance(text) <= width:
        return text
    words = text.split(" ")
    if len(words) <= 1:
        return metrics.elidedText(text, Qt.TextElideMode.ElideRight, width)
    lines: list[str] = []
    index = 0
    while index < len(words) and len(lines) < 2:
        current = words[index]
        index += 1
        while index < len(words):
            trial = f"{current} {words[index]}"
            if metrics.horizontalAdvance(trial) > width:
                break
            current = trial
            index += 1
        if len(lines) == 1 and index < len(words):
            current = metrics.elidedText(" ".join([current, *words[index:]]), Qt.TextElideMode.ElideRight, width)
            index = len(words)
        elif metrics.horizontalAdvance(current) > width:
            current = metrics.elidedText(current, Qt.TextElideMode.ElideRight, width)
        lines.append(current)
    return "\n".join(lines)


class ElidedLabel(QLabel):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._apply_elide()

    def minimumSizeHint(self) -> QSize:
        return QSize(0, max(self.fontMetrics().lineSpacing(), 1))

    def sizeHint(self) -> QSize:
        return QSize(0, self.minimumSizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.contentsRect().width()
        if width <= 0:
            if self.text() != self._full_text:
                super().setText(self._full_text)
            return
        elided = self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideMiddle, width)
        if self.text() != elided:
            super().setText(elided)


class WrappingLabel(QLabel):
    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setWordWrap(True)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        super().setText(_break_opportunities(text))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().lineSpacing())

    def sizeHint(self) -> QSize:
        width = 240
        height = self.heightForWidth(width)
        if height < 0:
            height = self.fontMetrics().lineSpacing()
        return QSize(width, height)

    def hasHeightForWidth(self) -> bool:
        return True


class FaceNameLabel(QLabel):
    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self._full_text = text
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setToolTip(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        line = max(self.fontMetrics().lineSpacing(), 1)
        self.setMaximumHeight(line * 2)
        super().setText(text)

    def hasHeightForWidth(self) -> bool:
        return False

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().lineSpacing())

    def sizeHint(self) -> QSize:
        return QSize(96, self.fontMetrics().lineSpacing())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = max(1, self.contentsRect().width())
        display = _elide_two_lines(self._full_text, width, self.fontMetrics())
        if self.text() != display:
            super().setText(display)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        width = max(1, self.contentsRect().width())
        display = _elide_two_lines(self._full_text, width, self.fontMetrics())
        if self.text() != display:
            super().setText(display)


class ButtonRowScroll(QScrollArea):
    def __init__(self, content: QWidget, parent=None) -> None:
        super().__init__(parent)
        self._fitting = False
        self.setWidget(content)
        self.setWidgetResizable(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        _use_surface(self, QPalette.ColorRole.Base)
        self.fit()

    def minimumSizeHint(self) -> QSize:
        content = self.widget()
        height = content.sizeHint().height() if content is not None else 0
        return QSize(0, max(height, 1))

    def sizeHint(self) -> QSize:
        return self.minimumSizeHint()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit()

    def fit(self) -> None:
        if self._fitting:
            return
        content = self.widget()
        if content is None:
            return
        self._fitting = True
        try:
            layout = content.layout()
            if layout is not None:
                layout.activate()
            hint = content.sizeHint()
            viewport_width = self.viewport().width()
            overflows = viewport_width > 0 and hint.width() > viewport_width
            bar = self.horizontalScrollBar().sizeHint().height() if overflows else 0
            target = QSize(max(hint.width(), viewport_width), max(hint.height(), 1))
            if content.size() != target:
                content.resize(target)
            fitted = target.height() + bar
            if self.height() != fitted:
                self.setFixedHeight(fitted)
        finally:
            self._fitting = False


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
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        _use_surface(self, QPalette.ColorRole.Base)
        self.clicked.connect(self._clicked)

    def _clicked(self, index: QModelIndex) -> None:
        item = self.model().item(index)
        if item is not None:
            self.image_clicked.emit(item)

    def selected_image_ids(self) -> list[int]:
        return [item.id for item in (self.model().item(index) for index in self.selectedIndexes()) if item]


class CalendarDayCell(QFrame):
    image_clicked = Signal(object)
    selection_changed = Signal()

    def __init__(
        self,
        day: date | None,
        items,
        pixmaps: dict[int, QPixmap],
        selected_ids: set[int],
        outside_month: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.day = day
        self.items = list(items)
        self._buttons: dict[int, QToolButton] = {}
        self.setObjectName("calendarOutsideDay" if outside_month else "calendarDay")
        self.setProperty("outsideMonth", outside_month)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumHeight(96)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setBackgroundRole(QPalette.ColorRole.Window if outside_month else QPalette.ColorRole.AlternateBase)
        self.setAutoFillBackground(True)
        self._thumb_button = 54
        self._thumb_icon = 48

        header = QHBoxLayout()
        header.setContentsMargins(4, 2, 4, 0)
        header.setSpacing(2)
        heading = QLabel(str(day.day) if day else "Unknown date")
        heading.setObjectName("calendarDayNumber")
        heading.setMinimumWidth(0)
        if outside_month:
            _mute_label(heading)
        header.addWidget(heading)
        if self.items:
            count = QLabel(str(len(self.items)))
            count.setToolTip(f"{len(self.items)} image(s)")
            count.setMinimumWidth(0)
            header.addWidget(count)
            self.select_button = QToolButton()
            self.select_button.setText("All")
            self.select_button.setToolTip("Select all images in this date")
            self.select_button.setCheckable(True)
            self.select_button.setAutoRaise(True)
            self.select_button.setMinimumWidth(0)
            self.select_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            self.select_button.clicked.connect(self._select_all)
            header.addWidget(self.select_button)
        else:
            self.select_button = None
        header.addStretch(1)

        self.image_layout = QGridLayout()
        self.image_layout.setContentsMargins(4, 2, 4, 4)
        self.image_layout.setSpacing(3)
        self.image_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        for index, item in enumerate(self.items):
            button = QToolButton()
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(self._thumb_icon, self._thumb_icon))
            button.setFixedSize(self._thumb_button, self._thumb_button)
            button.setToolTip(f"{item.relative_path}\nClick to open image details")
            button.setAccessibleName(Path(item.path).name)
            button.toggled.connect(lambda _checked, b=button: self._button_toggled(b))
            button.clicked.connect(lambda _checked=False, image=item: self.image_clicked.emit(image))
            self._buttons[item.id] = button
            self._set_button_pixmap(button, pixmaps.get(item.id))
            button.setChecked(item.id in selected_ids)
            self.image_layout.addWidget(button, index // 3, index % 3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addLayout(self.image_layout)
        self._update_select_button()

    def apply_thumb_size(self, button_size: int, icon_size: int) -> None:
        button_size = max(1, button_size)
        icon_size = max(1, min(icon_size, button_size))
        if button_size == self._thumb_button and icon_size == self._thumb_icon:
            return
        self._thumb_button = button_size
        self._thumb_icon = icon_size
        for button in self._buttons.values():
            button.setIconSize(QSize(icon_size, icon_size))
            button.setFixedSize(button_size, button_size)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.Mid))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    @staticmethod
    def _set_button_pixmap(button: QToolButton, pixmap: QPixmap | None) -> None:
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            button.setIcon(QIcon(pixmap))
            button.setText("")
        else:
            button.setIcon(QIcon())
            button.setText("…")

    def _button_toggled(self, button: QToolButton) -> None:
        self._update_select_button()
        self.selection_changed.emit()

    def _update_select_button(self) -> None:
        if self.select_button is not None:
            selected = bool(self.items) and all(button.isChecked() for button in self._buttons.values())
            self.select_button.blockSignals(True)
            self.select_button.setChecked(selected)
            self.select_button.blockSignals(False)

    def _select_all(self, checked: bool) -> None:
        for button in self._buttons.values():
            button.setChecked(checked)
        self._update_select_button()
        self.selection_changed.emit()

    def set_pixmap(self, image_id: int, pixmap: QPixmap) -> None:
        button = self._buttons.get(image_id)
        if button is not None:
            self._set_button_pixmap(button, pixmap)

    def selected_image_ids(self) -> list[int]:
        return [image_id for image_id, button in self._buttons.items() if button.isChecked()]


class CalendarView(QWidget):
    image_clicked = Signal(object)
    month_changed = Signal(object)
    _minimum_month = date.min.replace(day=1)
    _maximum_month = date.max.replace(day=1)

    def __init__(self, model: CalendarModel, parent=None) -> None:
        super().__init__(parent)
        self.model = model
        self._selected_ids: set[int] = set()
        self.current_month = date.today().replace(day=1)
        self.day_cells: dict[date, CalendarDayCell] = {}
        self.unknown_cell: CalendarDayCell | None = None
        self._syncing_navigation_controls = False

        self.previous_button = QPushButton("‹")
        self.previous_button.setToolTip("Previous month")
        self.previous_button.clicked.connect(lambda: self.set_month(self._offset_month(-1)))
        self.next_button = QPushButton("›")
        self.next_button.setToolTip("Next month")
        self.next_button.clicked.connect(lambda: self.set_month(self._offset_month(1)))
        self.today_button = QPushButton("Today")
        self.today_button.clicked.connect(lambda: self.set_month(date.today().replace(day=1)))
        self.month_selector = QComboBox()
        self.month_selector.addItems(
            [date(2000, month, 1).strftime("%B") for month in range(1, 13)]
        )
        self.month_selector.setToolTip("Select month")
        self.month_selector.setAccessibleName("Calendar month")
        self.month_selector.currentIndexChanged.connect(self._month_selector_changed)
        self.year_selector = QSpinBox()
        self.year_selector.setRange(date.min.year, date.max.year)
        self.year_selector.setKeyboardTracking(True)
        self.year_selector.setToolTip("Enter year")
        self.year_selector.setAccessibleName("Calendar year")
        self.year_selector.valueChanged.connect(self._year_selector_changed)
        self.month_label = QLabel()
        self.month_label.setObjectName("calendarMonthLabel")
        self.month_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.month_label.setMinimumWidth(0)
        month_font = self.month_label.font()
        month_font.setBold(True)
        self.month_label.setFont(month_font)
        navigation = QWidget()
        toolbar = QHBoxLayout(navigation)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self.previous_button)
        toolbar.addWidget(self.month_label, 1)
        toolbar.addWidget(self.month_selector)
        toolbar.addWidget(self.year_selector)
        toolbar.addWidget(self.today_button)
        toolbar.addWidget(self.next_button)
        self.navigation_scroll = ButtonRowScroll(navigation)

        self.weekday_layout = QGridLayout()
        self.weekday_layout.setContentsMargins(3, 0, 3, 0)
        self.weekday_layout.setSpacing(3)
        weekday_names = (
            ("Mon", "Monday"),
            ("Tue", "Tuesday"),
            ("Wed", "Wednesday"),
            ("Thu", "Thursday"),
            ("Fri", "Friday"),
            ("Sat", "Saturday"),
            ("Sun", "Sunday"),
        )
        for column, (name, full_name) in enumerate(weekday_names):
            label = QLabel(name)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setObjectName("calendarWeekday")
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setToolTip(full_name)
            _mute_label(label)
            self.weekday_layout.addWidget(label, 0, column)
            self.weekday_layout.setColumnStretch(column, 1)

        self.grid_widget = QWidget()
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setContentsMargins(3, 3, 3, 3)
        self.grid_layout.setSpacing(3)
        for column in range(7):
            self.grid_layout.setColumnStretch(column, 1)
            self.grid_layout.setColumnMinimumWidth(column, 0)
        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.grid_scroll.setWidget(self.grid_widget)
        self.grid_scroll.viewport().installEventFilter(self)
        _use_surface(self.grid_widget, QPalette.ColorRole.Base)

        self.unknown_title = QLabel("Unknown date")
        self.unknown_title.setObjectName("calendarUnknownTitle")
        self.unknown_layout = QVBoxLayout()
        self.unknown_layout.setContentsMargins(0, 0, 0, 0)
        self.unknown_layout.addWidget(self.unknown_title)
        self.unknown_area = QWidget()
        self.unknown_area.setLayout(self.unknown_layout)
        self.unknown_area.setVisible(False)

        _use_surface(self, QPalette.ColorRole.Base)
        layout = QVBoxLayout(self)
        layout.addWidget(self.navigation_scroll)
        layout.addLayout(self.weekday_layout)
        layout.addWidget(self.grid_scroll, 1)
        layout.addWidget(self.unknown_area)

        self.model.modelReset.connect(self._render)
        self.model.pixmap_changed.connect(self.set_pixmap)
        self._sync_navigation_controls()
        self._render()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.grid_scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._apply_thumb_sizes()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_thumb_sizes()

    def _day_column_width(self) -> int:
        width = self.grid_scroll.viewport().width()
        if width <= 0:
            return 0
        margins = self.grid_layout.contentsMargins()
        spacing = self.grid_layout.spacing()
        available = width - margins.left() - margins.right() - spacing * 6
        return max(0, available // 7)

    def _apply_thumb_sizes(self) -> None:
        column = self._day_column_width()
        if column <= 0:
            return
        button_size, icon_size = _thumb_metrics(column)
        for cell in self.day_cells.values():
            cell.setMaximumWidth(column)
            cell.apply_thumb_size(button_size, icon_size)
        if self.unknown_cell is not None:
            self.unknown_cell.apply_thumb_size(button_size, icon_size)
        bar = self.grid_scroll.verticalScrollBar()
        extra = bar.sizeHint().width() if bar.isVisible() else 0
        self.weekday_layout.setContentsMargins(3, 0, 3 + extra, 0)

    @staticmethod
    def _first_of_month(value: date) -> date:
        return value.replace(day=1)

    def _offset_month(self, offset: int) -> date:
        month_number = self.current_month.year * 12 + self.current_month.month - 1 + offset
        minimum_number = self._minimum_month.year * 12 + self._minimum_month.month - 1
        maximum_number = self._maximum_month.year * 12 + self._maximum_month.month - 1
        month_number = max(minimum_number, min(maximum_number, month_number))
        return date(month_number // 12, month_number % 12 + 1, 1)

    def _month_selector_changed(self, month_index: int) -> None:
        if not self._syncing_navigation_controls:
            self.set_month(date(self.year_selector.value(), month_index + 1, 1))

    def _year_selector_changed(self, year: int) -> None:
        if not self._syncing_navigation_controls:
            self.set_month(date(year, self.month_selector.currentIndex() + 1, 1))

    def _sync_navigation_controls(self) -> None:
        self._syncing_navigation_controls = True
        try:
            self.month_selector.blockSignals(True)
            self.year_selector.blockSignals(True)
            self.month_selector.setCurrentIndex(self.current_month.month - 1)
            self.year_selector.setValue(self.current_month.year)
        finally:
            self.month_selector.blockSignals(False)
            self.year_selector.blockSignals(False)
            self._syncing_navigation_controls = False
        self.previous_button.setEnabled(self.current_month > self._minimum_month)
        self.next_button.setEnabled(self.current_month < self._maximum_month)

    def set_month(self, month: date) -> None:
        month = self._first_of_month(month)
        if month == self.current_month:
            self._sync_navigation_controls()
            self._render()
            return
        self.current_month = month
        self._sync_navigation_controls()
        self._render()
        self.month_changed.emit(month)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            child = layout.takeAt(0)
            if child.widget() is not None:
                child.widget().deleteLater()

    def _render(self) -> None:
        all_ids = {item.id for item in self.model.items}
        self._selected_ids.intersection_update(all_ids)
        self.month_label.setText(self.current_month.strftime("%B %Y"))
        self._clear_layout(self.grid_layout)
        self.day_cells = {}
        first = self.current_month
        for position in range(42):
            try:
                day = first + timedelta(days=position - first.weekday())
            except OverflowError:
                cell = QWidget(self.grid_widget)
                cell.setMinimumHeight(96)
                cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
                self.grid_layout.addWidget(cell, position // 7, position % 7)
                continue
            in_month = day.month == first.month
            cell = CalendarDayCell(
                day,
                self.model.groups.get(day, []) if in_month else [],
                self.model.pixmaps,
                self._selected_ids,
                outside_month=not in_month,
                parent=self.grid_widget,
            )
            cell.image_clicked.connect(self.image_clicked)
            cell.selection_changed.connect(lambda c=cell: self._cell_selection_changed(c))
            self.grid_layout.addWidget(cell, position // 7, position % 7)
            self.day_cells[day] = cell

        while self.unknown_layout.count() > 1:
            child = self.unknown_layout.takeAt(1)
            if child.widget() is not None:
                child.widget().deleteLater()
        unknown_items = self.model.groups.get(None, [])
        self.unknown_cell = None
        if unknown_items:
            self.unknown_cell = CalendarDayCell(
                None,
                unknown_items,
                self.model.pixmaps,
                self._selected_ids,
                parent=self.unknown_area,
            )
            self.unknown_cell.image_clicked.connect(self.image_clicked)
            self.unknown_cell.selection_changed.connect(
                lambda c=self.unknown_cell: self._cell_selection_changed(c)
            )
            self.unknown_layout.addWidget(self.unknown_cell)
            self.unknown_area.setVisible(True)
        else:
            self.unknown_area.setVisible(False)
        self._apply_thumb_sizes()

    def _cell_selection_changed(self, cell: CalendarDayCell) -> None:
        cell_ids = {item.id for item in cell.items}
        self._selected_ids.difference_update(cell_ids)
        self._selected_ids.update(cell.selected_image_ids())

    def set_pixmap(self, image_id: int, pixmap: QPixmap) -> None:
        for cell in self.day_cells.values():
            cell.set_pixmap(image_id, pixmap)
        if self.unknown_cell is not None:
            self.unknown_cell.set_pixmap(image_id, pixmap)

    def selected_image_ids(self) -> list[int]:
        return [item.id for item in self.model.items if item.id in self._selected_ids]


@dataclass(frozen=True)
class ChronologicalDayGroup:
    day: date | None
    items: list[catalog.ImageListItem]
    month_marker: bool
    year_marker: bool
    marker_text: str


@dataclass(frozen=True)
class _ChronologicalRow:
    group: ChronologicalDayGroup
    top: int
    height: int
    title_top: int
    grid_top: int
    grid_x: int
    columns: int
    tile_size: int
    image_rows: int


class _ChronologicalCanvas(QWidget):
    image_clicked = Signal(object)
    selection_changed = Signal()

    _left_margin = 32
    _right_margin = 12
    _tile_gap = 8
    _tile_size_limit = 104
    _row_gap = 1

    def __init__(self, model: CalendarModel, parent=None) -> None:
        super().__init__(parent)
        self.model = model
        self.day_groups: list[ChronologicalDayGroup] = []
        self._rows: list[_ChronologicalRow] = []
        self._row_tops: list[int] = []
        self._row_by_date: dict[date | None, _ChronologicalRow] = {}
        self._item_locations: dict[int, tuple[int, int]] = {}
        self._ordered_items: list[catalog.ImageListItem] = []
        self._group_offsets: list[int] = []
        self._selected_ids: set[int] = set()
        self._anchor_id: int | None = None
        self._focus_id: int | None = None
        self._layout_width = 0
        self._content_height = 1
        self._reflowing = False
        self.setMinimumWidth(0)
        self.setMinimumHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("Chronological image timeline")
        self.setAccessibleDescription(
            "Images are grouped by date from oldest to newest. "
            "Circles mark the first pictured date in a month; diamonds mark the first pictured date in a year. "
            "Unknown dates appear last."
        )
        self.setToolTip(self.accessibleDescription())
        self.model.modelReset.connect(self.refresh)
        self.model.pixmap_changed.connect(self._pixmap_changed)
        self.refresh()

    def sizeHint(self) -> QSize:
        return QSize(640, max(self._content_height, 1))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, max(self._content_height, 1))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow(event.size().width())

    def refresh(self) -> None:
        selected_before = set(self._selected_ids)
        ordered_groups = sorted(
            ((day, items) for day, items in self.model.groups.items() if items),
            key=lambda group: (group[0] is None, group[0] or date.min),
        )
        seen_months: set[tuple[int, int]] = set()
        seen_years: set[int] = set()
        groups: list[ChronologicalDayGroup] = []
        for day, items in ordered_groups:
            if day is None:
                groups.append(ChronologicalDayGroup(None, items, False, False, ""))
                continue
            year_marker = day.year not in seen_years
            month_key = (day.year, day.month)
            month_marker = month_key not in seen_months
            seen_years.add(day.year)
            seen_months.add(month_key)
            if year_marker and month_marker:
                marker_text = day.strftime("%B %Y")
            elif year_marker:
                marker_text = str(day.year)
            elif month_marker:
                marker_text = day.strftime("%B")
            else:
                marker_text = ""
            groups.append(
                ChronologicalDayGroup(day, items, month_marker, year_marker, marker_text)
            )

        self.day_groups = groups
        self._ordered_items = [item for group in groups for item in group.items]
        self._item_locations = {}
        self._group_offsets = []
        offset = 0
        for row_index, group in enumerate(groups):
            self._group_offsets.append(offset)
            for item_index, item in enumerate(group.items):
                self._item_locations[item.id] = (row_index, item_index)
            offset += len(group.items)
        self._selected_ids.intersection_update(self._item_locations)
        if self._anchor_id not in self._item_locations:
            self._anchor_id = None
        if self._focus_id not in self._item_locations:
            self._focus_id = None
        self._reflow(self.width(), force=True)
        if self._selected_ids != selected_before:
            self.selection_changed.emit()

    def row_for_date(self, day: date | None) -> _ChronologicalRow | None:
        return self._row_by_date.get(day)

    def item_rect(self, image_id: int) -> QRect:
        location = self._item_locations.get(image_id)
        if location is None or location[0] >= len(self._rows):
            return QRect()
        row_index, item_index = location
        row = self._rows[row_index]
        column = item_index % row.columns
        line = item_index // row.columns
        step = row.tile_size + self._tile_gap
        return QRect(
            row.grid_x + column * step,
            row.grid_top + line * step,
            row.tile_size,
            row.tile_size,
        )

    def selected_image_ids(self) -> list[int]:
        return [item.id for item in self._ordered_items if item.id in self._selected_ids]

    def _ordered_index(self, image_id: int | None) -> int | None:
        location = self._item_locations.get(image_id)
        if location is None:
            return None
        row_index, item_index = location
        return self._group_offsets[row_index] + item_index

    def _reflow(self, width: int, force: bool = False) -> None:
        if self._reflowing:
            return
        width = width if width > 0 else self._layout_width or 640
        if not force and width == self._layout_width:
            return
        self._reflowing = True
        try:
            self._layout_width = width
            rows: list[_ChronologicalRow] = []
            row_by_date: dict[date | None, _ChronologicalRow] = {}
            row_tops: list[int] = []
            y = 8
            for group in self.day_groups:
                grid_x = min(self._left_margin, max(0, width - 1))
                available = max(1, width - grid_x - self._right_margin)
                tile_size = min(self._tile_size_limit, available)
                columns = max(1, (available + self._tile_gap) // (tile_size + self._tile_gap))
                image_rows = (len(group.items) + columns - 1) // columns
                title_top = y + 6 + (22 if group.marker_text else 0)
                grid_top = title_top + 20 + 7
                image_height = image_rows * tile_size + max(0, image_rows - 1) * self._tile_gap
                height = grid_top - y + image_height + 9
                row = _ChronologicalRow(
                    group,
                    y,
                    height,
                    title_top,
                    grid_top,
                    grid_x,
                    columns,
                    tile_size,
                    image_rows,
                )
                rows.append(row)
                row_tops.append(y)
                row_by_date[group.day] = row
                y += height + self._row_gap
            content_height = max(y, 240) if not rows else max(1, y)
            changed_height = content_height != self._content_height
            self._rows = rows
            self._row_tops = row_tops
            self._row_by_date = row_by_date
            self._content_height = content_height
            if changed_height or force:
                self.setMinimumHeight(content_height)
                self.updateGeometry()
            self.update()
        finally:
            self._reflowing = False

    def _pixmap_changed(self, image_id: int, _pixmap: QPixmap) -> None:
        rectangle = self.item_rect(image_id)
        if not rectangle.isNull():
            self.update(rectangle)

    def _row_at(self, y: int) -> _ChronologicalRow | None:
        if not self._rows:
            return None
        row_index = bisect_right(self._row_tops, y) - 1
        if row_index < 0:
            return None
        row = self._rows[row_index]
        return row if row.top <= y < row.top + row.height else None

    def _item_at(self, point: QPoint) -> catalog.ImageListItem | None:
        if not self._rows:
            return None
        row_index = bisect_right(self._row_tops, point.y()) - 1
        if row_index < 0:
            return None
        row = self._rows[row_index]
        if point.y() < row.grid_top or point.y() >= row.top + row.height:
            return None
        step = row.tile_size + self._tile_gap
        column_offset = point.x() - row.grid_x
        line_offset = point.y() - row.grid_top
        if column_offset < 0:
            return None
        column, column_remainder = divmod(column_offset, step)
        line, line_remainder = divmod(line_offset, step)
        if column_remainder >= row.tile_size or line_remainder >= row.tile_size:
            return None
        item_index = line * row.columns + column
        if item_index >= len(row.group.items):
            return None
        return row.group.items[item_index]

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = event.position().toPoint()
        item = self._item_at(point)
        old_selection = set(self._selected_ids)
        modifiers = event.modifiers()
        control = bool(
            modifiers
            & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        )
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if item is None:
            if not control and not shift:
                self._selected_ids.clear()
                self._anchor_id = None
                self._focus_id = None
        elif shift and self._ordered_index(self._anchor_id) is not None:
            first = self._ordered_index(self._anchor_id)
            last = self._ordered_index(item.id)
            if first is None or last is None:
                return
            lower, upper = sorted((first, last))
            range_ids = {entry.id for entry in self._ordered_items[lower : upper + 1]}
            if control:
                self._selected_ids.update(range_ids)
            else:
                self._selected_ids = range_ids
            self._focus_id = item.id
        elif control:
            if item.id in self._selected_ids:
                self._selected_ids.remove(item.id)
            else:
                self._selected_ids.add(item.id)
            self._anchor_id = item.id
            self._focus_id = item.id
        elif item is not None:
            self._selected_ids = {item.id}
            self._anchor_id = item.id
            self._focus_id = item.id
        if self._selected_ids != old_selection:
            self.selection_changed.emit()
            self.update()
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if item is not None:
            self.image_clicked.emit(item)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        point = event.position().toPoint()
        item = self._item_at(point)
        if item is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            tooltip = f"{item.relative_path}\n{item.status}\nClick to open image details"
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            row = self._row_at(point.y())
            if row is None:
                tooltip = self.accessibleDescription()
            elif row.group.day is None:
                tooltip = "Unknown date; no month or year marker"
            else:
                tooltip = row.group.day.strftime("%A, %d %B %Y")
                if row.group.marker_text:
                    tooltip = f"{row.group.marker_text}\n{tooltip}"
        if tooltip != self.toolTip():
            self.setToolTip(tooltip)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip(self.accessibleDescription())
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:
        delta = event.pixelDelta().y()
        if not delta:
            delta = round(
                event.angleDelta().y()
                / 120
                * self.fontMetrics().lineSpacing()
                * 3
            )
        scroll_area = self.parentWidget()
        while scroll_area is not None and not isinstance(scroll_area, QScrollArea):
            scroll_area = scroll_area.parentWidget()
        if scroll_area is None or not delta:
            super().wheelEvent(event)
            return
        scrollbar = scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.value() - delta)
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._selected_ids = {item.id for item in self._ordered_items}
            self._focus_id = self._ordered_items[-1].id if self._ordered_items else None
            self._anchor_id = self._focus_id
            self.selection_changed.emit()
            self.update()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape:
            if self._selected_ids:
                self._selected_ids.clear()
                self._anchor_id = None
                self._focus_id = None
                self.selection_changed.emit()
                self.update()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._focus_id is not None:
            location = self._item_locations.get(self._focus_id)
            if location is not None:
                row_index, item_index = location
                self.image_clicked.emit(self._rows[row_index].group.items[item_index])
                event.accept()
                return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(event.rect(), self.palette().color(QPalette.ColorRole.Base))
        if not self._rows:
            painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(
                event.rect().adjusted(24, 12, -24, -12),
                Qt.AlignmentFlag.AlignCenter,
                "No images in the catalogue yet",
            )
            return
        clip_top = event.rect().top()
        clip_bottom = event.rect().bottom()
        row_index = max(0, bisect_right(self._row_tops, clip_top) - 1)
        while row_index < len(self._rows):
            row = self._rows[row_index]
            if row.top > clip_bottom:
                break
            if row.top + row.height >= clip_top:
                self._paint_day(painter, row, event.rect())
            row_index += 1

    def _paint_day(self, painter: QPainter, row: _ChronologicalRow, clip: QRect) -> None:
        palette = self.palette()
        separator = palette.color(QPalette.ColorRole.Mid)
        bracket_x = 15
        bracket_top = row.top + 5
        bracket_bottom = row.top + row.height - 5
        painter.save()
        painter.setPen(QPen(separator, 1))
        painter.drawLine(bracket_x, bracket_top, bracket_x, bracket_bottom)
        painter.drawLine(bracket_x, bracket_top, bracket_x + 9, bracket_top)
        painter.drawLine(bracket_x, bracket_bottom, bracket_x + 9, bracket_bottom)
        separator_y = row.top + row.height - 1
        painter.drawLine(row.grid_x, separator_y, self.width() - 12, separator_y)

        group = row.group
        if group.marker_text:
            self._paint_break_marker(painter, row)
        title_colour = palette.color(
            QPalette.ColorRole.PlaceholderText if group.day is None else QPalette.ColorRole.Text
        )
        title_font = self.font()
        title_font.setBold(group.year_marker)
        painter.setFont(title_font)
        painter.setPen(title_colour)
        if group.day is None:
            noun = "image" if len(group.items) == 1 else "images"
            title = f"Unknown date · {len(group.items)} {noun}"
        else:
            full_day = f"{group.day.strftime('%A')}, {group.day.day} {group.day.strftime('%B %Y')}"
            noun = "image" if len(group.items) == 1 else "images"
            title = f"{full_day} · {len(group.items)} {noun}"
        title_rect = QRect(row.grid_x, row.title_top, max(0, self.width() - row.grid_x - 12), 20)
        title = QFontMetrics(title_font).elidedText(
            title, Qt.TextElideMode.ElideRight, title_rect.width()
        )
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title,
        )

        if row.image_rows:
            first_line = max(0, (clip.top() - row.grid_top) // (row.tile_size + self._tile_gap))
            last_line = min(
                row.image_rows - 1,
                max(0, (clip.bottom() - row.grid_top) // (row.tile_size + self._tile_gap)),
            )
            for line in range(first_line, last_line + 1):
                first_item = line * row.columns
                last_item = min(first_item + row.columns, len(group.items))
                for item_index in range(first_item, last_item):
                    item = group.items[item_index]
                    tile = self.item_rect(item.id)
                    if tile.right() < clip.left() or tile.left() > clip.right():
                        continue
                    self._paint_thumbnail(painter, tile, item)
        painter.restore()

    def _paint_break_marker(self, painter: QPainter, row: _ChronologicalRow) -> None:
        group = row.group
        marker_y = row.top + 11
        cursor_x = row.grid_x
        if group.year_marker:
            colour = QColor("#b87522")
            size = 12
            centre_x = cursor_x + size // 2
            centre_y = marker_y + size // 2
            painter.setPen(QPen(colour.darker(125), 1))
            painter.setBrush(colour)
            painter.drawPolygon(
                QPolygon(
                    [
                        QPoint(centre_x, marker_y),
                        QPoint(cursor_x + size, centre_y),
                        QPoint(centre_x, marker_y + size),
                        QPoint(cursor_x, centre_y),
                    ]
                )
            )
            cursor_x += size + 6
        if group.month_marker:
            colour = QColor("#397fae")
            size = 9
            painter.setPen(QPen(colour.darker(125), 1))
            painter.setBrush(colour)
            painter.drawEllipse(QRect(cursor_x, marker_y + 1, size, size))
            cursor_x += size + 6
        marker_font = self.font()
        marker_font.setBold(True)
        if group.year_marker:
            marker_font.setPointSizeF(max(marker_font.pointSizeF(), 10.0))
        painter.setFont(marker_font)
        painter.setPen(
            QColor("#8b541d") if group.year_marker else QColor("#2d6790")
        )
        marker_rect = QRect(cursor_x, row.top + 6, max(0, self.width() - cursor_x - 12), 22)
        marker_text = QFontMetrics(marker_font).elidedText(
            group.marker_text, Qt.TextElideMode.ElideRight, marker_rect.width()
        )
        painter.drawText(
            marker_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            marker_text,
        )

    def _paint_thumbnail(
        self, painter: QPainter, tile: QRect, item: catalog.ImageListItem
    ) -> None:
        palette = self.palette()
        selected = item.id in self._selected_ids
        painter.save()
        if selected:
            selection_fill = QColor(palette.color(QPalette.ColorRole.Highlight))
            selection_fill.setAlpha(32)
            painter.fillRect(tile, selection_fill)
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Highlight), 2))
        else:
            painter.fillRect(tile, palette.color(QPalette.ColorRole.Base))
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(tile.adjusted(0, 0, -1, -1))

        image_rect = tile.adjusted(5, 4, -5, -23)
        pixmap = self.model.pixmaps.get(item.id)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            width_ratio = image_rect.width() / pixmap.width()
            height_ratio = image_rect.height() / pixmap.height()
            scale = min(width_ratio, height_ratio)
            target_width = max(1, round(pixmap.width() * scale))
            target_height = max(1, round(pixmap.height() * scale))
            target = QRect(
                image_rect.left() + (image_rect.width() - target_width) // 2,
                image_rect.top() + (image_rect.height() - target_height) // 2,
                target_width,
                target_height,
            )
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(target, pixmap, pixmap.rect())
        else:
            painter.fillRect(image_rect, palette.color(QPalette.ColorRole.AlternateBase))
            painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
            painter.drawText(
                image_rect,
                Qt.AlignmentFlag.AlignCenter,
                "Loading…",
            )

        dot_size = min(8, max(4, tile.width() // 12))
        dot = QRect(tile.left() + 5, tile.bottom() - 16, dot_size, dot_size)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(status_colour(item.status))
        painter.drawEllipse(dot)
        label_rect = QRect(
            dot.right() + 5,
            tile.bottom() - 19,
            max(0, tile.right() - dot.right() - 8),
            14,
        )
        label = QFontMetrics(self.font()).elidedText(
            Path(item.relative_path).name, Qt.TextElideMode.ElideRight, label_rect.width()
        )
        painter.setFont(self.font())
        painter.setPen(palette.color(QPalette.ColorRole.Text))
        painter.drawText(
            label_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            label,
        )
        painter.restore()


class ChronologicalView(QWidget):
    image_clicked = Signal(object)
    selection_changed = Signal()

    def __init__(self, model: CalendarModel, parent=None) -> None:
        super().__init__(parent)
        self.model = model
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("chronologicalScrollArea")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setAccessibleName("Chronological timeline scroll area")
        self.scroll_area.setToolTip("Scroll vertically through dates, oldest first")
        self.canvas = _ChronologicalCanvas(model)
        self.scroll_area.setWidget(self.canvas)
        self.canvas.image_clicked.connect(self.image_clicked)
        self.canvas.selection_changed.connect(self.selection_changed)
        _use_surface(self, QPalette.ColorRole.Base)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.scroll_area)
        self.setAccessibleName("Chronological image view")
        self.setToolTip("Images grouped by date from oldest to newest")

    @property
    def day_groups(self) -> list[ChronologicalDayGroup]:
        return self.canvas.day_groups

    @property
    def day_rows(self) -> list[_ChronologicalRow]:
        return self.canvas._rows

    def selected_image_ids(self) -> list[int]:
        return self.canvas.selected_image_ids()


class FaceImageWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap = QPixmap()
        self._faces = ()
        self._visualise = False
        self._zoom = 1.0
        self._pan = QPointF()
        self._drag_position: QPointF | None = None
        self._drag_pan = QPointF()
        self.setMinimumSize(260, 190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setToolTip("Scroll to zoom; drag to pan; double-click to fit")

    def set_asset(self, pixmap: QPixmap, faces=()) -> None:
        self._pixmap = pixmap
        self._faces = tuple(faces)
        self.reset_view()
        self.update()

    def set_visualisation(self, enabled: bool) -> None:
        self._visualise = enabled
        self.update()

    def resizeEvent(self, event) -> None:
        old_size = event.oldSize()
        new_size = event.size()
        if old_size.width() > 0 and old_size.height() > 0:
            self._pan = QPointF(
                self._pan.x() * new_size.width() / old_size.width(),
                self._pan.y() * new_size.height() / old_size.height(),
            )
            self._clamp_pan()
        super().resizeEvent(event)

    def _fit_rect(self) -> QRect:
        if self._pixmap.isNull():
            return QRect()
        area = self.rect().adjusted(8, 8, -8, -8)
        scale = min(area.width() / self._pixmap.width(), area.height() / self._pixmap.height())
        target_width = max(1, round(self._pixmap.width() * scale))
        target_height = max(1, round(self._pixmap.height() * scale))
        return QRect(
            (self.width() - target_width) // 2,
            (self.height() - target_height) // 2,
            target_width,
            target_height,
        )

    def _display_rect(self) -> QRect:
        fit = self._fit_rect()
        if fit.isNull():
            return fit
        width = max(1, round(fit.width() * self._zoom))
        height = max(1, round(fit.height() * self._zoom))
        left = round(fit.left() + (fit.width() - width) / 2 + self._pan.x())
        top = round(fit.top() + (fit.height() - height) / 2 + self._pan.y())
        return QRect(left, top, width, height)

    def _clamp_pan(self) -> None:
        fit = self._fit_rect()
        if fit.isNull():
            self._pan = QPointF()
            return
        area = self.rect().adjusted(8, 8, -8, -8)
        width = max(1, round(fit.width() * self._zoom))
        height = max(1, round(fit.height() * self._zoom))
        max_x = max(0.0, (width - area.width()) / 2)
        max_y = max(0.0, (height - area.height()) / 2)
        self._pan = QPointF(
            max(-max_x, min(max_x, self._pan.x())),
            max(-max_y, min(max_y, self._pan.y())),
        )

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF()
        self._drag_position = None
        self.unsetCursor()

    def _zoom_at(self, position: QPointF, factor: float) -> None:
        if self._pixmap.isNull():
            return
        old_zoom = self._zoom
        new_zoom = max(1.0, min(8.0, old_zoom * factor))
        if new_zoom == old_zoom:
            return
        old_rect = self._display_rect()
        zoom_ratio = new_zoom / old_zoom
        fit = self._fit_rect()
        new_width = max(1, round(fit.width() * new_zoom))
        new_height = max(1, round(fit.height() * new_zoom))
        centred_left = fit.left() + (fit.width() - new_width) / 2
        centred_top = fit.top() + (fit.height() - new_height) / 2
        target_left = position.x() - (position.x() - old_rect.left()) * zoom_ratio
        target_top = position.y() - (position.y() - old_rect.top()) * zoom_ratio
        self._zoom = new_zoom
        self._pan = QPointF(target_left - centred_left, target_top - centred_top)
        self._clamp_pan()
        self.setCursor(
            Qt.CursorShape.OpenHandCursor if self._zoom > 1.0 else Qt.CursorShape.ArrowCursor
        )
        self.update()

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

    def wheelEvent(self, event) -> None:
        delta = event.pixelDelta().y()
        if not delta:
            delta = event.angleDelta().y()
        if not delta or self._pixmap.isNull():
            super().wheelEvent(event)
            return
        self._zoom_at(event.position(), 1.2 ** (delta / 120))
        event.accept()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._zoom > 1.0:
            self._drag_position = event.position()
            self._drag_pan = QPointF(self._pan)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_position is not None and event.buttons() & Qt.MouseButton.LeftButton:
            movement = event.position() - self._drag_position
            self._pan = self._drag_pan + movement
            self._clamp_pan()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_position is not None:
            self._drag_position = None
            self.setCursor(
                Qt.CursorShape.OpenHandCursor if self._zoom > 1.0 else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_view()
            self.update()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Base))
        if self._pixmap.isNull():
            painter.setPen(self.palette().color(QPalette.ColorRole.Text))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Image unavailable")
            return
        target = self._display_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(target, self._pixmap, self._pixmap.rect())
        if not self._visualise:
            return
        colours = (QColor("#ffe37a"), QColor("#67e8f9"), QColor("#f0a3ff"))
        for index, rectangle in enumerate(self.face_rects()):
            painter.setPen(colours[index % len(colours)])
            painter.drawRect(rectangle)


class FaceMatchReviewDialog(QDialog):
    merge_requested = Signal()
    reject_requested = Signal()

    def __init__(self, proposal: catalog.FaceMatchProposal, parent=None) -> None:
        super().__init__(parent)
        self.proposal = proposal
        self._busy = False
        self.setWindowTitle("Review face match")
        self.setModal(False)
        self.setMinimumWidth(520)

        heading = QLabel(f"Could this group be {proposal.target_person_name}?")
        heading.setObjectName("faceMatchHeading")
        heading.setWordWrap(True)
        samples = QHBoxLayout()
        samples.setSpacing(16)
        self.candidate_sample = self._sample_panel(
            "Candidate group", f"{proposal.face_count} face(s)"
        )
        self.target_sample = self._sample_panel(
            "Named person", proposal.target_person_name
        )
        samples.addWidget(self.candidate_sample[0])
        samples.addWidget(self.target_sample[0])

        similarity_text = (
            f"Similarity: {proposal.similarity:.3f}"
            if proposal.similarity is not None
            else "Similarity unavailable"
        )
        self.similarity_label = QLabel(similarity_text)
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setObjectName("faceMatchMessage")

        self.merge_button = QPushButton("Merge")
        self.merge_button.setAccessibleName(
            f"Merge candidate group into {proposal.target_person_name}"
        )
        self.not_same_button = QPushButton("Not the same person")
        self.not_same_button.setAccessibleName("Reject this person match")
        self.later_button = QPushButton("Later")
        self.merge_button.clicked.connect(lambda: self.merge_requested.emit())
        self.not_same_button.clicked.connect(lambda: self.reject_requested.emit())
        self.later_button.clicked.connect(lambda: self.reject())
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.later_button)
        actions.addWidget(self.not_same_button)
        actions.addWidget(self.merge_button)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addLayout(samples)
        layout.addWidget(self.similarity_label)
        layout.addWidget(self.message_label)
        layout.addLayout(actions)

    @staticmethod
    def _sample_panel(title: str, description: str) -> tuple[QFrame, QLabel]:
        panel = QFrame()
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel.setMinimumWidth(230)
        preview = QLabel("Loading sample…")
        preview.setFixedSize(200, 200)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setAccessibleName(f"{title} face sample")
        name = QLabel(description)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setWordWrap(True)
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel(title))
        layout.addWidget(preview, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(name)
        return panel, preview

    def set_sample(self, candidate: bool, pixmap: QPixmap) -> None:
        preview = self.candidate_sample[1] if candidate else self.target_sample[1]
        if pixmap.isNull():
            preview.setText("Sample unavailable")
        else:
            preview.setText("")
            preview.setPixmap(
                pixmap.scaled(
                    preview.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def set_message(self, message: str) -> None:
        self.message_label.setText(message)
        self.message_label.setVisible(bool(message))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.merge_button.setEnabled(not busy)
        self.not_same_button.setEnabled(not busy)
        self.later_button.setEnabled(not busy)

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
            return
        super().closeEvent(event)


class DetailPanel(QFrame):
    label_face_requested = Signal(int)

    def __init__(self, pool: QThreadPool, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self._serial = 0
        self._faces = ()
        self.title = ElidedLabel("Select an image to see details")
        self.title.setObjectName("detailTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("detailSubtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setMinimumWidth(0)
        _mute_label(self.subtitle)
        self.action_message = QLabel()
        self.action_message.setWordWrap(True)
        self.action_message.setObjectName("faceActionMessage")
        self.action_message.setVisible(False)
        self.image = FaceImageWidget()
        self.face_scroll = QScrollArea()
        self.face_scroll.setWidgetResizable(True)
        self.face_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.face_scroll.setMinimumHeight(152)
        self.face_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.face_strip = QWidget()
        self.face_layout = QHBoxLayout(self.face_strip)
        self.face_layout.setContentsMargins(6, 6, 6, 6)
        self.face_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.face_strip.setMinimumHeight(140)
        self.face_strip.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.face_scroll.setWidget(self.face_strip)
        self.setFrameShape(QFrame.Shape.NoFrame)
        _use_surface(self, QPalette.ColorRole.Base)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addWidget(self.action_message)
        layout.addWidget(self.image, 1)
        layout.addWidget(self.face_scroll)
        self.setMinimumHeight(380)

    def clear(self, message: str = "Select an image to see details") -> None:
        self._serial += 1
        self.title.set_full_text(message)
        self.subtitle.clear()
        self.subtitle.setToolTip("")
        self.set_action_message("")
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
        self.set_action_message("")
        if detail is None:
            self.clear("The selected image is no longer in the catalogue")
            return
        timestamp = detail.taken_at or detail.modified_at
        date_text = timestamp.strftime("%Y-%m-%d %H:%M") if timestamp else "Unknown date"
        self.title.set_full_text(detail.relative_path)
        status_text = f"{date_text}  ·  {detail.status}"
        self.subtitle.setText(status_text)
        self.subtitle.setToolTip(f"{detail.relative_path}  ·  {date_text}  ·  {detail.status}")
        self._faces = detail.faces if detail.status == "analysed" else ()
        task = ImageAssetTask(detail.path, self._faces, detail.thumb_path, self)
        task.signals.result.connect(lambda asset, s=serial: self._asset_ready(s, asset))
        task.signals.error.connect(lambda message, s=serial: self._asset_error(s, message))
        self.pool.start(task)

    def set_action_message(self, message: str) -> None:
        self.action_message.setText(message)
        self.action_message.setVisible(bool(message))

    def _asset_ready(self, serial: int, asset: ImageAsset) -> None:
        if serial != self._serial:
            return
        pixmap = QPixmap.fromImage(asset.image) if not asset.image.isNull() else QPixmap()
        self.image.set_asset(pixmap, self._faces)
        for face, crop in zip(self._faces, asset.crops):
            card = QToolButton()
            card.setObjectName("faceCropCard")
            card.setMinimumWidth(120)
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            card.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            card.setIconSize(QSize(96, 96))
            card.setAutoRaise(True)
            crop_pixmap = QPixmap.fromImage(crop) if not crop.isNull() else QPixmap()
            if not crop_pixmap.isNull():
                card.setIcon(QIcon(crop_pixmap))
            name = getattr(face, "person_name", None)
            person_id = getattr(face, "person_id", None)
            can_label = not name and person_id is not None and getattr(face, "id", None) is not None
            display_name = name or "Unknown / Unassigned"
            crop_status = "" if not crop_pixmap.isNull() else "No crop\n"
            card.setText(
                f"{crop_status}{display_name}\nLabel person"
                if can_label
                else f"{crop_status}{display_name}"
            )
            card.setAccessibleName(f"Face sample: {display_name}")
            card.setToolTip(display_name)
            if can_label:
                card.setAccessibleName("Unlabelled face group. Activate to label this person")
                card.setToolTip("Label this analyser-created face group")
                card.clicked.connect(lambda _checked=False, face_id=face.id: self.label_face_requested.emit(face_id))
            self.face_layout.addWidget(card)

    def _asset_error(self, serial: int, message: str) -> None:
        if serial == self._serial:
            logger.error("Could not load detail image: %s", message)
            self.image.set_asset(QPixmap())
            self.title.set_full_text("Could not load image")
            self.subtitle.setText(message)
            self.subtitle.setToolTip(message)


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
        self.breadcrumb_scroll = ButtonRowScroll(self.breadcrumbs)
        self.folder_scroll = ButtonRowScroll(self.folder_row)
        _use_surface(self, QPalette.ColorRole.Base)
        layout = QVBoxLayout(self)
        layout.addWidget(self.breadcrumb_scroll)
        layout.addWidget(self.folder_scroll)
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
        root_name = self.root.name or str(self.root)
        root_button = QPushButton(root_name)
        root_button.setToolTip(str(self.root) if self.root else root_name)
        root_button.clicked.connect(lambda: self.directory_changed.emit(Path()))
        self.breadcrumb_layout.addWidget(root_button)
        for index, part in enumerate(directory.parts):
            self.breadcrumb_layout.addWidget(QLabel("›"))
            button = QPushButton(part)
            button.setToolTip(part)
            button.clicked.connect(lambda _checked=False, i=index: self.directory_changed.emit(Path(*directory.parts[: i + 1])))
            self.breadcrumb_layout.addWidget(button)
        self.breadcrumb_layout.addStretch(1)
        self.breadcrumb_scroll.fit()
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
                button.setToolTip(folder)
                button.clicked.connect(lambda _checked=False, name=folder: self.directory_changed.emit(self.directory / name))
                self.folder_layout.addWidget(button)
        self.folder_layout.addStretch(1)
        self.folder_scroll.fit()

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
        diagnostic(f"Analysis start root={root} selected_image_ids={image_ids!r}")
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
        diagnostic(f"Analysis targets selected count={len(self._targets)}")
        for target in self._targets:
            diagnostic(
                f"Analysis target image_id={target.id} path={target.path!r} "
                f"status={target.status!r} content_hash={target.content_hash!r}"
            )
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
            diagnostic(
                f"DeepFace worker executable={sys.executable!r} "
                "arguments=['-m', 'imagelib.services.deepface_worker']"
            )
            environment = QProcessEnvironment.systemEnvironment()
            self.process.setProcessEnvironment(environment)
            try:
                self.process.start()
            except Exception as exc:
                diagnostic_exception("DeepFace worker process start failed", exc)
                self._active = False
                self.failed.emit(f"DeepFace worker start failed: {exc}")
        else:
            self._starting_after_finish = True
            self._process_generation = generation
            self.process.kill()

    def _process_started(self) -> None:
        diagnostic(f"DeepFace worker process started pid={self.process.processId()}")
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
        diagnostic(f"Analysis request sent: {json.dumps(request, ensure_ascii=False)}")
        try:
            self.process.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
        except Exception as exc:
            diagnostic_exception(f"Analysis request failed request_id={request_id!r}", exc)
            self._pending.pop(request_id, None)
            self._active = False
            self.failed.emit(f"Could not send analysis request: {exc}")

    def _read_output(self) -> None:
        while self.process.canReadLine():
            raw = bytes(self.process.readLine()).strip()
            if not raw:
                continue
            diagnostic(f"DeepFace worker raw stdout response: {raw.decode(errors='replace')}")
            try:
                response = json.loads(raw.decode())
                request_id = response.get("request_id")
                response_status = response.get("status", "ok" if response.get("ok") else "error")
                diagnostic(
                    f"DeepFace worker response parsed request_id={request_id!r} "
                    f"status={response_status!r}"
                )
                pending = self._pending.pop(request_id, None)
                if pending is None or not self._active:
                    if pending is None and self._active:
                        raise ValueError(f"unexpected DeepFace worker request_id: {request_id!r}")
                    continue
                target, _ = pending
                if response.get("ok") is False:
                    diagnostic(
                        f"Analysis response error image_id={target.id} path={target.path!r} "
                        f"error={response.get('error', 'DeepFace worker failed')!r}"
                    )
                self._responses.append((target, response))
                self.progress.emit(len(self._responses), len(self._targets))
                self.status.emit(f"Analysing {len(self._responses)} of {len(self._targets)}…")
                self._send_next(self._generation)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as exc:
                logger.exception("Invalid DeepFace worker response")
                diagnostic_exception("Invalid DeepFace worker response", exc)
                if self._active:
                    self._active = False
                    self.failed.emit(f"Invalid DeepFace worker response: {exc}")

    def _read_error_output(self) -> None:
        stderr = bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
        if stderr:
            logger.warning("DeepFace worker stderr: %s", stderr)
            diagnostic(f"DeepFace worker stderr: {stderr}")

    def _persist_batch(self, generation: int) -> None:
        if not self._active or generation != self._generation or not self._responses:
            return
        self.status.emit("Saving analysis results and rebuilding people…")
        responses = list(self._responses)
        diagnostic(
            f"Analysis persistence start responses={len(responses)} "
            f"image_ids={[target.id for target, _response in responses]!r}"
        )
        task = FunctionTask(lambda: analyser.persist_worker_batch(responses), self)
        task.signals.result.connect(lambda report, g=generation: self._batch_saved(g, report))
        task.signals.error.connect(lambda message, g=generation: self._persist_error(g, message))
        self._active = False
        self.pool.start(task)

    def _batch_saved(self, generation: int, report) -> None:
        diagnostic(f"Analysis persistence completed report={report!r}")
        errors = [result for result in getattr(report, "results", ()) if result.status == "error"]
        for result in getattr(report, "results", ()):
            diagnostic(
                f"Analysis result image_id={result.image_id} status={result.status!r} "
                f"accepted={getattr(result, 'accepted', None)} "
                f"faces={getattr(result, 'face_count', None)} error={result.error!r}"
            )
        for result in errors:
            logger.error(
                "Analysis failed for image %s: %s",
                result.image_id,
                result.error or "DeepFace worker failed",
            )
        if generation != self._generation:
            self.catalogue_changed.emit()
            return
        self.finished.emit(report)
        if errors:
            self.status.emit(
                f"Analysis complete with {len(errors)} error(s): "
                f"{errors[0].error or 'DeepFace worker failed'}"
            )
        else:
            self.status.emit("Analysis complete")

    def _persist_error(self, generation: int, message: str) -> None:
        logger.error("Analysis persistence callback failed for %s: %s", self._root, message)
        diagnostic(f"Analysis persistence failed root={self._root!r}: {message}")
        if generation == self._generation:
            self.failed.emit(f"Could not save analysis results: {message}")
        else:
            self.catalogue_changed.emit()

    def _process_error(self, error) -> None:
        logger.error("DeepFace worker process error: %s", error)
        diagnostic(f"DeepFace worker process error error={error!r}")
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
        diagnostic(f"DeepFace worker process finished exit_code={_exit_code} status={_exit_status!r}")
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
            diagnostic("DeepFace worker request sent: shutdown")
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
