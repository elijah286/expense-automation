"""Allow running with: python3 -m web"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import sys
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()

from dotenv import load_dotenv

from web.env_paths import env_file_paths, is_frozen, user_data_dir


def _bootstrap_playwright_browsers() -> None:
    """Point Playwright at bundled Chromium (copied next to the exe / in .app Resources)."""
    if not is_frozen():
        return
    exe = Path(sys.executable).resolve()
    candidates: list[Path] = [
        exe.parent / "ms-playwright",
    ]
    if sys.platform == "darwin" and exe.parent.name == "MacOS":
        candidates.insert(0, exe.parent.parent / "Resources" / "ms-playwright")
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "ms-playwright")
    for p in candidates:
        if p.is_dir():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(p))
            return


_bootstrap_playwright_browsers()

_env_file, _env_example = env_file_paths()
if is_frozen():
    user_data_dir().mkdir(parents=True, exist_ok=True)

if not _env_file.exists() and _env_example is not None and _env_example.exists():
    shutil.copy(_env_example, _env_file)

load_dotenv(_env_file)

try:
    import keychain_credentials

    keychain_credentials.warm_up()
except Exception:
    pass

from web.app import _kill_existing_on_port  # noqa: E402 — triggers page registration
from nicegui import ui  # noqa: E402

WEB_PORT = 8080
_kill_existing_on_port(WEB_PORT)

# Native window (pywebview / WKWebView on macOS) instead of the default browser tab.
# Set EXPENSE_AUTOMATOR_USE_BROWSER=1 to open in the system browser (e.g. devtools).
_use_browser = os.environ.get("EXPENSE_AUTOMATOR_USE_BROWSER", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

if _use_browser:
    ui.run(
        title="Expense Automator",
        port=WEB_PORT,
        reload=False,
        show=True,
        favicon="💰",
    )
else:
    ui.run(
        title="Expense Automator",
        port=WEB_PORT,
        reload=False,
        native=True,
        window_size=(1280, 800),
        favicon="💰",
    )
