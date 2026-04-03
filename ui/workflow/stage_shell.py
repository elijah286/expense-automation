from __future__ import annotations

import tkinter as tk
from tkinter import ttk


_STAGES: list[tuple[str, str]] = [
    ("dashboard", "Dashboard"),
    ("documents", "Documents"),
    ("oracle", "Oracle Transactions"),
    ("matching", "Matching Workspace"),
    ("review", "Final Review"),
    ("submission", "Submit"),
]

_REPORT_STATUS_STEPS: list[tuple[str, str]] = [
    ("docs", "Docs"),
    ("trans", "Trans"),
    ("match", "Match"),
    ("submit", "Submit"),
]


def build_workflow_stage_shell(app, parent: ttk.Frame) -> None:
    from ui.tabs import activity, documents, expense_report

    root = ttk.Frame(parent)
    root.pack(fill=tk.BOTH, expand=True)

    rail = ttk.Frame(root)
    rail.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
    ttk.Label(rail, text="Workflow", font=("SF Pro Text", 11, "bold")).pack(anchor=tk.W, pady=(0, 6))

    main = ttk.Frame(root)
    main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

    _build_report_header(app, main)

    stage_buttons: dict[str, ttk.Button] = {}
    stage_stack = ttk.Frame(main)
    stage_stack.pack(fill=tk.BOTH, expand=True)
    stage_frames: dict[str, ttk.Frame] = {}

    for stage_key, stage_label in _STAGES:
        btn = ttk.Button(rail, text=stage_label, width=22, command=lambda k=stage_key: app.show_workflow_stage(k))
        btn.pack(anchor=tk.W, pady=2, fill=tk.X)
        stage_buttons[stage_key] = btn

        frame = ttk.Frame(stage_stack)
        frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        stage_frames[stage_key] = frame

    app._workflow_stage_frames = stage_frames
    app._workflow_stage_buttons = stage_buttons
    app._workflow_stage_key = "dashboard"

    _build_dashboard_stage(app, stage_frames["dashboard"])
    documents.build_documents_tab(app, stage_frames["documents"])
    _build_oracle_stage(app, stage_frames["oracle"])
    expense_report.build_expense_report_tab(app, stage_frames["matching"])
    _build_review_stage(app, stage_frames["review"])
    _build_submission_stage(app, stage_frames["submission"], activity)

    app.show_workflow_stage("dashboard")


def _build_report_header(app, parent: ttk.Frame) -> None:
    """Compact report-selector bar with status indicators, shown above all workflow stages."""
    header = ttk.Frame(parent)
    header.pack(fill=tk.X, pady=(0, 2))

    ttk.Label(
        header, text="Report", font=("SF Pro Text", 10, "bold"), foreground="#555"
    ).pack(side=tk.LEFT, padx=(0, 6))

    app._matching_report_var = tk.StringVar(value="All (no filter)")
    app._matching_report_combo = ttk.Combobox(
        header,
        textvariable=app._matching_report_var,
        state="readonly",
        width=34,
        font=("SF Pro Text", 10),
    )
    app._matching_report_combo.pack(side=tk.LEFT, padx=(0, 6))
    app._matching_report_combo.bind(
        "<<ComboboxSelected>>", lambda _e: app._on_matching_report_selected()
    )
    app._matching_report_id_map: dict[str, str | None] = {}

    ttk.Button(
        header, text="+ New", command=app.on_create_new_report
    ).pack(side=tk.LEFT, padx=(0, 4))
    ttk.Button(
        header, text="Manage", command=lambda: app.show_workflow_stage("submission")
    ).pack(side=tk.LEFT)

    status_frame = ttk.Frame(header)
    status_frame.pack(side=tk.RIGHT, padx=(12, 0))
    app._report_header_status_dots: dict[str, ttk.Label] = {}
    for step_key, step_label in _REPORT_STATUS_STEPS:
        cell = ttk.Frame(status_frame)
        cell.pack(side=tk.LEFT, padx=(0, 10))
        dot = ttk.Label(
            cell, text="○", font=("SF Pro Text", 11), foreground="#ccc"
        )
        dot.pack(side=tk.LEFT, padx=(0, 2))
        ttk.Label(
            cell, text=step_label, font=("SF Pro Text", 9), foreground="#888"
        ).pack(side=tk.LEFT)
        app._report_header_status_dots[step_key] = dot

    ttk.Separator(parent, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(2, 6))


