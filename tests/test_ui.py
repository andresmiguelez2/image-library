from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from PySide6.QtCore import QProcess, QRect, QThreadPool, Qt
    from PySide6.QtGui import QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QStyleOptionViewItem
except (ImportError, OSError):
    pytest.skip("Qt libraries are unavailable", allow_module_level=True)

from imagelib.services import analyser, catalog
from imagelib.services.catalog import FaceDetail, FaceMatchProposal, ImageListItem
from imagelib.ui.main_window import (
    AnalysisCoordinator,
    CalendarView,
    DetailPanel,
    FaceImageWidget,
    FaceMatchReviewDialog,
    MainWindow,
)
from imagelib.ui.models import CalendarModel, ThumbnailDelegate, ThumbnailModel, status_colour
from imagelib.ui.workers import ImageAsset, ImageAssetTask


@pytest.fixture(scope="session")
def application():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_live_proposal_queries(monkeypatch):
    monkeypatch.setattr(catalog, "list_pending_face_match_proposals", lambda **_kwargs: [])


def image_item(
    image_id: int,
    path: str,
    status: str = "indexed",
    taken_at: datetime | None = None,
    modified_at: datetime | None = None,
) -> ImageListItem:
    return ImageListItem(
        id=image_id,
        path=path,
        relative_path=path,
        breadcrumbs=tuple(path.split("/")),
        thumb_path=None,
        taken_at=taken_at,
        modified_at=modified_at,
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


def test_calendar_model_keeps_date_groups_and_unknown_images(application):
    model = CalendarModel()
    model.set_groups({date(2026, 1, 2): [image_item(1, "one.jpg")], None: [image_item(2, "two.jpg")]})
    assert model.rowCount() == 2
    assert model.groups[date(2026, 1, 2)][0].id == 1
    assert model.groups[None][0].id == 2


def test_calendar_month_layout_places_images_on_their_dates(application):
    model = CalendarModel()
    model.set_groups(
        {
            date(2026, 1, 2): [image_item(1, "one.jpg")],
            date(2026, 1, 31): [image_item(2, "two.jpg")],
            None: [image_item(3, "unknown.jpg")],
        }
    )
    calendar = CalendarView(model)
    calendar.set_month(date(2026, 1, 1))

    assert len(calendar.day_cells) == 42
    assert [item.id for item in calendar.day_cells[date(2026, 1, 2)].items] == [1]
    assert [item.id for item in calendar.day_cells[date(2026, 1, 31)].items] == [2]
    assert calendar.unknown_cell is not None
    assert calendar.unknown_cell.items[0].id == 3
    assert calendar.month_label.text() == "January 2026"
    calendar.deleteLater()


def test_calendar_thumbnail_updates_after_model_pixmap_is_set(application):
    model = CalendarModel()
    model.set_groups({date(2026, 1, 2): [image_item(1, "one.jpg")]})
    calendar = CalendarView(model)
    calendar.set_month(date(2026, 1, 1))
    button = calendar.day_cells[date(2026, 1, 2)]._buttons[1]

    assert button.icon().isNull()
    model.set_pixmap(1, QPixmap(20, 20))

    assert not button.icon().isNull()
    calendar.deleteLater()


def test_calendar_navigation_renders_the_requested_month(application):
    model = CalendarModel()
    model.set_groups(
        {
            date(2026, 1, 2): [image_item(1, "one.jpg")],
            date(2026, 2, 2): [image_item(2, "two.jpg")],
        }
    )
    calendar = CalendarView(model)
    calendar.set_month(date(2026, 1, 1))
    calendar.set_month(date(2026, 2, 1))

    assert calendar.month_label.text() == "February 2026"
    assert [item.id for item in calendar.day_cells[date(2026, 2, 2)].items] == [2]
    calendar.deleteLater()


def test_calendar_direct_month_and_year_navigation_renders_images(application):
    model = CalendarModel()
    model.set_groups(
        {
            date(2031, 5, 2): [image_item(1, "distant.jpg")],
        }
    )
    calendar = CalendarView(model)

    calendar.month_selector.setCurrentIndex(4)
    assert calendar.current_month == date(date.today().year, 5, 1)
    calendar.year_selector.setValue(2031)

    assert calendar.current_month == date(2031, 5, 1)
    assert calendar.month_label.text() == "May 2031"
    assert [item.id for item in calendar.day_cells[date(2031, 5, 2)].items] == [1]
    calendar.deleteLater()


def test_calendar_navigation_controls_synchronise_without_signal_loops(application):
    model = CalendarModel()
    calendar = CalendarView(model)
    changes = []
    calendar.month_changed.connect(changes.append)

    calendar.set_month(date(1, 1, 1))
    assert calendar.month_selector.currentIndex() == 0
    assert calendar.year_selector.value() == 1
    assert not calendar.previous_button.isEnabled()
    assert calendar.next_button.isEnabled()
    assert changes == [date(1, 1, 1)]

    calendar.set_month(date(9999, 12, 1))
    assert calendar.month_selector.currentIndex() == 11
    assert calendar.year_selector.value() == 9999
    assert calendar.previous_button.isEnabled()
    assert not calendar.next_button.isEnabled()
    assert changes == [date(1, 1, 1), date(9999, 12, 1)]

    calendar.set_month(date(2026, 6, 1))
    changes.clear()
    calendar.month_selector.setCurrentIndex(6)
    assert calendar.current_month == date(2026, 7, 1)
    assert changes == [date(2026, 7, 1)]
    calendar.deleteLater()


def test_calendar_image_click_and_select_all_expose_image_ids(application):
    model = CalendarModel()
    model.set_groups(
        {date(2026, 1, 2): [image_item(1, "one.jpg"), image_item(2, "two.jpg")]}
    )
    calendar = CalendarView(model)
    calendar.set_month(date(2026, 1, 1))
    cell = calendar.day_cells[date(2026, 1, 2)]
    clicked = []
    calendar.image_clicked.connect(clicked.append)

    cell._buttons[1].click()
    assert clicked == [cell.items[0]]
    assert calendar.selected_image_ids() == [1]

    cell.select_button.click()
    assert calendar.selected_image_ids() == [1, 2]
    calendar.deleteLater()


def test_main_window_analysis_uses_calendar_selection(application):
    window = MainWindow()
    window._root_generation = 1
    window.calendar_model.set_groups({date(2026, 1, 2): [image_item(7, "seven.jpg")]})
    window.calendar.set_month(date(2026, 1, 1))
    window.calendar.day_cells[date(2026, 1, 2)].select_button.click()
    calls = []
    window.analysis.start = lambda root, image_ids=None: calls.append((root, image_ids))
    window.calendar_stack.setCurrentWidget(window.calendar)

    window._analyse_selected()

    assert calls == [(window._root, [7])]
    window.close()


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


def test_grouped_unlabelled_face_card_is_accessible_and_clickable(application):
    panel = DetailPanel(QThreadPool())
    panel._faces = (
        FaceDetail(10, 0, 0, 20, 20, None, 2, None),
        FaceDetail(11, 0, 0, 20, 20, None, 3, "Ada"),
        FaceDetail(12, 0, 0, 20, 20, None, None, None),
    )
    crop = QPixmap(96, 96).toImage()
    panel._asset_ready(0, ImageAsset(QPixmap(200, 200).toImage(), (crop, crop, crop)))
    cards = [panel.face_layout.itemAt(index).widget() for index in range(3)]
    requested = []
    panel.label_face_requested.connect(requested.append)

    assert cards[0].accessibleName() == "Unlabelled face group. Activate to label this person"
    assert "Label person" in cards[0].text()
    assert cards[1].text() == "Ada"
    assert "Label person" not in cards[1].text()
    assert "Label person" not in cards[2].text()
    cards[0].click()
    cards[1].click()
    cards[2].click()
    assert requested == [10]
    panel.deleteLater()


def test_face_match_dialog_compares_named_and_candidate_samples(application):
    proposal = FaceMatchProposal(
        1, 2, "Ada", 3, 4, 0.91, 10, "/photos/candidate.jpg", None,
        1, 2, 3, 4, 0.99,
        target_face_id=5,
        target_sample_image_id=11,
        target_sample_image_path="/photos/ada.jpg",
        target_face_x=5,
        target_face_y=6,
        target_face_w=7,
        target_face_h=8,
    )
    dialog = FaceMatchReviewDialog(proposal)
    actions = []
    dialog.merge_requested.connect(lambda: actions.append("merge"))
    dialog.reject_requested.connect(lambda: actions.append("reject"))
    dialog.set_sample(True, QPixmap(32, 32))
    dialog.set_sample(False, QPixmap(32, 32))

    assert "Could this group be Ada?" in dialog.layout().itemAt(0).widget().text()
    assert dialog.candidate_sample[1].pixmap() is not None
    assert dialog.target_sample[1].pixmap() is not None
    assert dialog.merge_button.text() == "Merge"
    assert dialog.not_same_button.text() == "Not the same person"
    dialog.merge_button.click()
    dialog.not_same_button.click()
    assert actions == ["merge", "reject"]
    dialog.deleteLater()


def test_main_window_smoke_with_startup_proposal_check(application):
    window = MainWindow()
    assert window.windowTitle() == "image-library"
    assert window.browser_button.isChecked()
    window.close()


class RecordingPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)

    def clear(self):
        self.tasks.clear()


