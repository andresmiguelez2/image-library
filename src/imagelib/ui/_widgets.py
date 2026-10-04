from pathlib import Path

from PySide6.QtGui import QFontMetrics, QPalette
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QWidget,
)

from imagelib.config import config


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
