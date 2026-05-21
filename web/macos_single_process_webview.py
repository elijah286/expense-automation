"""
Run pywebview on the main thread and NiceGUI/uvicorn in a worker thread so the app
looks like a native desktop application (no browser chrome).

macOS: WKWebView (one Dock icon, no Safari chrome).
Windows: Edge WebView2 (native window, no browser tabs).

NiceGUI's built-in native=True uses multiprocessing for pywebview, which duplicates
Dock icons on macOS and re-executes the frozen exe on Windows.

Set EXPENSE_AUTOMATOR_USE_BROWSER=1 to force opening in the default browser instead.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Callable

_SERVER_RUN_PATCHED = False
_POOL_PATCHED = False


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _is_frozen_macos() -> bool:
    if sys.platform != "darwin":
        return False
    return _is_frozen()


def patch_nicegui_skip_process_pool_on_frozen_macos() -> None:
    """NiceGUI's run.setup() creates a ProcessPoolExecutor.  On frozen builds (PyInstaller)
    this spawns workers that re-execute the frozen exe, causing duplicate Dock icons on
    macOS and re-entrant startup that kills the main process on Windows.

    We do not use NiceGUI's run.cpu_bound in this project; skipping the process pool
    avoids these problems entirely.
    """
    global _POOL_PATCHED
    if not _is_frozen() or _POOL_PATCHED:
        return

    import nicegui.run as ng_run

    def _setup_without_process_pool() -> None:
        ng_run.process_pool = None

    ng_run.setup = _setup_without_process_pool  # type: ignore[assignment]
    _POOL_PATCHED = True


def use_embedded_webview() -> bool:
    """Use pywebview on macOS only. Windows uses Edge app mode instead."""
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

        def _run_all() -> None:
            _original(self, sockets)

        thread = threading.Thread(target=_run_all, name="nicegui-uvicorn", daemon=False)
        thread.start()

        host = os.environ.get("NICEGUI_HOST", "127.0.0.1")
        port = int(os.environ.get("NICEGUI_PORT", "8587"))
        protocol = os.environ.get("NICEGUI_PROTOCOL", "http")
        connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        base_url = f"{protocol}://{connect_host}:{port}"

        # Wait for the server to respond with HTTP 200, not just open port
        import urllib.request
        import urllib.error

        deadline = time.time() + 120.0
        while time.time() < deadline:
            try:
                resp = urllib.request.urlopen(f"{base_url}/", timeout=2)
                if resp.status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            self.should_exit = True
            thread.join(timeout=5)
            raise RuntimeError("NiceGUI server did not respond on the expected port.")

        import webview

        # On Windows, use persistent storage in user data dir to avoid temp dir issues
        wv_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            from web.env_paths import user_data_dir
            storage = user_data_dir() / "webview_data"
            storage.mkdir(parents=True, exist_ok=True)
            wv_kwargs["storage_path"] = str(storage)

        webview.create_window(
            title="Expense Automator",
            url=f"{base_url}/",
            width=1280,
            height=800,
        )
        webview.start(private_mode=False, **wv_kwargs)
        self.should_exit = True
        thread.join(timeout=60)

    ng_server.Server.run = _patched  # type: ignore[method-assign]
    _SERVER_RUN_PATCHED = True
