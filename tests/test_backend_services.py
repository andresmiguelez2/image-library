from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from PIL import Image as PillowImage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from imagelib.db.models import Base, Face, Image, Person, Source
from imagelib.diagnostics import diagnostic
from imagelib.services import analyser, catalog, scanner
from imagelib.services.deepface_worker import run_worker


def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_complete_root_scan_reconciles_missing_rows_and_thumbnail(tmp_path, monkeypatch):
    factory = database()
    root = tmp_path / "images"
    root.mkdir()
    current = root / "current.jpg"
    PillowImage.new("RGB", (4, 3), "red").save(current)
    thumbnail_dir = tmp_path / "thumbnails"
    monkeypatch.setattr(scanner, "thumbnail_directory", lambda: thumbnail_dir)

    with factory() as session:
        source = Source(path=str(root))
        session.add(source)
        session.flush()
        old_thumbnail = thumbnail_dir / "old.jpg"
        old_thumbnail.parent.mkdir()
        old_thumbnail.write_bytes(b"thumbnail")
        stale = Image(
            source_id=source.id,
            path=str(root / "gone.jpg"),
            content_hash="gone",
            thumb_path=str(old_thumbnail),
        )
        session.add(stale)
        session.flush()
        session.add(Face(image_id=stale.id, x=1, y=1, w=2, h=2))
        session.commit()

    report = scanner.scan_root(root, session_factory=factory)

    with factory() as session:
        assert session.scalar(select(Image).where(Image.path == str(root / "gone.jpg"))) is None
        assert session.scalar(select(Face)) is None
    assert report.complete
    assert report.removed == 1
    assert not old_thumbnail.exists()


def test_complete_root_scan_removes_person_with_only_stale_face(tmp_path):
    factory = database()
    root = tmp_path / "images"
    root.mkdir()
    with factory() as session:
        source = Source(path=str(root))
        person = Person(embedding=[1.0] + [0.0] * 511)
        session.add_all([source, person])
        session.flush()
        stale = Image(
            source_id=source.id,
            path=str(root / "gone.jpg"),
            content_hash="gone",
            status="analysed",
            face_count=1,
        )
        session.add(stale)
        session.flush()
        session.add(
            Face(
                image_id=stale.id,
                person_id=person.id,
                x=1,
                y=1,
                w=2,
                h=2,
                embedding=[1.0] + [0.0] * 511,
            )
        )
        session.commit()

    report = scanner.scan_root(root, session_factory=factory)

    with factory() as session:
        assert session.scalar(select(Person)) is None
        assert session.scalar(select(Face)) is None
    assert report.complete and report.removed == 1


def test_complete_root_scan_refreshes_named_person_with_surviving_face(tmp_path):
    factory = database()
    root = tmp_path / "images"
    root.mkdir()
    current_path = root / "current.jpg"
    PillowImage.new("RGB", (4, 3), "red").save(current_path)
    surviving_embedding = [0.0, 1.0] + [0.0] * 510
    with factory() as session:
        source = Source(path=str(root))
        person = Person(name="Ada", embedding=[1.0] + [0.0] * 511)
        session.add_all([source, person])
        session.flush()
        current = Image(
            source_id=source.id,
            path=str(current_path),
            content_hash=scanner.sha256_file(current_path),
            status="analysed",
            face_count=1,
        )
        stale = Image(
            source_id=source.id,
            path=str(root / "gone.jpg"),
            content_hash="gone",
            status="analysed",
            face_count=1,
        )
        session.add_all([current, stale])
        session.flush()
        session.add_all(
            [
                Face(
                    image_id=current.id,
                    person_id=person.id,
                    x=1,
                    y=1,
                    w=2,
                    h=2,
                    embedding=surviving_embedding,
                ),
                Face(
                    image_id=stale.id,
                    person_id=person.id,
                    x=1,
                    y=1,
                    w=2,
                    h=2,
                    embedding=[1.0] + [0.0] * 511,
                ),
            ]
        )
        session.commit()

    scanner.scan_root(root, session_factory=factory)

    with factory() as session:
        person = session.scalar(select(Person))
        face = session.scalar(select(Face).where(Face.image_id == current.id))
        assert person is not None
        assert person.name == "Ada"
        assert person.cover_face_id == face.id
        assert person.embedding == surviving_embedding
        assert face.person_id == person.id


