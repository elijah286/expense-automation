"""Resolve config paths for development vs PyInstaller-frozen desktop builds."""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root_dev() -> Path:
    """Source checkout root (directory containing `web/`)."""
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Where settings and caches live (always ~/.expense-automator)."""
    return Path.home() / ".expense-automator"


def env_file_paths() -> tuple[Path, Path | None]:
    """
    Returns (target .env path, bundled .env.example to copy from, or None).

    Frozen builds keep .env next to other app data under the home directory
    because the app bundle is read-only.
    """
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        root = user_data_dir()
        bundled_example = Path(sys._MEIPASS) / ".env.example"
        return root / ".env", bundled_example if bundled_example.exists() else None
    root = project_root_dev()
    example = root / ".env.example"
    return root / ".env", example if example.exists() else None
