"""Documents tab: receipt files, parse status, matching hints, actions."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk


def build_documents_tab(app, parent: ttk.Frame) -> None:
    hint = ttk.Label(
        parent,
        text=(
            "Add receipts anytime (VPN off). Parsed = LLM read the file. "
            "Matched line / Approved come from the Expense report tab after you run matching. "
            "Preview applies camera EXIF first, then LLM display_rotation_quarter_turns from "
            "“Analyze new files” (or Rescan) if receipts look sideways. Drag to pan, wheel to zoom."
        ),
        foreground="#555",
        wraplength=720,
        justify=tk.LEFT,
    )
    hint.pack(anchor=tk.W, pady=(0, 8))

    add_row = ttk.Frame(parent)
    add_row.pack(fill=tk.X, pady=(0, 8))
    ttk.Button(add_row, text="Add files...", command=app.add_more_receipt_files).pack(
        side=tk.LEFT
    )
    ttk.Button(
        add_row,
        text="Analyze new files",
        command=app.analyze_new_receipt_files_vpn_off,
    ).pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(
        add_row,
        text="Export + down-convert all",
        command=app.export_and_optimize_all_receipts,
    ).pack(side=tk.LEFT, padx=(8, 0))

    paned = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
    app._documents_paned = paned
    paned.pack(fill=tk.BOTH, expand=True)

    left = ttk.Frame(paned)
    paned.add(left, weight=3)

    table_frame = ttk.Frame(left)
    table_frame.pack(fill=tk.BOTH, expand=True)

    columns = (
        "file",
        "parsed",
        "vendor",
        "receipt_date",
        "amount_local",
        "amount_usd",
        "confidence",
        "matched_line",
        "approved",
        "assign_to",
    )
    app.table = ttk.Treeview(
        table_frame,
        columns=columns,
        show="headings",
        height=14,
        selectmode="extended",
    )
    app.table.heading("file", text="File")
    app.table.heading("parsed", text="Parsed")
    app.table.heading("vendor", text="Vendor")
    app.table.heading("receipt_date", text="Date")
    app.table.heading("amount_local", text="Local")
    app.table.heading("amount_usd", text="USD")
    app.table.heading("confidence", text="Conf.")
    app.table.heading("matched_line", text="Line")
    app.table.heading("approved", text="Approved")
    app.table.heading("assign_to", text="Assign to")
    app.table.column("file", width=200)
    app.table.column("parsed", width=52, anchor=tk.CENTER)
    app.table.column("vendor", width=120)
    app.table.column("receipt_date", width=96, anchor=tk.CENTER)
    app.table.column("amount_local", width=140, anchor=tk.W)
    app.table.column("amount_usd", width=100, anchor=tk.W)
    app.table.column("confidence", width=48, anchor=tk.CENTER)
    app.table.column("matched_line", width=100, anchor=tk.CENTER)
    app.table.column("approved", width=64, anchor=tk.CENTER)
    app.table.column("assign_to", width=120)
    app.table.tag_configure("assigned", foreground="#8a8a8a")
    app.table.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

    yscroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=app.table.yview)
    yscroll.pack(side=tk.RIGHT, fill=tk.Y)
    app.table.configure(yscrollcommand=yscroll.set)

    right = ttk.Frame(paned, width=300)
    paned.add(right, weight=2)

    ttk.Label(right, text="Preview", font=("SF Pro Text", 11, "bold")).pack(anchor=tk.W, pady=(0, 4))
    ttk.Label(
        right,
        text=(
            "Scroll/trackpad = pan · Shift+scroll = horizontal · Ctrl or ⌘/⌥+scroll = zoom · drag = pan · "
            "Mac pinch: pyobjc in requirements (darwin). EXIF + LLM rotation; Rescan if sideways."
        ),
        font=("SF Pro Text", 9),
        foreground="#666",
        wraplength=280,
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
    app._documents_preview_canvas = tk.Canvas(
        canvas_wrap,
        width=280,
        height=360,
        bg="#1e1e1e",
        highlightthickness=1,
        highlightbackground="#555",
    )
    app._documents_preview_canvas.pack(fill=tk.BOTH, expand=True)
    app._register_receipt_preview_canvas(app._documents_preview_canvas)

    app.table.bind("<<TreeviewSelect>>", app._documents_on_receipt_select)

    action_row = ttk.Frame(parent)
    action_row.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(action_row, text="Delete selected", command=app.delete_selected_receipts).pack(
        side=tk.LEFT, padx=(0, 8)
    )
    ttk.Button(action_row, text="Rescan selected", command=app.rescan_selected_receipts).pack(
        side=tk.LEFT, padx=(0, 8)
    )
    ttk.Button(action_row, text="Delete all", command=app.delete_all_receipts).pack(
        side=tk.LEFT, padx=(0, 8)
    )
    ttk.Button(action_row, text="Rescan all", command=app.rescan_all_receipts).pack(side=tk.LEFT)

    app.table_menu = tk.Menu(app.root, tearoff=0)
    app.table_menu.add_command(label="Delete selected", command=app.delete_selected_receipts)
    app.table_menu.add_command(label="Rescan item", command=app.rescan_context_row_receipt)
    app.table_menu.add_command(label="Rescan selected", command=app.rescan_selected_receipts)
    app.table_menu.add_separator()
    app.table_menu.add_command(label="Delete all", command=app.delete_all_receipts)
    app.table_menu.add_command(label="Rescan all", command=app.rescan_all_receipts)
    app.table.bind("<Button-3>", app._show_table_context_menu)
    app.table.bind("<Control-Button-1>", app._show_table_context_menu)
    if sys.platform == "darwin":
        app.table.bind("<Button-2>", app._show_table_context_menu)
