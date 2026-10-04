from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QFrame, QListView, QSizePolicy

from imagelib.ui._widgets import _use_surface
from imagelib.ui.models import ThumbnailDelegate


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
