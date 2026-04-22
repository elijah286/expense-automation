"""Allow running with: python3 -m web"""

from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()

from dotenv import load_dotenv

from web.env_paths import env_file_paths, is_frozen, user_data_dir


def _bootstrap_playwright_browsers() -> None:
    """Ensure Playwright Chromium is available, downloading on first launch if needed."""
    browsers_dir = user_data_dir() / "ms-playwright"
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers_dir))

    if not is_frozen():
        return

    # Also check bundled locations (legacy installs that still ship Chromium).
    exe = Path(sys.executable).resolve()
    bundled: list[Path] = [exe.parent / "ms-playwright"]
    if sys.platform == "darwin" and exe.parent.name == "MacOS":
        bundled.insert(0, exe.parent.parent / "Resources" / "ms-playwright")
    if hasattr(sys, "_MEIPASS"):
        bundled.append(Path(sys._MEIPASS) / "ms-playwright")
    for p in bundled:
        if p.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(p)
            return

    # No bundled Chromium — download on demand into ~/.expense-automator/ms-playwright.
    if not any(browsers_dir.glob("chromium-*")):
        log = logging.getLogger("expense_automator")
        log.info("Chromium not found — downloading via Playwright (one-time setup)...")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers_dir)},
            check=True,
        )


_bootstrap_playwright_browsers()

_env_file, _env_example = env_file_paths()
if is_frozen():
    user_data_dir().mkdir(parents=True, exist_ok=True)

if not _env_file.exists() and _env_example is not None and _env_example.exists():
    shutil.copy(_env_example, _env_file)

load_dotenv(_env_file)

from web.app import _kill_existing_on_port  # noqa: E402 — triggers page registration
from web.macos_single_process_webview import (  # noqa: E402
    patch_nicegui_server_run,
    patch_nicegui_skip_process_pool_on_frozen_macos,
    use_embedded_webview,
)

patch_nicegui_skip_process_pool_on_frozen_macos()
patch_nicegui_server_run()

from nicegui import ui  # noqa: E402

WEB_PORT = 8080
_kill_existing_on_port(WEB_PORT)

# macOS .app (frozen): embedded pywebview on main thread + server in a thread → one Dock
# icon, no Safari. Override with EXPENSE_AUTOMATOR_USE_BROWSER=1 for Safari.
# NiceGUI native=True uses a second process for webview → duplicate Dock icons.
_use_native = os.environ.get("EXPENSE_AUTOMATOR_NATIVE", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

_run_kw: dict = {
    "title": "Expense Automator",
    "port": WEB_PORT,
    "reload": False,
    "favicon": "💰",
}

if use_embedded_webview():
    ui.run(
        **_run_kw,
        show=False,
        native=False,
        host="127.0.0.1",
    )
elif _use_native:
    ui.run(
        **_run_kw,
        native=True,
        window_size=(1280, 800),
    )
else:
    ui.run(
        **_run_kw,
        show=True,
    )
