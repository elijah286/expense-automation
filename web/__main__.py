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

    # --- Early crash logging for frozen builds (no console on Windows) ---
    def _setup_crash_logging():
        if not getattr(sys, "frozen", False):
            return
        try:
            from web.env_paths import user_data_dir as _udd
            log_dir = _udd()
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "app.log"
            logging.basicConfig(
                filename=str(log_file),
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
            logging.getLogger("expense_automator").info("App starting (frozen=%s, platform=%s)", True, sys.platform)
        except Exception:
            pass

    _setup_crash_logging()

    def _install_playwright_chromium() -> None:
        """Install Playwright Chromium without spawning the frozen executable again."""
        from playwright.__main__ import main as playwright_main

        argv_prev = sys.argv[:]
        try:
            sys.argv = [argv_prev[0], "install", "chromium"]
            # Suppress noisy download progress output (Chromium, FFmpeg, etc.)
            _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
            sys.stdout = open(os.devnull, "w")
            sys.stderr = open(os.devnull, "w")
            try:
                playwright_main()
            except SystemExit:
                pass  # playwright_main() calls sys.exit(0) on success
            finally:
                sys.stdout.close()
                sys.stderr.close()
                sys.stdout, sys.stderr = _orig_stdout, _orig_stderr
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
            _startup.set_chromium_downloading(True)

    _bootstrap_playwright_browsers()

    def _background_launch_setup() -> None:
        """Run all first-launch tasks: check for update, download if available,
        then download Chromium if needed.  Splash screen tracks progress."""
        from web import startup as _st
        from web.updater import check_for_update, download_update
        log = logging.getLogger("expense_automator")

        # --- Phase 1: Check for updates ---
        _st.set_update_checking(True)
        try:
            from web.app import _VERSION
            info = check_for_update(_VERSION)
            _st.set_update_info(info)
        except Exception as exc:
            log.warning("Update check failed: %s", exc)
            info = None
        finally:
            _st.set_update_checking(False)

        # --- Phase 2: Download & apply update if available ---
        if info and getattr(sys, "frozen", False):
            is_mac = sys.platform == "darwin"
            is_win = sys.platform == "win32"
            asset_url = info.get("macos_url", "") if is_mac else info.get("windows_url", "")
            if asset_url and (is_mac or is_win):
                _st.set_update_downloading(True)
                try:
                    def _on_progress(downloaded, total):
                        if total > 0:
                            _st.set_update_progress(downloaded / total)

                    log.info("Downloading update v%s...", info["version"])
                    installer_path = download_update(asset_url, on_progress=_on_progress)
                    _st.set_update_downloading(False)
                    _st.set_update_applying(True)

                    if is_mac:
                        from web.updater import apply_macos_update
                        log.info("Applying macOS update...")
                        apply_macos_update(installer_path)
                    else:
                        from web.updater import apply_windows_update
                        log.info("Applying Windows update...")
                        apply_windows_update(installer_path)

                    import time; time.sleep(1)
                    os._exit(0)  # Force-quit so updater script can replace the app
                except Exception as exc:
                    log.error("Auto-update failed: %s", exc)
                    _st.set_update_error(str(exc))
                finally:
                    _st.set_update_downloading(False)
                    _st.set_update_applying(False)

        # --- Phase 3: Download Chromium if needed ---
        if _st.chromium_downloading():
            try:
                log.info("Starting Chromium download...")
                _install_playwright_chromium()
                log.info("Chromium download complete.")
            except Exception as exc:
                log.error("Chromium download failed: %s", exc)
                _st.set_chromium_error(str(exc))
            finally:
                _st.set_chromium_downloading(False)

        _st.set_setup_done(True)

    _env_file, _env_example = env_file_paths()
    if is_frozen():
        user_data_dir().mkdir(parents=True, exist_ok=True)

    if not _env_file.exists() and _env_example is not None and _env_example.exists():
        shutil.copy(_env_example, _env_file)

    load_dotenv(_env_file)

    _log = logging.getLogger("expense_automator")
    _log.info("Importing web.app...")

    try:
        from web.app import _kill_existing_on_port  # noqa: E402 — triggers page registration
    except Exception:
        _log.exception("Failed to import web.app")
        raise

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

    _log.info("Starting NiceGUI server on port %d...", WEB_PORT)

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

    # Start background launch setup (update check + Chromium download).
    threading.Thread(target=_background_launch_setup, daemon=True).start()

    try:
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
                host="127.0.0.1",
            )
    except Exception:
        _log.exception("ui.run() crashed")
        raise
