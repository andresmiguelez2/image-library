from datetime import datetime
from io import StringIO
from pathlib import Path

from PIL import Image as PillowImage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from imagelib.db.models import Base, Face, Image, Person, Source
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


def test_process_batch_rebuild_api_uses_fake_faces_without_deepface(tmp_path):
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


def test_deepface_worker_loads_model_once():
    input_stream = StringIO('{"op":"analyse","path":"one.jpg"}\n{"op":"analyse","path":"two.jpg"}\n{"op":"shutdown"}\n')
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
    assert len(models) == 1
    assert '"ok": true' in lines[0]
    assert '"ok": true' in lines[1]
