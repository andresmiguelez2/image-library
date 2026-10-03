from pathlib import Path
import re

import pytest
from sqlalchemy import create_engine, delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from imagelib.db.models import Base, Face, FaceMatchDecision, Image, Person, Source


def test_person_names_are_unique_case_sensitive_and_nullable():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [Person(name="Ada"), Person(name="ada"), Person(), Person()]
        )
        session.commit()

        session.add(Person(name="Ada"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        assert session.scalar(select(Person).where(Person.name == "Ada")) is not None
        assert session.scalar(select(Person).where(Person.name == "ada")) is not None
        assert len(session.scalars(select(Person).where(Person.name.is_(None))).all()) == 2

    constraint_names = {
        constraint["name"] for constraint in inspect(engine).get_unique_constraints("persons")
    }
    assert "uq_persons_name" in constraint_names


def test_match_decisions_survive_transient_cluster_rebuild():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        source = Source(path="/photos")
        named_person = Person(name="Ada")
        transient_person = Person()
        session.add_all([source, named_person, transient_person])
        session.flush()
        image = Image(source_id=source.id, path="/photos/one.jpg", content_hash="hash")
        session.add(image)
        session.flush()
        face = Face(image_id=image.id, person_id=transient_person.id, x=1, y=2, w=3, h=4)
        session.add(face)
        session.flush()
        session.add(
            FaceMatchDecision(
                face_id=face.id,
                target_person_id=named_person.id,
                status="rejected",
            )
        )
        session.commit()

        # Cluster rebuilds clear face assignments and delete unnamed Person rows.
        session.execute(update(Face).values(person_id=None))
        session.execute(delete(Person).where(Person.name.is_(None)))
        session.commit()

        decision = session.get(FaceMatchDecision, (face.id, named_person.id))
        assert decision is not None
        assert decision.status == "rejected"
        assert session.get(Face, face.id) is not None
        assert session.get(Person, named_person.id).name == "Ada"


def test_match_decision_schema_constraints_and_migration_preflight():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    inspector = inspect(engine)

    assert "face_match_decisions" in inspector.get_table_names()
    assert set(inspector.get_pk_constraint("face_match_decisions")["constrained_columns"]) == {
        "face_id",
        "target_person_id",
    }
    indexes = inspector.get_indexes("face_match_decisions")
    accepted_index = next(
        index
        for index in indexes
        if index["name"] == "uq_face_match_decisions_one_accepted_per_face"
    )
    assert accepted_index["unique"]
    assert accepted_index["column_names"] == ["face_id"]
    foreign_keys = inspector.get_foreign_keys("face_match_decisions")
    assert {foreign_key["options"].get("ondelete") for foreign_key in foreign_keys} == {
        "CASCADE",
        "RESTRICT",
    }

    migration = (Path(__file__).parents[1] / "scripts/001_face_labelling.sql").read_text()
    assert "GROUP BY name" in migration
    assert "HAVING COUNT(*) > 1" in migration
    assert "RAISE EXCEPTION" in migration
    assert "ON_ERROR_STOP on" in migration
    assert re.search(r"ADD\s+CONSTRAINT uq_persons_name UNIQUE \(name\)", migration)
