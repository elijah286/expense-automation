from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import playwright


def ensure_chromium_executable(executable_path: str) -> str:
    executable = Path(executable_path)
    if executable.is_file():
        return str(executable)

    driver_dir = Path(playwright.__file__).resolve().parent / "driver"
    node = next(
        (candidate for candidate in (driver_dir / "node", driver_dir / "node.exe") if candidate.is_file()),
        None,
    )
    cli = driver_dir / "package" / "cli.js"
    command = (
        [str(node), str(cli), "install", "chromium"]
        if node is not None and cli.is_file()
        else [sys.executable, "-m", "playwright", "install", "chromium"]
    )

    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Chromium is required for browser automation but could not be "
            "installed automatically. Check your internet connection and try again."
        ) from exc

    if not executable.is_file():
        raise RuntimeError(
            "Playwright installed Chromium, but its expected executable is still missing: "
            f"{executable}"
        )
    return str(executable)