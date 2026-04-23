"""Vendor Classification tab: vendor → expense-type mapping with search and sortable columns."""

from __future__ import annotations

import re
import tkinter as tk
from pathlib import Path
from tkinter import ttk

VENDOR_CACHE_DISPLAY_PATH = Path.home() / ".expense-automator" / "vendor_expense_types.json"


def _norm_vendor_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def build_expense_types_tab(app, parent: ttk.Frame) -> None:
    app._expense_types_llm_label = ttk.Label(
        parent,
        text="",
        foreground="#333",
        wraplength=800,
        justify=tk.LEFT,
    )
    app._expense_types_llm_label.pack(anchor=tk.W, pady=(0, 8))

    ttk.Label(parent, text=f"Cache file: {VENDOR_CACHE_DISPLAY_PATH}", foreground="#555").pack(
        anchor=tk.W, pady=(0, 6)
    )

    # --- search-as-you-type ---
    search_frame = ttk.Frame(parent)
    search_frame.pack(fill=tk.X, pady=(0, 6))
    ttk.Label(search_frame, text="Search:").pack(side=tk.LEFT, padx=(0, 6))
    app._expense_types_search_var = tk.StringVar()
    search_entry = ttk.Entry(search_frame, textvariable=app._expense_types_search_var, width=40)
    search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    app._expense_types_search_var.trace_add("write", lambda *_a: app._refill_expense_types_tree())

    # --- treeview with sortable columns ---
    tree_frame = ttk.Frame(parent)
    tree_frame.pack(fill=tk.BOTH, expand=True)

    cols = ("vendor_key", "expense_type")
    app.expense_types_tree = ttk.Treeview(
        tree_frame,
        columns=cols,
        show="headings",
        height=18,
        selectmode=tk.BROWSE,
    )
    app.expense_types_tree.heading("vendor_key", text="Merchant \u25b2\u25bc", anchor=tk.W)
    app.expense_types_tree.heading("expense_type", text="Expense Type \u25b2\u25bc", anchor=tk.W)
    app.expense_types_tree.column("vendor_key", width=320, stretch=True)
    app.expense_types_tree.column("expense_type", width=320, stretch=True)
    ys = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=app.expense_types_tree.yview)
    app.expense_types_tree.configure(yscrollcommand=ys.set)
    app.expense_types_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    ys.pack(side=tk.RIGHT, fill=tk.Y)

    # Sort state: column key -> ascending boolean
    app._expense_types_sort_col = "vendor_key"
    app._expense_types_sort_asc = True

    def _on_heading_click(col: str) -> None:
        if app._expense_types_sort_col == col:
            app._expense_types_sort_asc = not app._expense_types_sort_asc
        else:
            app._expense_types_sort_col = col
            app._expense_types_sort_asc = True
        _update_heading_labels()
        app._refill_expense_types_tree()

    def _update_heading_labels() -> None:
        for c, label in (("vendor_key", "Merchant"), ("expense_type", "Expense Type")):
            if c == app._expense_types_sort_col:
                arrow = " \u25b2" if app._expense_types_sort_asc else " \u25bc"
            else:
                arrow = ""
            app.expense_types_tree.heading(c, text=f"{label}{arrow}")

    app.expense_types_tree.heading(
        "vendor_key", command=lambda: _on_heading_click("vendor_key")
    )
    app.expense_types_tree.heading(
        "expense_type", command=lambda: _on_heading_click("expense_type")
    )
    _update_heading_labels()

    app._expense_types_vendor_var = tk.StringVar()
    app._expense_types_type_var = tk.StringVar()

    def on_tree_select(_event: object | None = None) -> None:
        sel = app.expense_types_tree.selection()
        if not sel:
            return
        app._expense_types_sync_form_from_meta(sel[0])

    app.expense_types_tree.bind("<<TreeviewSelect>>", on_tree_select)
    app.expense_types_tree.bind("<Double-1>", lambda e: app._expense_types_type_combo.focus_set())

    edit_frame = ttk.LabelFrame(parent, text="Add or edit mapping", padding=10)
    edit_frame.pack(fill=tk.X, pady=(10, 0))
    ttk.Label(edit_frame, text="Vendor").grid(row=0, column=0, sticky="nw", pady=4)
    ttk.Entry(edit_frame, textvariable=app._expense_types_vendor_var, width=48).grid(
        row=0, column=1, sticky="we", pady=4, padx=(8, 0)
    )
    ttk.Label(edit_frame, text="Expense type").grid(row=1, column=0, sticky="nw", pady=4)
    app._expense_types_type_combo = ttk.Combobox(
        edit_frame,
        textvariable=app._expense_types_type_var,
        width=46,
        state="normal",
    )
    app._expense_types_type_combo.grid(row=1, column=1, sticky="we", pady=4, padx=(8, 0))
    edit_frame.columnconfigure(1, weight=1)

    def save_mapping() -> None:
        new_key = _norm_vendor_key(app._expense_types_vendor_var.get())
        type_raw = app._expense_types_type_var.get().strip()
        sel = app.expense_types_tree.selection()
        prior_vk: str | None = None
        portal_options: list[str] | None = None
        if sel:
            meta = app._expense_types_tree_iid_meta.get(sel[0])
            if meta:
                prior_vk = str(meta.get("vendor_key") or "")
                if new_key == prior_vk:
                    portal_options = list(meta.get("options") or [])
        ok, err = app._expense_types_commit_mapping(
            prior_vk,
            new_key,
            type_raw,
            portal_options=portal_options,
        )
        if not ok:
            app.set_status(f"Vendor cache save blocked: {err}")
            return

    def delete_selected() -> None:
        sel = app.expense_types_tree.selection()
        if not sel:
            app.set_status("Delete vendor row: select a table row first.")
            return
        meta = app._expense_types_tree_iid_meta.get(sel[0])
        key = str(meta.get("vendor_key", "")) if meta else ""
        if not key:
            app.set_status("Delete vendor row: select a row with a vendor key.")
            return
        app.vendor_expense_cache.pop(key, None)
        app._persist_vendor_expense_cache()
        app._expense_types_vendor_var.set("")
        app._expense_types_type_var.set("")
        app._expense_types_type_combo.configure(values=[], state="normal")
        app._expense_types_type_combo.set("")
        app.set_status(f'Vendor expense cache: removed "{key}" (row may still appear from scraped lines).')
        app.refresh_all_tabs()

    btn_row = ttk.Frame(parent, padding=(0, 10, 0, 0))
    btn_row.pack(fill=tk.X)
    ttk.Button(btn_row, text="Scan new vendors", command=app.scan_new_vendors_expense_types).pack(
        side=tk.LEFT
    )
    ttk.Button(btn_row, text="Delete selected", command=delete_selected).pack(side=tk.LEFT, padx=(8, 0))
    ttk.Button(btn_row, text="Save mapping", command=save_mapping).pack(side=tk.LEFT, padx=(8, 0))

    app._expense_types_refill_tree = app._refill_expense_types_tree
    app._refill_expense_types_tree()
