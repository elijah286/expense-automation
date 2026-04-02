"""Block interpreters known to abort() inside Tk on macOS before tkinter is imported."""

from __future__ import annotations

import os
import sys

# Xcode / CLT Pythons link against ancient system Tcl/Tk; TkpInit often calls Tcl_Panic → abort.
_BAD_DARWIN_PYTHON_MARKERS = (
    "Python3.framework",
    "Xcode.app/Contents/Developer/Library/Frameworks/Python",
    "CommandLineTools/Library/Frameworks/Python3.framework",
)


def ensure_gui_runtime_ok() -> None:
    if os.environ.get("EXPENSE_AUTOMATOR_SKIP_GUI_RUNTIME_GUARD"):
        return
    if sys.platform != "darwin":
        return
    exe = os.path.realpath(sys.executable)
    if any(marker in exe for marker in _BAD_DARWIN_PYTHON_MARKERS):
        print(
            "Refusing to start: this interpreter's Tk/tkinter aborts on macOS (Apple/Xcode Python).\n"
            f"Interpreter: {sys.executable}\n"
            "Run with the project virtualenv instead, for example:\n"
            "  .venv-rpa/bin/python receipt_automation_ui.py\n"
            "In Cursor/VS Code, set Python: default interpreter to .venv-rpa/bin/python.",
            file=sys.stderr,
        )
        sys.exit(2)
    # Reduces noisy deprecation paths; harmless if unused.
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