def test_face_group_label_service_runs_in_worker_and_refreshes_name(
    application, monkeypatch
):
    from imagelib.ui import main_window as ui_main_window

    window = MainWindow()
    pool = RecordingPool()
    window.pool = pool
    window._root_generation = 4
    monkeypatch.setattr(ui_main_window.QInputDialog, "getText", lambda *_args: ("Ada", True))
    calls = []
    monkeypatch.setattr(
        analyser,
        "label_face_group",
        lambda face_id, name, **kwargs: (
            calls.append((face_id, name, kwargs["root"]))
            or analyser.GroupLabelResult(9, name, 3)
        ),
    )

    window._request_face_label(12)

    assert calls == []
    pool.tasks.pop(0).run()
    assert calls == [(12, "Ada", window._root)]
    assert "Labelled 3 face(s) as Ada" in window.detail.action_message.text()
    assert len(pool.tasks) == 2
    window.close()


def test_face_group_label_service_errors_are_visible(application, monkeypatch):
    from imagelib.ui import main_window as ui_main_window

    window = MainWindow()
    pool = RecordingPool()
    window.pool = pool
    window._root_generation = 2
    monkeypatch.setattr(ui_main_window.QInputDialog, "getText", lambda *_args: ("Ada", True))

    def duplicate_name(*_args, **_kwargs):
        raise ValueError("Person name 'Ada' is already in use")

    monkeypatch.setattr(analyser, "label_face_group", duplicate_name)
    window._request_face_label(12)
    pool.tasks.pop(0).run()

    assert "Person name 'Ada' is already in use" in window.detail.action_message.text()
    assert "Could not label face group" in window.status_label.text()
    window.close()


