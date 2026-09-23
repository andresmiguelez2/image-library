"""Synchronous read-model queries for browser, calendar, and detail views."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable

from sqlalchemy import or_, select

from imagelib.config import active_root
from imagelib.db.models import IMAGE_STATUSES, Face, Image, Person
from imagelib.db.session import SessionLocal


@dataclass(frozen=True)
class ImageListItem:
    """Small image row suitable for a worker-to-UI result."""

    id: int
    path: str
    relative_path: str
    breadcrumbs: tuple[str, ...]
    thumb_path: str | None
    taken_at: datetime | None
    modified_at: datetime | None
    width: int | None
    height: int | None
    face_count: int
    status: str


@dataclass(frozen=True)
class FaceDetail:
    id: int
    x: float
    y: float
    w: float
    h: float
    confidence: float | None
    person_id: int | None
    person_name: str | None


@dataclass(frozen=True)
class ImageDetail:
    id: int
    path: str
    relative_path: str
    breadcrumbs: tuple[str, ...]
    thumb_path: str | None
    taken_at: datetime | None
    modified_at: datetime | None
    width: int | None
    height: int | None
    size_bytes: int | None
    gps_lat: float | None
    gps_lon: float | None
    gps_place: str | None
    camera_make: str | None
    camera_model: str | None
    status: str
    error: str | None
    faces: tuple[FaceDetail, ...]


def _root(root: str | Path | None) -> Path:
    return active_root(root)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _relative(path: str, root: Path) -> tuple[str, tuple[str, ...]]:
    relative = Path(path).resolve().relative_to(root.resolve())
    parts = relative.parts
    return relative.as_posix(), parts


def _list_item(image: Image, root: Path) -> ImageListItem:
    relative_path, breadcrumbs = _relative(image.path, root)
    return ImageListItem(
        id=image.id,
        path=image.path,
        relative_path=relative_path,
        breadcrumbs=breadcrumbs,
        thumb_path=image.thumb_path,
        taken_at=image.taken_at,
        modified_at=image.modified_at,
        width=image.width,
        height=image.height,
        face_count=image.face_count,
        status=image.status,
    )


def _images_in_root(session, root: Path) -> list[Image]:
    return [
        image
        for image in session.scalars(select(Image).order_by(Image.path))
        if _under(Path(image.path), root)
    ]


def filter_images(
    *,
    persons: Iterable[int | str] | None = None,
    locations: Iterable[str] | None = None,
    date_from: datetime | date | None = None,
    date_to: datetime | date | None = None,
    face_count: int | bool | None = None,
    root: str | Path | None = None,
    session_factory=SessionLocal,
) -> list[Image]:
    """Return ORM image rows matching common catalog filters.

    ``root`` scopes results to the active root.  The returned rows are fully
    scalar-loaded and can be converted with :func:`list_images` for UI use.
    ``persons`` accepts person IDs and/or names; ``locations`` matches
    ``gps_place``.  A true ``face_count`` asks for images containing faces.
    """
    selected_root = _root(root)
    with session_factory() as session:
        images = _images_in_root(session, selected_root)
        person_values = set(persons or [])
        location_values = set(locations or [])
        person_ids = {value for value in person_values if isinstance(value, int)}
        person_names = {value for value in person_values if isinstance(value, str)}
        result = []
        lower_bound = (
            datetime.combine(date_from, time.min) if isinstance(date_from, date) and not isinstance(date_from, datetime) else date_from
        )
        upper_bound = (
            datetime.combine(date_to, time.max) if isinstance(date_to, date) and not isinstance(date_to, datetime) else date_to
        )
        for image in images:
            if lower_bound is not None and (image.taken_at is None or image.taken_at < lower_bound):
                continue
            if upper_bound is not None and (image.taken_at is None or image.taken_at > upper_bound):
                continue
            if face_count is True and image.face_count <= 0:
                continue
            if isinstance(face_count, int) and not isinstance(face_count, bool) and image.face_count != face_count:
                continue
            if location_values and image.gps_place not in location_values:
                continue
            if person_values:
                matching = session.scalars(
                    select(Face)
                    .join(Person, isouter=True)
                    .where(
                        Face.image_id == image.id,
                        or_(
                            Face.person_id.in_(person_ids) if person_ids else False,
                            Person.name.in_(person_names) if person_names else False,
                        ),
                    )
                ).first()
                if matching is None:
                    continue
            result.append(image)
        return result


def list_images(
    *,
    root: str | Path | None = None,
    directory: str | Path | None = None,
    persons: Iterable[int | str] | None = None,
    locations: Iterable[str] | None = None,
    date_from: datetime | date | None = None,
    date_to: datetime | date | None = None,
    face_count: int | bool | None = None,
    offset: int = 0,
    limit: int | None = None,
    session_factory=SessionLocal,
) -> list[ImageListItem]:
    """List browser rows with root-relative paths and breadcrumb parts."""
    selected_root = _root(root)
    rows = filter_images(
        persons=persons,
        locations=locations,
        date_from=date_from,
        date_to=date_to,
        face_count=face_count,
        root=selected_root,
        session_factory=session_factory,
    )
    if directory is not None:
        selected_directory = Path(directory)
        if selected_directory.is_absolute():
            try:
                selected_directory = selected_directory.resolve().relative_to(selected_root)
            except ValueError:
                return []
        directory_text = selected_directory.as_posix().strip(".").strip("/")
        rows = [
            row
            for row in rows
            if not directory_text
            or Path(row.path).resolve().relative_to(selected_root).parent.as_posix()
            == directory_text
        ]
    rows = rows[offset: (offset + limit) if limit is not None else None]
    return [_list_item(row, selected_root) for row in rows]


def browser_images(**kwargs) -> list[ImageListItem]:
    """Alias used by browser coordinators."""
    return list_images(**kwargs)


def list_browser_images(**kwargs) -> list[ImageListItem]:
    """Alias for :func:`list_images`."""
    return list_images(**kwargs)


def calendar_groups(
    *, root: str | Path | None = None, session_factory=SessionLocal
) -> dict[date | None, list[ImageListItem]]:
    """Group images by capture date, falling back to file modification date."""
    selected_root = _root(root)
    with session_factory() as session:
        groups: dict[date | None, list[ImageListItem]] = {}
        for image in _images_in_root(session, selected_root):
            timestamp = image.taken_at or image.modified_at
            group = timestamp.date() if timestamp is not None else None
            groups.setdefault(group, []).append(_list_item(image, selected_root))
        return dict(sorted(groups.items(), key=lambda item: (item[0] is None, item[0] or date.min)))


def group_images_by_date(**kwargs) -> dict[date | None, list[ImageListItem]]:
    """Alias for :func:`calendar_groups`."""
    return calendar_groups(**kwargs)


def status_counts(*, root: str | Path | None = None, session_factory=SessionLocal) -> dict[str, int]:
    """Return counts for every image status below the active root."""
    selected_root = _root(root)
    with session_factory() as session:
        counts = Counter(image.status for image in _images_in_root(session, selected_root))
    return {status: counts.get(status, 0) for status in IMAGE_STATUSES}


def active_root_status_counts(**kwargs) -> dict[str, int]:
    """Alias for the status panel query."""
    return status_counts(**kwargs)


def get_status_counts(**kwargs) -> dict[str, int]:
    """Alias for :func:`status_counts`."""
    return status_counts(**kwargs)


def get_image_detail(
    image_id: int | None = None,
    *,
    path: str | Path | None = None,
    root: str | Path | None = None,
    session_factory=SessionLocal,
) -> ImageDetail | None:
    """Return image metadata and faces with their current person names."""
    if image_id is None and path is None:
        raise ValueError("image_id or path is required")
    selected_root = _root(root)
    with session_factory() as session:
        statement = select(Image)
        if image_id is not None:
            statement = statement.where(Image.id == image_id)
        else:
            requested_path = Path(path).expanduser()
            if not requested_path.is_absolute():
                requested_path = selected_root / requested_path
            statement = statement.where(Image.path == str(requested_path.resolve()))
        image = session.scalar(statement)
        if image is None or not _under(Path(image.path), selected_root):
            return None
        faces = session.execute(
            select(Face, Person.name)
            .join(Person, Face.person_id == Person.id, isouter=True)
            .where(Face.image_id == image.id)
            .order_by(Face.id)
        ).all()
        relative_path, breadcrumbs = _relative(image.path, selected_root)
        return ImageDetail(
            id=image.id,
            path=image.path,
            relative_path=relative_path,
            breadcrumbs=breadcrumbs,
            thumb_path=image.thumb_path,
            taken_at=image.taken_at,
            modified_at=image.modified_at,
            width=image.width,
            height=image.height,
            size_bytes=image.size_bytes,
            gps_lat=image.gps_lat,
            gps_lon=image.gps_lon,
            gps_place=image.gps_place,
            camera_make=image.camera_make,
            camera_model=image.camera_model,
            status=image.status,
            error=image.error,
            faces=tuple(
                FaceDetail(
                    id=face.id,
                    x=face.x,
                    y=face.y,
                    w=face.w,
                    h=face.h,
                    confidence=face.confidence,
                    person_id=face.person_id,
                    person_name=person_name,
                )
                for face, person_name in faces
            ),
        )


def image_detail(*args, **kwargs) -> ImageDetail | None:
    """Alias for :func:`get_image_detail`."""
    return get_image_detail(*args, **kwargs)
