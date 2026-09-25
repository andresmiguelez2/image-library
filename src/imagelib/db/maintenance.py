"""Small data-maintenance helpers for the image catalogue."""

from collections import defaultdict

from sqlalchemy import select

from imagelib.db.models import Face, Person


def reconcile_persons(session) -> None:
    """Remove empty persons and refresh surviving cluster metadata."""
    faces_by_person = defaultdict(list)
    for face in session.scalars(select(Face).where(Face.person_id.is_not(None))):
        faces_by_person[face.person_id].append(face)

    for person in session.scalars(select(Person)):
        faces = faces_by_person.get(person.id, [])
        if not faces:
            session.delete(person)
            continue

        embedded_faces = [face for face in faces if face.embedding is not None]
        if embedded_faces:
            person.embedding = [
                sum(float(face.embedding[index]) for face in embedded_faces) / len(embedded_faces)
                for index in range(len(embedded_faces[0].embedding))
            ]
        else:
            person.embedding = None
        person.cover_face_id = embedded_faces[0].id if embedded_faces else faces[0].id