def _proposal() -> FaceMatchProposal:
    return FaceMatchProposal(
        31, 42, "Ada", 51, 2, 0.94, 61, "/photos/candidate.jpg", None,
        1, 2, 3, 4, 0.99,
        target_face_id=71,
        target_sample_image_id=81,
        target_sample_image_path="/photos/ada.jpg",
        target_face_x=5,
        target_face_y=6,
        target_face_w=7,
        target_face_h=8,
    )


@pytest.mark.parametrize("accept", [True, False])
def test_proposal_resolution_runs_service_in_worker(application, monkeypatch, accept):
    window = MainWindow()
    pool = RecordingPool()
    window.pool = pool
    window._root_generation = 3
    proposal = _proposal()
    window._pending_proposals_ready(window._proposal_query_serial, 3, [proposal])
    dialog = window._proposal_dialog
    assert dialog is not None
    calls = []
    if accept:
        def service(face_id, target_person_id, **kwargs):
            calls.append(("merge", face_id, target_person_id, kwargs["root"]))
            return analyser.GroupLabelResult(target_person_id, "Ada", 2)

        monkeypatch.setattr(analyser, "accept_face_match_proposal", service)
        dialog.merge_button.click()
    else:
        def service(face_id, target_person_id, **kwargs):
            calls.append(("reject", face_id, target_person_id, kwargs["root"]))
            return analyser.FaceMatchResolutionResult(face_id, target_person_id, "rejected", 2)

        monkeypatch.setattr(analyser, "reject_face_match_proposal", service)
        dialog.not_same_button.click()

    assert calls == []
    pool.tasks[-1].run()
    expected = "merge" if accept else "reject"
    assert calls == [(expected, proposal.face_id, proposal.target_person_id, window._root)]
    assert window._proposal_dialog is None
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
