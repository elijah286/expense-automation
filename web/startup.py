"""Shared state for first-launch Chromium download progress."""

from __future__ import annotations

import threading

_lock = threading.Lock()
_downloading = False
_error: str | None = None


def is_downloading() -> bool:
    with _lock:
        return _downloading


def download_error() -> str | None:
    with _lock:
        return _error


def set_downloading(val: bool) -> None:
    global _downloading
    with _lock:
        _downloading = val


def set_error(err: str | None) -> None:
    global _error
    with _lock:
        _error = err