def test_cancelled_scan_never_reconciles(tmp_path):
    factory = database()
    root = tmp_path / "images"
    root.mkdir()
    image = root / "current.jpg"
    PillowImage.new("RGB", (4, 3), "red").save(image)
    with factory() as session:
        source = Source(path=str(root))
        session.add(source)
        session.flush()
        session.add(Image(source_id=source.id, path=str(root / "gone.jpg"), content_hash="gone"))
        session.commit()

    report = scanner.scan_root(root, session_factory=factory, cancel=lambda: True)

    with factory() as session:
        assert session.scalar(select(Image).where(Image.path == str(root / "gone.jpg"))) is not None
    assert report.cancelled and not report.complete


def test_catalog_browser_calendar_detail_and_counts(tmp_path):
    factory = database()
    root = tmp_path / "images"
    (root / "holiday").mkdir(parents=True)
    first_path = root / "holiday" / "first.jpg"
    second_path = root / "second.jpg"
    for path in (first_path, second_path):
        PillowImage.new("RGB", (8, 6), "blue").save(path)
    with factory() as session:
        source = Source(path=str(root))
        person = Person(name="Ada", embedding=[0.0] * 512)
        session.add_all([source, person])
        session.flush()
        first = Image(
            source_id=source.id,
            path=str(first_path),
            content_hash="first",
            status="analysed",
            taken_at=datetime(2026, 1, 2, 10),
            face_count=1,
        )
        second = Image(
            source_id=source.id,
            path=str(second_path),
            content_hash="second",
            status="indexed",
            taken_at=datetime(2026, 1, 2, 11),
        )
        session.add_all([first, second])
        session.flush()
        session.add(Face(image_id=first.id, person_id=person.id, x=1, y=2, w=3, h=4))
        session.commit()

    rows = catalog.list_images(root=root, session_factory=factory)
    detail = catalog.get_image_detail(first.id, root=root, session_factory=factory)
    groups = catalog.calendar_groups(root=root, session_factory=factory)
    counts = catalog.status_counts(root=root, session_factory=factory)

    assert [row.relative_path for row in rows] == ["holiday/first.jpg", "second.jpg"]
    assert rows[0].breadcrumbs == ("holiday", "first.jpg")
    assert detail is not None and detail.faces[0].person_name == "Ada"
    assert list(groups) == [datetime(2026, 1, 2).date()]
    assert counts["analysed"] == 1 and counts["indexed"] == 1


