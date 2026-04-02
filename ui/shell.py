"""Main window: header, workflow chips, notebook."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def build_main_shell(app) -> None:
    import receipt_automation_ui as rui

    from ui.tabs import activity, documents, expense_report, expense_types, settings_tab

    container = ttk.Frame(app.root, padding=12)
    container.pack(fill=tk.BOTH, expand=True)

    header_row = ttk.Frame(container)
    header_row.pack(fill=tk.X)

    ttk.Label(header_row, text="Expense automator", font=("SF Pro Text", 17, "bold")).pack(side=tk.LEFT)

    chk_outer = ttk.Frame(header_row)
    chk_outer.pack(side=tk.LEFT, padx=(20, 12))
    ttk.Label(chk_outer, text="State:", font=("SF Pro Text", 9), foreground="#666").pack(
        side=tk.LEFT, padx=(0, 6)
    )
    chk_inner = ttk.Frame(chk_outer)
    chk_inner.pack(side=tk.LEFT)
    for key, short in rui.WORKFLOW_CHECKLIST:
        cell = ttk.Frame(chk_inner)
        cell.pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(cell, text=short, font=("SF Pro Text", 8), foreground="#888").pack()
        lb = ttk.Label(cell, text="·", width=2, font=("SF Pro Text", 10, "bold"), foreground="#bbb")
        lb.pack()
        app._workflow_chk_labels[key] = lb

    ttk.Button(header_row, text="Settings", command=app.focus_settings_tab).pack(side=tk.RIGHT)

    nb = ttk.Notebook(container)
    nb.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
    app.main_notebook = nb

    f_docs = ttk.Frame(nb, padding=8)
    nb.add(f_docs, text="Documents")
    documents.build_documents_tab(app, f_docs)
    app._frame_documents = f_docs

    f_report = ttk.Frame(nb, padding=8)
    nb.add(f_report, text="Expense report")
    expense_report.build_expense_report_tab(app, f_report)
    app._frame_expense_report = f_report

    f_types = ttk.Frame(nb, padding=8)
    nb.add(f_types, text="Expense types")
    expense_types.build_expense_types_tab(app, f_types)
    app._frame_expense_types = f_types

    f_act = ttk.Frame(nb, padding=8)
    nb.add(f_act, text="Activity")
    activity.build_activity_tab(app, f_act)

    f_set = ttk.Frame(nb, padding=8)
    nb.add(f_set, text="Settings")
    settings_tab.build_settings_tab(app, f_set)
    app._frame_settings = f_set

    nb.bind("<<NotebookTabChanged>>", app._on_notebook_tab_changed)

    status_outer = ttk.Frame(container)
    status_outer.pack(fill=tk.X, pady=(8, 0))
    ttk.Label(status_outer, text="Status", font=("SF Pro Text", 9), foreground="#666").pack(anchor=tk.W)
    app._status_bar = ttk.Label(
        status_outer,
        text="Ready.",
        anchor=tk.W,
        font=("SF Pro Text", 10),
        foreground="#222",
        wraplength=960,
        justify=tk.LEFT,
    )
    app._status_bar.pack(fill=tk.X, pady=(2, 0))
