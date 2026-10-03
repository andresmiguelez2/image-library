"""Qt models and delegates for image collections."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QStyledItemDelegate

from imagelib.services.catalog import ImageListItem


THUMBNAIL_SIZE = QSize(180, 172)
STATUS_COLOURS = {
    "analysed": "#45c46b",
    "error": "#ef5350",
    "indexed": "#f0a43c",
    "pending": "#f0a43c",
}


def status_colour(status: str | None) -> QColor:
    return QColor(STATUS_COLOURS.get(status, "#8a939e"))


class ThumbnailModel(QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[ImageListItem] = []
        self.pixmaps: dict[int, QPixmap] = {}

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.items):
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return Path(item.path).name
        if role == Qt.ItemDataRole.UserRole:
            return item
        if role == Qt.ItemDataRole.DecorationRole:
            return self.pixmaps.get(item.id)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{item.relative_path}\n{item.status}"
        if role == Qt.ItemDataRole.SizeHintRole:
            return THUMBNAIL_SIZE
        return None

    def set_items(self, items: list[ImageListItem]) -> None:
        self.beginResetModel()
        self.items = list(items)
        self.pixmaps = {}
        self.endResetModel()

    def set_pixmap(self, image_id: int, pixmap: QPixmap) -> None:
        self.pixmaps[image_id] = pixmap
        for row, item in enumerate(self.items):
            if item.id == image_id:
                self.dataChanged.emit(self.index(row), self.index(row), [Qt.ItemDataRole.DecorationRole])
                break

    def item(self, index: QModelIndex) -> ImageListItem | None:
        return index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None


class ThumbnailDelegate(QStyledItemDelegate):
    @staticmethod
    def _pixmap_rect(image_rect: QRect, pixmap: QPixmap) -> QRect:
        scaled = pixmap.scaled(
            image_rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        target = QRect(image_rect.topLeft(), scaled.size())
        target.moveLeft(image_rect.left() + (image_rect.width() - scaled.width()) // 2)
        target.moveTop(image_rect.top() + (image_rect.height() - scaled.height()) // 2)
        return target

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setFont(option.font)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        palette = option.palette
        background = palette.color(QPalette.ColorRole.Highlight if selected else QPalette.ColorRole.Base)
        painter.fillRect(option.rect, background)
        text_colour = palette.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        metrics = option.fontMetrics
        margin = 8
        dot = 8
        gap = 6
        text_height = max(metrics.lineSpacing(), 1)
        text_band = max(text_height, dot) + 8
        image_rect = option.rect.adjusted(margin, margin, -margin, -text_band)
        if image_rect.width() < 1 or image_rect.height() < 1:
            image_rect = option.rect.adjusted(margin, margin, -margin, -margin)
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(
                image_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            target = self._pixmap_rect(image_rect, pixmap)
            painter.drawPixmap(target, scaled)
        else:
            painter.fillRect(image_rect, palette.color(QPalette.ColorRole.AlternateBase))
            painter.setPen(text_colour)
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, "Loading…")
        text_width = max(1, option.rect.width() - margin * 2 - dot - gap)
        text_top = option.rect.bottom() - 4 - text_height
        text_rect = QRect(option.rect.left() + margin + dot + gap, text_top, text_width, text_height)
        name = index.data() or ""
        elided = metrics.elidedText(str(name), Qt.TextElideMode.ElideRight, text_rect.width())
        painter.setPen(text_colour)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)
        dot_rect = QRect(option.rect.left() + margin, text_rect.center().y() - dot // 2, dot, dot)
        item = index.data(Qt.ItemDataRole.UserRole)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(status_colour(getattr(item, "status", None)))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.drawEllipse(dot_rect)
        if option.state & QStyle.StateFlag.State_HasFocus:
            focus_colour = palette.color(
                QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Highlight
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(focus_colour)
            painter.drawRect(option.rect.adjusted(1, 1, -2, -2))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        return THUMBNAIL_SIZE


class CalendarModel(ThumbnailModel):
    pixmap_changed = Signal(int, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.groups: dict[date | None, list[ImageListItem]] = {}

    def set_groups(self, groups: dict[date | None, list[ImageListItem]]) -> None:
        items: list[ImageListItem] = []
        self.beginResetModel()
        self.groups = {group: list(group_items) for group, group_items in groups.items()}
        for group_items in self.groups.values():
            items.extend(group_items)
        self.items = items
        self.pixmaps = {}
        self.endResetModel()

    def set_pixmap(self, image_id: int, pixmap: QPixmap) -> None:
        super().set_pixmap(image_id, pixmap)
        self.pixmap_changed.emit(image_id, pixmap)
