"""Shared launch state — tracks update check, update download, and Chromium setup."""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()

# Chromium download
_chromium_downloading = False
_chromium_error: str | None = None

# Update check / download
_update_checking = False
_update_downloading = False
_update_info: dict[str, Any] | None = None
_update_progress: float = 0.0  # 0..1
_update_error: str | None = None
_update_applying = False

# Overall
_setup_done = False


# --- Chromium ----------------------------------------------------------

def chromium_downloading() -> bool:
    with _lock:
        return _chromium_downloading

def chromium_error() -> str | None:
    with _lock:
        return _chromium_error

def set_chromium_downloading(val: bool) -> None:
    global _chromium_downloading
    with _lock:
        _chromium_downloading = val

def set_chromium_error(err: str | None) -> None:
    global _chromium_error
    with _lock:
        _chromium_error = err


# --- Compat aliases (used by existing __main__.py code) ----------------

def is_downloading() -> bool:
    return chromium_downloading()

def download_error() -> str | None:
    return chromium_error()

def set_downloading(val: bool) -> None:
    set_chromium_downloading(val)

def set_error(err: str | None) -> None:
    set_chromium_error(err)


# --- Update ------------------------------------------------------------

def update_checking() -> bool:
    with _lock:
        return _update_checking

def set_update_checking(val: bool) -> None:
    global _update_checking
    with _lock:
        _update_checking = val

def update_downloading() -> bool:
    with _lock:
        return _update_downloading

def set_update_downloading(val: bool) -> None:
    global _update_downloading
    with _lock:
        _update_downloading = val

def update_info() -> dict[str, Any] | None:
    with _lock:
        return _update_info

def set_update_info(info: dict[str, Any] | None) -> None:
    global _update_info
    with _lock:
        _update_info = info

def update_progress() -> float:
    with _lock:
        return _update_progress

def set_update_progress(pct: float) -> None:
    global _update_progress
    with _lock:
        _update_progress = pct

def update_error() -> str | None:
    with _lock:
        return _update_error

def set_update_error(err: str | None) -> None:
    global _update_error
    with _lock:
        _update_error = err

def update_applying() -> bool:
    with _lock:
        return _update_applying

def set_update_applying(val: bool) -> None:
    global _update_applying
    with _lock:
        _update_applying = val


# --- Overall splash needed? --------------------------------------------

def splash_active() -> bool:
    """True while any launch task is still running."""
    with _lock:
        return _chromium_downloading or _update_checking or _update_downloading or _update_applying

def setup_done() -> bool:
    with _lock:
        return _setup_done

def set_setup_done(val: bool) -> None:
    global _setup_done
    with _lock:
        _setup_done = val

def current_status() -> str:
    """Human-readable status for the splash screen."""
    with _lock:
        if _update_checking:
            return "Checking for updates\u2026"
        if _update_downloading:
            pct = int(_update_progress * 100)
            return f"Downloading update\u2026 {pct}%"
        if _update_applying:
            return "Installing update\u2026"
        if _chromium_downloading:
            return "Setting up browser engine\u2026"
    return "Starting\u2026"
