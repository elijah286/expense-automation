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
    try:
        root = Path(__file__).resolve().parent.parent
        return root.joinpath("VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


@app.route("/")
def index():
    return render_template("index.html", app_version=_read_version())


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
