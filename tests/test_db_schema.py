import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from imagelib.db.models import (
    Base,
    Face,
    FaceMatchDecision,
    Image,
    ImageLocation,
    Person,
    Source,
)


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
        image = Image(content_hash="hash")
        session.add(image)
        session.flush()
        session.add(
            ImageLocation(
                image_id=image.id,
                source_id=source.id,
                path="/photos/one.jpg",
            )
        )
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


def test_image_identity_and_location_constraints():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        source = Source(path="/photos")
        other_source = Source(path="/archive")
        image = Image(content_hash="sha256-a")
        other_image = Image(content_hash="sha256-b")
        session.add_all([source, other_source, image, other_image])
        session.flush()
        location = ImageLocation(
            image_id=image.id,
            source_id=source.id,
            path="/photos/a.jpg",
        )
        session.add(location)
        session.commit()

        assert image.locations == [location]
        assert source.locations == [location]

        session.add(Image(content_hash="sha256-a"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add(
            ImageLocation(
                image_id=other_image.id,
                source_id=other_source.id,
                path="/photos/a.jpg",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    inspector = inspect(engine)
    image_unique = {
        constraint["name"] for constraint in inspector.get_unique_constraints("images")
    }
    location_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints("image_locations")
    }
    assert "uq_images_content_hash" in image_unique
    assert "uq_image_locations_path" in location_unique
    assert {column["name"] for column in inspector.get_columns("images")} >= {
        "content_hash",
        "status",
    }
    assert not {"source_id", "path"} & {
        column["name"] for column in inspector.get_columns("images")
    }
    assert {index["name"] for index in inspector.get_indexes("image_locations")} == {
        "ix_image_locations_image_id",
        "ix_image_locations_source_id",
    }
    foreign_keys = inspector.get_foreign_keys("image_locations")
    assert {foreign_key["referred_table"] for foreign_key in foreign_keys} == {
        "images",
        "sources",
    }
    assert {foreign_key["options"].get("ondelete") for foreign_key in foreign_keys} == {
        "CASCADE"
    }

    bootstrap = (Path(__file__).parents[1] / "scripts/init_db.sql").read_text()
    assert re.search(
        r"content_hash\s+TEXT NOT NULL,.*?CONSTRAINT uq_images_content_hash UNIQUE \(content_hash\)",
        bootstrap,
        re.DOTALL,
    )
    assert re.search(r"CREATE TABLE IF NOT EXISTS image_locations\s*\(", bootstrap)
    assert "CONSTRAINT uq_image_locations_path UNIQUE (path)" in bootstrap


def test_content_location_migration_is_transactional_and_preserves_face_ids():
    migration = (
        Path(__file__).parents[1] / "scripts/002_content_locations.sql"
    ).read_text()

    assert "\\set ON_ERROR_STOP on" in migration
    assert re.search(r"\bBEGIN;", migration)
    assert re.search(r"\bCOMMIT;", migration)
    assert "multiple image rows containing faces" in migration
    assert "conflicting non-NULL metadata" in migration
    assert "Cannot merge duplicate values that are not SHA-256 hashes" in migration
    assert "inconsistent face counts" in migration
    assert "CREATE TABLE image_locations" in migration
    assert "INSERT INTO image_locations (image_id, source_id, path)" in migration
    assert "UPDATE faces AS face" in migration
    assert "ADD CONSTRAINT uq_images_content_hash UNIQUE (content_hash)" in migration
    assert "DROP COLUMN source_id" in migration
    assert "DROP COLUMN path" in migration
    assert "face_match_decisions" not in migration
    assert "cover_face_id" not in migration