def test_catalog_person_groups_and_any_all_search_are_root_scoped(tmp_path):
    factory = database()
    root = tmp_path / "images"
    outside = tmp_path / "outside"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    outside.mkdir()
    paths = {
        "combined": root / "a" / "combined.jpg",
        "ada": root / "b" / "ada.jpg",
        "unnamed": root / "unnamed.jpg",
        "ungrouped": root / "unassigned.jpg",
        "outside": outside / "outside.jpg",
    }
    dates = {
        "combined": datetime(2026, 2, 1, 12),
        "ada": datetime(2026, 2, 2, 12),
        "unnamed": datetime(2026, 2, 3, 12),
        "ungrouped": datetime(2026, 2, 4, 12),
        "outside": datetime(2026, 2, 5, 12),
    }
    with factory() as session:
        source = Source(path=str(root))
        outside_source = Source(path=str(outside))
        ada = Person(name="Ada")
        unnamed = Person()
        outside_only = Person(name="Outside only")
        session.add_all([source, outside_source, ada, unnamed, outside_only])
        session.flush()
        images = {}
        for label, path in paths.items():
            images[label] = Image(
                source_id=outside_source.id if label == "outside" else source.id,
                path=str(path),
                content_hash=label,
                thumb_path=f"thumbs/{label}.jpg",
                taken_at=dates[label],
                face_count=2 if label == "combined" else 1,
            )
        session.add_all(images.values())
        session.flush()
        faces = [
            Face(image_id=images["combined"].id, person_id=ada.id, x=1, y=2, w=3, h=4),
            Face(image_id=images["combined"].id, person_id=unnamed.id, x=5, y=6, w=7, h=8),
            Face(image_id=images["ada"].id, person_id=ada.id, x=9, y=10, w=11, h=12),
            Face(image_id=images["unnamed"].id, person_id=unnamed.id, x=13, y=14, w=15, h=16),
            Face(image_id=images["ungrouped"].id, person_id=None, x=17, y=18, w=19, h=20),
            Face(image_id=images["outside"].id, person_id=ada.id, x=21, y=22, w=23, h=24),
            Face(image_id=images["outside"].id, person_id=outside_only.id, x=25, y=26, w=27, h=28),
        ]
        session.add_all(faces)
        session.flush()
        ada.cover_face_id = faces[5].id
        session.commit()
        ada_id = ada.id
        unnamed_id = unnamed.id

    groups = catalog.list_person_groups(root=root, session_factory=factory)
    by_id = {group.person_id: group for group in groups}
    assert set(by_id) == {ada_id, unnamed_id}
    ada_group = by_id[ada_id]
    assert ada_group.name == "Ada"
    assert ada_group.face_count == 2 and ada_group.image_count == 2
    assert ada_group.representative_image_path == str(paths["combined"])
    assert ada_group.representative_thumbnail_path == "thumbs/combined.jpg"
    assert (
        ada_group.representative_face_x,
        ada_group.representative_face_y,
        ada_group.representative_face_w,
        ada_group.representative_face_h,
    ) == (1, 2, 3, 4)
    assert by_id[unnamed_id].name is None
    assert by_id[unnamed_id].face_count == 2 and by_id[unnamed_id].image_count == 2

    any_rows = catalog.list_images(
        root=root, persons=[ada_id, unnamed_id], session_factory=factory
    )
    all_rows = catalog.list_images(
        root=root,
        persons=[ada_id, unnamed_id],
        person_match="all",
        session_factory=factory,
    )
    assert [Path(row.path).name for row in any_rows] == [
        "combined.jpg",
        "ada.jpg",
        "unnamed.jpg",
    ]
    assert len({row.id for row in any_rows}) == len(any_rows)
    assert [Path(row.path).name for row in all_rows] == ["combined.jpg"]
    assert [
        Path(row.path).name
        for row in catalog.list_images(
            root=root,
            directory="a",
            persons=[ada_id, unnamed_id],
            person_match="all",
            session_factory=factory,
        )
    ] == ["combined.jpg"]
    assert catalog.list_images(
        root=root,
        directory="b",
        persons=[ada_id, unnamed_id],
        person_match="all",
        session_factory=factory,
    ) == []
    assert [
        Path(row.path).name
        for row in catalog.list_images(
            root=root, persons=["Ada"], session_factory=factory
        )
    ] == ["combined.jpg", "ada.jpg"]

    calendar = catalog.calendar_groups(
        root=root,
        persons=[ada_id, unnamed_id],
        person_match="all",
        session_factory=factory,
    )
    assert list(calendar) == [datetime(2026, 2, 1).date()]
    assert [Path(row.path).name for rows in calendar.values() for row in rows] == [
        "combined.jpg"
    ]


def test_analysis_target_selection_scopes_root_and_retries_explicit_errors(tmp_path):
    factory = database()
    root = tmp_path / "images"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    with factory() as session:
        source = Source(path=str(root))
        other_source = Source(path=str(other))
        session.add_all([source, other_source])
        session.flush()
        session.add_all(
            [
                Image(source_id=source.id, path=str(root / "indexed.jpg"), content_hash="1", status="indexed"),
                Image(source_id=source.id, path=str(root / "error.jpg"), content_hash="2", status="error"),
                Image(source_id=source.id, path=str(root / "done.jpg"), content_hash="3", status="analysed"),
                Image(source_id=other_source.id, path=str(other / "outside.jpg"), content_hash="4", status="indexed"),
            ]
        )
        session.commit()

    targets = analyser.select_analysis_targets(root=root, session_factory=factory)
    retry = analyser.select_analysis_targets(
        root=root, paths=["error.jpg"], session_factory=factory
    )

    assert [Path(image.path).name for image in targets] == ["indexed.jpg"]
    assert [Path(image.path).name for image in retry] == ["error.jpg"]


