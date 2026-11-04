"""Settings tab: portal, optional secure sign-in, approver, OpenAI, Photos, TLS."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import keychain_credentials


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
    try:
        oracle = keychain_credentials.get_oracle_credentials()
        oracle_error = ""
    except RuntimeError as exc:
        oracle = {}
        oracle_error = str(exc)
    app._settings_oracle_user_var = tk.StringVar(value=oracle.get("username", ""))
    app._settings_oracle_password_var = tk.StringVar()

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
        text=oracle_error or (
            "Saved sign-in rejected: enter your current password to re-enable login."
            if oracle.get("rejected") == "true"
            else "Optional Oracle sign-in is stored only in the system keychain. MFA remains manual."
        ),
        foreground="#666",
    ).grid(row=r, column=1, sticky="w", pady=(0, 8))
    r += 1
    ttk.Label(frame, text="Oracle username (optional)").grid(row=r, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=app._settings_oracle_user_var, width=58).grid(row=r, column=1, sticky="we")
    r += 1
    ttk.Label(frame, text="Oracle password").grid(row=r, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=app._settings_oracle_password_var, width=58, show="*").grid(
        row=r, column=1, sticky="we"
    )
    r += 1
    ttk.Label(frame, text="Leave blank to keep saved password. HTTPS portal only.").grid(
        row=r, column=1, sticky="w"
    )
    r += 1

    def forget_oracle() -> None:
        error = keychain_credentials.delete_oracle_credentials()
        if error:
            app.set_status(error)
            return
        app._settings_oracle_user_var.set("")
        app._settings_oracle_password_var.set("")
        app.set_status("Saved Oracle sign-in removed.")

    ttk.Button(frame, text="Forget saved sign-in", command=forget_oracle).grid(row=r, column=1, sticky="w")
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
    oracle_warning = None
    if app._settings_oracle_user_var.get().strip() or app._settings_oracle_password_var.get():
        try:
            oracle_warning = keychain_credentials.save_oracle_credentials(
                new_settings.legacy_url, app._settings_oracle_user_var.get(),
                app._settings_oracle_password_var.get() or None,
            )
        except (ValueError, RuntimeError) as exc:
            oracle_warning = str(exc)
    app._settings_oracle_password_var.set("")
    if oracle_warning:
        app.set_status("Other settings saved. " + oracle_warning)
        app.refresh_all_tabs()
        return
    if key_warning:
        app.set_status("Settings saved. " + key_warning)
    else:
        app.set_status("Settings saved.")
    app.refresh_all_tabs()
