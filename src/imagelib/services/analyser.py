"""Stage-two face detection and embedding analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Callable

from sklearn.cluster import DBSCAN
from sqlalchemy import delete, select, update

from imagelib.config import config
from imagelib.db.models import Face, Image, Person
from imagelib.db.session import SessionLocal

_model = None
_model_lock = Lock()


@dataclass
class AnalysisReport:
    discovered: int = 0
    analysed: int = 0
    faces: int = 0
    errors: int = 0


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
        model=model,
        detector_backend=config["analysis"].get("detector_backend", "retinaface"),
        enforce_detection=False,
    )
    return result if isinstance(result, list) else [result]


def _face_values(item: dict) -> tuple[float, float, float, float, float | None, list[float]]:
    area = item.get("facial_area") or {}
    embedding = [float(value) for value in item["embedding"]]
    if len(embedding) != 512:
        raise ValueError(f"Facenet returned {len(embedding)} dimensions, expected 512")
    return (
        float(area.get("x", 0)),
        float(area.get("y", 0)),
        float(area.get("w", 0)),
        float(area.get("h", 0)),
        item.get("face_confidence", item.get("confidence")),
        embedding,
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


def _rebuild_person_clusters(session) -> None:
    """Rebuild the person clusters from the faces currently in the database."""
    faces = list(
        session.scalars(
            select(Face).where(Face.embedding.is_not(None)).order_by(Face.id)
        )
    )

    session.execute(update(Face).values(person_id=None))
    session.execute(update(Person).values(cover_face_id=None))
    session.execute(delete(Person))

    if not faces:
        session.commit()
        return

    embeddings = [face.embedding for face in faces]
    analysis_config = config.get("analysis", {})
    labels = DBSCAN(
        eps=float(analysis_config.get("cluster_eps", 0.6)),
        min_samples=int(analysis_config.get("cluster_min_samples", 2)),
        metric="cosine",
    ).fit_predict(embeddings)

    for label in sorted(set(labels)):
        if label == -1:
            continue
        cluster_faces = [face for face, face_label in zip(faces, labels) if face_label == label]
        centroid = [
            sum(float(face.embedding[index]) for face in cluster_faces) / len(cluster_faces)
            for index in range(512)
        ]
        person = Person(embedding=centroid, cover_face_id=cluster_faces[0].id)
        session.add(person)
        session.flush()
        for face in cluster_faces:
            face.person_id = person.id
    session.commit()


def analyse_images(
    *, session_factory=SessionLocal, progress: Callable[[Image], None] | None = None
) -> AnalysisReport:
    """Analyse indexed images once, reusing one Facenet model per process."""
    report = AnalysisReport()
    with session_factory() as session:
        targets = list(session.scalars(select(Image).where(Image.status == "indexed")))
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
            except Exception as exc:
                _mark_error(session, image, exc)
                report.errors += 1
        if targets:
            _rebuild_person_clusters(session)
    return report