def test_cluster_rebuild_keeps_named_person(tmp_path, monkeypatch):
    factory = database()
    root = tmp_path / "images"
    root.mkdir()
    with factory() as session:
        source = Source(path=str(root))
        session.add(source)
        session.flush()
        for index in range(2):
            session.add(
                Image(
                    source_id=source.id,
                    path=str(root / f"photo-{index}.jpg"),
                    content_hash=str(index),
                    status="indexed",
                )
            )
        session.commit()
    embedding = [1.0] + [0.0] * 511
    monkeypatch.setattr(analyser, "_get_model", lambda: object())
    monkeypatch.setattr(analyser, "_represent", lambda path, model: [{
        "embedding": embedding,
        "facial_area": {"x": 1, "y": 1, "w": 2, "h": 2},
    }])
    analyser.analyse_all(root, session_factory=factory)
    with factory() as session:
        person = session.scalar(select(Person))
        person.name = "Ada"
        person_id = person.id
        image = session.scalar(select(Image).where(Image.path.like("%photo-0.jpg")))
        image.status = "indexed"
        session.commit()

    analyser.analyse_images(root=root, session_factory=factory)

    with factory() as session:
        person = session.scalar(select(Person).where(Person.id == person_id))
        assert person is not None and person.name == "Ada"


def test_process_worker_response_persists_atomically_and_rejects_stale_hash(tmp_path):
    factory = database()
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"worker-input")
    content_hash = scanner.sha256_file(image_path)
    with factory() as session:
        source = Source(path=str(tmp_path))
        session.add(source)
        session.flush()
        image = Image(
            source_id=source.id,
            path=str(image_path),
            content_hash=content_hash,
            status="indexed",
        )
        session.add(image)
        session.commit()
        image_id = image.id

    payload = {
        "ok": True,
        "faces": [
            {
                "embedding": [0.1] * 512,
                "facial_area": {"x": 1, "y": 2, "w": 10, "h": 12},
                "face_confidence": 0.98,
            }
        ],
    }
    result = analyser.persist_worker_response(
        image_id,
        payload,
        expected_content_hash=content_hash,
        session_factory=factory,
    )

    image_path.write_bytes(b"changed-after-worker-started")
    stale = analyser.persist_worker_response(
        image_id,
        payload,
        expected_content_hash=content_hash,
        session_factory=factory,
    )
    with factory() as session:
        image = session.scalar(select(Image).where(Image.id == image_id))
        assert image.status == "analysed"
        assert image.face_count == 1
        assert session.scalar(select(Face).where(Face.image_id == image_id)) is not None
    assert result.accepted and result.status == "analysed"
    assert not stale.accepted and stale.status == "stale"


def test_process_batch_rebuild_api_uses_fake_faces_without_deepface(tmp_path, capsys):
    factory = database()
    embedding = [1.0] + [0.0] * 511
    with factory() as session:
        source = Source(path=str(tmp_path))
        session.add(source)
        session.flush()
        for index in range(2):
            path = tmp_path / f"photo-{index}.jpg"
            path.write_bytes(str(index).encode())
            session.add(
                Image(
                    source_id=source.id,
                    path=str(path),
                    content_hash=scanner.sha256_file(path),
                    status="indexed",
                )
            )
        session.commit()

    targets = analyser.select_analysis_targets(root=tmp_path, session_factory=factory)
    batch = analyser.persist_worker_batch(
        [
            (
                target,
                [
                    {
                        "embedding": embedding,
                        "facial_area": {"x": 1, "y": 1, "w": 2, "h": 2},
                    }
                ],
            )
            for target in targets
        ],
        session_factory=factory,
    )

    assert [result.status for result in batch.results] == ["analysed", "analysed"]
    assert batch.clusters.faces == 2
    assert batch.clusters.clustered_faces == 2
    stderr = capsys.readouterr().err
    assert "Analysis persistence service started" in stderr
    assert "Analysis persistence image completed image_id=" in stderr
    assert "Analysis persistence service completed" in stderr


