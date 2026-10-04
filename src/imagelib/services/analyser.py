"""Stage-two face detection and embedding analysis."""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from threading import Lock

from imagelib.config import config
from imagelib.db.models import Face, Image
from imagelib.db.session import SessionLocal
from imagelib.services.analysis_clustering import (
    _rebuild_person_clusters,
)
from imagelib.services.analysis_persistence import (
    _delete_image_faces,
    _face_values,
    _mark_error,
    _select_analysis_targets,
    _worker_error_text,
    _worker_result,
)
from imagelib.services.analysis_types import (
    AnalysisReport,
    WorkerAnalysisResult,
)

_model = None
_model_lock = Lock()


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
        except Exception as exc:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
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
