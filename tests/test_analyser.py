from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from imagelib.db.models import Base, Face, Image, Source
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
