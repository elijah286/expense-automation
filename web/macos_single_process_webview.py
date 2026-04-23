"""
macOS: run WKWebView (pywebview) on the process main thread and NiceGUI/uvicorn in a
worker thread so the app stays responsive (one Dock icon, no Safari chrome).

NiceGUI's built-in native=True uses multiprocessing for pywebview, which duplicates
Dock icons. Opening the system browser avoids that but looks like Safari.

Set EXPENSE_AUTOMATOR_USE_BROWSER=1 to force Safari + single process (legacy).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Callable

_SERVER_RUN_PATCHED = False
_POOL_PATCHED = False


def _is_frozen_macos() -> bool:
    if sys.platform != "darwin":
        return False
    return bool(getattr(sys, "frozen", False))


def patch_nicegui_skip_process_pool_on_frozen_macos() -> None:
    """NiceGUI's run.setup() builds a ProcessPoolExecutor, which allocates multiprocessing
    queues. On macOS those spawn a resource_tracker helper that re-runs the frozen .app
    and often shows a second, non-responsive Dock icon (PyInstaller + multiprocessing).

    We do not use NiceGUI's run.cpu_bound in this project; skipping the process pool
    avoids the extra process.
    """
    global _POOL_PATCHED
    if not _is_frozen_macos() or _POOL_PATCHED:
        return

    import nicegui.run as ng_run

    def _setup_without_process_pool() -> None:
        ng_run.process_pool = None

    ng_run.setup = _setup_without_process_pool  # type: ignore[assignment]
    _POOL_PATCHED = True


def use_embedded_webview() -> bool:
    if sys.platform != "darwin":
        return False
    if os.environ.get("EXPENSE_AUTOMATOR_NATIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if os.environ.get("EXPENSE_AUTOMATOR_USE_BROWSER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    from web.env_paths import is_frozen

    if is_frozen():
        return True
    return os.environ.get("EXPENSE_AUTOMATOR_EMBEDDED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def patch_nicegui_server_run() -> None:
    global _SERVER_RUN_PATCHED
    if sys.platform != "darwin" or _SERVER_RUN_PATCHED:
        return

    import nicegui.server as ng_server

    _original: Callable[..., Any] = ng_server.Server.run

    def _patched(self: Any, sockets: Any = None) -> None:
        if not use_embedded_webview():
            return _original(self, sockets)

        from nicegui import helpers

        def _run_all() -> None:
            _original(self, sockets)

        thread = threading.Thread(target=_run_all, name="nicegui-uvicorn", daemon=False)
        thread.start()

        host = os.environ.get("NICEGUI_HOST", "127.0.0.1")
        port = int(os.environ.get("NICEGUI_PORT", "8080"))
        protocol = os.environ.get("NICEGUI_PROTOCOL", "http")
        connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host

        deadline = time.time() + 120.0
        while time.time() < deadline:
            if helpers.is_port_open(connect_host, port):
                break
            time.sleep(0.05)
        else:
            self.should_exit = True
            thread.join(timeout=5)
            raise RuntimeError("NiceGUI server did not accept connections on the expected port.")

        import webview

        webview.create_window(
            title="Expense Automator",
            url=f"{protocol}://{connect_host}:{port}/",
            width=1280,
            height=800,
        )
        webview.start()
        self.should_exit = True
        thread.join(timeout=60)

    ng_server.Server.run = _patched  # type: ignore[method-assign]
    _SERVER_RUN_PATCHED = True
