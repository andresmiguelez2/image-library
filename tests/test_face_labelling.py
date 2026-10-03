from math import cos, sin, radians
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from imagelib.db.models import Base, Face, FaceMatchDecision, Image, Person, Source
from imagelib.db.maintenance import reconcile_persons
from imagelib.services import analyser, catalog


def _database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _vector(angle: float) -> list[float]:
    return [cos(radians(angle)), sin(radians(angle))] + [0.0] * 510


def _add_group(factory, root: Path, name: str, angle: float, count: int = 2) -> list[int]:
    with factory() as session:
        source = session.scalar(select(Source).where(Source.path == str(root)))
        if source is None:
            source = Source(path=str(root))
            session.add(source)
            session.flush()
        face_ids = []
        for index in range(count):
            path = root / f"{name}-{index}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            image = Image(
                source_id=source.id,
                path=str(path),
                content_hash=f"{name}-{index}",
                status="analysed",
                face_count=1,
            )
            session.add(image)
            session.flush()
            offset = (index - (count - 1) / 2) * 0.3
            face = Face(
                image_id=image.id,
                x=3,
                y=4,
                w=20,
                h=24,
                confidence=0.99,
                embedding=_vector(angle + offset),
            )
            session.add(face)
            session.flush()
            face_ids.append(face.id)
        session.commit()
        return face_ids


def _setup(tmp_path, monkeypatch):
    factory = _database()
    root = tmp_path / "images"
    root.mkdir()
    monkeypatch.setitem(analyser.config["analysis"], "cluster_eps", 0.01)
    monkeypatch.setitem(analyser.config["analysis"], "cluster_min_samples", 2)
    monkeypatch.setitem(analyser.config["analysis"], "similarity_threshold", 0.7)
    return factory, root


def _person_for_face(factory, face_id):
    with factory() as session:
        face = session.get(Face, face_id)
        return session.get(Person, face.person_id) if face.person_id is not None else None


def _faces_person_ids(factory, face_ids):
    with factory() as session:
        return [session.get(Face, face_id).person_id for face_id in face_ids]


def test_group_labelling_proposals_acceptance_and_rebuild_durability(tmp_path, monkeypatch):
    factory, root = _setup(tmp_path, monkeypatch)
    ada_faces = _add_group(factory, root, "ada-source", 0)
    candidate_b = _add_group(factory, root, "candidate-b", 30)
    candidate_c = _add_group(factory, root, "candidate-c", -30)
    unrelated = _add_group(factory, root, "unrelated", 90)
    noise = _add_group(factory, root, "noise", 145, count=1)

    analyser.rebuild_person_clusters(session_factory=factory)
    labelled = analyser.label_face_group(ada_faces[0], "Ada", root=root, session_factory=factory)

    assert labelled.face_count == 2
    assert _faces_person_ids(factory, ada_faces) == [labelled.person_id] * 2
    with factory() as session:
        decisions = list(
            session.scalars(
                select(FaceMatchDecision).where(
                    FaceMatchDecision.face_id.in_(ada_faces),
                    FaceMatchDecision.target_person_id == labelled.person_id,
                )
            )
        )
        assert len(decisions) == 2
        assert {decision.status for decision in decisions} == {"accepted"}

    analyser.rebuild_person_clusters(session_factory=factory)
    proposals = catalog.list_pending_face_match_proposals(root=root, session_factory=factory)
    assert len(proposals) == 2
    assert {proposal.target_person_id for proposal in proposals} == {labelled.person_id}
    assert {proposal.face_count for proposal in proposals} == {2}
    assert all(proposal.target_person_name == "Ada" for proposal in proposals)
    assert all(proposal.sample_image_path.startswith(str(root)) for proposal in proposals)
    assert all(proposal.similarity > 0.7 for proposal in proposals)
    assert set(_faces_person_ids(factory, candidate_b + candidate_c)).isdisjoint(
        {labelled.person_id}
    )
    assert all(_person_for_face(factory, face_id).name is None for face_id in unrelated)
    assert _person_for_face(factory, noise[0]) is None

    accepted = analyser.accept_face_match_proposal(
        proposals[0].face_id,
        labelled.person_id,
        root=root,
        session_factory=factory,
    )
    candidate_faces = candidate_b if proposals[0].face_id in candidate_b else candidate_c
    assert accepted.person_id == labelled.person_id
    assert accepted.face_count == 2
    assert _faces_person_ids(factory, candidate_faces) == [labelled.person_id] * 2

    analyser.rebuild_person_clusters(session_factory=factory)
    assert _faces_person_ids(factory, candidate_faces) == [labelled.person_id] * 2
    with factory() as session:
        canonical = session.get(Person, labelled.person_id)
        assert canonical.name == "Ada"
        accepted_rows = list(
            session.scalars(
                select(FaceMatchDecision).where(
                    FaceMatchDecision.face_id.in_(candidate_faces),
                    FaceMatchDecision.target_person_id == labelled.person_id,
                )
            )
        )
        assert {row.status for row in accepted_rows} == {"accepted"}


