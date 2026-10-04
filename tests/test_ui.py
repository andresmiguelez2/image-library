from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path

import pytest

try:
    from PySide6.QtCore import QProcess, QRect, Qt, QThreadPool
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
from imagelib.ui.models import (
    CalendarModel,
    ThumbnailDelegate,
    ThumbnailModel,
    status_colour,
)
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
    pixmap = QPixmap(20, 20)
    pixmap.fill("#112233")
    model.set_pixmap(1, pixmap)
    canvas = QImage(180, 172, QImage.Format.Format_ARGB32)
    canvas.fill("#000000")
    painter = QPainter(canvas)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 180, 172)
    ThumbnailDelegate().paint(painter, option, model.index(0))
    painter.end()

    error = status_colour("error")
    painted = [
        (x, y)
        for y in range(canvas.height())
        for x in range(canvas.width())
        if canvas.pixelColor(x, y) == error
    ]
    assert painted
    assert all(8 <= x <= 15 for x, _y in painted)
    margin = 8
    dot = 8
    text_height = max(option.fontMetrics.lineSpacing(), 1)
    text_top = option.rect.bottom() - 4 - text_height
    centre_y = text_top + text_height // 2
    assert canvas.pixelColor(margin + dot // 2, centre_y) == error


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


def person_group(person_id: int, name: str | None = None, image_path: str = ""):
    return catalog.PersonGroup(
        person_id=person_id,
        name=name,
        face_count=3,
        image_count=2,
        representative_face_id=person_id + 100,
        representative_face_x=2,
        representative_face_y=3,
        representative_face_w=8,
        representative_face_h=9,
        representative_image_id=person_id + 200,
        representative_image_path=image_path,
        representative_thumbnail_path=None,
    )


def test_people_gallery_cards_are_checkable_accessible_and_apply_any_or_all(application):
    from imagelib.ui.main_window import PeopleView

    view = PeopleView()
    view.begin_edit((), "any")
    view.set_groups([person_group(4, "Ada"), person_group(5)])
    actions = []
    view.apply_requested.connect(lambda ids, mode: actions.append((ids, mode)))

    assert not view.apply_button.isEnabled()
    assert view.cards[4].text().startswith("Ada\n3 faces · 2 images")
    assert view.cards[5].text().startswith("Unlabelled group 5")
    assert view.cards[4].isCheckable()
    assert "Not selected" in view.cards[4].accessibleDescription()

    view.cards[4].click()
    view.cards[5].click()
    view.all_button.click()
    assert view.apply_button.isEnabled()
    assert "Selected" in view.cards[4].accessibleDescription()
    view.apply_button.click()

    assert actions == [([4, 5], "all")]
    view.deleteLater()


@pytest.mark.parametrize("previous_view", ["browser", "calendar"])
def test_people_picker_apply_and_cancel_preserve_active_filter_and_return_view(
    application, monkeypatch, previous_view
):
    window = MainWindow()
    pool = RecordingPool()
    window.pool = pool
    if previous_view == "browser":
        window.browser.set_directory(Path("holiday"))
    else:
        window.calendar_button.setChecked(True)
    window._people_filter_ids = (4,)
    window._people_filter_mode = "all"
    groups = [person_group(4, "Ada"), person_group(5, "Grace")]
    monkeypatch.setattr(catalog, "list_person_groups", lambda **_kwargs: groups)

    window._open_people_picker()
    assert window.calendar_stack.currentWidget() is window.people_view
    pool.tasks.pop(0).run()
    window.people_view.cards[5].click()
    window.people_view.any_button.click()
    window._cancel_people_picker()

    assert window._people_filter_ids == (4,)
    assert window._people_filter_mode == "all"
    assert window.calendar_stack.currentWidget() is (
        window.browser if previous_view == "browser" else window.calendar
    )
    if previous_view == "browser":
        assert window.browser.directory == Path("holiday")

    pool.tasks.clear()
    window._open_people_picker()
    pool.tasks.pop(0).run()
    assert window.people_view.cards[4].isChecked()
    assert not window.people_view.cards[5].isChecked()
    assert window.people_view.all_button.isChecked()
    window.people_view.cards[5].click()
    window.people_view.apply_button.click()

    assert window._people_filter_ids == (4, 5)
    assert window._people_filter_mode == "all"
    assert window.calendar_stack.currentWidget() is (
        window.browser if previous_view == "browser" else window.calendar
    )
    if previous_view == "browser":
        assert window.browser.directory == Path("holiday")
    assert window._selected_image_id is None
    assert not window.people_filter_label.isHidden()
    window.close()


@pytest.mark.parametrize("person_match", ["any", "all"])
def test_people_filter_is_passed_to_refresh_and_folder_navigation(
    application, monkeypatch, person_match
):
    window = MainWindow()
    pool = RecordingPool()
    window.pool = pool
    window._people_filter_ids = (12, 15)
    window._people_filter_mode = person_match
    window.browser.set_directory(Path("events"))
    browser_calls = []
    calendar_calls = []
    monkeypatch.setattr(catalog, "status_counts", lambda **_kwargs: {"pending": 0, "indexed": 1, "analysed": 2, "error": 0})

    def browser_images(**kwargs):
        browser_calls.append(kwargs)
        return []

    def calendar_groups(**kwargs):
        calendar_calls.append(kwargs)
        return {}

    monkeypatch.setattr(catalog, "browser_images", browser_images)
    monkeypatch.setattr(catalog, "calendar_groups", calendar_groups)

    window._refresh(window._root_generation)
    pool.tasks.pop(0).run()

    assert [call.get("directory") for call in browser_calls] == [Path("events"), None]
    assert all(
        call["persons"] == (12, 15) and call["person_match"] == person_match
        for call in browser_calls
    )
    assert calendar_calls == [
        {"root": window._root, "persons": (12, 15), "person_match": person_match}
    ]

    window._browse_directory(Path("events/2026"))
    pool.tasks.pop().run()
    assert browser_calls[-1]["directory"] == Path("events/2026")
    assert browser_calls[-1]["persons"] == (12, 15)
    assert browser_calls[-1]["person_match"] == person_match
    assert window.total_label.text() == "Total 3"
    window.close()


def test_clearing_people_filter_restores_unfiltered_queries(application, monkeypatch):
    window = MainWindow()
    pool = RecordingPool()
    window.pool = pool
    window._people_filter_ids = (12,)
    window._people_filter_mode = "all"
    calls = []
    monkeypatch.setattr(catalog, "status_counts", lambda **_kwargs: {})
    monkeypatch.setattr(catalog, "browser_images", lambda **kwargs: calls.append(kwargs) or [])
    monkeypatch.setattr(catalog, "calendar_groups", lambda **_kwargs: {})

    window._clear_people_filter()
    pool.tasks.pop(0).run()

    assert window._people_filter_ids == ()
    assert window._people_filter_mode == "any"
    assert all(call["persons"] is None and call["person_match"] == "any" for call in calls)
    assert window.people_filter_label.isHidden()
    window.close()


def test_people_root_change_resets_filter_and_ignores_stale_group_result(
    application, monkeypatch, tmp_path
):
    window = MainWindow()
    pool = RecordingPool()
    window.pool = pool
    window._people_filter_ids = (4,)
    window._people_filter_mode = "all"
    monkeypatch.setattr(catalog, "list_person_groups", lambda **_kwargs: [person_group(99)])
    window._open_people_picker()
    stale_groups_task = pool.tasks.pop(0)
    old_generation = window._root_generation
    window._people_groups_ready(window._people_query_serial, old_generation, [person_group(99)])
    stale_crop_task = pool.tasks.pop(0)

    window._root_validated(window._validation_serial, (tmp_path, True))
    stale_groups_task.run()
    stale_crop_task.run()

    assert window._root_generation == old_generation + 1
    assert window._people_filter_ids == ()
    assert window._people_filter_mode == "any"
    assert window.calendar_stack.currentWidget() is window.browser
    assert window.people_view.cards == {}
    window.close()


def test_people_representative_face_crop_is_loaded_in_worker(application, tmp_path):
    from PIL import Image as PillowImage

    path = tmp_path / "representative.png"
    PillowImage.new("RGB", (24, 24), "red").save(path)
    window = MainWindow()
    pool = RecordingPool()
    window.pool = pool
    window._root_generation = 1
    group = person_group(4, "Ada", str(path))
    window._people_query_serial = 2
    window.people_view.set_groups([group])

    window._people_groups_ready(2, 1, [group])
    crop_task = pool.tasks.pop(0)
    crop_task.run()

    assert not window.people_view.cards[4].icon().isNull()
    window.close()


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
