"""Analysis target selection and worker-response persistence."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from imagelib.db.models import Face, Image, Person
from imagelib.db.session import SessionLocal
from imagelib.diagnostics import diagnostic
from imagelib.services.analysis_clustering import _cluster_summary, rebuild_person_clusters
from imagelib.services.analysis_types import AnalysisTarget, WorkerAnalysisResult, WorkerBatchReport
from imagelib.services.scanner import sha256_file


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def select_analysis_targets(
    *,
    root: str | Path | None = None,
    image_ids: list[int] | None = None,
    paths: list[str | Path] | None = None,
    include_errors: bool = False,
    force: bool = False,
    session_factory=SessionLocal,
) -> list[AnalysisTarget]:
    """Select analysis work without loading or detecting any face.

    By default only ``indexed`` images are returned.  ``error`` images are
    returned when explicitly selected by ID/path (or with ``include_errors``),
    making failed work retryable without retrying every failed image.  An
    analysed image requires ``force=True``.  When ``root`` is supplied every
    result is guaranteed to be below that root.
    """
    selected_root = Path(root).expanduser().resolve() if root is not None else None
    explicit = image_ids is not None or paths is not None
    requested_ids = set(image_ids or [])
    requested_paths = set()
    for value in paths or []:
        path = Path(value).expanduser()
        if selected_root is not None and not path.is_absolute():
            path = selected_root / path
        requested_paths.add(str(path.resolve()))

    with session_factory() as session:
        rows = _select_analysis_targets(
            session,
            selected_root,
            requested_ids,
            requested_paths,
            explicit,
            include_errors,
            force,
        )
        return [
            AnalysisTarget(
                id=image.id,
                path=image.path,
                content_hash=image.content_hash,
                status=image.status,
            )
            for image in rows
        ]


def analysis_targets(**kwargs) -> list[AnalysisTarget]:
    """Alias for :func:`select_analysis_targets` used by coordinators."""
    return select_analysis_targets(**kwargs)


def _select_analysis_targets(
    session,
    selected_root: Path | None,
    requested_ids: set[int],
    requested_paths: set[str],
    explicit: bool,
    include_errors: bool,
    force: bool,
) -> list[Image]:
    candidates = list(session.scalars(select(Image).order_by(Image.id)))
    result = []
    for image in candidates:
        if selected_root is not None and not _path_is_under(Path(image.path), selected_root):
            continue
        if explicit and image.id not in requested_ids and str(Path(image.path).resolve()) not in requested_paths:
            continue
        if force:
            result.append(image)
        elif image.status == "indexed":
            result.append(image)
        elif explicit and include_errors and image.status == "error":
            result.append(image)
        elif explicit and image.status == "error" and (
            image.id in requested_ids or str(Path(image.path).resolve()) in requested_paths
        ):
            result.append(image)
    return result


def _face_values(item: Mapping[str, Any]) -> tuple[float, float, float, float, float | None, list[float]]:
    area = item.get("facial_area") or {}
    embedding = [float(value) for value in item["embedding"]]
    if len(embedding) != 512:
        raise ValueError(f"Facenet returned {len(embedding)} dimensions, expected 512")
    confidence = item.get("face_confidence", item.get("confidence"))
    return (
        float(area.get("x", 0)),
        float(area.get("y", 0)),
        float(area.get("w", 0)),
        float(area.get("h", 0)),
        float(confidence) if confidence is not None else None,
        embedding,
    )


def _worker_error_text(error: object) -> str:
    if isinstance(error, Exception):
        return f"{type(error).__name__}: {error}"[:4000]
    return str(error)[:4000]


def _delete_image_faces(session, image_id: int) -> None:
    face_ids = list(session.scalars(select(Face.id).where(Face.image_id == image_id)))
    if face_ids:
        session.execute(
            update(Person)
            .where(Person.cover_face_id.in_(face_ids))
            .values(cover_face_id=None)
        )
    session.execute(delete(Face).where(Face.image_id == image_id))


def _worker_result(
    image: Image,
    *,
    accepted: bool,
    status: str,
    face_count: int = 0,
    error: str | None = None,
) -> WorkerAnalysisResult:
    return WorkerAnalysisResult(
        image_id=image.id,
        accepted=accepted,
        status=status,
        face_count=face_count,
        content_hash=image.content_hash,
        error=error,
    )


def persist_worker_response(
    image_id: int,
    response: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    expected_content_hash: str | None = None,
    session_factory=SessionLocal,
) -> WorkerAnalysisResult:
    """Atomically persist one Qt-worker response without importing DeepFace.

    ``response`` may be the worker envelope (``{"ok": true, "faces": [...]}``)
    or the raw face dictionary sequence.  The selected content hash should be
    supplied from :class:`AnalysisTarget`; if it no longer matches the DB, or
    the current file hash, the response is rejected and no faces/status are
    changed.  Worker errors and malformed face payloads become retryable
    ``error`` image states.
    """
    with session_factory() as session:
        image = session.scalar(select(Image).where(Image.id == image_id))
        if image is None:
            return WorkerAnalysisResult(
                image_id=image_id,
                accepted=False,
                status="missing",
                face_count=0,
                error="Image no longer exists",
            )

        if expected_content_hash is not None:
            if image.content_hash != expected_content_hash:
                return _worker_result(
                    image,
                    accepted=False,
                    status="stale",
                    error="Image content hash changed before the worker response arrived",
                )
            try:
                current_hash = sha256_file(Path(image.path))
            except OSError as exc:
                return _worker_result(image, accepted=False, status="stale", error=_worker_error_text(exc))
            if current_hash != expected_content_hash:
                return _worker_result(
                    image,
                    accepted=False,
                    status="stale",
                    error="Image content hash changed before the worker response arrived",
                )

        worker_error = None
        if isinstance(response, Mapping):
            if response.get("ok") is False:
                worker_error = response.get("error", "Worker analysis failed")
            raw_faces = response.get("faces", [])
        else:
            raw_faces = response

        if worker_error is not None:
            values = None
            error_text = _worker_error_text(worker_error)
        else:
            try:
                if isinstance(raw_faces, (str, bytes)):
                    raise TypeError("Worker faces must be a sequence of dictionaries")
                values = [_face_values(item) for item in raw_faces]
                error_text = None
            except Exception as exc:
                values = None
                error_text = _worker_error_text(exc)

        _delete_image_faces(session, image.id)
        if values is None:
            image.face_count = 0
            image.status = "error"
            image.error = error_text
            session.commit()
            return _worker_result(
                image,
                accepted=True,
                status="error",
                error=error_text,
            )

        valid_values = [value for value in values if value[2] > 0 and value[3] > 0]
        for x, y, width, height, confidence, embedding in valid_values:
            session.add(
                Face(
                    image_id=image.id,
                    x=x,
                    y=y,
                    w=width,
                    h=height,
                    confidence=confidence,
                    embedding=embedding,
                )
            )
        image.face_count = len(valid_values)
        image.status = "analysed"
        image.error = None
        session.commit()
        return _worker_result(
            image,
            accepted=True,
            status="analysed",
            face_count=len(valid_values),
        )


def persist_worker_faces(
    image_id: int,
    faces: Sequence[Mapping[str, Any]],
    *,
    expected_content_hash: str | None = None,
    session_factory=SessionLocal,
) -> WorkerAnalysisResult:
    """Persist raw face dictionaries; convenience wrapper around the envelope API."""
    return persist_worker_response(
        image_id,
        faces,
        expected_content_hash=expected_content_hash,
        session_factory=session_factory,
    )


def _mark_error(session, image: Image, exc: Exception) -> None:
    session.rollback()
    current = session.scalar(select(Image).where(Image.id == image.id))
    if current is not None:
        _delete_image_faces(session, current.id)
        current.face_count = 0
        current.status = "error"
        current.error = f"{type(exc).__name__}: {exc}"[:4000]
        session.commit()


def persist_worker_batch(
    responses: Iterable[
        tuple[AnalysisTarget, Mapping[str, Any] | Sequence[Mapping[str, Any]]]
    ],
    *,
    session_factory=SessionLocal,
    progress: Callable[[WorkerAnalysisResult], None] | None = None,
) -> WorkerBatchReport:
    """Persist worker responses and rebuild clusters once after the batch.

    Each response is hash-checked against its selected target.  A stale or
    failed response is reported in-place and does not prevent other responses
    from being persisted or clustered.
    """
    diagnostic("Analysis persistence service started")
    target_list = []
    result_list = []
    for target, response in responses:
        target_list.append(target)
        response_status = (
            response.get("status", "ok" if response.get("ok") else "error")
            if isinstance(response, Mapping)
            else "faces"
        )
        diagnostic(
            f"Analysis persistence image start image_id={target.id} path={target.path!r} "
            f"status={target.status!r} content_hash={target.content_hash!r} "
            f"response_status={response_status!r}"
        )
        if isinstance(response, Mapping) and response.get("ok") is False:
            diagnostic(
                f"Analysis persistence response error image_id={target.id} "
                f"error={response.get('error', 'Worker analysis failed')!r}"
            )
        result = persist_worker_response(
            target.id,
            response,
            expected_content_hash=target.content_hash,
            session_factory=session_factory,
        )
        result_list.append(result)
        diagnostic(
            f"Analysis persistence image completed image_id={result.image_id} "
            f"status={result.status!r} accepted={result.accepted} "
            f"faces={result.face_count} error={result.error!r}"
        )
        if progress:
            progress(result)
    if any(result.accepted for result in result_list):
        clusters = rebuild_person_clusters(session_factory=session_factory)
    else:
        with session_factory() as session:
            clusters = _cluster_summary(session)
    report = WorkerBatchReport(tuple(target_list), tuple(result_list), clusters)
    diagnostic(
        f"Analysis persistence service completed targets={len(report.targets)} "
        f"results={len(report.results)} clusters={report.clusters!r}"
    )
    return report
