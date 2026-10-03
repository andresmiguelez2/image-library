"""Stage-two face detection and embedding analysis."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Mapping, Sequence

from sklearn.cluster import DBSCAN
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from imagelib.config import active_root, config
from imagelib.db.models import Face, FaceMatchDecision, Image, Person
from imagelib.db.session import SessionLocal
from imagelib.diagnostics import diagnostic
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


@dataclass(frozen=True)
class GroupLabelResult:
    """Named identity and face count after resolving one current face group."""

    person_id: int
    person_name: str
    face_count: int


@dataclass(frozen=True)
class FaceMatchResolutionResult:
    """Result of accepting or rejecting one group-to-person proposal."""

    face_id: int
    target_person_id: int
    status: str
    group_face_count: int


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
        img_path=BytesIO(path.read_bytes()),
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


def _cluster_summary(session) -> ClusterReport:
    faces = list(session.scalars(select(Face)))
    persons = list(session.scalars(select(Person)))
    return ClusterReport(
        faces=len(faces),
        clustered_faces=sum(face.person_id is not None for face in faces),
        persons=len(persons),
        named_persons=sum(person.name is not None for person in persons),
    )


def _cosine_similarity(left, right) -> float:
    left_norm = sum(float(value) * float(value) for value in left) ** 0.5
    right_norm = sum(float(value) * float(value) for value in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return sum(float(a) * float(b) for a, b in zip(left, right)) / (left_norm * right_norm)


def _centroid(faces: Sequence[Face]) -> list[float]:
    dimensions = len(faces[0].embedding)
    return [
        sum(float(face.embedding[index]) for face in faces) / len(faces)
        for index in range(dimensions)
    ]


def _group_for_face(session, face_id: int, root: str | Path | None) -> tuple[Face, Person, list[Face]]:
    face = session.scalar(select(Face).where(Face.id == face_id))
    if face is None:
        raise ValueError(f"Face {face_id} no longer exists")
    image = session.get(Image, face.image_id)
    if image is None:
        raise ValueError(f"Image for face {face_id} no longer exists")
    selected_root = active_root(root)
    try:
        Path(image.path).resolve().relative_to(selected_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Face {face_id} is outside the selected image root") from exc
    if face.person_id is None:
        raise ValueError(f"Face {face_id} is ungrouped DBSCAN noise and cannot be labelled")
    person = session.scalar(
        select(Person)
        .where(Person.id == face.person_id)
        .execution_options(populate_existing=True)
    )
    if person is None:
        raise ValueError(f"Face {face_id} no longer belongs to an available group")
    group = list(
        session.scalars(
            select(Face)
            .where(Face.person_id == person.id)
            .order_by(Face.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    selected_face = next((member for member in group if member.id == face_id), None)
    if selected_face is None:
        raise ValueError(f"Face group containing {face_id} changed concurrently")
    face = selected_face
    person = session.scalar(
        select(Person)
        .where(Person.id == person.id)
        .execution_options(populate_existing=True)
    )
    if person is None:
        raise ValueError(f"Face {face_id} no longer belongs to an available group")
    if person.name is not None:
        return face, person, group
    if not group or any(member.embedding is None for member in group):
        raise ValueError(f"Face {face_id} does not belong to a labelable analyser group")
    return face, person, group


def _decisions_for_group(session, group: Sequence[Face]) -> dict[tuple[int, int], FaceMatchDecision]:
    face_ids = [face.id for face in group]
    if not face_ids:
        return {}
    decisions = session.scalars(
        select(FaceMatchDecision)
        .where(FaceMatchDecision.face_id.in_(face_ids))
        .with_for_update()
    )
    return {(decision.face_id, decision.target_person_id): decision for decision in decisions}


def _assign_group_to_person(
    session,
    group: Sequence[Face],
    source_person: Person,
    target: Person,
) -> GroupLabelResult:
    if target.name is None:
        raise ValueError("The target person must have a name")
    decisions = _decisions_for_group(session, group)
    rejected = [
        decision
        for decision in decisions.values()
        if decision.target_person_id == target.id and decision.status == "rejected"
    ]
    if rejected:
        raise ValueError(
            f"This face group was rejected as {target.name!r}; it cannot be assigned to that identity"
        )
    other_accepted = [
        decision
        for decision in decisions.values()
        if decision.status == "accepted" and decision.target_person_id != target.id
    ]
    if other_accepted:
        raise ValueError("This face group is already accepted as a different named person")

    existing_faces = list(
        session.scalars(
            select(Face)
            .where(Face.person_id == target.id, Face.embedding.is_not(None))
            .order_by(Face.id)
        )
    )
    combined_faces = existing_faces + list(group)
    centroid = _centroid(combined_faces)
    for face in group:
        decision = decisions.get((face.id, target.id))
        if decision is None:
            session.add(
                FaceMatchDecision(face_id=face.id, target_person_id=target.id, status="accepted")
            )
        else:
            decision.status = "accepted"
        face.person_id = target.id
    target.embedding = centroid
    target.cover_face_id = (existing_faces or list(group))[0].id
    session.flush()
    if source_person.id != target.id and source_person.name is None:
        session.delete(source_person)
    return GroupLabelResult(target.id, target.name, len(group))


def label_face_group(
    face_id: int,
    name: str,
    *,
    root: str | Path | None = None,
    session_factory=SessionLocal,
) -> GroupLabelResult:
    """Give a new exact-case-sensitive name to the analyser group containing ``face_id``.

    DBSCAN noise cannot be labelled. The decision is persisted against every
    stable face ID in the group so subsequent cluster rebuilds retain it.
    """
    if not name or not name.strip():
        raise ValueError("Person name cannot be empty")
    try:
        with session_factory() as session:
            _, source_person, group = _group_for_face(session, face_id, root)
            if source_person.name is not None:
                if source_person.name == name:
                    return GroupLabelResult(source_person.id, source_person.name, len(group))
                raise ValueError("This face already belongs to a named person")
            duplicate = session.scalar(select(Person).where(Person.name == name))
            if duplicate is not None:
                raise ValueError(f"Person name {name!r} is already in use")
            target = Person(name=name, embedding=_centroid(group), cover_face_id=group[0].id)
            session.add(target)
            session.flush()
            result = _assign_group_to_person(session, group, source_person, target)
            session.commit()
            return result
    except IntegrityError as exc:
        raise ValueError(f"Person name {name!r} is already in use or the group changed concurrently") from exc


def assign_face_group_to_person(
    face_id: int,
    target_person_id: int,
    *,
    root: str | Path | None = None,
    session_factory=SessionLocal,
) -> GroupLabelResult:
    """Explicitly assign a current analyser group to an existing named person.

    This API enforces persisted rejections and is intended for deliberate
    service-side assignment; it does not bypass a rejected group/identity pair.
    """
    try:
        with session_factory() as session:
            target = session.scalar(
                select(Person).where(Person.id == target_person_id).with_for_update()
            )
            if target is None or target.name is None:
                raise ValueError(f"Named person {target_person_id} does not exist")
            _, source_person, group = _group_for_face(session, face_id, root)
            if source_person.name is not None:
                if source_person.id == target.id:
                    return GroupLabelResult(target.id, target.name, len(group))
                raise ValueError("This face already belongs to a different named person")
            result = _assign_group_to_person(session, group, source_person, target)
            session.commit()
            return result
    except IntegrityError as exc:
        raise ValueError("The group could not be assigned because its identity changed concurrently") from exc


def accept_face_match_proposal(
    face_id: int,
    target_person_id: int,
    *,
    root: str | Path | None = None,
    session_factory=SessionLocal,
) -> GroupLabelResult:
    """Accept a pending group proposal and merge the full group to its canonical person."""
    try:
        with session_factory() as session:
            target = session.scalar(
                select(Person).where(Person.id == target_person_id).with_for_update()
            )
            if target is None or target.name is None:
                raise ValueError(f"Named person {target_person_id} does not exist")
            _, source_person, group = _group_for_face(session, face_id, root)
            if source_person.id == target.id:
                return GroupLabelResult(target.id, target.name, len(group))
            decisions = _decisions_for_group(session, group)
            if any(
                decision.target_person_id == target.id and decision.status == "rejected"
                for decision in decisions.values()
            ):
                raise ValueError(f"This face group was rejected as {target.name!r}")
            if not any(
                decision.target_person_id == target.id
                and decision.status in ("proposed", "accepted")
                for decision in decisions.values()
            ):
                raise ValueError("No pending proposal exists for this face group and named person")
            if source_person.name is not None and source_person.id != target.id:
                raise ValueError("This face group already belongs to a different named person")
            result = _assign_group_to_person(session, group, source_person, target)
            session.commit()
            return result
    except IntegrityError as exc:
        raise ValueError("The proposal could not be accepted because the group changed concurrently") from exc


def reject_face_match_proposal(
    face_id: int,
    target_person_id: int,
    *,
    root: str | Path | None = None,
    session_factory=SessionLocal,
) -> FaceMatchResolutionResult:
    """Persist a hard group-to-person exclusion for a pending proposal."""
    try:
        with session_factory() as session:
            target = session.scalar(
                select(Person).where(Person.id == target_person_id).with_for_update()
            )
            if target is None or target.name is None:
                raise ValueError(f"Named person {target_person_id} does not exist")
            _, source_person, group = _group_for_face(session, face_id, root)
            if source_person.name is not None:
                raise ValueError("A named face group cannot be rejected as another person")
            decisions = _decisions_for_group(session, group)
            if any(
                decision.target_person_id == target.id and decision.status == "accepted"
                for decision in decisions.values()
            ):
                raise ValueError(f"This face group was already accepted as {target.name!r}")
            if not any(
                decision.target_person_id == target.id
                and decision.status in ("proposed", "rejected")
                for decision in decisions.values()
            ):
                raise ValueError("No pending proposal exists for this face group and named person")
            for face in group:
                decision = decisions.get((face.id, target.id))
                if decision is None:
                    session.add(
                        FaceMatchDecision(face_id=face.id, target_person_id=target.id, status="rejected")
                    )
                else:
                    decision.status = "rejected"
            session.commit()
            return FaceMatchResolutionResult(face_id, target.id, "rejected", len(group))
    except IntegrityError as exc:
        raise ValueError("The proposal could not be rejected because the group changed concurrently") from exc


def _record_cluster_proposals(
    session,
    cluster_faces: Sequence[Face],
    statuses_by_target: Mapping[int, set[str]],
    rejected_targets: set[int],
    named_by_id: Mapping[int, Person],
    named_persons: Sequence[Person],
    named_embeddings: Mapping[int, list[float] | None],
    decisions_by_key: dict[tuple[int, int], FaceMatchDecision],
    threshold: float,
) -> None:
    centroid = _centroid(cluster_faces)
    proposed_targets = {
        target_id
        for target_id, statuses in statuses_by_target.items()
        if "proposed" in statuses and target_id not in rejected_targets
    }
    for candidate in named_persons:
        candidate_embedding = named_embeddings.get(candidate.id)
        if candidate_embedding is None or candidate.id in rejected_targets:
            continue
        if _cosine_similarity(candidate_embedding, centroid) >= threshold:
            proposed_targets.add(candidate.id)

    for target_id in proposed_targets:
        if target_id not in named_by_id:
            continue
        for face in cluster_faces:
            key = (face.id, target_id)
            decision = decisions_by_key.get(key)
            if decision is None:
                decision = FaceMatchDecision(
                    face_id=face.id,
                    target_person_id=target_id,
                    status="proposed",
                )
                session.add(decision)
                decisions_by_key[key] = decision


def _rebuild_person_clusters(session) -> ClusterReport:
    """Rebuild clusters, partitioning conflicting decisions around accepted face anchors."""
    named_persons = list(
        session.scalars(select(Person).where(Person.name.is_not(None)).with_for_update())
    )
    faces = list(
        session.scalars(
            select(Face)
            .where(Face.embedding.is_not(None))
            .order_by(Face.id)
            .with_for_update()
        )
    )

    decisions = list(
        session.scalars(select(FaceMatchDecision).with_for_update())
    )
    decisions_by_key = {
        (decision.face_id, decision.target_person_id): decision for decision in decisions
    }
    named_by_id = {person.id: person for person in named_persons}
    named_embeddings = {
        person.id: list(person.embedding) if person.embedding is not None else None
        for person in named_persons
    }

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

    threshold = float(config.get("analysis", {}).get("similarity_threshold", 0.6))
    named_group_faces: dict[int, list[Face]] = {}

    for label in sorted(set(labels)):
        if label == -1:
            continue
        cluster_faces = [face for face, face_label in zip(faces, labels) if face_label == label]
        centroid = _centroid(cluster_faces)
        cluster_ids = {face.id for face in cluster_faces}
        statuses_by_target: dict[int, set[str]] = {}
        for (decision_face_id, target_id), decision in decisions_by_key.items():
            if decision_face_id in cluster_ids:
                statuses_by_target.setdefault(target_id, set()).add(decision.status)
        rejected_targets = {
            target_id
            for target_id, statuses in statuses_by_target.items()
            if "rejected" in statuses
        }
        all_accepted_targets = {
            target_id
            for target_id, statuses in statuses_by_target.items()
            if (
                "accepted" in statuses
                and target_id in named_by_id
            )
        }
        needs_face_level_partition = (
            len(all_accepted_targets) > 1
            or bool(all_accepted_targets & rejected_targets)
        )
        accepted_targets = all_accepted_targets - rejected_targets
        if needs_face_level_partition:
            assigned_ids = set()
            for target_id in all_accepted_targets:
                accepted_faces = [
                    face
                    for face in cluster_faces
                    if (
                        (decision := decisions_by_key.get((face.id, target_id))) is not None
                        and decision.status == "accepted"
                    )
                ]
                if not accepted_faces:
                    continue
                target = named_by_id[target_id]
                named_group_faces.setdefault(target_id, []).extend(accepted_faces)
                for face in accepted_faces:
                    face.person_id = target.id
                    assigned_ids.add(face.id)

            ambiguous_faces = [face for face in cluster_faces if face.id not in assigned_ids]
            if ambiguous_faces:
                ambiguous_person = Person(
                    embedding=_centroid(ambiguous_faces),
                    cover_face_id=ambiguous_faces[0].id,
                )
                session.add(ambiguous_person)
                session.flush()
                for face in ambiguous_faces:
                    face.person_id = ambiguous_person.id
                ambiguous_ids = {face.id for face in ambiguous_faces}
                ambiguous_statuses: dict[int, set[str]] = {}
                for (decision_face_id, target_id), decision in decisions_by_key.items():
                    if decision_face_id in ambiguous_ids:
                        ambiguous_statuses.setdefault(target_id, set()).add(decision.status)
                _record_cluster_proposals(
                    session,
                    ambiguous_faces,
                    ambiguous_statuses,
                    rejected_targets,
                    named_by_id,
                    named_persons,
                    named_embeddings,
                    decisions_by_key,
                    threshold,
                )
            continue

        if len(accepted_targets) == 1:
            person = named_by_id[next(iter(accepted_targets))]
            named_group_faces.setdefault(person.id, []).extend(cluster_faces)
            for face in cluster_faces:
                face.person_id = person.id
            continue

        person = Person(embedding=centroid, cover_face_id=cluster_faces[0].id)
        session.add(person)
        session.flush()
        for face in cluster_faces:
            face.person_id = person.id
        _record_cluster_proposals(
            session,
            cluster_faces,
            statuses_by_target,
            rejected_targets,
            named_by_id,
            named_persons,
            named_embeddings,
            decisions_by_key,
            threshold,
        )

    for person_id, assigned_faces in named_group_faces.items():
        person = named_by_id[person_id]
        person.embedding = _centroid(assigned_faces)
        person.cover_face_id = min(assigned_faces, key=lambda face: face.id).id
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
                _delete_image_faces(session, image.id)
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
