from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from imagelib.db.models import Base, Face, Image as ImageRow, ImageLocation, Source
from imagelib.services import scanner
from imagelib.services.scanner import (
    SUPPORTED_SUFFIXES,
    _metadata,
    iter_image_paths,
    sha256_file,
    thumbnail_path_for,
)


@pytest.fixture
def test_session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory


def test_discovery_only_returns_supported_image_files(tmp_path: Path) -> None:
    image = tmp_path / "nested" / "photo.PNG"
    image.parent.mkdir()
    Image.new("RGB", (12, 7), "red").save(image)
    Image.new("RGB", (2, 2), "blue").save(tmp_path / "photo.jpg")
    (tmp_path / "movie.mp4").write_bytes(b"not an image")
    (tmp_path / "notes.jpeg.txt").write_text("no")

    found = iter_image_paths([tmp_path])

    assert found == sorted([image.resolve(), (tmp_path / "photo.jpg").resolve()])
    assert SUPPORTED_SUFFIXES == {".png", ".jpg", ".jpeg"}


def test_hash_metadata_and_thumbnail_path_do_not_modify_source(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "photo.png"
    Image.new("RGBA", (20, 10), (1, 2, 3, 255)).save(source)
    before = source.read_bytes()
    monkeypatch.setattr(
        "imagelib.services.scanner.thumbnail_directory", lambda: tmp_path / "thumbs"
    )

    digest = sha256_file(source)
    metadata = _metadata(source)
    destination = thumbnail_path_for(source, digest)

    assert len(digest) == 64
    assert metadata["width"] == 20
    assert metadata["height"] == 10
    assert destination == tmp_path / "thumbs" / f"{digest}.jpg"
    assert source.read_bytes() == before


def test_scan_persists_and_skips_unchanged_files(tmp_path, monkeypatch, test_session_factory) -> None:
    source = tmp_path / "photo.jpg"
    Image.new("RGB", (10, 8), "red").save(source)
    monkeypatch.setattr(scanner, "iter_image_paths", lambda: [source.resolve()])
    monkeypatch.setattr(scanner, "watched_directories", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(scanner, "thumbnail_directory", lambda: tmp_path / "thumbs")

    first = scanner.scan_watched_dirs(session_factory=test_session_factory)
    second = scanner.scan_watched_dirs(session_factory=test_session_factory)

    with test_session_factory() as session:
        row = session.scalar(select(ImageRow))
        assert row.status == "indexed"
        assert row.width == 10 and row.height == 8
        assert row.locations[0].path == str(source.resolve())
        assert Path(row.thumb_path).exists()
    assert first.indexed == 1
    assert second.unchanged == 1


def test_changed_file_removes_old_faces_and_corrupt_file_isolated(
    tmp_path, monkeypatch, test_session_factory
) -> None:
    source = tmp_path / "photo.png"
    Image.new("RGB", (10, 8), "red").save(source)
    monkeypatch.setattr(scanner, "iter_image_paths", lambda: [source.resolve()])
    monkeypatch.setattr(scanner, "watched_directories", lambda: [tmp_path.resolve()])
    monkeypatch.setattr(scanner, "thumbnail_directory", lambda: tmp_path / "thumbs")
    scanner.scan_watched_dirs(session_factory=test_session_factory)
    with test_session_factory() as session:
        location = session.scalar(
            select(ImageLocation).where(ImageLocation.path == str(source.resolve()))
        )
        row = session.get(ImageRow, location.image_id)
        session.add(Face(image_id=row.id, x=1, y=1, w=2, h=2))
        session.commit()

    Image.new("RGB", (11, 9), "blue").save(source)
    changed = scanner.scan_watched_dirs(session_factory=test_session_factory)
    with test_session_factory() as session:
        location = session.scalar(
            select(ImageLocation).where(ImageLocation.path == str(source.resolve()))
        )
        row = session.get(ImageRow, location.image_id)
        assert row.status == "indexed"
        assert row.width == 11
        assert row.content_hash == sha256_file(source)
        assert session.scalars(select(Face).where(Face.image_id == row.id)).all() == []
        assert len(list(session.scalars(select(ImageRow)))) == 1
    assert changed.indexed == 1

    source.write_bytes(b"not a png")
    failed = scanner.scan_watched_dirs(session_factory=test_session_factory)
    with test_session_factory() as session:
        location = session.scalar(
            select(ImageLocation).where(ImageLocation.path == str(source.resolve()))
        )
        row = session.get(ImageRow, location.image_id)
        assert row.status == "error"
        assert "UnidentifiedImageError" in row.error
    assert failed.errors == 1


def test_identical_copies_share_one_image_and_add_two_locations(
    tmp_path, monkeypatch, test_session_factory
) -> None:
    root = tmp_path / "images"
    root.mkdir()
    first = root / "first.jpg"
    second = root / "nested" / "second.jpg"
    second.parent.mkdir()
    Image.new("RGB", (10, 8), "red").save(first)
    second.write_bytes(first.read_bytes())
    monkeypatch.setattr(scanner, "thumbnail_directory", lambda: tmp_path / "thumbs")

    report = scanner.scan_root(root, session_factory=test_session_factory)

    with test_session_factory() as session:
        rows = list(session.scalars(select(ImageRow)))
        locations = list(session.scalars(select(ImageLocation).order_by(ImageLocation.path)))
        assert len(rows) == 1
        assert [location.path for location in locations] == [str(first), str(second)]
        assert {location.image_id for location in locations} == {rows[0].id}
    assert report.indexed == 1
    assert report.unchanged == 1


def test_move_keeps_logical_image_and_face_ids(tmp_path, monkeypatch, test_session_factory) -> None:
    root = tmp_path / "images"
    root.mkdir()
    original = root / "original.jpg"
    renamed = root / "renamed.jpg"
    Image.new("RGB", (10, 8), "red").save(original)
    monkeypatch.setattr(scanner, "thumbnail_directory", lambda: tmp_path / "thumbs")
    scanner.scan_root(root, session_factory=test_session_factory)
    with test_session_factory() as session:
        image_row = session.scalar(select(ImageRow))
        image_id = image_row.id
        face = Face(image_id=image_id, x=1, y=1, w=2, h=2)
        session.add(face)
        session.commit()
        face_id = face.id

    renamed.write_bytes(original.read_bytes())
    original.unlink()
    scanner.scan_root(root, session_factory=test_session_factory)

    with test_session_factory() as session:
        image_row = session.scalar(select(ImageRow))
        assert image_row.id == image_id
        assert [location.path for location in image_row.locations] == [str(renamed)]
        assert session.get(Face, face_id) is not None


def test_reconciliation_prunes_missing_location_but_keeps_shared_image(
    tmp_path, monkeypatch, test_session_factory
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = first_root / "photo.jpg"
    second = second_root / "copy.jpg"
    Image.new("RGB", (10, 8), "red").save(first)
    second.write_bytes(first.read_bytes())
    monkeypatch.setattr(scanner, "thumbnail_directory", lambda: tmp_path / "thumbs")
    scanner.scan_root(first_root, session_factory=test_session_factory)
    scanner.scan_root(second_root, session_factory=test_session_factory)
    with test_session_factory() as session:
        image_row = session.scalar(select(ImageRow))
        image_id = image_row.id
        face = Face(image_id=image_id, x=1, y=1, w=2, h=2)
        session.add(face)
        session.commit()
        face_id = face.id

    first.unlink()
    report = scanner.scan_root(first_root, session_factory=test_session_factory)

    with test_session_factory() as session:
        image_row = session.get(ImageRow, image_id)
        assert image_row is not None
        assert [location.path for location in image_row.locations] == [str(second)]
        assert session.get(Face, face_id) is not None
    assert report.removed == 0


def test_changed_copy_moves_location_without_deleting_shared_old_identity(
    tmp_path, monkeypatch, test_session_factory
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    changed_path = first_root / "photo.jpg"
    surviving_copy = second_root / "copy.jpg"
    Image.new("RGB", (10, 8), "red").save(changed_path)
    surviving_copy.write_bytes(changed_path.read_bytes())
    monkeypatch.setattr(scanner, "thumbnail_directory", lambda: tmp_path / "thumbs")
    scanner.scan_root(first_root, session_factory=test_session_factory)
    scanner.scan_root(second_root, session_factory=test_session_factory)
    with test_session_factory() as session:
        old_image = session.scalar(select(ImageRow))
        old_hash = old_image.content_hash
        face = Face(image_id=old_image.id, x=1, y=1, w=2, h=2)
        session.add(face)
        session.commit()
        old_image_id, face_id = old_image.id, face.id

    Image.new("RGB", (11, 9), "blue").save(changed_path)
    scanner.scan_root(first_root, session_factory=test_session_factory)

    with test_session_factory() as session:
        old_image = session.get(ImageRow, old_image_id)
        new_location = session.scalar(
            select(ImageLocation).where(ImageLocation.path == str(changed_path))
        )
        assert old_image is not None and old_image.content_hash == old_hash
        assert [location.path for location in old_image.locations] == [str(surviving_copy)]
        assert new_location.image_id != old_image_id
        assert session.get(Face, face_id) is not None


def test_failed_root_scan_does_not_prune_recorded_locations(
    tmp_path, monkeypatch, test_session_factory
) -> None:
    root = tmp_path / "images"
    root.mkdir()
    existing = root / "existing.jpg"
    missing = root / "missing.jpg"
    Image.new("RGB", (10, 8), "red").save(existing)
    monkeypatch.setattr(scanner, "thumbnail_directory", lambda: tmp_path / "thumbs")
    scanner.scan_root(root, session_factory=test_session_factory)
    with test_session_factory() as session:
        image_row = session.scalar(select(ImageRow))
        missing_image = ImageRow(content_hash="missing", status="indexed")
        session.add(missing_image)
        session.flush()
        source = session.scalar(select(Source).where(Source.path == str(root)))
        session.add(
            ImageLocation(image_id=missing_image.id, source=source, path=str(missing))
        )
        session.commit()

    existing.write_bytes(b"corrupt image")
    report = scanner.scan_root(root, session_factory=test_session_factory)

    with test_session_factory() as session:
        locations = set(session.scalars(select(ImageLocation.path)))
        assert str(missing) in locations
        assert str(existing) in locations
    assert report.errors == 1
    assert not report.reconciled


def test_unavailable_root_retains_its_recorded_image_and_location(
    tmp_path, test_session_factory
) -> None:
    root = tmp_path / "offline"
    with test_session_factory() as session:
        source = Source(path=str(root))
        session.add(source)
        session.flush()
        image_row = ImageRow(content_hash="offline", status="analysed", face_count=1)
        session.add(image_row)
        session.flush()
        location = ImageLocation(
            image_id=image_row.id,
            source_id=source.id,
            path=str(root / "photo.jpg"),
        )
        session.add(location)
        session.commit()
        image_id = image_row.id

    report = scanner.scan_root(root, session_factory=test_session_factory)

    with test_session_factory() as session:
        assert session.get(ImageRow, image_id) is not None
        assert session.scalar(select(ImageLocation).where(ImageLocation.path == location.path))
    assert not report.complete and not report.reconciled
