"""Qt models and delegates for image collections."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QStyledItemDelegate

from imagelib.services.catalog import ImageListItem


THUMBNAIL_SIZE = QSize(180, 172)


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
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.fillRect(option.rect, QColor("#2b3e55" if selected else "#20252b"))
        pixmap = index.data(Qt.ItemDataRole.DecorationRole)
        image_rect = option.rect.adjusted(8, 8, -8, -36)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            scaled = pixmap.scaled(image_rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            target = self._pixmap_rect(image_rect, pixmap)
            painter.drawPixmap(target, scaled)
        else:
            painter.fillRect(image_rect, QColor("#15181c"))
            painter.drawText(image_rect, Qt.AlignmentFlag.AlignCenter, "Loading…")
        text_rect = option.rect.adjusted(8, option.rect.height() - 28, -8, -6)
        painter.setPen(QColor("#edf1f5"))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, index.data())
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        return THUMBNAIL_SIZE


@dataclass(frozen=True)
class CalendarEntry:
    heading: bool
    label: str
    item: ImageListItem | None = None


class CalendarModel(ThumbnailModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entries: list[CalendarEntry] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if entry.heading:
            if role == Qt.ItemDataRole.DisplayRole:
                return entry.label
            if role == Qt.ItemDataRole.SizeHintRole:
                return QSize(180, 40)
            return None
        item = entry.item
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

    def flags(self, index):
        flags = super().flags(index)
        if index.isValid() and self.entries[index.row()].heading:
            return Qt.ItemFlag.ItemIsEnabled
        return flags

    def set_groups(self, groups: dict[date | None, list[ImageListItem]]) -> None:
        entries: list[CalendarEntry] = []
        items: list[ImageListItem] = []
        for group, group_items in groups.items():
            entries.append(CalendarEntry(True, group.isoformat() if group else "Unknown date"))
            entries.extend(CalendarEntry(False, "", item) for item in group_items)
            items.extend(group_items)
        self.beginResetModel()
        self.entries = entries
        self.items = items
        self.pixmaps = {}
        self.endResetModel()

    def item(self, index: QModelIndex) -> ImageListItem | None:
        if not index.isValid() or self.entries[index.row()].heading:
            return None
        return self.entries[index.row()].item


class CalendarDelegate(ThumbnailDelegate):
    def paint(self, painter, option, index) -> None:
        model = index.model()
        if model.entries[index.row()].heading:
            painter.save()
            painter.fillRect(option.rect, QColor("#303944"))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(option.rect.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter, index.data())
            painter.restore()
            return
        super().paint(painter, option, index)

    def sizeHint(self, option, index) -> QSize:
        return QSize(180, 40) if index.model().entries[index.row()].heading else super().sizeHint(option, index)
