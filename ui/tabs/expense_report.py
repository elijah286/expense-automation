"""Expense report tab: scraped Oracle lines, receipt assignments, preview, approvals."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk


def build_expense_report_tab(app, parent: ttk.Frame) -> None:
    app._expense_report_summary = ttk.Label(
        parent,
        text="",
        foreground="#555",
        wraplength=900,
        justify=tk.LEFT,
    )
    app._expense_report_summary.pack(anchor=tk.W, pady=(0, 8))

    ttk.Label(
        parent,
        text=(
            "Create report (VPN on): includes only rows whose receipt file exists on disk, saves approvals, "
            "then runs the Oracle-aligned flow: Step 1 open browser, Step 2 login, Step 3 navigate to Expenses Home, "
            "then Step 4.1-4.6 in the Oracle wizard. Step 4.1 verifies/sets purpose + approver, "
            "Step 4.2 verifies selected Expense Report transactions across all table pages, "
            "Step 4.3 updates expense type/justification then handles missing-receipt and line-error fixes, "
            "Steps 4.4-4.5 click Next only, and Step 4.6 uploads attachments for rows with documents. "
            "Resume previous report: choose a step in this convention, navigate Chromium manually to that page, "
            "then continue; automation verifies the selected step context before proceeding. "
            "Click the Include checkbox cell ([x]/[ ]) to toggle a row; right-click to choose a file, ask the LLM which receipt "
            "matches selected line(s), or remove lines from cache. Ctrl+A / Cmd+A selects all rows."
        ),
        wraplength=900,
        justify=tk.LEFT,
        foreground="#444",
    ).pack(anchor=tk.W, pady=(0, 6))

    btn_row = ttk.Frame(parent)
    btn_row.pack(fill=tk.X, pady=(0, 8))
    ttk.Button(
        btn_row,
        text="Launch browser & scrape expenses",
        command=app.on_expense_report_launch_browser_and_scrape,
    ).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(
        btn_row,
        text="Analyze line items",
        command=app.on_expense_report_analyze_line_items,
    ).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(
        btn_row,
        text="Analyze unmatched items",
        command=app.on_expense_report_analyze_unmatched_line_items,
    ).pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(
        btn_row,
        text="Analyze selected line items",
        command=app.on_expense_report_match_selected_lines,
    ).pack(side=tk.LEFT)

    action_row = ttk.Frame(parent)
    action_row.pack(fill=tk.X, pady=(0, 8))
    ttk.Button(
        action_row,
        text="Rescan selected for match",
        command=app.on_expense_report_match_selected_lines,
    ).pack(side=tk.LEFT, padx=(0, 8))
    ttk.Button(
        action_row,
        text="Remove selected from report",
        command=app._on_matching_remove_from_report,
    ).pack(side=tk.LEFT, padx=(0, 8))

    review_row = ttk.Frame(parent)
    review_row.pack(fill=tk.X, pady=(0, 8))
    ttk.Label(review_row, text="Review queue:", foreground="#555").pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(
        review_row,
        text="Attention only",
        command=app.on_expense_report_show_attention_only,
    ).pack(side=tk.LEFT, padx=(0, 6))
    ttk.Button(
        review_row,
        text="Show all",
        command=app.on_expense_report_show_all_rows,
    ).pack(side=tk.LEFT)
    app._expense_report_filter_label = ttk.Label(review_row, text="", foreground="#0b57d0")
    app._expense_report_filter_label.pack(side=tk.RIGHT)

    paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
    app._expense_report_paned = paned
    paned.pack(fill=tk.BOTH, expand=True)

    left = ttk.Frame(paned)
    paned.add(left, weight=3)
    center = ttk.Frame(paned, width=300)
    paned.add(center, weight=2)
    right = ttk.Frame(paned, width=340)
    paned.add(right, weight=2)

    cols = (
        "include",
        "line_id",
        "merchant",
        "date",
        "amount",
        "currency",
        "expense_type",
        "file",
        "receipt_missing",
        "conf",
        "reason",
    )
    app.expense_report_tree = ttk.Treeview(
        left, columns=cols, show="headings", height=18, selectmode=tk.EXTENDED
    )
    app.expense_report_tree.heading("include", text="Include")
    app.expense_report_tree.heading("line_id", text="Line")
    app.expense_report_tree.heading("merchant", text="Merchant")
    app.expense_report_tree.heading("date", text="Date")
    app.expense_report_tree.heading("amount", text="Amt")
    app.expense_report_tree.heading("currency", text="Cur")
    app.expense_report_tree.heading("expense_type", text="Expense type")
    app.expense_report_tree.heading("file", text="Receipt file")
    app.expense_report_tree.heading("receipt_missing", text="Receipt Missing")
    app.expense_report_tree.heading("conf", text="Conf.")
    app.expense_report_tree.heading("reason", text="LLM note")
    app.expense_report_tree.column("include", width=56, anchor=tk.CENTER)
    app.expense_report_tree.column("line_id", width=70)
    app.expense_report_tree.column("merchant", width=140)
    app.expense_report_tree.column("date", width=88)
    app.expense_report_tree.column("amount", width=72)
    app.expense_report_tree.column("currency", width=44)
    app.expense_report_tree.column("expense_type", width=200)
    app.expense_report_tree.column("file", width=160)
    app.expense_report_tree.column("receipt_missing", width=100, anchor=tk.CENTER)
    app.expense_report_tree.column("conf", width=44, anchor=tk.CENTER)
    app.expense_report_tree.column("reason", width=140)
    app.expense_report_tree.tag_configure("low_confidence", background="#fff6cc")
    ys = ttk.Scrollbar(left, orient=tk.VERTICAL, command=app.expense_report_tree.yview)
    app.expense_report_tree.configure(yscrollcommand=ys.set)
    app.expense_report_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    ys.pack(side=tk.RIGHT, fill=tk.Y)

    ttk.Label(center, text="Match Decision", font=("SF Pro Text", 11, "bold")).pack(anchor=tk.W, pady=(0, 4))
    ttk.Label(
        center,
        text=(
            "Inspect confidence and evidence before approval. "
            "Keyboard: A accept, R reject, M manual pick, N/P next/prev."
        ),
        wraplength=290,
        justify=tk.LEFT,
        foreground="#666",
    ).pack(anchor=tk.W, pady=(0, 6))
    app._matching_line_var = tk.StringVar(value="Line: —")
    app._matching_conf_var = tk.StringVar(value="Confidence: —")
    app._matching_receipt_var = tk.StringVar(value="Suggested receipt: —")
    ttk.Label(center, textvariable=app._matching_line_var, foreground="#333").pack(anchor=tk.W, pady=1)
    ttk.Label(center, textvariable=app._matching_conf_var, foreground="#333").pack(anchor=tk.W, pady=1)
    ttk.Label(center, textvariable=app._matching_receipt_var, foreground="#333", wraplength=290).pack(
        anchor=tk.W, pady=1
    )

    ttk.Label(center, text="Evidence", font=("SF Pro Text", 10, "bold")).pack(anchor=tk.W, pady=(8, 2))
    app._matching_reason_text = tk.Text(
        center,
        height=10,
        wrap=tk.WORD,
        font=("SF Pro Text", 9),
        foreground="#444",
        background="#f7f7f7",
        relief=tk.SOLID,
        borderwidth=1,
    )
    app._matching_reason_text.pack(fill=tk.BOTH, expand=True)
    app._matching_reason_text.insert("1.0", "Select a row to inspect match rationale.")
    app._matching_reason_text.configure(state=tk.DISABLED)

    match_btn_row = ttk.Frame(center)
    match_btn_row.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(match_btn_row, text="Accept selected", command=app.on_matching_accept_selected).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(match_btn_row, text="Reject selected", command=app.on_matching_reject_selected).pack(
        side=tk.LEFT, padx=(0, 6)
    )
    ttk.Button(match_btn_row, text="Manual pick file", command=app.on_matching_manual_pick).pack(side=tk.LEFT)
    bulk_row = ttk.Frame(center)
    bulk_row.pack(fill=tk.X, pady=(6, 0))
    ttk.Button(
        bulk_row,
        text="Accept all high-confidence",
        command=app.on_matching_accept_all_high_confidence,
    ).pack(side=tk.LEFT)

    ttk.Label(right, text="Preview", font=("SF Pro Text", 11, "bold")).pack(anchor=tk.W, pady=(0, 4))
    ttk.Label(
        right,
        text=(
            "Trackpad/mouse: drag = pan · two-finger scroll = pan · Shift+scroll = horizontal · "
            "Ctrl+scroll = zoom (⌘/⌥ scroll on Mac) · on Mac, pinch-to-zoom uses PyObjC (see requirements). "
            "Buttons still zoom/fit/rotate. Use ↻ +90° if the preview is sideways. "
            "To re-parse receipt images (Documents tab: Rescan / Analyze new files), switch to Documents."
        ),
        font=("SF Pro Text", 9),
        foreground="#666",
        wraplength=300,
        justify=tk.LEFT,
    ).pack(anchor=tk.W, pady=(0, 6))

    prev_ctrl = ttk.Frame(right)
    prev_ctrl.pack(fill=tk.X, pady=(0, 6))
    ttk.Button(prev_ctrl, text="−", width=3, command=app._expense_report_preview_zoom_out).pack(
        side=tk.LEFT, padx=(0, 4)
    )
    ttk.Button(prev_ctrl, text="+", width=3, command=app._expense_report_preview_zoom_in).pack(
        side=tk.LEFT, padx=(0, 4)
    )
    ttk.Button(prev_ctrl, text="Fit", command=app._expense_report_preview_reset_view).pack(
        side=tk.LEFT, padx=(0, 8)
    )
    ttk.Button(prev_ctrl, text="↻ +90°", command=app._expense_report_preview_rotate_90_cw).pack(side=tk.LEFT)

    canvas_wrap = ttk.Frame(right)
    canvas_wrap.pack(fill=tk.BOTH, expand=True)
    app._assignments_preview_canvas = tk.Canvas(
        canvas_wrap,
        width=300,
        height=360,
        bg="#1e1e1e",
        highlightthickness=1,
        highlightbackground="#555",
    )
    app._assignments_preview_canvas.pack(fill=tk.BOTH, expand=True)

    note_hdr = ttk.Frame(right)
    note_hdr.pack(fill=tk.X, pady=(8, 2))
    ttk.Label(
        note_hdr,
        text="Matched image + full LLM note",
        font=("SF Pro Text", 10, "bold"),
    ).pack(side=tk.LEFT, anchor=tk.W)
    ttk.Button(
        note_hdr,
        text="Copy note",
        command=app._expense_report_copy_selected_note,
    ).pack(side=tk.RIGHT)
    app._expense_report_llm_note_text = tk.Text(
        right,
        height=6,
        wrap=tk.WORD,
        font=("SF Pro Text", 9),
        foreground="#444",
        background="#f7f7f7",
        relief=tk.SOLID,
        borderwidth=1,
    )
    app._expense_report_llm_note_text.pack(fill=tk.X, expand=False)
    app._expense_report_llm_note_text.insert("1.0", "Select a row to view the full LLM note.")
    app._expense_report_llm_note_text.configure(state=tk.DISABLED)
    app._expense_report_preview_setup_interaction()

    app._assign_row_paths = {}
    app._assign_row_include = {}

    app.expense_report_tree.bind(
        "<<TreeviewSelect>>",
        lambda e: app._assignments_on_select(),
    )
    app.expense_report_tree.bind("<ButtonRelease-1>", app._assignments_toggle_include)
    app.expense_report_tree.bind("<Button-3>", app._expense_report_context_menu)
    if sys.platform == "darwin":
        app.expense_report_tree.bind("<Button-2>", app._expense_report_context_menu)
        app.expense_report_tree.bind("<Control-Button-1>", app._expense_report_context_menu)

    app.expense_report_tree.bind("<Control-a>", app._expense_report_select_all)
    app.expense_report_tree.bind("<Control-A>", app._expense_report_select_all)
    if sys.platform == "darwin":
        app.expense_report_tree.bind("<Command-a>", app._expense_report_select_all)
        app.expense_report_tree.bind("<Command-A>", app._expense_report_select_all)
    app.expense_report_tree.bind("<KeyPress>", app._matching_workspace_on_keypress)

    create_row = ttk.Frame(parent, padding=(0, 10, 0, 0))
    create_row.pack(fill=tk.X)
    ttk.Button(create_row, text="Create report", command=app.on_create_report).pack(side=tk.LEFT)
    ttk.Button(
        create_row,
        text="Resume previous report",
        command=app.on_resume_previous_expense_report,
    ).pack(side=tk.LEFT, padx=(14, 0))

    raw_outer = ttk.LabelFrame(parent, text="Raw cache path (for power users)", padding=6)
    raw_outer.pack(fill=tk.X, pady=(10, 0))
    app._expense_report_raw_path = ttk.Label(raw_outer, text="", foreground="#666", font=("Menlo", 10))
    app._expense_report_raw_path.pack(anchor=tk.W)
