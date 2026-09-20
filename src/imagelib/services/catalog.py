"""Read-model helpers: filter/sort/paginate the image catalog."""

from __future__ import annotations


def filter_images(*, persons=None, locations=None, date_from=None, date_to=None, face_count=None):
    """Query images with the given filters.

    Stub: returns nothing. Filtering falls out of the schema:
    - persons: join faces.person_id in (...)
    - locations: gps_place in (...) or gps_lat IS NOT NULL
    - dates: taken_at BETWEEN date_from AND date_to
    - faces: face_count > 0
    """
    # TODO(step: catalog): SQLAlchemy 2 select() with optional predicates.
    raise NotImplementedError