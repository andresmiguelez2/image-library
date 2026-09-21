"""Stage-one image ingest.

This module deliberately does not detect faces.  Analysis is a separate stage
gated by ``Image.status`` and ``Image.content_hash``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from PIL import ExifTags
from PIL import Image as PILImage
from sqlalchemy import delete, select

from imagelib.config import thumbnail_directory, watched_directories
from imagelib.db.models import Face, Image, Source
from imagelib.db.session import SessionLocal

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg"}


@dataclass
class ScanReport:
    discovered: int = 0
    indexed: int = 0
    unchanged: int = 0
    errors: int = 0


def iter_image_paths(directories: Iterable[Path] | None = None) -> list[Path]:
    """Return supported regular files, without considering video files."""
    paths: set[Path] = set()
    for directory in directories if directories is not None else watched_directories():
        if not directory.is_dir():
            continue
        paths.update(
            path.resolve()
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        )
    return sorted(paths)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def thumbnail_path_for(image_path: Path, content_hash: str | None = None) -> Path:
    """Return a deterministic cached-thumbnail path without touching the source."""
    content_hash = content_hash or sha256_file(image_path)
    return thumbnail_directory() / f"{content_hash}.jpg"


def _gps_value(value) -> float | None:
    try:
        if hasattr(value, "values"):
            value = value.values
        if len(value) == 3:
            return float(value[0]) + float(value[1]) / 60 + float(value[2]) / 3600
        return float(value)
    except (TypeError, ValueError, IndexError, ZeroDivisionError):
        return None


def _metadata(path: Path) -> dict:
    with PILImage.open(path) as image:
        image.verify()
        width, height = image.size
    with PILImage.open(path) as image:
        exif = image.getexif()
        tags = {ExifTags.TAGS.get(key, key): value for key, value in exif.items()}
        gps = tags.get("GPSInfo", {})
        if hasattr(gps, "items"):
            gps = {ExifTags.GPSTAGS.get(key, key): value for key, value in gps.items()}
        lat = _gps_value(gps.get("GPSLatitude"))
        lon = _gps_value(gps.get("GPSLongitude"))
        if lat is not None and gps.get("GPSLatitudeRef", "N") in ("S", b"S"):
            lat = -lat
        if lon is not None and gps.get("GPSLongitudeRef", "E") in ("W", b"W"):
            lon = -lon
        taken = tags.get("DateTimeOriginal") or tags.get("DateTime")
        try:
            taken = datetime.strptime(str(taken), "%Y:%m:%d %H:%M:%S") if taken else None
        except ValueError:
            taken = None
        return {
            "width": width,
            "height": height,
            "taken_at": taken,
            "gps_lat": lat,
            "gps_lon": lon,
            "camera_make": tags.get("Make"),
            "camera_model": tags.get("Model"),
        }


def _write_thumbnail(path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with PILImage.open(path) as image:
        image.thumbnail((512, 512))
        image.convert("RGB").save(destination, "JPEG", quality=88)


def _source(session, path: Path) -> Source:
    source = session.scalar(select(Source).where(Source.path == str(path)))
    if source is None:
        source = Source(path=str(path))
        session.add(source)
        session.flush()
    return source


def _remove_faces(session, image_id: int) -> None:
    session.execute(delete(Face).where(Face.image_id == image_id))


def _source_path(path: Path) -> Path:
    roots = watched_directories()
    matching = [root for root in roots if path == root or root in path.parents]
    return max(matching, key=lambda value: len(value.parts)) if matching else path.parent


def scan_watched_dirs(*, session_factory=SessionLocal, progress: Callable[[Path], None] | None = None) -> ScanReport:
    """Hash and index supported files, committing each file independently."""
    paths = iter_image_paths()
    report = ScanReport(discovered=len(paths))
    with session_factory() as session:
        for path in paths:
            content_hash = ""
            if progress:
                progress(path)
            try:
                content_hash = sha256_file(path)
                existing = session.scalar(select(Image).where(Image.path == str(path)))
                if existing is not None and existing.content_hash == content_hash:
                    report.unchanged += 1
                    continue
                source = _source(session, _source_path(path))
                if existing is None:
                    existing = Image(path=str(path), source=source, content_hash=content_hash)
                    session.add(existing)
                    session.flush()
                else:
                    existing.source = source
                    existing.content_hash = content_hash
                    _remove_faces(session, existing.id)
                metadata = _metadata(path)
                thumb = thumbnail_path_for(path, content_hash)
                _write_thumbnail(path, thumb)
                for key, value in metadata.items():
                    setattr(existing, key, value)
                existing.size_bytes = path.stat().st_size
                existing.modified_at = datetime.fromtimestamp(path.stat().st_mtime)
                existing.thumb_path = str(thumb)
                existing.face_count = 0
                existing.status = "indexed"
                existing.error = None
                session.commit()
                report.indexed += 1
            except Exception as exc:
                session.rollback()
                try:
                    source = _source(session, _source_path(path))
                    existing = session.scalar(select(Image).where(Image.path == str(path)))
                    if existing is None:
                        existing = Image(path=str(path), source=source, content_hash=content_hash)
                        session.add(existing)
                        session.flush()
                    else:
                        existing.content_hash = content_hash
                        _remove_faces(session, existing.id)
                    existing.status = "error"
                    existing.face_count = 0
                    existing.error = f"{type(exc).__name__}: {exc}"[:4000]
                    session.commit()
                except Exception:
                    session.rollback()
                report.errors += 1
    return report
