"""Settings tab: URL, Oracle approver, OpenAI, Photos, TLS."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


def build_settings_tab(app, parent: ttk.Frame) -> None:
    outer = ttk.Frame(parent)
    outer.pack(fill=tk.BOTH, expand=True)
    canvas = tk.Canvas(outer, highlightthickness=0)
    ys = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
    frame = ttk.Frame(canvas, padding=14)
    win_id = canvas.create_window((0, 0), window=frame, anchor=tk.NW)

    def _on_configure(_event: tk.Event) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(win_id, width=canvas.winfo_width())

    frame.bind("<Configure>", _on_configure)
    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    ys.pack(side=tk.RIGHT, fill=tk.Y)
    canvas.configure(yscrollcommand=ys.set)

    app._settings_url_var = tk.StringVar(value=app.settings.legacy_url)
    app._settings_approver_var = tk.StringVar(value=app.settings.approver)
    app._settings_model_var = tk.StringVar(value=app.settings.openai_model)
    app._settings_limit_var = tk.StringVar(value=str(app.settings.photos_limit))
    app._settings_export_var = tk.StringVar(value=app.settings.photos_export_dir)
    app._settings_api_var = tk.StringVar(value=app.get_openai_key())
    app._settings_tls_var = tk.StringVar(value=app.settings.openai_http_verify)

    r = 0
    ttk.Label(frame, text="Legacy URL").grid(row=r, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=app._settings_url_var, width=58).grid(row=r, column=1, sticky="we", pady=6)
    r += 1
    ttk.Label(frame, text="Approver (Oracle display name)").grid(row=r, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=app._settings_approver_var, width=58).grid(
        row=r, column=1, sticky="we", pady=6
    )
    r += 1
    ttk.Label(
        frame,
        text="Used to find the approver in Oracle. Example: Last, First",
        foreground="#666",
    ).grid(row=r, column=1, sticky="w", pady=(0, 4))
    r += 1
    ttk.Label(
        frame,
        text="Oracle login is manual in the browser; credentials are not stored.",
        foreground="#666",
    ).grid(row=r, column=1, sticky="w", pady=(0, 8))
    r += 1
    ttk.Label(frame, text="OpenAI model").grid(row=r, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=app._settings_model_var, width=58).grid(row=r, column=1, sticky="we", pady=6)
    r += 1
    ttk.Label(frame, text="Photos limit").grid(row=r, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=app._settings_limit_var, width=10).grid(row=r, column=1, sticky="w", pady=6)
    r += 1
    ttk.Label(frame, text="Photos export dir").grid(row=r, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=app._settings_export_var, width=58).grid(row=r, column=1, sticky="we", pady=6)
    r += 1
    ttk.Label(frame, text="OpenAI API key").grid(row=r, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=app._settings_api_var, width=58, show="*").grid(
        row=r, column=1, sticky="we", pady=6
    )
    r += 1
    ttk.Label(
        frame,
        text="API key: keychain + local fallback.",
        foreground="#666",
    ).grid(row=r, column=1, sticky="w", pady=(0, 4))
    r += 1
    ttk.Label(frame, text="OpenAI TLS / CA").grid(row=r, column=0, sticky="nw", pady=6)
    ttk.Entry(frame, textvariable=app._settings_tls_var, width=58).grid(row=r, column=1, sticky="we", pady=6)
    r += 1
    ttk.Label(
        frame,
        text=(
            "Corporate SSL: path to org root CA .pem, or false to skip verify (insecure). "
            "Empty = use OPENAI_HTTP_VERIFY from .env if set."
        ),
        foreground="#666",
        wraplength=520,
        justify=tk.LEFT,
    ).grid(row=r, column=1, sticky="w", pady=(0, 10))
    r += 1

    frame.columnconfigure(1, weight=1)

    btn_row = ttk.Frame(frame)
    btn_row.grid(row=r, column=1, sticky="e", pady=(8, 0))
    ttk.Button(btn_row, text="Save settings", command=lambda: _save(app)).pack(side=tk.RIGHT)


def _save(app) -> None:
    from receipt_automation_ui import AppSettings

    try:
        parsed_limit = int(app._settings_limit_var.get().strip())
    except ValueError:
        app.set_status("Settings not saved: Photos limit must be a number.")
        return
    new_settings = AppSettings(
        legacy_url=app._settings_url_var.get().strip(),
        approver=app._settings_approver_var.get().strip(),
        openai_model=app._settings_model_var.get().strip() or AppSettings.openai_model,
        openai_http_verify=app._settings_tls_var.get().strip(),
        photos_limit=max(parsed_limit, 1),
        photos_export_dir=app._settings_export_var.get().strip() or "./photos-exports",
    )
    app.save_settings(new_settings)
    app._openai_client = None
    key_warning = app.set_openai_key(app._settings_api_var.get())
    try:
        import keychain_credentials

        keychain_credentials.delete_keychain_expense_password()
    except Exception:
        pass
    try:
        payload = app._read_settings_payload()
        payload.pop("expense_username", None)
        app._write_settings_payload(payload)
    except Exception:
        pass
    if key_warning:
        app.set_status("Settings saved. " + key_warning)
    else:
        app.set_status("Settings saved.")
    app.refresh_all_tabs()
