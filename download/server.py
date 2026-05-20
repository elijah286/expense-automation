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
_release_cache: dict[str, object] = {"version": None, "ts": 0}
_CACHE_TTL = 300  # 5 minutes


def _read_version() -> str:
    """Return the version of the latest published GitHub release."""
    now = time.time()
    if _release_cache["version"] and (now - _release_cache["ts"]) < _CACHE_TTL:
        return _release_cache["version"]

    # Try GitHub API for the actual latest release
    try:
        url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            tag = data.get("tag_name", "").lstrip("v")
            if tag:
                _release_cache["version"] = tag
                _release_cache["ts"] = now
                return tag
    except Exception:
        pass

    # If cached value exists, return it even if stale
    if _release_cache["version"]:
        return _release_cache["version"]

    # Last resort: VERSION file
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
    return render_template("index.html", app_version=_read_version())


@app.route("/macos-setup")
def macos_setup():
    """Show macOS setup instructions and start the download automatically."""
    return render_template("macos-setup.html", app_version=_read_version())


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
