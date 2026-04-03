"""Main window: persistent run bar + stage-based workflow shell."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def build_main_shell(app) -> None:
    import receipt_automation_ui as rui

    from ui.tabs import expense_types, settings_tab
    from ui.workflow import build_workflow_stage_shell

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

    run_strip = ttk.Frame(container)
    run_strip.pack(fill=tk.X, pady=(8, 0))
    app._global_run_phase_var = tk.StringVar(value="Phase: Idle")
    app._global_run_progress_var = tk.StringVar(value="Progress: 0%")
    app._global_run_attention_var = tk.StringVar(value="Attention: None")
    app._global_run_message_var = tk.StringVar(value="Waiting for next action.")
    ttk.Label(run_strip, textvariable=app._global_run_phase_var, foreground="#0b57d0").pack(
        side=tk.LEFT, padx=(0, 12)
    )
    ttk.Label(run_strip, textvariable=app._global_run_progress_var, foreground="#333").pack(
        side=tk.LEFT, padx=(0, 12)
    )
    ttk.Label(run_strip, textvariable=app._global_run_attention_var, foreground="#b06000").pack(
        side=tk.LEFT
    )
    app._global_run_progress = ttk.Progressbar(
        container, orient=tk.HORIZONTAL, mode="determinate", maximum=100
    )
    app._global_run_progress.pack(fill=tk.X, pady=(4, 0))
    ttk.Label(
        container,
        textvariable=app._global_run_message_var,
        foreground="#555",
        wraplength=960,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(2, 0))

    nb = ttk.Notebook(container)
    nb.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
    app.main_notebook = nb

    f_workflow = ttk.Frame(nb, padding=8)
    nb.add(f_workflow, text="Workflow")
    build_workflow_stage_shell(app, f_workflow)
    app._frame_workflow = f_workflow

    f_set = ttk.Frame(nb, padding=8)
    nb.add(f_set, text="Settings")
    settings_tab.build_settings_tab(app, f_set)
    app._frame_settings = f_set

    f_class = ttk.Frame(nb, padding=8)
    nb.add(f_class, text="Vendor Classification")
    ttk.Label(
        f_class,
        text="Vendor Classification",
        font=("SF Pro Text", 13, "bold"),
    ).pack(anchor=tk.W, pady=(0, 6))
    ttk.Label(
        f_class,
        text=(
            "Define the Expense Type to use whenever a merchant name matches exactly. "
            "New merchants discovered during scanning are added automatically and classified by the LLM. "
            "You can override any classification here."
        ),
        wraplength=980,
        justify=tk.LEFT,
        foreground="#555",
    ).pack(anchor=tk.W, pady=(0, 8))
    expense_types.build_expense_types_tab(app, f_class)
    app._frame_vendor_classification = f_class

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
