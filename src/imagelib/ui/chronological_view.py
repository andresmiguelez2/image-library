from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPalette, QPen, QPolygon, QPixmap
from PySide6.QtWidgets import QFrame, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from imagelib.services import catalog
from imagelib.ui._widgets import _use_surface
from imagelib.ui.models import CalendarModel, status_colour


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
