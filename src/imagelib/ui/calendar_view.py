from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from imagelib.ui._widgets import ButtonRowScroll, _mute_label, _thumb_metrics, _use_surface
from imagelib.ui.models import CalendarModel


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
