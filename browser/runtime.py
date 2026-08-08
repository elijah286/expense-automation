from __future__ import annotations

from contextlib import contextmanager
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import playwright


def _browser_cache_dir(executable: Path) -> Path:
    for directory in (executable.parent, *executable.parents):
        if directory.name.startswith("chromium-"):
            return directory.parent
    return executable.parent


@contextmanager
def _chromium_install_lock(cache_dir: Path) -> Iterator[None]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / ".chromium-install.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.write(b"0")
            lock_file.flush()
            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.25)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ensure_chromium_executable(executable_path: str) -> str:
    executable = Path(executable_path)
    if executable.is_file():
        return str(executable)

    with _chromium_install_lock(_browser_cache_dir(executable)):
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
            subprocess.run(command, check=True, text=True, capture_output=True)
        except OSError as exc:
            raise RuntimeError(
                "Chromium is required for browser automation but its installer could not start: "
                f"{exc}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(
                "Chromium is required for browser automation but could not be installed. "
                f"{detail[-1200:]}"
            ) from exc

    if not executable.is_file():
        raise RuntimeError(
            "Playwright installed Chromium, but its expected executable is still missing: "
            f"{executable}"
        )
    return str(executable)