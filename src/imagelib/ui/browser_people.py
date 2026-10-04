from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from imagelib.services import catalog
from imagelib.ui._widgets import ButtonRowScroll, _use_surface
from imagelib.ui.collection_view import CollectionView
from imagelib.ui.models import ThumbnailModel


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


class PeopleView(QWidget):
    apply_requested = Signal(object, str)
    back_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._groups: dict[int, catalog.PersonGroup] = {}
        self.cards: dict[int, QToolButton] = {}
        self._draft_ids: set[int] = set()
        self._draft_mode = "any"

        heading = QLabel("Choose people")
        heading.setObjectName("peopleHeading")
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        description = QLabel("Select one or more groups, then choose how they should match.")
        description.setWordWrap(True)

        self.any_button = QRadioButton("Any selected person")
        self.any_button.setAccessibleName("Match any selected person")
        self.all_button = QRadioButton("All selected people")
        self.all_button.setAccessibleName("Match all selected people")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.any_button)
        self.mode_group.addButton(self.all_button)
        self.any_button.setChecked(True)
        self.any_button.toggled.connect(self._mode_changed)
        self.all_button.toggled.connect(self._mode_changed)

        mode_row = QHBoxLayout()
        mode_row.addWidget(self.any_button)
        mode_row.addWidget(self.all_button)
        mode_row.addStretch(1)

        self.message = QLabel("People groups will appear here.")
        self.message.setWordWrap(True)
        self.message.setAccessibleName("People gallery status")
        self.gallery = QWidget()
        self.gallery_layout = QGridLayout(self.gallery)
        self.gallery_layout.setContentsMargins(0, 0, 0, 0)
        self.gallery_layout.setSpacing(12)
        self.gallery_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(self.gallery)
        self.scroll.viewport().installEventFilter(self)

        self.apply_button = QPushButton("Apply")
        self.apply_button.setAccessibleName("Apply people filter")
        self.apply_button.setEnabled(False)
        self.back_button = QPushButton("Cancel / Back")
        self.back_button.setAccessibleName("Cancel people selection and return")
        self.apply_button.clicked.connect(self._apply)
        self.back_button.clicked.connect(lambda: self.back_requested.emit())

        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.back_button)
        actions.addWidget(self.apply_button)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(description)
        layout.addLayout(mode_row)
        layout.addWidget(self.message)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(actions)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._layout_cards()
        return super().eventFilter(watched, event)

    def begin_edit(self, selected_ids, mode: str) -> None:
        self._draft_ids = set(selected_ids)
        self._draft_mode = mode if mode in {"any", "all"} else "any"
        self.any_button.setChecked(self._draft_mode == "any")
        self.all_button.setChecked(self._draft_mode == "all")
        self._update_selection_state()

    def set_loading(self) -> None:
        self._clear_cards()
        self.apply_button.setEnabled(False)
        self.message.setText("Loading people groups…")
        self.message.setVisible(True)

    def set_groups(self, groups) -> None:
        groups = list(groups)
        self._clear_cards()
        self._groups = {group.person_id: group for group in groups}
        self._draft_ids.intersection_update(self._groups)
        for group in groups:
            label = group.name or f"Unlabelled group {group.person_id}"
            faces = f"{group.face_count} face" + ("s" if group.face_count != 1 else "")
            images = f"{group.image_count} image" + ("s" if group.image_count != 1 else "")
            card = QToolButton(self.gallery)
            card.setObjectName("personCard")
            card.setCheckable(True)
            card.setAutoRaise(False)
            card.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            card.setIconSize(QSize(128, 128))
            card.setMinimumSize(168, 210)
            card.setMaximumWidth(220)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            card.setText(f"{label}\n{faces} · {images}")
            card.setAccessibleName(label)
            card.setAccessibleDescription(f"{faces}, {images}. Not selected.")
            card.setToolTip(f"{label}\n{faces} · {images}")
            card.toggled.connect(lambda checked, person_id=group.person_id: self._card_toggled(person_id, checked))
            card.setChecked(group.person_id in self._draft_ids)
            self.cards[group.person_id] = card
        self.message.setText("No grouped people are available for this library." if not groups else "")
        self.message.setVisible(not groups)
        self._layout_cards()
        self._update_selection_state()

    def set_crop(self, person_id: int, pixmap: QPixmap) -> None:
        card = self.cards.get(person_id)
        if card is not None and not pixmap.isNull():
            card.setIcon(QIcon(pixmap))

    def set_error(self, message: str) -> None:
        self._clear_cards()
        self._draft_ids.clear()
        self.message.setText(f"Could not load people groups: {message}")
        self.message.setVisible(True)
        self._update_selection_state()

    def reset(self) -> None:
        self._draft_ids.clear()
        self._draft_mode = "any"
        self._clear_cards()
        self.any_button.setChecked(True)
        self.message.setText("People groups will appear here.")
        self.message.setVisible(True)
        self._update_selection_state()

    def _clear_cards(self) -> None:
        self._groups.clear()
        self.cards.clear()
        while self.gallery_layout.count():
            child = self.gallery_layout.takeAt(0)
            if child.widget() is not None:
                child.widget().deleteLater()

    def _layout_cards(self) -> None:
        if not self.cards:
            return
        columns = max(1, self.scroll.viewport().width() // 190)
        for column in range(self.gallery_layout.columnCount()):
            self.gallery_layout.setColumnStretch(column, 0)
        while self.gallery_layout.count():
            self.gallery_layout.takeAt(0)
        for index, card in enumerate(self.cards.values()):
            self.gallery_layout.addWidget(card, index // columns, index % columns)
        for column in range(columns):
            self.gallery_layout.setColumnStretch(column, 1)

    def _card_toggled(self, person_id: int, checked: bool) -> None:
        if checked:
            self._draft_ids.add(person_id)
        else:
            self._draft_ids.discard(person_id)
        self._update_selection_state()

    def _mode_changed(self, checked: bool) -> None:
        if checked:
            self._draft_mode = "all" if self.all_button.isChecked() else "any"

    def _update_selection_state(self) -> None:
        for person_id, card in self.cards.items():
            selected = person_id in self._draft_ids
            card.setAccessibleDescription(
                f"{self._groups[person_id].face_count} faces, "
                f"{self._groups[person_id].image_count} images. "
                f"{'Selected' if selected else 'Not selected'}."
            )
        self.apply_button.setEnabled(bool(self._draft_ids))

    def _apply(self) -> None:
        if self._draft_ids:
            ordered_ids = [person_id for person_id in self._groups if person_id in self._draft_ids]
            ordered_ids.extend(sorted(self._draft_ids.difference(ordered_ids)))
            self.apply_requested.emit(ordered_ids, self._draft_mode)
