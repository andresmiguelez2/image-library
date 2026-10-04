import json
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QThreadPool, QTimer, Signal

from imagelib.diagnostics import diagnostic, diagnostic_exception
from imagelib.services import analyser
from imagelib.ui.workers import FunctionTask

logger = logging.getLogger("imagelib.ui.main_window")


class AnalysisCoordinator(QObject):
    status = Signal(str)
    progress = Signal(int, int)
    finished = Signal(object)
    failed = Signal(str)
    catalogue_changed = Signal()

    def __init__(self, pool: QThreadPool, parent=None) -> None:
        super().__init__(parent)
        self.pool = pool
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.started.connect(self._process_started)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.readyReadStandardError.connect(self._read_error_output)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._process_finished)
        self._generation = 0
        self._active = False
        self._root = Path()
        self._targets = []
        self._queue_index = 0
        self._pending: dict[str, tuple[analyser.AnalysisTarget, dict]] = {}
        self._responses: list[tuple[analyser.AnalysisTarget, dict]] = []
        self._starting_after_finish = False
        self._process_generation = None
        self._shutdown_requested = False

    def start(self, root: Path, image_ids: list[int] | None = None) -> None:
        self.cancel()
        self._generation += 1
        generation = self._generation
        self._active = True
        self._root = root
        self._targets = []
        self._pending = {}
        self._responses = []
        self._queue_index = 0
        diagnostic(f"Analysis start root={root} selected_image_ids={image_ids!r}")
        self.status.emit("Selecting images for analysis…")
        task = FunctionTask(
            lambda: analyser.select_analysis_targets(root=root, image_ids=image_ids),
            self,
        )
        task.signals.result.connect(lambda targets, g=generation: self._targets_ready(g, targets))
        task.signals.error.connect(lambda message, g=generation: self._selection_error(g, message))
        self.pool.start(task)

    def cancel(self) -> None:
        self._generation += 1
        self._active = False
        self._targets = []
        self._pending.clear()
        self._responses.clear()
        self._starting_after_finish = False
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
        self.status.emit("Analysis cancelled")

    def _targets_ready(self, generation: int, targets) -> None:
        if not self._active or generation != self._generation:
            return
        self._targets = list(targets)
        diagnostic(f"Analysis targets selected count={len(self._targets)}")
        for target in self._targets:
            diagnostic(
                f"Analysis target image_id={target.id} path={target.path!r} "
                f"status={target.status!r} content_hash={target.content_hash!r}"
            )
        if not self._targets:
            self._active = False
            self.status.emit("No eligible images to analyse")
            self.finished.emit(None)
            return
        self.status.emit(f"Analysing 0 of {len(self._targets)}…")
        self._start_process_when_available(generation)

    def _selection_error(self, generation: int, message: str) -> None:
        if self._active and generation == self._generation:
            logger.error("Analysis target selection failed for %s: %s", self._root, message)
            self._active = False
            self.failed.emit(f"Could not select analysis targets: {message}")

    def _start_process_when_available(self, generation: int) -> None:
        if not self._active or generation != self._generation:
            return
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self._process_generation = generation
            self._starting_after_finish = False
            self.process.setProgram(sys.executable)
            self.process.setArguments(["-m", "imagelib.services.deepface_worker"])
            diagnostic(
                f"DeepFace worker executable={sys.executable!r} "
                "arguments=['-m', 'imagelib.services.deepface_worker']"
            )
            environment = QProcessEnvironment.systemEnvironment()
            self.process.setProcessEnvironment(environment)
            try:
                self.process.start()
            except Exception as exc:
                diagnostic_exception("DeepFace worker process start failed", exc)
                self._active = False
                self.failed.emit(f"DeepFace worker start failed: {exc}")
        else:
            self._starting_after_finish = True
            self._process_generation = generation
            self.process.kill()

    def _process_started(self) -> None:
        diagnostic(f"DeepFace worker process started pid={self.process.processId()}")
        if self._active and not self._starting_after_finish and self._process_generation == self._generation:
            self._send_next(self._generation)

    def _send_next(self, generation: int) -> None:
        if not self._active or generation != self._generation:
            return
        if self.process.state() != QProcess.ProcessState.Running:
            return
        if self._queue_index >= len(self._targets):
            self._persist_batch(generation)
            return
        target = self._targets[self._queue_index]
        request_id = f"{generation}:{self._queue_index}"
        self._pending[request_id] = (target, {})
        self._queue_index += 1
        request = {"op": "analyse", "path": target.path, "request_id": request_id}
        diagnostic(f"Analysis request sent: {json.dumps(request, ensure_ascii=False)}")
        try:
            self.process.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
        except Exception as exc:
            diagnostic_exception(f"Analysis request failed request_id={request_id!r}", exc)
            self._pending.pop(request_id, None)
            self._active = False
            self.failed.emit(f"Could not send analysis request: {exc}")

    def _read_output(self) -> None:
        while self.process.canReadLine():
            raw = bytes(self.process.readLine()).strip()
            if not raw:
                continue
            diagnostic(f"DeepFace worker raw stdout response: {raw.decode(errors='replace')}")
            try:
                response = json.loads(raw.decode())
                request_id = response.get("request_id")
                response_status = response.get("status", "ok" if response.get("ok") else "error")
                diagnostic(
                    f"DeepFace worker response parsed request_id={request_id!r} "
                    f"status={response_status!r}"
                )
                pending = self._pending.pop(request_id, None)
                if pending is None or not self._active:
                    if pending is None and self._active:
                        raise ValueError(f"unexpected DeepFace worker request_id: {request_id!r}")
                    continue
                target, _ = pending
                if response.get("ok") is False:
                    diagnostic(
                        f"Analysis response error image_id={target.id} path={target.path!r} "
                        f"error={response.get('error', 'DeepFace worker failed')!r}"
                    )
                self._responses.append((target, response))
                self.progress.emit(len(self._responses), len(self._targets))
                self.status.emit(f"Analysing {len(self._responses)} of {len(self._targets)}…")
                self._send_next(self._generation)
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, ValueError) as exc:
                logger.exception("Invalid DeepFace worker response")
                diagnostic_exception("Invalid DeepFace worker response", exc)
                if self._active:
                    self._active = False
                    self.failed.emit(f"Invalid DeepFace worker response: {exc}")

    def _read_error_output(self) -> None:
        stderr = bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
        if stderr:
            logger.warning("DeepFace worker stderr: %s", stderr)
            diagnostic(f"DeepFace worker stderr: {stderr}")

    def _persist_batch(self, generation: int) -> None:
        if not self._active or generation != self._generation or not self._responses:
            return
        self.status.emit("Saving analysis results and rebuilding people…")
        responses = list(self._responses)
        diagnostic(
            f"Analysis persistence start responses={len(responses)} "
            f"image_ids={[target.id for target, _response in responses]!r}"
        )
        task = FunctionTask(lambda: analyser.persist_worker_batch(responses), self)
        task.signals.result.connect(lambda report, g=generation: self._batch_saved(g, report))
        task.signals.error.connect(lambda message, g=generation: self._persist_error(g, message))
        self._active = False
        self.pool.start(task)

    def _batch_saved(self, generation: int, report) -> None:
        diagnostic(f"Analysis persistence completed report={report!r}")
        errors = [result for result in getattr(report, "results", ()) if result.status == "error"]
        for result in getattr(report, "results", ()):
            diagnostic(
                f"Analysis result image_id={result.image_id} status={result.status!r} "
                f"accepted={getattr(result, 'accepted', None)} "
                f"faces={getattr(result, 'face_count', None)} error={result.error!r}"
            )
        for result in errors:
            logger.error(
                "Analysis failed for image %s: %s",
                result.image_id,
                result.error or "DeepFace worker failed",
            )
        if generation != self._generation:
            self.catalogue_changed.emit()
            return
        self.finished.emit(report)
        if errors:
            self.status.emit(
                f"Analysis complete with {len(errors)} error(s): "
                f"{errors[0].error or 'DeepFace worker failed'}"
            )
        else:
            self.status.emit("Analysis complete")

    def _persist_error(self, generation: int, message: str) -> None:
        logger.error("Analysis persistence callback failed for %s: %s", self._root, message)
        diagnostic(f"Analysis persistence failed root={self._root!r}: {message}")
        if generation == self._generation:
            self.failed.emit(f"Could not save analysis results: {message}")
        else:
            self.catalogue_changed.emit()

    def _process_error(self, error) -> None:
        logger.error("DeepFace worker process error: %s", error)
        diagnostic(f"DeepFace worker process error error={error!r}")
        if self._starting_after_finish:
            return
        if (
            self._active
            and self._process_generation == self._generation
            and not self._shutdown_requested
        ):
            self._active = False
            self.failed.emit(f"DeepFace worker error: {error}")

    def _process_finished(self, _exit_code, _exit_status) -> None:
        diagnostic(f"DeepFace worker process finished exit_code={_exit_code} status={_exit_status!r}")
        if (
            self._starting_after_finish
            and self._active
            and self._process_generation == self._generation
            and not self._shutdown_requested
        ):
            self._starting_after_finish = False
            self._start_process_when_available(self._generation)
        elif (
            self._active
            and self._process_generation == self._generation
            and self._targets
        ):
            logger.error("DeepFace worker stopped before analysis completed")
            self._active = False
            self.failed.emit("DeepFace worker stopped before analysis completed")

    def shutdown(self) -> None:
        self._shutdown_requested = True
        self._active = False
        self._generation += 1
        if self.process.state() == QProcess.ProcessState.Running:
            diagnostic("DeepFace worker request sent: shutdown")
            self.process.write(b'{"op":"shutdown","request_id":"shutdown"}\n')
            QTimer.singleShot(1200, self.process.kill)
        elif self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