def test_rejection_is_hard_persistent_and_duplicate_names_are_clean_errors(tmp_path, monkeypatch):
    factory, root = _setup(tmp_path, monkeypatch)
    ada_faces = _add_group(factory, root, "ada-source", 0)
    candidate = _add_group(factory, root, "candidate", 30)
    unrelated = _add_group(factory, root, "unrelated", 90)
    noise = _add_group(factory, root, "noise", 145, count=1)
    analyser.rebuild_person_clusters(session_factory=factory)
    ada = analyser.label_face_group(ada_faces[0], "Ada", root=root, session_factory=factory)
    analyser.rebuild_person_clusters(session_factory=factory)

    proposal = next(
        item
        for item in catalog.list_pending_face_match_proposals(root=root, session_factory=factory)
        if item.face_id in candidate
    )
    rejected = analyser.reject_face_match_proposal(
        proposal.face_id, ada.person_id, root=root, session_factory=factory
    )
    assert rejected.status == "rejected" and rejected.group_face_count == 2
    with factory() as session:
        rows = list(
            session.scalars(
                select(FaceMatchDecision).where(
                    FaceMatchDecision.face_id.in_(candidate),
                    FaceMatchDecision.target_person_id == ada.person_id,
                )
            )
        )
        assert len(rows) == 2
        assert {row.status for row in rows} == {"rejected"}

    analyser.rebuild_person_clusters(session_factory=factory)
    assert all(
        item.target_person_id != ada.person_id
        for item in catalog.list_pending_face_match_proposals(root=root, session_factory=factory)
    )
    assert all(_person_for_face(factory, face_id).name is None for face_id in candidate)
    with pytest.raises(ValueError, match="rejected"):
        analyser.assign_face_group_to_person(
            candidate[0], ada.person_id, root=root, session_factory=factory
        )
    with pytest.raises(ValueError, match="rejected"):
        analyser.accept_face_match_proposal(
            candidate[0], ada.person_id, root=root, session_factory=factory
        )

    with pytest.raises(ValueError, match="already in use"):
        analyser.label_face_group(unrelated[0], "Ada", root=root, session_factory=factory)
    with pytest.raises(ValueError, match="noise"):
        analyser.label_face_group(noise[0], "Noise", root=root, session_factory=factory)

    relabelled = analyser.label_face_group(
        candidate[0], "Grace", root=root, session_factory=factory
    )
    assert _faces_person_ids(factory, candidate) == [relabelled.person_id] * 2
    analyser.rebuild_person_clusters(session_factory=factory)
    assert _faces_person_ids(factory, candidate) == [relabelled.person_id] * 2
    with factory() as session:
        assert session.get(Person, ada.person_id).name == "Ada"
        assert session.get(Person, relabelled.person_id).name == "Grace"
        rejected_rows = list(
            session.scalars(
                select(FaceMatchDecision).where(
                    FaceMatchDecision.face_id.in_(candidate),
                    FaceMatchDecision.target_person_id == ada.person_id,
                )
            )
        )
        assert {row.status for row in rejected_rows} == {"rejected"}


def test_reconcile_preserves_named_person_referenced_by_durable_decision(tmp_path):
    factory = _database()
    root = tmp_path / "images"
    root.mkdir()
    with factory() as session:
        source = Source(path=str(root))
        target = Person(name="Ada", embedding=_vector(0))
        session.add_all([source, target])
        session.flush()
        image = Image(
            source_id=source.id,
            path=str(root / "candidate.jpg"),
            content_hash="candidate",
            status="analysed",
            face_count=1,
        )
        session.add(image)
        session.flush()
        face = Face(image_id=image.id, x=0, y=0, w=2, h=2, embedding=_vector(30))
        session.add(face)
        session.flush()
        session.add(
            FaceMatchDecision(face_id=face.id, target_person_id=target.id, status="proposed")
        )
        target_id = target.id
        session.commit()

    with factory() as session:
        reconcile_persons(session)
        session.commit()
        assert session.get(Person, target_id) is not None
        decision = session.scalar(
            select(FaceMatchDecision).where(FaceMatchDecision.target_person_id == target_id)
        )
        session.delete(decision)
        reconcile_persons(session)
        session.commit()
        assert session.get(Person, target_id) is None


def test_face_replacement_clears_person_cover_reference(tmp_path):
    factory = _database()
    root = tmp_path / "images"
    root.mkdir()
    with factory() as session:
        source = Source(path=str(root))
        session.add(source)
        session.flush()
        image = Image(
            source_id=source.id,
            path=str(root / "photo.jpg"),
            content_hash="photo",
            status="analysed",
            face_count=1,
        )
        session.add(image)
        session.flush()
        face = Face(image_id=image.id, x=0, y=0, w=2, h=2, embedding=_vector(0))
        session.add(face)
        session.flush()
        person = Person(name="Ada", cover_face_id=face.id, embedding=_vector(0))
        session.add(person)
        session.commit()
        image_id = image.id
        person_id = person.id

    with factory() as session:
        analyser._delete_image_faces(session, image_id)
        session.commit()
        assert session.get(Face, face.id) is None
        assert session.get(Person, person_id).cover_face_id is None


def test_proposal_catalog_and_mutations_are_root_scoped(tmp_path, monkeypatch):
    factory, root = _setup(tmp_path, monkeypatch)
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    ada_faces = _add_group(factory, root, "ada-source", 0)
    in_root_candidate = _add_group(factory, root, "candidate-in", 30)
    outside_candidate = _add_group(factory, outside_root, "candidate-out", 40)
    analyser.rebuild_person_clusters(session_factory=factory)
    ada = analyser.label_face_group(ada_faces[0], "Ada", root=root, session_factory=factory)
    analyser.rebuild_person_clusters(session_factory=factory)

    proposals = catalog.list_pending_face_match_proposals(root=root, session_factory=factory)
    assert len(proposals) == 1
    assert proposals[0].face_id in in_root_candidate
    with pytest.raises(ValueError, match="outside the selected image root"):
        analyser.reject_face_match_proposal(
            outside_candidate[0], ada.person_id, root=root, session_factory=factory
        )