def test_deepface_worker_loads_model_once():
    input_stream = StringIO(
        '{"op":"analyse","path":"one.jpg","request_id":"one"}\n'
        '{"op":"analyse","path":"two.jpg","request_id":"two"}\n'
        '{"op":"shutdown","request_id":"stop"}\n'
    )
    output_stream = StringIO()
    models = []

    def model_factory():
        model = object()
        models.append(model)
        return model

    run_worker(
        input_stream,
        output_stream,
        model_factory=model_factory,
        representer=lambda path, model: [{"path": path, "same": model is models[0]}],
    )

    lines = [line for line in output_stream.getvalue().splitlines()]
    responses = [json.loads(line) for line in lines]
    assert len(models) == 1
    assert [response["request_id"] for response in responses] == ["one", "two", "stop"]
    assert all(response["ok"] for response in responses)


def test_deepface_worker_redirects_model_output_and_returns_errors(capsys):
    input_stream = StringIO('{"op":"analyse","path":"one.jpg"}\n{"op":"shutdown"}\n')
    output_stream = StringIO()

    def model_factory():
        print("TensorFlow startup noise")
        return object()

    def representer(_path, _model):
        print("DeepFace representer noise")
        raise RuntimeError("analysis failed")

    run_worker(input_stream, output_stream, model_factory=model_factory, representer=representer)

    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert responses[0]["ok"] is False
    assert "analysis failed" in responses[0]["error"]
    assert responses[1]["ok"] is True
    stderr = capsys.readouterr().err
    assert "TensorFlow startup noise" in stderr
    assert "DeepFace representer noise" in stderr
    assert "DeepFace worker request operation='analyse'" in stderr
    assert "path='one.jpg'" in stderr
    assert "RuntimeError: analysis failed" in stderr
    assert "Traceback (most recent call last)" in stderr


def test_diagnostic_flushes_to_supplied_stream():
    class Stream:
        def __init__(self):
            self.values = []
            self.flush_count = 0

        def write(self, value):
            self.values.append(value)

        def flush(self):
            self.flush_count += 1

    stream = Stream()
    diagnostic("flushed", stream=stream)

    assert "flushed" in "".join(stream.values)
    assert stream.flush_count == 1


def test_deepface_worker_default_representer_matches_installed_api(tmp_path, monkeypatch):
    calls = []

    class DeepFace:
        @staticmethod
        def represent(*, img_path, model_name, detector_backend, enforce_detection):
            calls.append(
                {
                    "img_path": img_path,
                    "model_name": model_name,
                    "detector_backend": detector_backend,
                    "enforce_detection": enforce_detection,
                }
            )
            return {"embedding": [0.1] * 512}

    from imagelib.services import deepface_worker

    image_path = tmp_path / "foto-ñ.jpg"
    image_path.write_bytes(b"fixture")
    fake_deepface = ModuleType("deepface")
    fake_deepface.DeepFace = DeepFace
    monkeypatch.setitem(sys.modules, "deepface", fake_deepface)

    result = deepface_worker._default_representer(str(image_path), object())

    assert result == [{"embedding": [0.1] * 512}]
    assert isinstance(calls[0]["img_path"], BytesIO)
    assert calls[0]["img_path"].read() == b"fixture"
    assert calls[0]["model_name"] == "Facenet512"
    assert calls[0]["detector_backend"] == deepface_worker.config["analysis"].get("detector_backend", "retinaface")
    assert calls[0]["enforce_detection"] is False


def test_deepface_worker_uses_configured_detector(tmp_path, monkeypatch):
    calls = []

    class DeepFace:
        @staticmethod
        def represent(**kwargs):
            calls.append(kwargs)
            return []

    from imagelib.services import deepface_worker

    monkeypatch.setattr(deepface_worker, "config", {"analysis": {"detector_backend": "opencv"}})
    fake_deepface = ModuleType("deepface")
    fake_deepface.DeepFace = DeepFace
    monkeypatch.setitem(sys.modules, "deepface", fake_deepface)
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"fixture")
    deepface_worker._default_representer(str(image_path), object())

    assert calls[0]["detector_backend"] == "opencv"
