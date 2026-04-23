"""Activity tab: VPN-grouped actions, step sequence, live log."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def build_activity_tab(app, parent: ttk.Frame) -> None:
    app._activity_hint = ttk.Label(
        parent,
        text="",
        foreground="#0b57d0",
        wraplength=900,
        justify=tk.LEFT,
    )
    app._activity_hint.pack(anchor=tk.W, pady=(0, 8))

    status_panel = ttk.LabelFrame(parent, text="Run status", padding=8)
    status_panel.pack(fill=tk.X, pady=(0, 8))
    top = ttk.Frame(status_panel)
    top.pack(fill=tk.X)
    app._run_status_phase_var = tk.StringVar(value="Phase: Idle")
    app._run_status_progress_var = tk.StringVar(value="Progress: 0%")
    app._run_status_attention_var = tk.StringVar(value="Attention: None")
    app._run_status_message_var = tk.StringVar(value="Waiting for next action.")
    ttk.Label(top, textvariable=app._run_status_phase_var, foreground="#0b57d0").pack(
        side=tk.LEFT, padx=(0, 14)
    )
    ttk.Label(top, textvariable=app._run_status_progress_var, foreground="#333").pack(
        side=tk.LEFT, padx=(0, 14)
    )
    ttk.Label(top, textvariable=app._run_status_attention_var, foreground="#b06000").pack(side=tk.LEFT)
    app._run_status_progress = ttk.Progressbar(status_panel, orient=tk.HORIZONTAL, mode="determinate", maximum=100)
    app._run_status_progress.pack(fill=tk.X, pady=(6, 4))
    ttk.Label(
        status_panel,
        textvariable=app._run_status_message_var,
        foreground="#555",
        wraplength=900,
        justify=tk.LEFT,
    ).pack(anchor=tk.W)
    status_actions = ttk.Frame(status_panel)
    status_actions.pack(fill=tk.X, pady=(6, 0))
    app._run_status_attention_btn = ttk.Button(
        status_actions,
        text="Review attention items",
        command=app.on_focus_attention_items,
    )
    app._run_status_attention_btn.pack(side=tk.LEFT)

    actions = ttk.LabelFrame(parent, text="Actions (order is flexible — use what you need)", padding=8)
    actions.pack(fill=tk.X, pady=(0, 10))

    row_a = ttk.Frame(actions)
    row_a.pack(fill=tk.X, pady=2)
    ttk.Label(row_a, text="VPN on", width=10, foreground="#555").pack(side=tk.LEFT, padx=(0, 4))
    ttk.Button(row_a, text="Open Oracle", command=app.on_step_login).pack(side=tk.LEFT, padx=2)
    ttk.Button(row_a, text="Scrape Step 2", command=app.on_vpn_collect_llm_prompts).pack(side=tk.LEFT, padx=2)
    app.workflow_take_browser_btn = ttk.Button(
        row_a,
        text="Stop & take browser",
        command=app.on_stop_sequence_release_browser,
        state=tk.DISABLED,
    )
    app.workflow_take_browser_btn.pack(side=tk.RIGHT, padx=(12, 0))

    row_b = ttk.Frame(actions)
    row_b.pack(fill=tk.X, pady=2)
    ttk.Label(row_b, text="VPN off", width=10, foreground="#555").pack(side=tk.LEFT, padx=(0, 4))
    ttk.Button(row_b, text="Resolve types", command=app.on_vpn_resolve_llm_cache).pack(side=tk.LEFT, padx=2)
    ttk.Button(row_b, text="Match lines", command=app.on_match_receipts_off_vpn).pack(side=tk.LEFT, padx=2)
    ttk.Button(row_b, text="Open Expense report", command=app.focus_expense_report_tab).pack(side=tk.LEFT, padx=2)

    row_c = ttk.Frame(actions)
    row_c.pack(fill=tk.X, pady=2)
    ttk.Label(row_c, text="Advanced", width=10, foreground="#999").pack(side=tk.LEFT, padx=(0, 4))
    ttk.Button(row_c, text="Standard Step 3 (live LLM)", command=app.on_step_populate_expense_report).pack(
        side=tk.LEFT, padx=2
    )

    body = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
    app._activity_paned = body
    body.pack(fill=tk.BOTH, expand=True)

    progress_panel = ttk.Frame(body)
    body.add(progress_panel, weight=1)
    log_panel = ttk.Frame(body, width=420)
    body.add(log_panel, weight=2)

    ttk.Label(progress_panel, text="Step sequence", font=("SF Pro Text", 11, "bold")).pack(
        anchor=tk.W, pady=(0, 6)
    )
    act_frame = ttk.Frame(progress_panel)
    act_frame.pack(fill=tk.BOTH, expand=True)
    act_cols = ("activity", "status")
    app.activity_table = ttk.Treeview(
        act_frame,
        columns=act_cols,
        show="headings",
        height=16,
        selectmode=tk.BROWSE,
    )
    app.activity_table.heading("activity", text="Step")
    app.activity_table.heading("status", text="Status")
    app.activity_table.column("activity", width=220, stretch=True)
    app.activity_table.column("status", width=88, anchor=tk.CENTER, stretch=False)
    app.activity_table.tag_configure("pending", foreground="#666666")
    app.activity_table.tag_configure("current", foreground="#0b57d0")
    app.activity_table.tag_configure("done", foreground="#1e7e34")
    app.activity_table.tag_configure("stopped", foreground="#b06000")
    app.activity_table.tag_configure("run", foreground="#0b57d0")
    app.activity_table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    act_y = ttk.Scrollbar(act_frame, orient=tk.VERTICAL, command=app.activity_table.yview)
    act_y.pack(side=tk.RIGHT, fill=tk.Y)
    app.activity_table.configure(yscrollcommand=act_y.set)

    prog_btn_row = ttk.Frame(progress_panel)
    prog_btn_row.pack(fill=tk.X, pady=(8, 0))
    app.stop_automation_btn = ttk.Button(
        prog_btn_row,
        text="Stop automation",
        command=app.on_stop_step3_automation,
        state=tk.DISABLED,
    )
    app.stop_automation_btn.pack(fill=tk.X, pady=(0, 6))
    app.stop_receipt_llm_btn = ttk.Button(
        prog_btn_row,
        text="Stop receipt LLM",
        command=app.on_stop_receipt_llm_parse,
        state=tk.DISABLED,
    )
    app.stop_receipt_llm_btn.pack(fill=tk.X, pady=(0, 6))
    app.stop_release_browser_btn = ttk.Button(
        prog_btn_row,
        text="Stop & take browser",
        command=app.on_stop_sequence_release_browser,
        state=tk.DISABLED,
    )
    app.stop_release_browser_btn.pack(fill=tk.X, pady=(0, 6))
    ttk.Button(
        prog_btn_row,
        text="Restart from selected step",
        command=app.on_restart_from_selected_activity,
    ).pack(fill=tk.X)
    app.resume_after_crash_btn = ttk.Button(
        prog_btn_row,
        text="Resume after crash (open in-progress report)",
        command=app.on_resume_automation_after_crash,
        state=tk.DISABLED,
    )
    app.resume_after_crash_btn.pack(fill=tk.X, pady=(6, 0))

    ttk.Label(log_panel, text="Live log", font=("SF Pro Text", 11, "bold")).pack(anchor=tk.W, pady=(0, 4))
    log_toolbar = ttk.Frame(log_panel)
    log_toolbar.pack(fill=tk.X, pady=(0, 4))
    ttk.Label(
        log_toolbar,
        text="LLM · cache · browser · net",
        foreground="#666",
    ).pack(side=tk.LEFT)
    ttk.Button(log_toolbar, text="Clear log", command=app._clear_activity_log).pack(side=tk.RIGHT)

    log_inner = ttk.Frame(log_panel)
    log_inner.pack(fill=tk.BOTH, expand=True)
    app.activity_log = tk.Text(
        log_inner,
        height=14,
        wrap=tk.WORD,
        font=("Menlo", 11),
        bg="#252526",
        fg="#e8e8e8",
        insertbackground="#e8e8e8",
        selectbackground="#264f78",
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground="#3c3c3c",
        state=tk.DISABLED,
    )
    app.activity_log.tag_configure("log_llm", foreground="#7cc4ff")
    app.activity_log.tag_configure("log_cache", foreground="#9cdc8e")
    app.activity_log.tag_configure("log_browser", foreground="#e6c07b")
    app.activity_log.tag_configure("log_net", foreground="#ff9e64")
    app.activity_log.tag_configure("log_warn", foreground="#ffcc66")
    app.activity_log.tag_configure("log_err", foreground="#ff7b72")
    app.activity_log.tag_configure("log_step", foreground="#d4b3ff")
    log_scroll = ttk.Scrollbar(log_inner, orient=tk.VERTICAL, command=app.activity_log.yview)
    app.activity_log.configure(yscrollcommand=log_scroll.set)
    app.activity_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    from datetime import datetime

    _ready = "Ready. Log updates during automation (LLM waits may take several seconds)."
    _ts = datetime.now().strftime("%H:%M:%S")
    app.activity_log.configure(state=tk.NORMAL)
    app.activity_log.insert(tk.END, f"[{_ts}] {_ready}\n")
    app.activity_log.see(tk.END)
    app.activity_log.configure(state=tk.DISABLED)