def _build_dashboard_stage(app, parent: ttk.Frame) -> None:
    ttk.Label(parent, text="Run Dashboard", font=("SF Pro Text", 13, "bold")).pack(anchor=tk.W, pady=(0, 8))
    app._workflow_dashboard_kpi_var = tk.StringVar(value="Transactions: 0 | Matched: 0 | Unmatched: 0")
    app._workflow_dashboard_ready_var = tk.StringVar(value="Readiness: Not ready")
    app._workflow_dashboard_next_var = tk.StringVar(value="Next step: Import documents")

    ttk.Label(parent, textvariable=app._workflow_dashboard_kpi_var, foreground="#333").pack(anchor=tk.W, pady=2)
    ttk.Label(parent, textvariable=app._workflow_dashboard_ready_var, foreground="#0b57d0").pack(anchor=tk.W, pady=2)
    ttk.Label(parent, textvariable=app._workflow_dashboard_next_var, foreground="#555").pack(anchor=tk.W, pady=2)

    cta = ttk.Frame(parent)
    cta.pack(fill=tk.X, pady=(10, 0))
    ttk.Button(cta, text="Resume Workflow", command=app.on_workflow_resume).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(cta, text="Open Exceptions", command=app.on_focus_attention_items).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(cta, text="Refresh Dashboard", command=app.refresh_workflow_views).pack(side=tk.LEFT)


def _build_oracle_stage(app, parent: ttk.Frame) -> None:
    ttk.Label(parent, text="Oracle Transactions", font=("SF Pro Text", 13, "bold")).pack(anchor=tk.W, pady=(0, 8))
    app._oracle_stage_summary_var = tk.StringVar(value="No scraped lines yet.")
    ttk.Label(parent, textvariable=app._oracle_stage_summary_var, foreground="#555").pack(anchor=tk.W, pady=(0, 6))

    btns = ttk.Frame(parent)
    btns.pack(fill=tk.X, pady=(0, 8))
    ttk.Button(btns, text="Open Oracle", command=app.on_step_login).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(btns, text="Scrape Step 2", command=app.on_vpn_collect_llm_prompts).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(btns, text="Retry failed pages", command=app.on_vpn_collect_llm_prompts).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(btns, text="Refresh", command=app.refresh_oracle_transactions_view).pack(side=tk.LEFT)

    cols = ("line_id", "merchant", "date", "amount", "currency", "match_status", "class_status")
    tree = ttk.Treeview(parent, columns=cols, show="headings", height=18)
    tree.heading("line_id", text="Line")
    tree.heading("merchant", text="Vendor")
    tree.heading("date", text="Date")
    tree.heading("amount", text="Amount")
    tree.heading("currency", text="Cur")
    tree.heading("match_status", text="Match")
    tree.heading("class_status", text="Class")
    tree.column("line_id", width=80, stretch=False)
    tree.column("merchant", width=220, stretch=True)
    tree.column("date", width=110, stretch=False)
    tree.column("amount", width=90, stretch=False)
    tree.column("currency", width=55, stretch=False)
    tree.column("match_status", width=110, stretch=False)
    tree.column("class_status", width=120, stretch=False)
    tree.pack(fill=tk.BOTH, expand=True)
    app.oracle_transactions_tree = tree


