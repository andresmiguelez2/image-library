"""Explicit diagnostics for the UI analysis pipeline."""

from __future__ import annotations

import sys
import traceback
from typing import TextIO


def diagnostic(message: object, *, stream: TextIO | None = None) -> None:
    print(
        f"[imagelib diagnostic] {message}",
        file=stream if stream is not None else sys.stderr,
        flush=True,
    )


def diagnostic_exception(message: str, error: BaseException) -> None:
    diagnostic(f"{message}: {type(error).__name__}: {error}\n{traceback.format_exc()}")
