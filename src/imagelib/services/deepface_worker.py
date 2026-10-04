"""Long-lived, Qt-free DeepFace worker protocol.

The worker reads one JSON object per line and writes one JSON object per line:

``{"op": "analyse", "path": "/absolute/photo.jpg", "request_id": "42"}``
    ``{"ok": true, "faces": [...], "request_id": "42"}``
``{"op": "shutdown"}``
    ``{"ok": true}``

The model is built lazily once in this process.  A UI coordinator may launch
this module with ``python -m imagelib.services.deepface_worker`` using
``QProcess``; no Qt import is required here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import redirect_stdout
from io import BytesIO
from pathlib import Path
from typing import IO, Callable

from imagelib.config import config
from imagelib.diagnostics import diagnostic, diagnostic_exception
from typing_extensions import Self


def _default_model_factory():
    from deepface import DeepFace

    return DeepFace.build_model("Facenet512")


def _default_representer(path: str, model):
    from deepface import DeepFace

    result = DeepFace.represent(
        img_path=BytesIO(Path(path).read_bytes()),
        model_name="Facenet512",
        detector_backend=config.get("analysis", {}).get("detector_backend", "retinaface"),
        enforce_detection=False,
    )
    return result if isinstance(result, list) else [result]


def _json_default(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


def run_worker(
    input_stream: IO[str] | None = None,
    output_stream: IO[str] | None = None,
    *,
    model_factory: Callable[[], object] | None = None,
    representer: Callable[[str, object], list[dict]] | None = None,
) -> None:
    """Serve JSON-line requests until EOF or a shutdown request.

    Factories are injectable so the protocol can be tested without importing
    TensorFlow or DeepFace.
    """
    input_stream = input_stream or sys.stdin
    output_stream = output_stream or sys.stdout
    model_factory = model_factory or _default_model_factory
    representer = representer or _default_representer
    model = None
    diagnostic(f"DeepFace worker started executable={sys.executable!r}")

    def reply(value: dict) -> None:
        output_stream.write(json.dumps(value, default=_json_default) + "\n")
        output_stream.flush()

    def call_model(callback, *args):
        with redirect_stdout(sys.stderr):
            return callback(*args)

    for line in input_stream:
        if not line.strip():
            continue
        request_id = None
        request_path = None
        try:
            request = json.loads(line)
            if isinstance(request, dict):
                request_path = request.get("path")
            operation = request.get("op")
            request_id = request.get("request_id")
            diagnostic(
                f"DeepFace worker request operation={operation!r} "
                f"request_id={request_id!r} path={request_path!r}"
            )
            if operation == "shutdown":
                reply({"ok": True, "request_id": request_id})
                return
            if operation != "analyse":
                raise ValueError(f"unknown operation: {operation}")
            path = str(Path(request["path"]).expanduser().resolve())
            if model is None:
                model = call_model(model_factory)
            reply({"ok": True, "faces": call_model(representer, path, model), "request_id": request_id})
        except Exception as exc:
            reply(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:4000],
                    "request_id": request_id,
                }
            )
            diagnostic_exception(
                f"DeepFace worker request failed request_id={request_id!r} path={request_path!r}",
                exc,
            )


class DeepFaceWorkerClient:
    """Blocking coordinator client intended to run outside the Qt event loop."""

    def __init__(self, executable: str | None = None):
        command = [executable or sys.executable, "-m", "imagelib.services.deepface_worker"]
        diagnostic(f"DeepFace worker executable={command[0]!r} arguments={command[1:]!r}")
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            diagnostic_exception("DeepFace worker process start failed", exc)
            raise
        diagnostic(f"DeepFace worker process started pid={self.process.pid}")

    def analyse(self, path: str | Path) -> list[dict]:
        """Send one image to the worker and return its face dictionaries."""
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("DeepFace worker streams are unavailable")
        request = {"op": "analyse", "path": str(path)}
        diagnostic(f"DeepFace worker request sent: {json.dumps(request, ensure_ascii=False)}")
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        raw = self.process.stdout.readline()
        diagnostic(f"DeepFace worker raw stdout response: {raw.rstrip()!r}")
        response = json.loads(raw)
        diagnostic(
            f"DeepFace worker response parsed request_id={response.get('request_id')!r} "
            f"status={'ok' if response.get('ok') else 'error'!r}"
        )
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "DeepFace worker failed"))
        return response.get("faces", [])

    def close(self) -> None:
        """Ask the worker to exit and terminate it if it does not respond."""
        if self.process.poll() is not None:
            return
        try:
            if self.process.stdin is not None and self.process.stdout is not None:
                diagnostic("DeepFace worker request sent: shutdown")
                self.process.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                self.process.stdin.flush()
                self.process.stdout.readline()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            self.process.wait(timeout=5)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


if __name__ == "__main__":
    run_worker()
