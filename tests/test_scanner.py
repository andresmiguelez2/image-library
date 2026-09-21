from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from imagelib.db.models import Base, Face, Image as ImageRow
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
        row = session.scalar(select(ImageRow).where(ImageRow.path == str(source.resolve())))
        assert row.status == "indexed"
        assert row.width == 10 and row.height == 8
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
        row = session.scalar(select(ImageRow).where(ImageRow.path == str(source.resolve())))
        session.add(Face(image_id=row.id, x=1, y=1, w=2, h=2))
        session.commit()

    Image.new("RGB", (11, 9), "blue").save(source)
    changed = scanner.scan_watched_dirs(session_factory=test_session_factory)
    with test_session_factory() as session:
        row = session.scalar(select(ImageRow).where(ImageRow.path == str(source.resolve())))
        assert row.status == "indexed"
        assert row.width == 11
        assert session.scalars(select(Face).where(Face.image_id == row.id)).all() == []
    assert changed.indexed == 1

    source.write_bytes(b"not a png")
    failed = scanner.scan_watched_dirs(session_factory=test_session_factory)
    with test_session_factory() as session:
        row = session.scalar(select(ImageRow).where(ImageRow.path == str(source.resolve())))
        assert row.status == "error"
        assert "UnidentifiedImageError" in row.error
    assert failed.errors == 1
