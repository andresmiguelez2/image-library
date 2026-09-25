from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from imagelib.db.models import Base, Face, Image, Person, Source
from imagelib.services import analyser


def test_analyser_reuses_model_and_persists_faces(tmp_path: Path, monkeypatch) -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"fixture")
    with factory() as session:
        source = Source(path=str(tmp_path))
        session.add(source)
        session.flush()
        session.add(
            Image(path=str(image_path), source_id=source.id, content_hash="hash", status="indexed")
        )
        session.commit()

    calls = []
    monkeypatch.setattr(analyser, "_model", None)
    monkeypatch.setattr(analyser, "_get_model", lambda: calls.append("model") or object())
    monkeypatch.setattr(
        analyser,
        "_represent",
        lambda path, model: [
            {
                "embedding": [0.1] * 512,
                "facial_area": {"x": 1, "y": 2, "w": 20, "h": 30},
                "face_confidence": 0.99,
            }
        ],
    )

    report = analyser.analyse_images(session_factory=factory)

    with factory() as session:
        image = session.scalar(select(Image))
        face = session.scalar(select(Face))
        assert image.status == "analysed"
        assert image.face_count == 1
        assert face.embedding == [0.1] * 512
    assert report.analysed == 1
    assert report.faces == 1
    assert calls == ["model"]


def test_represent_matches_installed_deepface_api(tmp_path: Path, monkeypatch) -> None:
    from deepface import DeepFace

    image_path = tmp_path / "photo.jpg"
    calls = []

    def represent(*, img_path, model_name, detector_backend, enforce_detection):
        calls.append(
            {
                "img_path": img_path,
                "model_name": model_name,
                "detector_backend": detector_backend,
                "enforce_detection": enforce_detection,
            }
        )
        return {"embedding": [0.1] * 512}

    monkeypatch.setattr(DeepFace, "represent", represent)

    result = analyser._represent(image_path, object())

    assert result == [{"embedding": [0.1] * 512}]
    assert calls == [
        {
            "img_path": str(image_path),
            "model_name": "Facenet512",
            "detector_backend": analyser.config["analysis"].get("detector_backend", "retinaface"),
            "enforce_detection": False,
        }
    ]


def _analysis_database(tmp_path: Path, count: int):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        source = Source(path=str(tmp_path))
        session.add(source)
        session.flush()
        for index in range(count):
            path = tmp_path / f"photo-{index}.jpg"
            path.write_bytes(b"fixture")
            session.add(
                Image(path=str(path), source_id=source.id, content_hash=str(index), status="indexed")
            )
        session.commit()
    return factory


def _face(embedding):
    return {
        "embedding": embedding,
        "facial_area": {"x": 1, "y": 2, "w": 20, "h": 30},
        "face_confidence": 0.99,
    }


def test_analyser_clusters_faces_and_stores_centroids(tmp_path: Path, monkeypatch) -> None:
    factory = _analysis_database(tmp_path, 3)
    first = [1.0, 0.0] + [0.0] * 510
    second = [0.99, 0.01] + [0.0] * 510
    noise = [0.0, 1.0] + [0.0] * 510
    results = {
        "photo-0.jpg": [_face(first)],
        "photo-1.jpg": [_face(second)],
        "photo-2.jpg": [_face(noise)],
    }
    monkeypatch.setitem(analyser.config["analysis"], "cluster_eps", 0.01)
    monkeypatch.setitem(analyser.config["analysis"], "cluster_min_samples", 2)
    monkeypatch.setattr(analyser, "_get_model", lambda: object())
    monkeypatch.setattr(analyser, "_represent", lambda path, model: results[path.name])

    analyser.analyse_images(session_factory=factory)

    with factory() as session:
        person = session.scalar(select(Person))
        faces = list(session.scalars(select(Face).order_by(Face.id)))
        assert person is not None
        assert len(person.embedding) == 512
        assert person.embedding[0] == (first[0] + second[0]) / 2
        assert [face.person_id is not None for face in faces] == [True, True, False]
        assert faces[0].person_id == faces[1].person_id
        assert faces[2].person_id is None


def test_reanalysis_removes_stale_clusters_and_handles_no_face(tmp_path: Path, monkeypatch) -> None:
    factory = _analysis_database(tmp_path, 2)
    embedding = [1.0] + [0.0] * 511
    monkeypatch.setitem(analyser.config["analysis"], "cluster_min_samples", 2)
    monkeypatch.setattr(analyser, "_get_model", lambda: object())
    monkeypatch.setattr(analyser, "_represent", lambda path, model: [_face(embedding)])
    analyser.analyse_images(session_factory=factory)

    with factory() as session:
        image = session.scalar(select(Image).where(Image.path.like("%photo-0.jpg")))
        image.status = "indexed"
        session.commit()
    monkeypatch.setattr(analyser, "_represent", lambda path, model: [])
    analyser.analyse_images(session_factory=factory)

    with factory() as session:
        assert session.scalar(select(Person)) is None
        remaining_face = session.scalar(select(Face))
        assert remaining_face is not None
        assert remaining_face.person_id is None
        removed = session.scalar(select(Image).where(Image.path.like("%photo-0.jpg")))
        assert removed.face_count == 0
