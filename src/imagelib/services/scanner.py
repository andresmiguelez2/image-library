"""Stage-one image ingest and safe root reconciliation.

The scanner only changes catalog data and cached thumbnails.  Original files
are opened read-only and are never used as a deletion target.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from PIL import ExifTags
from PIL import Image as PILImage
from sqlalchemy import delete, select, update

from imagelib.config import active_root, thumbnail_directory, watched_directories
from imagelib.db.maintenance import reconcile_persons
from imagelib.db.models import Face, FaceMatchDecision, Image, ImageLocation, Person, Source
from imagelib.db.session import SessionLocal

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg"}


@dataclass
class ScanReport:
    """Counters and completion state for one root scan."""

    root: Path | None = None
    discovered: int = 0
    indexed: int = 0
    unchanged: int = 0
    errors: int = 0
    removed: int = 0
    cancelled: bool = False
    complete: bool = True
    reconciled: bool = False
    error: str | None = None


class InvalidRootError(ValueError):
    """Raised when a requested root is not an existing readable directory."""


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _discover_paths(root: Path) -> tuple[list[Path], bool]:
    complete = True
    paths: set[Path] = set()

    def onerror(_error: OSError) -> None:
        nonlocal complete
        complete = False

    for directory, _subdirectories, filenames in os.walk(root, onerror=onerror):
        for filename in filenames:
            path = Path(directory) / filename
            try:
                if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                    resolved = path.resolve()
                    if _under(resolved, root):
                        paths.add(resolved)
            except OSError:
                complete = False
    return sorted(paths), complete


def iter_image_paths(directories: Iterable[Path] | None = None) -> list[Path]:
    """Return supported regular image files below the supplied directories."""
    paths: set[Path] = set()
    for directory in directories if directories is not None else watched_directories():
        directory = Path(directory).expanduser().resolve()
        if directory.is_dir():
            found, _complete = _discover_paths(directory)
            paths.update(found)
    return sorted(paths)


def sha256_file(path: Path) -> str:
    """Hash a file in chunks, without changing it."""
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
    face_ids = list(session.scalars(select(Face.id).where(Face.image_id == image_id)))
    if face_ids:
        session.execute(
            update(Person)
            .where(Person.cover_face_id.in_(face_ids))
            .values(cover_face_id=None)
        )
        session.execute(delete(FaceMatchDecision).where(FaceMatchDecision.face_id.in_(face_ids)))
    session.execute(delete(Face).where(Face.image_id == image_id))


def _source_path(path: Path) -> Path:
    """Return the deepest configured watched root containing ``path``."""
    roots = watched_directories()
    matching = [root for root in roots if _under(path, root)]
    return max(matching, key=lambda value: len(value.parts)) if matching else path.parent


def _cancelled(cancel: Callable[[], bool] | object | None) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    is_set = getattr(cancel, "is_set", None)
    return bool(is_set()) if is_set is not None else False


def _safe_remove_thumbnail(path: str | None, protected_roots: Iterable[Path] = ()) -> None:
    if not path:
        return
    candidate = Path(path).expanduser()
    cache = thumbnail_directory().resolve()
    try:
        candidate.resolve(strict=False).relative_to(cache)
    except ValueError:
        return
    if any(_under(candidate, root) for root in protected_roots):
        return
    if candidate.is_symlink() or not candidate.is_file():
        return
    try:
        candidate.unlink()
    except OSError:
        pass


def _reconcile(session, root: Path, found: set[str]) -> tuple[int, list[str]]:
    stale_locations = []
    for location in session.scalars(select(ImageLocation)):
        path = Path(location.path)
        if _under(path, root) and str(path.resolve()) not in found:
            stale_locations.append(location)
    thumbnails: list[str] = []
    affected_image_ids = {location.image_id for location in stale_locations}
    for location in stale_locations:
        session.delete(location)
    session.flush()

    removed_images = []
    for image_id in affected_image_ids:
        image = session.get(Image, image_id)
        if image is None or session.scalar(
            select(ImageLocation.id).where(ImageLocation.image_id == image_id).limit(1)
        ) is not None:
            continue
        if image.thumb_path:
            thumbnails.append(image.thumb_path)
        _remove_faces(session, image.id)
        session.delete(image)
        removed_images.append(image)
    session.flush()
    if removed_images:
        reconcile_persons(session)
    session.commit()
    for thumbnail in set(thumbnails):
        still_used = session.scalar(select(Image.id).where(Image.thumb_path == thumbnail))
        if still_used is None:
            _safe_remove_thumbnail(thumbnail, [root, *watched_directories()])
    return len(removed_images), thumbnails


def scan_root(
    root: str | Path | None = None,
    *,
    session_factory=SessionLocal,
    progress: Callable[[Path], None] | None = None,
    cancel: Callable[[], bool] | object | None = None,
    _paths: list[Path] | None = None,
    _discovery_complete: bool | None = None,
) -> ScanReport:
    """Index one root and reconcile missing rows only after a complete scan.

    ``progress`` is called with each discovered path.  ``cancel`` may be a
    callable or an event-like object with ``is_set``.  Cancellation, discovery
    errors, file errors, and invalid roots never reconcile database rows.
    """
    selected_root = active_root(root)
    report = ScanReport(root=selected_root)
    if not selected_root.is_dir() or not os.access(selected_root, os.R_OK | os.X_OK):
        report.complete = False
        report.error = f"Unreadable or invalid root: {selected_root}"
        return report

    if _paths is None:
        paths, discovery_complete = _discover_paths(selected_root)
    else:
        paths, discovery_complete = sorted(_paths), (
            True if _discovery_complete is None else _discovery_complete
        )
    report.discovered = len(paths)
    found = {str(path.resolve()) for path in paths}
    with session_factory() as session:
        source = _source(session, selected_root)
        session.commit()
        removed_image_ids: set[int] = set()
        removed_thumbnails: set[str] = set()
        for path in paths:
            if _cancelled(cancel):
                report.cancelled = True
                report.complete = False
                break
            if progress:
                progress(path)
            content_hash = ""
            try:
                content_hash = sha256_file(path)
                path_string = str(path.resolve())
                location = session.scalar(
                    select(ImageLocation).where(ImageLocation.path == path_string)
                )
                previous_image_id = location.image_id if location is not None else None
                if (
                    location is not None
                    and location.image.content_hash == content_hash
                ):
                    location.source = source
                    report.unchanged += 1
                    session.commit()
                    continue

                image = session.scalar(
                    select(Image).where(Image.content_hash == content_hash)
                )
                is_new_image = image is None
                if image is None:
                    metadata = _metadata(path)
                    thumb = thumbnail_path_for(path, content_hash)
                    _write_thumbnail(path, thumb)
                    image = Image(
                        content_hash=content_hash,
                        size_bytes=path.stat().st_size,
                        modified_at=datetime.fromtimestamp(path.stat().st_mtime),
                        thumb_path=str(thumb),
                        status="indexed",
                    )
                    for key, value in metadata.items():
                        setattr(image, key, value)
                    session.add(image)
                    session.flush()

                if location is None:
                    location = ImageLocation(
                        image_id=image.id,
                        source=source,
                        path=path_string,
                    )
                    session.add(location)
                else:
                    location.image = image
                    location.source = source

                session.flush()
                if previous_image_id is not None and previous_image_id != image.id:
                    previous_image = session.get(Image, previous_image_id)
                    if previous_image is not None and session.scalar(
                        select(ImageLocation.id)
                        .where(ImageLocation.image_id == previous_image_id)
                        .limit(1)
                    ) is None:
                        if previous_image.thumb_path:
                            removed_thumbnails.add(previous_image.thumb_path)
                        _remove_faces(session, previous_image.id)
                        session.delete(previous_image)
                        removed_image_ids.add(previous_image_id)
                session.commit()
                if is_new_image:
                    report.indexed += 1
                else:
                    report.unchanged += 1
            except Exception as exc:
                session.rollback()
                if content_hash:
                    try:
                        source = _source(session, selected_root)
                        path_string = str(path.resolve())
                        location = session.scalar(
                            select(ImageLocation).where(ImageLocation.path == path_string)
                        )
                        previous_image_id = location.image_id if location is not None else None
                        image = session.scalar(
                            select(Image).where(Image.content_hash == content_hash)
                        )
                        if image is None:
                            image = Image(
                                content_hash=content_hash,
                                size_bytes=path.stat().st_size,
                                modified_at=datetime.fromtimestamp(path.stat().st_mtime),
                                status="error",
                                error=f"{type(exc).__name__}: {exc}"[:4000],
                            )
                            session.add(image)
                            session.flush()
                        if location is None:
                            session.add(
                                ImageLocation(
                                    image_id=image.id,
                                    source=source,
                                    path=path_string,
                                )
                            )
                        else:
                            location.image = image
                            location.source = source
                        session.flush()
                        if previous_image_id is not None and previous_image_id != image.id:
                            previous_image = session.get(Image, previous_image_id)
                            if previous_image is not None and session.scalar(
                                select(ImageLocation.id)
                                .where(ImageLocation.image_id == previous_image_id)
                                .limit(1)
                            ) is None:
                                if previous_image.thumb_path:
                                    removed_thumbnails.add(previous_image.thumb_path)
                                _remove_faces(session, previous_image.id)
                                session.delete(previous_image)
                                removed_image_ids.add(previous_image_id)
                        session.commit()
                    except Exception:
                        session.rollback()
                report.errors += 1

        if removed_image_ids:
            reconcile_persons(session)
            session.commit()
        for thumbnail in removed_thumbnails:
            still_used = session.scalar(select(Image.id).where(Image.thumb_path == thumbnail))
            if still_used is None:
                _safe_remove_thumbnail(thumbnail, [selected_root, *watched_directories()])
        if _cancelled(cancel):
            report.cancelled = True
        report.complete = discovery_complete and not report.cancelled and report.errors == 0
        if report.complete:
            report.removed, _thumbnails = _reconcile(session, selected_root, found)
            report.reconciled = True
    return report


def _merge_reports(reports: list[ScanReport]) -> ScanReport:
    result = ScanReport()
    result.discovered = sum(report.discovered for report in reports)
    result.indexed = sum(report.indexed for report in reports)
    result.unchanged = sum(report.unchanged for report in reports)
    result.errors = sum(report.errors for report in reports)
    result.removed = sum(report.removed for report in reports)
    result.cancelled = any(report.cancelled for report in reports)
    result.complete = bool(reports) and all(report.complete for report in reports)
    result.reconciled = bool(reports) and all(report.reconciled for report in reports)
    result.error = next((report.error for report in reports if report.error), None)
    return result


def scan_watched_dirs(
    *,
    session_factory=SessionLocal,
    progress: Callable[[Path], None] | None = None,
    cancel: Callable[[], bool] | object | None = None,
) -> ScanReport:
    """Backward-compatible scan of every configured watched directory."""
    paths = iter_image_paths()
    reports = []
    for root in watched_directories():
        root_paths = [path for path in paths if _under(path, root)]
        if not root.is_dir():
            reports.append(scan_root(root, session_factory=session_factory, progress=progress, cancel=cancel))
            continue
        reports.append(
            scan_root(
                root,
                session_factory=session_factory,
                progress=progress,
                cancel=cancel,
                _paths=root_paths,
                _discovery_complete=_discover_paths(root)[1],
            )
        )
    return _merge_reports(reports)