def _build_review_stage(app, parent: ttk.Frame) -> None:
    ttk.Label(parent, text="Final Review", font=("SF Pro Text", 13, "bold")).pack(anchor=tk.W, pady=(0, 8))
    app._review_readiness_var = tk.StringVar(value="Not ready")
    app._review_summary_var = tk.StringVar(value="Matched: 0 | Missing: 0 | Low confidence: 0")
    ttk.Label(parent, textvariable=app._review_readiness_var, foreground="#0b57d0").pack(anchor=tk.W, pady=(0, 4))
    ttk.Label(parent, textvariable=app._review_summary_var, foreground="#555").pack(anchor=tk.W, pady=(0, 6))

    ttk.Label(parent, text="Blockers", font=("SF Pro Text", 10, "bold")).pack(anchor=tk.W, pady=(4, 2))
    app._review_blockers_list = tk.Listbox(parent, height=10)
    app._review_blockers_list.pack(fill=tk.BOTH, expand=True)

    btns = ttk.Frame(parent)
    btns.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(btns, text="Fix selected blocker", command=app.on_final_review_fix_selected).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(btns, text="Run final validation", command=app.refresh_final_review_view).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(btns, text="Submit", command=app.on_create_report).pack(side=tk.LEFT)
    app._review_blockers_list.bind("<Double-1>", lambda _e: app.on_final_review_fix_selected())


def _build_submission_stage(app, parent: ttk.Frame, activity_tab_module) -> None:
    ttk.Label(parent, text="Submit", font=("SF Pro Text", 13, "bold")).pack(anchor=tk.W, pady=(0, 4))
    app._submission_vpn_var = tk.StringVar(value="Turn VPN ON to proceed.")
    app._submission_status_var = tk.StringVar(value="Select a report and click Submit to begin automation.")
    ttk.Label(parent, textvariable=app._submission_vpn_var, foreground="#b06000").pack(anchor=tk.W, pady=(0, 2))
    ttk.Label(parent, textvariable=app._submission_status_var, foreground="#555").pack(anchor=tk.W, pady=(0, 6))

    cols = ("report_name", "lines", "created", "status")
    tree = ttk.Treeview(parent, columns=cols, show="headings", height=10, selectmode="browse")
    tree.heading("report_name", text="Report Name")
    tree.heading("lines", text="Lines")
    tree.heading("created", text="Created")
    tree.heading("status", text="Submitted")
    tree.column("report_name", width=300, stretch=True)
    tree.column("lines", width=60, stretch=False, anchor=tk.CENTER)
    tree.column("created", width=140, stretch=False, anchor=tk.CENTER)
    tree.column("status", width=120, stretch=False, anchor=tk.CENTER)
    tree.pack(fill=tk.BOTH, expand=True, pady=(0, 8))
    app._submit_reports_tree = tree

    btns = ttk.Frame(parent)
    btns.pack(fill=tk.X, pady=(0, 8))
    ttk.Button(btns, text="Submit selected report", command=app.on_submit_selected_report).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(btns, text="Delete selected report", command=app.on_delete_selected_report).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(btns, text="Open Oracle", command=app.on_step_login).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(btns, text="Resume previous report", command=app.on_resume_previous_expense_report).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(btns, text="Refresh", command=app.refresh_submit_reports_table).pack(side=tk.LEFT)

    timeline_frame = ttk.LabelFrame(parent, text="Automation timeline", padding=6)
    timeline_frame.pack(fill=tk.BOTH, expand=False, pady=(0, 8))
    app._submission_timeline_text = tk.Text(
        timeline_frame,
        height=6,
        wrap=tk.WORD,
        font=("Menlo", 10),
        state=tk.DISABLED,
        background="#f7f7f7",
    )
    app._submission_timeline_text.pack(fill=tk.BOTH, expand=True)

    detail_frame = ttk.LabelFrame(parent, text="Detailed activity log", padding=6)
    detail_frame.pack(fill=tk.BOTH, expand=True)
    activity_tab_module.build_activity_tab(app, detail_frame)
