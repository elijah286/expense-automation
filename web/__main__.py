"""Allow running with: python3 -m web"""

from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import sys
import threading
from pathlib import Path

if __name__ == "__main__":
    multiprocessing.freeze_support()

    def _install_playwright_chromium() -> None:
        """Install Playwright Chromium without spawning the frozen executable again."""
        from playwright.__main__ import main as playwright_main

        argv_prev = sys.argv[:]
        try:
            sys.argv = [argv_prev[0], "install", "chromium"]
            playwright_main()
        finally:
            sys.argv = argv_prev

    # --install-chromium: download Chromium and exit (used by installers).
    if "--install-chromium" in sys.argv:
        from web.env_paths import user_data_dir as _udd

        _dest = _udd() / "ms-playwright"
        _dest.mkdir(parents=True, exist_ok=True)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_dest)
        _install_playwright_chromium()
        sys.exit(0)

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
        # Check that the *correct* Chromium revision exists, not just any old one.
        try:
            from playwright.sync_api import sync_playwright as _sw
            _p = _sw().start()
            _expected_exe = Path(_p.chromium.executable_path)
            _p.stop()
            _need_install = not _expected_exe.exists()
        except Exception:
            _need_install = not any(browsers_dir.glob("chromium-*"))

        if _need_install:
            log = logging.getLogger("expense_automator")
            log.info("Chromium not found or outdated — will download in background...")
            from web import startup as _startup
            _startup.set_downloading(True)

    _bootstrap_playwright_browsers()

    def _background_chromium_download() -> None:
        """Download Chromium in background so the UI can launch immediately."""
        from web import startup as _startup
        log = logging.getLogger("expense_automator")
        try:
            log.info("Starting background Chromium download...")
            _install_playwright_chromium()
            log.info("Chromium download complete.")
        except Exception as exc:
            log.error("Chromium download failed: %s", exc)
            _startup.set_error(str(exc))
        finally:
            _startup.set_downloading(False)

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

    # Start background Chromium download if needed (UI launches immediately).
    from web import startup as _startup_state
    if _startup_state.is_downloading():
        threading.Thread(target=_background_chromium_download, daemon=True).start()

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
