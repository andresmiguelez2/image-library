from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

try:
    from PySide6.QtCore import QProcess, QRect, QThreadPool, Qt
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication
except (ImportError, OSError):
    pytest.skip("Qt libraries are unavailable", allow_module_level=True)

from imagelib.services.catalog import ImageListItem
from imagelib.ui.main_window import AnalysisCoordinator, DetailPanel, MainWindow
from imagelib.ui.models import CalendarModel, ThumbnailDelegate, ThumbnailModel
from imagelib.ui.workers import ImageAsset, ImageAssetTask


@pytest.fixture(scope="session")
def application():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def image_item(image_id: int, path: str) -> ImageListItem:
    return ImageListItem(
        id=image_id,
        path=path,
        relative_path=path,
        breadcrumbs=tuple(path.split("/")),
        thumb_path=None,
        taken_at=None,
        modified_at=None,
        width=10,
        height=10,
        face_count=0,
        status="indexed",
    )


def test_thumbnail_model_exposes_rows_and_multi_selection_data(application):
    model = ThumbnailModel()
    model.set_items([image_item(1, "one.jpg"), image_item(2, "two.jpg")])
    assert model.rowCount() == 2
    assert model.index(0, 0).data(Qt.ItemDataRole.UserRole).id == 1


def test_thumbnail_delegate_keeps_portrait_and_landscape_bounds(application):
    image_rect = QRect(0, 0, 164, 128)
    delegate = ThumbnailDelegate()

    portrait = delegate._pixmap_rect(image_rect, QPixmap(80, 160))
    landscape = delegate._pixmap_rect(image_rect, QPixmap(320, 120))

    assert (portrait.width(), portrait.height()) == (64, 128)
    assert (landscape.width(), landscape.height()) == (164, 61)


def test_calendar_model_has_date_sections_and_unknown_group(application):
    model = CalendarModel()
    model.set_groups({date(2026, 1, 2): [image_item(1, "one.jpg")], None: [image_item(2, "two.jpg")]})
    assert model.rowCount() == 4
    assert model.index(0, 0).data() == "2026-01-02"
    assert model.index(2, 0).data() == "Unknown date"
    assert model.item(model.index(3, 0)).id == 2


def test_image_asset_worker_loads_face_crops(application, tmp_path):
    from PIL import Image as PillowImage

    path = tmp_path / "photo.jpg"
    PillowImage.new("RGB", (10, 10), "red").save(path)

    class Face:
        x, y, w, h = 2, 3, 4, 5

    assets = []
    task = ImageAssetTask(str(path), (Face(),))
    task.signals.result.connect(assets.append)
    task.run()

    assert assets[0].crops[0].width() == 4
    assert assets[0].crops[0].height() == 5


def test_detail_face_ribbon_has_room_for_face_cards(application):
    panel = DetailPanel(QThreadPool())

    class Face:
        person_name = "Person"

    panel._faces = (Face(),)
    panel._asset_ready(0, ImageAsset(QPixmap(200, 200).toImage(), (QPixmap(96, 96).toImage(),)))

    assert panel.face_scroll.minimumHeight() >= 152
    assert panel.face_strip.minimumHeight() >= 140
    assert panel.face_layout.itemAt(0).widget().sizeHint().height() >= 108
    panel.deleteLater()


def test_main_window_smoke_without_startup_database_query(application):
    window = MainWindow()
    assert window.windowTitle() == "image-library"
    assert window.browser_button.isChecked()
    window.close()


def test_browser_ignores_catalogue_result_for_previous_directory(application):
    window = MainWindow()
    window._root_generation = 1
    window._browser_serial = 2
    window.browser.set_directory(Path("new"))

    window._catalog_ready(
        1,
        ({"pending": 0, "indexed": 0, "analysed": 0, "error": 0}, [], {}, []),
        Path("old"),
        1,
    )

    assert window.browser.directory.parts == ("new",)
    assert window.browser.model.rowCount() == 0
    window.close()


def test_changing_root_clears_previous_browser_rows(application):
    window = MainWindow()
    window.browser.model.set_items([image_item(1, "old.jpg")])

    window.browser.set_root(Path("new-root"))

    assert window.browser.model.rowCount() == 0
    window.close()


def test_analysis_does_not_write_to_process_being_replaced(application):
    class RunningProcess:
        def __init__(self):
            self.killed = False

        def state(self):
            return QProcess.ProcessState.Running

        def kill(self):
            self.killed = True

        def write(self, _data):
            raise AssertionError("a replacement process must not receive requests")

    coordinator = AnalysisCoordinator(QThreadPool())
    process = RunningProcess()
    coordinator.process = process
    coordinator._generation = 1
    coordinator._active = True
    coordinator._targets = [object()]

    coordinator._start_process_when_available(1)

    assert process.killed
    assert coordinator._starting_after_finish


def test_cancelled_analysis_refreshes_after_late_persistence(application):
    coordinator = AnalysisCoordinator(QThreadPool())
    coordinator._generation = 2
    changed = []
    coordinator.catalogue_changed.connect(lambda: changed.append(True))

    coordinator._batch_saved(1, object())

    assert changed == [True]
