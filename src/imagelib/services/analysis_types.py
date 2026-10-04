"""Shared result types for face analysis services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AnalysisReport:
    discovered: int = 0
    analysed: int = 0
    faces: int = 0
    errors: int = 0
    results: tuple[WorkerAnalysisResult, ...] = ()


@dataclass(frozen=True)
class AnalysisTarget:
    """Stable data passed from catalog selection to a worker coordinator."""

    id: int
    path: str
    content_hash: str
    status: str


@dataclass(frozen=True)
class WorkerAnalysisResult:
    """Outcome of persisting one response from the long-lived worker."""

    image_id: int
    accepted: bool
    status: str
    face_count: int
    content_hash: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ClusterReport:
    """Summary returned after rebuilding all person clusters."""

    faces: int
    clustered_faces: int
    persons: int
    named_persons: int


@dataclass(frozen=True)
class WorkerBatchReport:
    """Typed result for a coordinator batch followed by reclustering."""

    targets: tuple[AnalysisTarget, ...]
    results: tuple[WorkerAnalysisResult, ...]
    clusters: ClusterReport


@dataclass(frozen=True)
class GroupLabelResult:
    """Named identity and face count after resolving one current face group."""

    person_id: int
    person_name: str
    face_count: int


@dataclass(frozen=True)
class FaceMatchResolutionResult:
    """Result of accepting or rejecting one group-to-person proposal."""

    face_id: int
    target_person_id: int
    status: str
    group_face_count: int
