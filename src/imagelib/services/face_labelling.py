"""Face-group labelling and match-decision operations."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from imagelib.config import active_root
from imagelib.db.models import Face, FaceMatchDecision, Image, Person
from imagelib.db.session import SessionLocal
from imagelib.services.analysis_clustering import _centroid
from imagelib.services.analysis_types import FaceMatchResolutionResult, GroupLabelResult


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
