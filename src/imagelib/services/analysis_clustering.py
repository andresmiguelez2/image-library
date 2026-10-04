"""Face clustering and durable identity proposal handling."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sklearn.cluster import DBSCAN
from sqlalchemy import delete, select, update

from imagelib.config import config
from imagelib.db.models import Face, FaceMatchDecision, Person
from imagelib.db.session import SessionLocal
from imagelib.services.analysis_types import ClusterReport


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
    if not faces:
        raise ValueError("Cannot compute a centroid for an empty face group")
    embeddings = [face.embedding for face in faces if face.embedding is not None]
    if len(embeddings) != len(faces):
        raise ValueError("Cannot compute a centroid for a face without an embedding")
    dimensions = len(embeddings[0])
    return [
        sum(float(embedding[index]) for embedding in embeddings) / len(embeddings)
        for index in range(dimensions)
    ]


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
