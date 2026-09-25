"""Stage-two face detection and embedding analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence

from sklearn.cluster import DBSCAN
from sqlalchemy import delete, select, update

from imagelib.config import config
from imagelib.db.models import Face, Image, Person
from imagelib.db.session import SessionLocal
from imagelib.services.scanner import sha256_file

_model = None
_model_lock = Lock()


@dataclass
class AnalysisReport:
    discovered: int = 0
    analysed: int = 0
    faces: int = 0
    errors: int = 0
    results: tuple["WorkerAnalysisResult", ...] = ()


@dataclass(frozen=True)
class AnalysisTarget:
    """Stable data passed from catalog selection to a worker coordinator."""

    id: int
    path: str
    content_hash: str
    status: str


@dataclass(frozen=True)
class WorkerAnalysisResult:
    """Outcome of persisting one response from the long-lived worker."""

    image_id: int
    accepted: bool
    status: str
    face_count: int
    content_hash: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ClusterReport:
    """Summary returned after rebuilding all person clusters."""

    faces: int
    clustered_faces: int
    persons: int
    named_persons: int


@dataclass(frozen=True)
class WorkerBatchReport:
    """Typed result for a coordinator batch followed by reclustering."""

    targets: tuple[AnalysisTarget, ...]
    results: tuple[WorkerAnalysisResult, ...]
    clusters: ClusterReport


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


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                from deepface import DeepFace

                _model = DeepFace.build_model("Facenet512")
    return _model


def _represent(path: Path, model) -> list[dict]:
    from deepface import DeepFace

    result = DeepFace.represent(
        img_path=str(path),
        model_name="Facenet512",
        detector_backend=config["analysis"].get("detector_backend", "retinaface"),
        enforce_detection=False,
    )
    return result if isinstance(result, list) else [result]


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

        session.execute(delete(Face).where(Face.image_id == image.id))
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
        session.execute(delete(Face).where(Face.image_id == current.id))
        current.face_count = 0
        current.status = "error"
        current.error = f"{type(exc).__name__}: {exc}"[:4000]
        session.commit()


def _cluster_summary(session) -> ClusterReport:
    faces = list(session.scalars(select(Face)))
    persons = list(session.scalars(select(Person)))
    return ClusterReport(
        faces=len(faces),
        clustered_faces=sum(face.person_id is not None for face in faces),
        persons=len(persons),
        named_persons=sum(person.name is not None for person in persons),
    )


def _rebuild_person_clusters(session) -> ClusterReport:
    """Rebuild clusters while retaining named persons as stable identities."""
    named_persons = list(session.scalars(select(Person).where(Person.name.is_not(None))))
    faces = list(
        session.scalars(
            select(Face).where(Face.embedding.is_not(None)).order_by(Face.id)
        )
    )

    session.execute(update(Face).values(person_id=None))
    session.execute(update(Person).values(cover_face_id=None))
    session.execute(delete(Person).where(Person.name.is_(None)))

    if not faces:
        session.commit()
        return _cluster_summary(session)

    embeddings = [face.embedding for face in faces]
    analysis_config = config.get("analysis", {})
    labels = DBSCAN(
        eps=float(analysis_config.get("cluster_eps", 0.6)),
        min_samples=int(analysis_config.get("cluster_min_samples", 2)),
        metric="cosine",
    ).fit_predict(embeddings)

    available_named = list(named_persons)
    threshold = float(config.get("analysis", {}).get("similarity_threshold", 0.6))

    def cosine(left, right) -> float:
        left_norm = sum(float(value) * float(value) for value in left) ** 0.5
        right_norm = sum(float(value) * float(value) for value in right) ** 0.5
        if not left_norm or not right_norm:
            return 0.0
        return sum(float(a) * float(b) for a, b in zip(left, right)) / (left_norm * right_norm)

    for label in sorted(set(labels)):
        if label == -1:
            continue
        cluster_faces = [face for face, face_label in zip(faces, labels) if face_label == label]
        centroid = [
            sum(float(face.embedding[index]) for face in cluster_faces) / len(cluster_faces)
            for index in range(512)
        ]
        person = None
        best = None
        for candidate in available_named:
            if candidate.embedding is None:
                continue
            similarity = cosine(candidate.embedding, centroid)
            if best is None or similarity > best[0]:
                best = (similarity, candidate)
        if best is not None and best[0] >= threshold:
            person = best[1]
            available_named.remove(person)
            person.embedding = centroid
            person.cover_face_id = cluster_faces[0].id
        else:
            person = Person(embedding=centroid, cover_face_id=cluster_faces[0].id)
            session.add(person)
            session.flush()
        for face in cluster_faces:
            face.person_id = person.id
    session.commit()
    return _cluster_summary(session)


def rebuild_person_clusters(*, session_factory=SessionLocal) -> ClusterReport:
    """Rebuild clusters after a worker batch, retaining named persons."""
    with session_factory() as session:
        return _rebuild_person_clusters(session)


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
    target_list = []
    result_list = []
    for target, response in responses:
        target_list.append(target)
        result = persist_worker_response(
            target.id,
            response,
            expected_content_hash=target.content_hash,
            session_factory=session_factory,
        )
        result_list.append(result)
        if progress:
            progress(result)
    if any(result.accepted for result in result_list):
        clusters = rebuild_person_clusters(session_factory=session_factory)
    else:
        with session_factory() as session:
            clusters = _cluster_summary(session)
    return WorkerBatchReport(tuple(target_list), tuple(result_list), clusters)


def analyse_images(
    *,
    root: str | Path | None = None,
    image_ids: list[int] | None = None,
    paths: list[str | Path] | None = None,
    include_errors: bool = False,
    force: bool = False,
    session_factory=SessionLocal,
    progress: Callable[[Image], None] | None = None,
) -> AnalysisReport:
    """Analyse selected images once, reusing one Facenet model per process.

    Pass ``root`` with no IDs or paths for analysis-all in that root.  Pass
    explicit IDs or paths to retry errors.  The function is synchronous so the
    UI can run it in a worker thread and receive progress callbacks.
    """
    report = AnalysisReport()
    results: list[WorkerAnalysisResult] = []
    with session_factory() as session:
        selected_root = Path(root).expanduser().resolve() if root is not None else None
        requested_ids = set(image_ids or [])
        requested_paths = set()
        for value in paths or []:
            path = Path(value).expanduser()
            if selected_root is not None and not path.is_absolute():
                path = selected_root / path
            requested_paths.add(str(path.resolve()))
        targets = _select_analysis_targets(
            session,
            selected_root,
            requested_ids,
            requested_paths,
            image_ids is not None or paths is not None,
            include_errors,
            force,
        )
        report.discovered = len(targets)
        model = None
        model_error = None
        try:
            if targets:
                model = _get_model()
        except Exception as exc:
            model_error = exc
        for image in targets:
            if progress:
                progress(image)
            try:
                if model_error is not None:
                    raise model_error
                values = [_face_values(item) for item in _represent(Path(image.path), model)]
                session.execute(delete(Face).where(Face.image_id == image.id))
                for x, y, width, height, confidence, embedding in values:
                    if width <= 0 or height <= 0:
                        continue
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
                image.face_count = sum(1 for value in values if value[2] > 0 and value[3] > 0)
                image.status = "analysed"
                image.error = None
                session.commit()
                report.analysed += 1
                report.faces += image.face_count
                results.append(
                    _worker_result(
                        image,
                        accepted=True,
                        status="analysed",
                        face_count=image.face_count,
                    )
                )
            except Exception as exc:
                _mark_error(session, image, exc)
                report.errors += 1
                results.append(
                    _worker_result(
                        image,
                        accepted=True,
                        status="error",
                        error=_worker_error_text(exc),
                    )
                )
        if targets:
            _rebuild_person_clusters(session)
    report.results = tuple(results)
    return report


def analyse_all(
    root: str | Path,
    *,
    session_factory=SessionLocal,
    progress: Callable[[Image], None] | None = None,
) -> AnalysisReport:
    """Analyse eligible images below one root, skipping analysed/error rows."""
    return analyse_images(root=root, session_factory=session_factory, progress=progress)


def analysis_all(*args, **kwargs) -> AnalysisReport:
    """US-spelling alias for :func:`analyse_all` for coordinator code."""
    return analyse_all(*args, **kwargs)
