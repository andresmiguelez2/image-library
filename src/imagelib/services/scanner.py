"""Two-stage ingest pipeline.

Stage 1 (this module): walk watched dirs, hash files, extract EXIF/GPS metadata,
generate thumbnails, and flip unchanged images to ''indexed'' so the UI can show
them immediately.
Stage 2 (analyser, future): deepface detection/embeddings -> ''analysed''.

Incremental rule: an image whose content_hash is unchanged is skipped entirely
(the DB status column is what prevents reprocessing).
"""

from __future__ import annotations

from pathlib import Path


def scan_watched_dirs() -> None:
    """Walk configured directories and index new/changed images.

    Stub: pipeline wiring lands in a later pass.
    """
    # TODO(step 2): for each watched dir, rglob image extensions,
    # hash via hashlib.sha256, compare to DB content_hash, extract EXIF/GPS,
    # write thumbnail to thumbnail_dir, upsert rows, set status='indexed'.
    raise NotImplementedError


def thumbnail_path_for(image_path: Path) -> Path:
    """Destination for an image's cached thumbnail."""
    # TODO(step 2): deterministic path under config thumbnail_dir keyed by hash.
    raise NotImplementedError