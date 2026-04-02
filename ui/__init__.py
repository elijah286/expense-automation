"""Modular Tkinter layout for Expense automator."""

from gui_runtime_guard import ensure_gui_runtime_ok

ensure_gui_runtime_ok()

from ui.shell import build_main_shell

__all__ = ["build_main_shell"]
