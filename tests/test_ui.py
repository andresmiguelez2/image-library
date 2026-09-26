from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

try:
    from PySide6.QtCore import QProcess, QRect, QThreadPool, Qt
    from PySide6.QtGui import QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QStyleOptionViewItem
except (ImportError, OSError):
    pytest.skip("Qt libraries are unavailable", allow_module_level=True)

from imagelib.services.catalog import ImageListItem
from imagelib.services import analyser
from imagelib.ui.main_window import AnalysisCoordinator, DetailPanel, FaceImageWidget, MainWindow
from imagelib.ui.models import CalendarModel, ThumbnailDelegate, ThumbnailModel, status_colour
from imagelib.ui.workers import ImageAsset, ImageAssetTask


@pytest.fixture(scope="session")
def application():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def image_item(image_id: int, path: str, status: str = "indexed") -> ImageListItem:
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
        status=status,
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


@pytest.mark.parametrize(
    ("status", "colour"),
    [("analysed", "#45c46b"), ("error", "#ef5350"), ("indexed", "#f0a43c"), ("pending", "#f0a43c")],
)
def test_thumbnail_status_colour(status, colour):
    assert status_colour(status).name() == colour


def test_thumbnail_delegate_paints_status_border(application):
    model = ThumbnailModel()
    model.set_items([image_item(1, "one.jpg", "error")])
    model.set_pixmap(1, QPixmap(20, 20))
    canvas = QImage(180, 172, QImage.Format.Format_ARGB32)
    canvas.fill("#000000")
    painter = QPainter(canvas)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 180, 172)
    ThumbnailDelegate().paint(painter, option, model.index(0))
    painter.end()

    assert canvas.pixelColor(27, 9) == status_colour("error")


def test_detail_face_rectangles_preserve_letterbox_geometry(application):
    widget = FaceImageWidget()
    widget.resize(400, 300)

    class Face:
        x, y, w, h = 20, 30, 40, 50

    widget.set_asset(QPixmap(200, 100), (Face(),))
    rectangle = widget.face_rects()[0]

    assert rectangle == QRect(46, 112, 77, 96)
    widget.set_visualisation(False)
    assert widget._visualise is False


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


def test_analysis_coordinator_logs_per_image_worker_errors(application, caplog):
    class Result:
        image_id = 42
        status = "error"
        error = "TypeError: unsupported DeepFace.represent argument"

    class Report:
        results = (Result(),)

    coordinator = AnalysisCoordinator(QThreadPool())
    coordinator._generation = 1
    statuses = []
    coordinator.status.connect(statuses.append)

    with caplog.at_level("ERROR"):
        coordinator._batch_saved(1, Report())

    assert "image 42" in caplog.text
    assert "unsupported DeepFace.represent argument" in caplog.text
    assert statuses == ["Analysis complete with 1 error(s): TypeError: unsupported DeepFace.represent argument"]


def test_analysis_coordinator_fails_on_unmatched_single_image_response(application, capsys):
    class Process:
        def __init__(self):
            self.read = True

        def canReadLine(self):
            return self.read

        def readLine(self):
            self.read = False
            return b'{"ok": true, "faces": []}\n'

    coordinator = AnalysisCoordinator(QThreadPool())
    coordinator.process = Process()
    coordinator._active = True
    errors = []
    coordinator.failed.connect(errors.append)

    coordinator._read_output()

    assert not coordinator._active
    assert errors == ["Invalid DeepFace worker response: unexpected DeepFace worker request_id: None"]
    stderr = capsys.readouterr().err
    assert 'DeepFace worker raw stdout response: {"ok": true, "faces": []}' in stderr
    assert "response parsed request_id=None status='ok'" in stderr


def test_analysis_coordinator_logs_worker_stderr_without_failing(application, caplog):
    class Process:
        def readAllStandardError(self):
            return b"cudart_stub.cc:31] Could not find cuda drivers on your machine"

    coordinator = AnalysisCoordinator(QThreadPool())
    coordinator.process = Process()
    coordinator._active = True
    errors = []
    coordinator.failed.connect(errors.append)

    with caplog.at_level("WARNING"):
        coordinator._read_error_output()

    assert coordinator._active
    assert errors == []
    assert "DeepFace worker stderr" in caplog.text


def test_analysis_coordinator_prints_analysis_targets(application, capsys):
    class Process:
        def state(self):
            return QProcess.ProcessState.NotRunning

        def setProgram(self, _program):
            pass

        def setArguments(self, _arguments):
            pass

        def setProcessEnvironment(self, _environment):
            pass

        def start(self):
            pass

    coordinator = AnalysisCoordinator(QThreadPool())
    coordinator.process = Process()
    coordinator._active = True
    coordinator._generation = 1
    coordinator._targets_ready(
        1,
        [analyser.AnalysisTarget(7, "/photos/foto-ñ.jpg", "abc123", "indexed")],
    )

    stderr = capsys.readouterr().err
    assert "Analysis targets selected count=1" in stderr
    assert "image_id=7" in stderr
    assert "foto-ñ.jpg" in stderr
    assert "status='indexed'" in stderr
    assert "content_hash='abc123'" in stderr
    assert "DeepFace worker executable=" in stderr


def test_analysis_coordinator_prints_start_root_and_selected_ids(application, capsys):
    class Pool:
        def start(self, task):
            self.task = task

    coordinator = AnalysisCoordinator(QThreadPool())
    coordinator.pool = Pool()
    coordinator.start(Path("/photos"), image_ids=[7, 8])

    stderr = capsys.readouterr().err
    assert "Analysis start root=/photos selected_image_ids=[7, 8]" in stderr


def test_analysis_coordinator_prints_sent_request(application, capsys):
    class Process:
        def __init__(self):
            self.requests = []

        def state(self):
            return QProcess.ProcessState.Running

        def write(self, request):
            self.requests.append(request)

    process = Process()
    coordinator = AnalysisCoordinator(QThreadPool())
    coordinator.process = process
    coordinator._active = True
    coordinator._generation = 1
    coordinator._targets = [analyser.AnalysisTarget(7, "/photos/foto-ñ.jpg", "abc123", "indexed")]

    coordinator._send_next(1)

    stderr = capsys.readouterr().err
    assert "Analysis request sent:" in stderr
    assert '"request_id": "1:0"' in stderr
    assert process.requests[0].endswith(b"\n")


def test_analysis_coordinator_prints_process_exit_diagnostics(application, capsys):
    coordinator = AnalysisCoordinator(QThreadPool())
    coordinator._active = True
    coordinator._generation = 1
    coordinator._process_generation = 1
    coordinator._targets = [object()]
    errors = []
    coordinator.failed.connect(errors.append)

    coordinator._process_finished(17, QProcess.ExitStatus.CrashExit)

    stderr = capsys.readouterr().err
    assert "exit_code=17" in stderr
    assert "status=" in stderr
    assert errors == ["DeepFace worker stopped before analysis completed"]
