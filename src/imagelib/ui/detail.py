import logging

from PySide6.QtCore import QPointF, QRect, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from imagelib.services import catalog
from imagelib.ui._widgets import ElidedLabel, _mute_label, _use_surface
from imagelib.ui.workers import ImageAsset, ImageAssetTask

logger = logging.getLogger("imagelib.ui.main_window")


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
