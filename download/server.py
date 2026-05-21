"""
Expense Automator — Download landing page.

Lightweight Flask server that serves a public download page.
Deployed on Railway.
"""

import os
import time
from pathlib import Path
from flask import Flask, render_template
import urllib.request
import json

app = Flask(__name__)

_GITHUB_REPO = "elijah286/expense-automation"
_release_cache: dict[str, object] = {"version": None, "macos_url": None, "windows_url": None, "ts": 0}
_CACHE_TTL = 300  # 5 minutes


def _fetch_latest_release() -> dict[str, str]:
    """Return version + asset URLs from the latest GitHub release (cached)."""
    now = time.time()
    if _release_cache["version"] and (now - _release_cache["ts"]) < _CACHE_TTL:
        return _release_cache

    try:
        url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            tag = data.get("tag_name", "").lstrip("v")
            macos_url = ""
            windows_url = ""
            for asset in data.get("assets", []):
                name = asset.get("name", "")
                dl = asset.get("browser_download_url", "")
                if name.endswith(".dmg"):
                    macos_url = dl
                elif name.endswith(".exe"):
                    windows_url = dl
            if tag:
                _release_cache["version"] = tag
                _release_cache["macos_url"] = macos_url
                _release_cache["windows_url"] = windows_url
                _release_cache["ts"] = now
    except Exception:
        pass

    return _release_cache


def _read_version() -> str:
    """Return the version of the latest published GitHub release."""
    info = _fetch_latest_release()
    if info["version"]:
        return info["version"]
    return _read_version_file()


def _read_version_file() -> str:
    if env_version := os.environ.get("APP_VERSION"):
        return env_version.strip()

    possible_paths = [
        Path(__file__).resolve().parent.parent / "VERSION",
        Path("/app/VERSION"),
        Path.cwd() / "VERSION",
    ]

    for path in possible_paths:
        try:
            if path.is_file():
                return path.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    return "unknown"


@app.route("/")
def index():
    info = _fetch_latest_release()
    return render_template(
        "index.html",
        app_version=_read_version(),
        macos_url=info.get("macos_url") or "",
        windows_url=info.get("windows_url") or "",
    )


@app.route("/macos-setup")
def macos_setup():
    """Show macOS setup instructions and start the download automatically."""
    info = _fetch_latest_release()
    return render_template(
        "macos-setup.html",
        app_version=_read_version(),
        macos_url=info.get("macos_url") or "",
    )


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
