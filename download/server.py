"""
Expense Automator — Download landing page.

Lightweight Flask server that serves a public download page.
Deployed on Railway.
"""

import os
from pathlib import Path
from flask import Flask, render_template

app = Flask(__name__)


def _read_version() -> str:
    # Try environment variable first (set during build).
    if env_version := os.environ.get("APP_VERSION"):
        return env_version.strip()
    
    # Try multiple file paths.
    possible_paths = [
        Path(__file__).resolve().parent.parent / "VERSION",  # Development
        Path("/app/VERSION"),  # Docker/Railway container root
        Path.cwd() / "VERSION",  # Current working directory
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
