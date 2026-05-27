"""
Expense Automator — Modern Web UI (NiceGUI).

Run:  python3 -m web.app
macOS `.app`: embedded pywebview window (see `web/macos_single_process_webview.py`). From source, the default is usually the system browser unless `EXPENSE_AUTOMATOR_EMBEDDED=1`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from nicegui import app, ui

import keychain_credentials
from persistence.atomic_json import atomic_write_json, load_json_or_quarantine
from web.env_paths import user_data_dir


def _prefs_path() -> Path:
    return user_data_dir() / "preferences.json"


def _load_prefs() -> dict:
    return load_json_or_quarantine(_prefs_path(), {})


def _save_pref(key: str, value: Any) -> None:
    prefs = _load_prefs()
    prefs[key] = value
    atomic_write_json(_prefs_path(), prefs)


# Only gate keychain access if user hasn't previously consented
_prefs = _load_prefs()
if not _prefs.get("keychain_consented"):
    keychain_credentials.enable_keychain_access_gate()
else:
    # User previously consented — warm up keychain immediately
    try:
        keychain_credentials.warm_up()
    except Exception:
        pass

from web.service import ExpenseService, ExpenseReportGroup, MatchReviewItem, ReceiptDoc, ReportReadiness, TransactionRow
from portal_expense_types import PORTAL_EXPENSE_TYPE_OPTIONS, get_expense_type_options
from web.activity_log import activity_log

svc = ExpenseService()

def _read_version() -> str:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    try:
        return (base / "VERSION").read_text().strip()
    except FileNotFoundError:
        return "0.0.0"

_VERSION = _read_version()

# ---------------------------------------------------------------------------
# In-app update state
# ---------------------------------------------------------------------------
_update_info: dict[str, Any] | None = None
_update_checked = False
_update_lock = threading.Lock()


def _check_update_background() -> None:
    """Run update check in background. Stores result in _update_info."""
    global _update_info, _update_checked
    from web.updater import check_for_update
    try:
        result = check_for_update(_VERSION)
        with _update_lock:
            _update_info = result
            _update_checked = True
    except Exception:
        with _update_lock:
            _update_checked = True


def _ensure_update_check() -> None:
    """Trigger background update check once per session."""
    with _update_lock:
        if _update_checked:
            return
    threading.Thread(target=_check_update_background, daemon=True).start()


# Background task tracking
_task_lock = threading.Lock()
_running_tasks: dict[str, dict[str, Any]] = {}


_STALE_TASK_TIMEOUT_S = 90


def _is_task_running(task_name: str) -> bool:
    """Check if a named background task is currently running."""
    with _task_lock:
        t = _running_tasks.get(task_name)
        return bool(t and t.get("running"))


def _run_background(task_name: str, fn, on_done_msg: str, on_done: Callable | None = None):
    """Run fn in a background thread, tracking status."""
    with _task_lock:
        existing = _running_tasks.get(task_name)
        if existing and existing.get("running"):
            last_activity = existing.get("last_activity", 0)
            if last_activity and (time.time() - last_activity) < _STALE_TASK_TIMEOUT_S:
                ui.notify(f"{task_name} is already running", type="warning")
                return
            activity_log.emit(
                "info",
                f"{task_name} appears stalled \u2014 starting new attempt.",
            )

    status_lines: list[str] = []
    activity_log.clear_cancel()
    activity_log.set_active_task(task_name)
    activity_log.emit("step", f"Starting {task_name}\u2026")

    def _on_status(msg: str):
        status_lines.append(msg)
        activity_log.emit("info", msg)
        with _task_lock:
            task = _running_tasks.get(task_name)
            if task:
                task["last_activity"] = time.time()

    def _worker():
        with _task_lock:
            _running_tasks[task_name] = {
                "running": True,
                "status": status_lines,
                "last_activity": time.time(),
            }
        try:
            result = fn(on_status=_on_status)
            if activity_log.is_cancel_requested():
                status_lines.append("Stopped by user")
                activity_log.emit("info", f"{task_name} stopped by user")
            else:
                status_lines.append(f"Done: {result}")
                activity_log.emit("success", f"{task_name} complete")
            if on_done:
                # Schedule UI callback on the main event loop, not from this thread
                try:
                    ui.timer(0.1, lambda: on_done(result), once=True)
                except Exception:
                    pass
        except Exception as exc:
            status_lines.append(f"Error: {exc}")
            activity_log.emit("error", f"{task_name} failed: {exc}")
        finally:
            with _task_lock:
                _running_tasks[task_name] = {"running": False, "status": status_lines}
            activity_log.clear_cancel()
            activity_log.clear_active_task()

    ui.notify(f"Started {task_name}...", type="info")
    threading.Thread(target=_worker, daemon=True).start()


def _start_auto_match():
    """Check for unscanned documents before running auto-match; prompt user if any found."""
    unscanned = [r for r in svc.get_receipts() if not r.analyzed]
    if not unscanned:
        _run_background("Matching", svc.run_full_matching_pipeline, "Matching complete")
        return

    count = len(unscanned)
    with ui.dialog() as dlg, ui.card().style(
        "min-width:420px;max-width:520px;border-radius:16px;padding:28px"
    ):
        ui.label("Unscanned documents").classes("text-lg font-bold mb-2")
        ui.html(
            f"""
            <div style="font-size:0.9rem;line-height:1.55;color:#475569">
              <p style="margin:0 0 12px 0">
                <b>{count} document{'s have' if count != 1 else ' has'}</b> been added but not yet
                reviewed for vendor and currency information.
              </p>
              <p style="margin:0 0 12px 0">
                Would you like to scan these first before running the match?
                Unscanned items will not be evaluated for a match.
              </p>
            </div>
            """
        )
        with ui.row().classes("items-center justify-end gap-2 w-full mt-4"):
            def _skip():
                dlg.close()
                _run_background("Matching", svc.run_full_matching_pipeline, "Matching complete")

            def _scan_first():
                dlg.close()

                def _do_scan(on_status):
                    svc.analyze_receipts(on_status=on_status)
                    if activity_log.is_cancel_requested():
                        return {"cancelled": True}
                    return svc.run_full_matching_pipeline(on_status=on_status)

                _run_background(
                    "Scan & Match",
                    _do_scan,
                    "Scan and matching complete",
                )

            ui.button("Skip, match anyway", on_click=_skip).props("flat no-caps")
            ui.button("Scan first, then match", on_click=_scan_first).props(
                "no-caps unelevated color=primary"
            ).classes("action-btn")
    dlg.open()


def _open_oracle_manual_login_dialog(on_continue: Callable[[], None]) -> None:
    """Explain that Oracle credentials are entered only in the browser; then run *on_continue*."""
    with ui.dialog() as dlg, ui.card().style(
        "min-width:420px;max-width:520px;border-radius:16px;padding:28px"
    ):
        ui.label("Oracle sign-in").classes("text-lg font-bold text-slate-800 mb-2")
        ui.html(
            """
            <div style="font-size:0.9rem;line-height:1.55;color:#475569">
              <p style="margin:0 0 12px 0">
                For privacy, <b>your Oracle username and password are not stored</b> in this app.
                Chromium will open to your portal URL.
              </p>
              <p style="margin:0 0 12px 0">
                Sign in in the browser window (including 2FA if your organization requires it).
                When the app detects that you are logged in, automation <b>continues automatically</b>.
              </p>
              <p style="margin:0">
                Keep the browser window open until the run finishes.
              </p>
            </div>
            """
        )
        with ui.row().classes("items-center justify-end gap-2 w-full mt-4"):
            ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

            def _go() -> None:
                dlg.close()
                on_continue()

            ui.button("Continue", icon="arrow_forward", on_click=_go).props(
                "no-caps unelevated color=primary"
            ).classes("action-btn")
    dlg.open()


_keychain_notice_clients: set[str] = set()


def _finalize_keychain_notice_client(client_id: str) -> None:
    _keychain_notice_clients.discard(client_id)


def _run_keychain_unlock_then_reload() -> None:
    done = threading.Event()

    def worker() -> None:
        try:
            keychain_credentials.grant_keychain_access_after_user_consent()
            # Persist consent so we don't ask again
            _save_pref("keychain_consented", True)
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()

    def wait_reload() -> None:
        if done.is_set():
            try:
                ui.navigate.reload()
            except Exception:
                pass
        else:
            ui.timer(0.15, wait_reload, once=True)

    ui.timer(0.15, wait_reload, once=True)


def _show_keychain_secure_storage_dialog(client_id: str) -> None:
    """Explain the OS keychain prompt before keyring access (web UI only)."""
    with ui.dialog() as dlg, ui.card().style(
        "min-width:420px;max-width:540px;border-radius:16px;padding:28px"
    ):
        ui.label("Secure storage on your computer").classes("text-lg font-bold text-slate-800 mb-2")
        ui.html(
            """
            <div style="font-size:0.9rem;line-height:1.55;color:#475569">
              <p style="margin:0 0 12px 0">
                This app stores your <b>OpenAI API key</b> in your system's secure vault
                (macOS Keychain, Windows Credential Manager, or similar), not as plain text in a file.
              </p>
              <p style="margin:0 0 12px 0">
                Next, your <b>operating system</b> may ask for your password, Touch&nbsp;ID, or to allow
                <b>Python</b> (or this app) to access the keychain. That dialog is from the OS so only
                you can use the stored key—it is <b>not</b> a website or third-party login.
              </p>
              <p style="margin:0 0 12px 0">
                Choose <b>Continue</b> when you are ready for that step.
              </p>
              <p style="margin:0;font-size:0.85rem;color:#64748b">
                If macOS asks more than once, pick <b>Always Allow</b> (not only <b>Allow</b>)
                so the app can reuse the stored key without a new prompt each time.
                Saving your API key from setup may show one additional prompt to store it.
              </p>
            </div>
            """
        )
        with ui.row().classes("items-center justify-end gap-2 w-full mt-4"):
            dont_show = ui.checkbox("Don't show again").props("dense").classes(
                "text-sm"
            ).style("margin-right:auto")
            ui.button("Not now", on_click=dlg.close).props("flat no-caps")

            def _go() -> None:
                _finalize_keychain_notice_client(client_id)
                dlg.close()
                _run_keychain_unlock_then_reload()

            ui.button("Continue", icon="vpn_key", on_click=_go).props(
                "no-caps unelevated color=primary"
            ).classes("action-btn")

    def _on_hide() -> None:
        if dont_show.value:
            # User chose not to see this again — auto-consent on next launch
            _save_pref("keychain_consented", True)
            keychain_credentials.grant_keychain_access_after_user_consent()
        if keychain_credentials.is_keychain_access_gated():
            _finalize_keychain_notice_client(client_id)

    dlg.on("hide", _on_hide)
    dlg.open()


def _schedule_keychain_consent_if_needed() -> None:
    if not keychain_credentials.is_keychain_access_gated():
        return
    # Don't show keychain consent when setup is still needed — there is no
    # API key to store yet, and opening a second dialog on top of the setup
    # dialog breaks keyboard focus in WKWebView.
    if not svc.credentials_ready():
        return
    try:
        client_id = str(ui.context.client.id)
    except Exception:
        return
    if client_id in _keychain_notice_clients:
        return
    _keychain_notice_clients.add(client_id)

    def _show() -> None:
        _show_keychain_secure_storage_dialog(client_id)

    ui.timer(0.05, _show, once=True)


# ---------------------------------------------------------------------------
# Terminal panel — real-time activity log
# ---------------------------------------------------------------------------

_TERMINAL_HTML = """\
<div class="terminal-wrapper terminal-collapsed" id="terminal-wrapper">
  <div class="terminal-resize" id="terminal-resize"></div>
  <div class="terminal-header" id="terminal-header">
    <span class="material-icons terminal-icon">terminal</span>
    <span class="terminal-title">Terminal</span>
    <span class="terminal-badge" id="terminal-badge">
      <span class="terminal-badge-dot"></span>
      <span id="terminal-badge-text"></span>
      <span class="material-icons terminal-stop-btn" id="terminal-stop-btn"
            title="Stop operation"
            onclick="event.stopPropagation();fetch('/api/cancel-task',{method:'POST'});this.style.display='none'"
            style="display:none;font-size:16px;cursor:pointer;color:#ef4444;margin-left:6px;vertical-align:middle"
            >stop_circle</span>
    </span>
    <span class="terminal-status" id="terminal-status"></span>
    <div class="terminal-actions">
      <span class="material-icons terminal-action-btn"
            onclick="event.stopPropagation();document.getElementById('terminal-content').innerHTML=''"
            title="Clear log">delete_outline</span>
      <span class="material-icons terminal-action-btn terminal-toggle"
            id="terminal-toggle-icon"
            title="Toggle terminal">expand_less</span>
    </div>
  </div>
  <div class="terminal-content" id="terminal-content"></div>
</div>
"""

_TERMINAL_JS = """\
(function(){
  var hdr=document.getElementById('terminal-header');
  var wrap=document.getElementById('terminal-wrapper');
  var handle=document.getElementById('terminal-resize');
  if(!hdr||!wrap||!handle)return;

  hdr.addEventListener('click',function(){
    wrap.classList.toggle('terminal-collapsed');
  });

  var dragging=false,startY=0,startH=0;
  handle.addEventListener('mousedown',function(e){
    e.preventDefault();
    if(wrap.classList.contains('terminal-collapsed'))return;
    dragging=true;startY=e.clientY;
    startH=wrap.offsetHeight;
    document.body.style.cursor='ns-resize';
    document.body.style.userSelect='none';
  });
  window.addEventListener('mousemove',function(e){
    if(!dragging)return;
    var h=startH+(startY-e.clientY);
    if(h<60)h=60;if(h>window.innerHeight*0.7)h=window.innerHeight*0.7;
    wrap.style.height=h+'px';
  });
  window.addEventListener('mouseup',function(){
    if(!dragging)return;
    dragging=false;
    document.body.style.cursor='';
    document.body.style.userSelect='';
  });
})();
"""


def _build_terminal():
    """Inject the live terminal panel at the bottom of every page."""
    ui.html(_TERMINAL_HTML)
    ui.run_javascript(_TERMINAL_JS)

    entries, count = activity_log.get_entries_since(0)
    if entries:
        html_str = _format_terminal_entries(entries)
        ui.run_javascript(
            'var el=document.getElementById("terminal-content");'
            f'if(el){{el.innerHTML={json.dumps(html_str)};el.scrollTop=el.scrollHeight;}}'
        )

    seen = [count]

    def _poll():
        new_entries, new_count = activity_log.get_entries_since(seen[0])
        state = activity_log.get_state()
        js: list[str] = []

        if new_entries:
            html_str = _format_terminal_entries(new_entries)
            js.append(
                'var tc=document.getElementById("terminal-content");'
                f'if(tc){{tc.insertAdjacentHTML("beforeend",{json.dumps(html_str)});'
                'tc.scrollTop=tc.scrollHeight;}'
            )
            seen[0] = new_count

        active = state.get("active_task", "")
        if active:
            plabel = state.get("progress_label", "")
            badge_text = active
            if plabel:
                badge_text += f" ({plabel})"
            elif state.get("progress", 0) > 0:
                badge_text += f" ({int(state['progress'] * 100)}%)"
            js.append(
                'var b=document.getElementById("terminal-badge");'
                'if(b)b.classList.add("terminal-badge-active");'
                f'var bt=document.getElementById("terminal-badge-text");'
                f'if(bt)bt.textContent={json.dumps(badge_text)};'
                'var sb=document.getElementById("terminal-stop-btn");'
                'if(sb)sb.style.display="inline";'
            )
        else:
            js.append(
                'var b=document.getElementById("terminal-badge");'
                'if(b)b.classList.remove("terminal-badge-active");'
                'var sb=document.getElementById("terminal-stop-btn");'
                'if(sb)sb.style.display="none";'
            )

        processing = list(state.get("processing_items", set()))
        pjson = json.dumps(processing)
        js.append(
            f'var items={pjson};'
            'document.querySelectorAll(".match-status-cell").forEach(function(c){'
            'var lid=c.getAttribute("data-lineid"),'
            'dot=c.querySelector(".status-dot"),'
            'sp=c.querySelector(".row-spinner");'
            'if(items.indexOf(lid)!==-1){'
            'if(dot)dot.style.display="none";'
            'if(!sp){var s=document.createElement("span");'
            's.className="row-spinner";c.appendChild(s);}'
            '}else{'
            'if(dot)dot.style.display="";'
            'if(sp)sp.remove();'
            '}});'
        )

        if js:
            ui.run_javascript("\n".join(js))

    ui.timer(0.5, _poll)


def _format_terminal_entries(entries) -> str:
    """Render log entries as terminal HTML lines."""
    def _e(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    parts: list[str] = []
    for e in entries:
        ts = time.strftime("%H:%M:%S", time.localtime(e.timestamp))
        cat = e.category
        msg = _e(e.message)
        raw_text = f"{ts}  {cat.upper()}  {e.message}"
        # Use JSON encoding for safe embedding in onclick attribute
        raw_js = json.dumps(raw_text)
        parts.append(
            f'<div class="terminal-line terminal-{cat}">'
            f'<span class="terminal-time">{ts}</span>'
            f'<span class="terminal-cat">{cat}</span>'
            f'<span class="terminal-msg">{msg}</span>'
            f'<span class="terminal-copy" title="Copy" onclick="navigator.clipboard.writeText({_e(raw_js)});'
            f'this.textContent=&quot;check&quot;;setTimeout(()=>this.textContent=&quot;content_copy&quot;,800)"'
            f'>content_copy</span>'
            f'</div>'
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Image serving — receipts live anywhere on the filesystem
# ---------------------------------------------------------------------------

from fastapi import Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402


@app.get("/api/image")
async def serve_image(path: str) -> Response:
    p = Path(path).expanduser()
    if p.is_file():
        return FileResponse(str(p))
    return Response(status_code=404, content="Not found")


@app.get("/api/pdf-thumb")
async def serve_pdf_thumb(path: str) -> Response:
    """Return a PNG thumbnail of the first page of a PDF."""
    p = Path(path).expanduser()
    if not p.is_file() or p.suffix.lower() != ".pdf":
        return Response(status_code=404, content="Not found")
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(str(p))
        page = doc[0]
        pix = page.get_pixmap(dpi=72)
        png_bytes = pix.tobytes("png")
        doc.close()
        return Response(content=png_bytes, media_type="image/png")
    except Exception:
        return Response(status_code=500, content="Could not render PDF thumbnail")


ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tiff", ".pdf"}


@app.post("/api/upload")
async def api_upload(request: Request, files: list[UploadFile]) -> JSONResponse:
    report_id = (request.query_params.get("report") or "").strip()
    imported: list[str] = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            continue
        content = await f.read()
        dest = svc.import_uploaded_content(f.filename or "upload", content)
        if dest:
            imported.append(dest)
    if report_id and imported:
        svc.assign_receipts_to_report(imported, report_id)
    return JSONResponse({"imported": imported, "count": len(imported)})


@app.post("/api/cancel-task")
async def api_cancel_task() -> JSONResponse:
    """Request cancellation of the current background task."""
    activity_log.request_cancel()
    activity_log.emit("info", "Stop requested — finishing current item…")
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Custom CSS
# ---------------------------------------------------------------------------

_GOOGLE_FONTS_HTML = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet"'
    ' href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700'
    "&family=JetBrains+Mono:wght@400;500;600&display=swap"
    '" media="print" onload="this.media=\'all\'">'
)

CUSTOM_CSS = """

:root {
    --color-high: #16a34a;
    --color-medium: #d97706;
    --color-low: #dc2626;
    --color-unmatched: #6b7280;

    --bg-page: #f1f5f9;
    --bg-card: #ffffff;
    --bg-surface: #f8fafc;
    --bg-row-hover: #f8fafc;
    --bg-row-hover-blue: #f0f7ff;
    --bg-row-selected: #eff6ff;

    --text-primary: #0f172a;
    --text-secondary: #1e293b;
    --text-body: #334155;
    --text-muted: #64748b;
    --text-subtle: #94a3b8;

    --border-default: #e2e8f0;
    --border-subtle: #f1f5f9;

    --badge-high-bg: #dcfce7;
    --badge-high-color: #15803d;
    --badge-medium-bg: #fef3c7;
    --badge-medium-color: #b45309;
    --badge-low-bg: #fee2e2;
    --badge-low-color: #b91c1c;
    --badge-unmatched-bg: #f1f5f9;
    --badge-unmatched-color: #64748b;
}

body.body--dark {
    --bg-page: #0a1628;
    --bg-card: #1e293b;
    --bg-surface: #162032;
    --bg-row-hover: #243147;
    --bg-row-hover-blue: #1a2f4a;
    --bg-row-selected: #1a2f4a;

    --text-primary: #f1f5f9;
    --text-secondary: #e2e8f0;
    --text-body: #cbd5e1;
    --text-muted: #94a3b8;
    --text-subtle: #64748b;

    --border-default: #334155;
    --border-subtle: #253347;

    --badge-high-bg: rgba(22,163,74,0.15);
    --badge-high-color: #4ade80;
    --badge-medium-bg: rgba(217,119,6,0.15);
    --badge-medium-color: #fbbf24;
    --badge-low-bg: rgba(220,38,38,0.15);
    --badge-low-color: #f87171;
    --badge-unmatched-bg: rgba(100,116,139,0.15);
    --badge-unmatched-color: #94a3b8;
}

body {
    font-family: 'Inter', system-ui, -apple-system, sans-serif !important;
    background: var(--bg-page) !important;
    color: var(--text-primary);
}

.q-drawer { background: #ffffff !important; border-right: 1px solid #e2e8f0; }
body.body--dark .q-drawer { background: #0f172a !important; border-right: 1px solid rgba(255,255,255,0.07); }
.q-drawer.detail-side-drawer {
    background: var(--bg-card) !important;
    border-left: 1px solid var(--border-default);
}
.q-drawer.detail-side-drawer .detail-panel {
    position: static;
    max-height: none;
    overflow-x: hidden;
    overflow-y: visible;
    box-shadow: none;
    border-radius: 0;
}

.nicegui-content {
    padding: 0 !important;
}

.page-container {
    max-width: 1400px;
    margin: 0 auto;
    padding: 32px 40px 280px;
    box-sizing: border-box;
    width: 100%;
    min-width: 0;
}

/* Title + primary actions: wrap so buttons are never clipped at the window edge */
.page-hero-row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 1rem 1.5rem;
    width: 100%;
    max-width: 100%;
    box-sizing: border-box;
    margin-bottom: 1.5rem;
}
.page-hero-title {
    min-width: 0;
    flex: 1 1 200px;
}
.page-hero-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: flex-end;
    gap: 0.75rem;
    margin-left: auto;
    max-width: 100%;
    min-width: 0;
}
.page-hero-actions.column-end {
    align-items: flex-end;
}
.page-hero-actions.is-stack {
    flex-direction: column;
    align-items: flex-end;
    align-self: flex-end;
}

.stat-card {
    background: var(--bg-card);
    border-radius: 12px;
    padding: 24px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    transition: box-shadow 0.2s;
    height: 90px;
    display: flex;
    align-items: center;
    cursor: pointer;
}
.stat-card:hover { box-shadow: 0 4px 12px rgba(0,0,0,0.12); }
.stat-number { font-size: 2rem; font-weight: 700; line-height: 1.2; }
.stat-label { font-size: 0.85rem; color: var(--text-muted); margin-top: 4px; font-weight: 500; }

.confidence-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 10px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}
.badge-high { background: var(--badge-high-bg); color: var(--badge-high-color); }
.badge-medium { background: var(--badge-medium-bg); color: var(--badge-medium-color); }
.badge-low { background: var(--badge-low-bg); color: var(--badge-low-color); }
.badge-unmatched { background: var(--badge-unmatched-bg); color: var(--badge-unmatched-color); }

.receipt-card {
    background: var(--bg-card);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    transition: all 0.2s;
    cursor: pointer;
}
.receipt-card:hover {
    box-shadow: 0 8px 24px rgba(0,0,0,0.12);
    transform: translateY(-2px);
}
.receipt-card.receipt-selected { box-shadow: 0 0 0 2px #3b82f6, 0 2px 8px rgba(59,130,246,0.15); transform: none; }
.receipt-card .remove-doc { opacity: 0; }
.receipt-card:hover .remove-doc { opacity: 1; color: #94a3b8; }
.receipt-card:hover .remove-doc:hover { color: #ef4444; background: #fef2f2; }

.match-card {
    background: var(--bg-card);
    border-radius: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    overflow: hidden;
}

.matching-layout, .documents-layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 420px);
    gap: 24px;
    align-items: start;
    width: 100%;
    max-width: 100%;
    min-width: 0;
    box-sizing: border-box;
}

.doc-detail-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
}
.doc-detail-table th {
    text-align: left;
    padding: 6px 10px;
    font-weight: 600;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    background: var(--bg-surface);
    border-bottom: 1px solid var(--border-default);
}
.doc-detail-table td {
    padding: 6px 10px;
    color: var(--text-body);
    border-bottom: 1px solid var(--border-subtle);
    vertical-align: top;
}
.doc-detail-table tr:last-child td { border-bottom: none; }
.doc-detail-label { color: var(--text-muted); font-weight: 500; font-size: 0.78rem; white-space: nowrap; }
.doc-detail-value { font-weight: 500; }

.match-table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    background: var(--bg-card);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.match-table th {
    padding: 12px 16px;
    text-align: left;
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    background: var(--bg-surface);
    border-bottom: 1px solid var(--border-default);
    white-space: nowrap;
}
.match-table td {
    padding: 10px 16px;
    font-size: 0.84rem;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-subtle);
    white-space: nowrap;
}
.match-table tr:last-child td { border-bottom: none; }
.match-table tbody tr { cursor: pointer; transition: background 0.1s; }
.match-table tbody tr:hover td { background: var(--bg-row-hover-blue); }
.match-table tbody tr.row-selected td { background: var(--bg-row-selected); box-shadow: inset 3px 0 0 #3b82f6; }
.match-table tbody tr.row-high td:first-child { box-shadow: inset 3px 0 0 var(--color-high); }
.match-table tbody tr.row-medium td:first-child { box-shadow: inset 3px 0 0 var(--color-medium); }
.match-table tbody tr.row-low td:first-child { box-shadow: inset 3px 0 0 var(--color-low); }
.match-table tbody tr.row-unmatched td:first-child { box-shadow: inset 3px 0 0 var(--color-unmatched); }
.match-table tbody tr.row-selected td:first-child { box-shadow: inset 3px 0 0 #3b82f6; }

.match-table .cell-merchant { font-weight: 600; max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
.match-table .cell-amount { font-variant-numeric: tabular-nums; font-weight: 500; }
.match-table .cell-receipt { max-width: 140px; overflow: hidden; text-overflow: ellipsis; color: var(--text-muted); font-size: 0.78rem; }

.detail-panel {
    background: var(--bg-card);
    border-radius: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
    overflow-x: hidden;
    overflow-y: auto;
    max-height: calc(100vh - 120px);
    position: sticky;
    top: 100px;
}
.detail-panel-header {
    padding: 20px 24px 16px;
    border-bottom: 1px solid var(--border-subtle);
}
.detail-panel-body {
    padding: 20px 24px;
}
.detail-panel-actions {
    padding: 16px 24px;
    border-top: 1px solid var(--border-subtle);
    background: var(--bg-surface);
}

.status-dot {
    width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex-shrink: 0;
}
.status-dot-high { background: var(--color-high); }
.status-dot-medium { background: var(--color-medium); }
.status-dot-low { background: var(--color-low); }
.status-dot-unmatched { background: var(--color-unmatched); }

.nav-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 14px;
    border-radius: 8px;
    color: #94a3b8;
    text-decoration: none;
    font-size: 0.8rem;
    font-weight: 500;
    transition: all 0.15s;
    cursor: pointer;
    margin: 1px 8px;
    white-space: nowrap;
    overflow: hidden;
}
.nav-item { color: #475569; }
.nav-item:hover { background: #f1f5f9; color: #1e293b; }
.nav-item.active { background: #3b82f6; color: white; }
body.body--dark .nav-item { color: #94a3b8; }
body.body--dark .nav-item:hover { background: #1e293b; color: #e2e8f0; }

/* Header light/dark */
.ea-header { background: #ffffff !important; border-bottom: 1px solid #e2e8f0 !important; }
.ea-header .header-title { color: #0f172a; }
.ea-header .header-menu-btn { color: #475569; }
body.body--dark .ea-header { background: #0f172a !important; border-bottom: 1px solid rgba(255,255,255,0.07) !important; }
body.body--dark .ea-header .header-title { color: #f1f5f9; }
body.body--dark .ea-header .header-menu-btn { color: rgba(255,255,255,0.65); }

/* Theme switcher light/dark */
.ea-theme-toggle-btn { color: #475569; }
body.body--dark .ea-theme-toggle-btn { color: rgba(255,255,255,0.65); }
.ea-theme-dropdown { background: #ffffff; border: 1px solid #e2e8f0; }
body.body--dark .ea-theme-dropdown { background: #1e293b; border: 1px solid rgba(255,255,255,0.1); box-shadow: 0 8px 28px rgba(0,0,0,0.45); }
.ea-theme-menu-item { color: #334155; }
body.body--dark .ea-theme-menu-item { color: #e2e8f0; }

/* Sidebar section titles light/dark */
.q-drawer .nav-section-title { color: #94a3b8; }
body.body--dark .q-drawer .nav-section-title { color: #475569; }

/* Hamburger menu button — hidden on wide viewports */
.hamburger-btn { display: none !important; }
@media (max-width: 767px) {
    .hamburger-btn { display: inline-flex !important; }
}

.action-btn {
    border-radius: 10px !important;
    padding: 10px 24px !important;
    font-weight: 600 !important;
    text-transform: none !important;
    letter-spacing: 0 !important;
}

/* ---- Report header bar (sub-header) ---- */
.report-header-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 28px;
    background: var(--bg-card);
    border-bottom: 1px solid var(--border-default);
    position: sticky;
    top: 0;
    z-index: 5;
}
.report-step-indicator {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: 0.72rem;
    font-weight: 600;
    color: #cbd5e1;
    letter-spacing: 0.01em;
}
.report-step-indicator.step-complete { color: #16a34a; }
.report-step-indicator.step-partial  { color: #d97706; }
.report-step-indicator.step-pending  { color: #cbd5e1; }


.section-title {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-primary);
    margin-bottom: 8px;
}

.section-subtitle {
    font-size: 0.9rem;
    color: var(--text-muted);
    margin-bottom: 24px;
}

.data-table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    background: var(--bg-card);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.data-table th {
    padding: 14px 20px;
    text-align: left;
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
    background: var(--bg-surface);
    border-bottom: 1px solid var(--border-default);
}
.data-table td {
    padding: 14px 20px;
    font-size: 0.875rem;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-subtle);
}
.data-table tr:last-child td { border-bottom: none; }
.data-table tr:hover td { background: var(--bg-row-hover); }

.empty-state {
    text-align: center;
    padding: 80px 40px;
    color: var(--text-subtle);
}
.empty-state .icon { font-size: 3rem; margin-bottom: 16px; }
.empty-state .title { font-size: 1.1rem; font-weight: 600; color: var(--text-muted); margin-bottom: 8px; }
.empty-state .desc { font-size: 0.9rem; }

.classify-table {
    background: var(--bg-card);
    border-radius: 12px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    overflow: hidden;
}
.classify-header {
    display: grid;
    grid-template-columns: 1fr 360px;
    gap: 0;
    padding: 8px 16px;
    background: var(--bg-surface);
    border-bottom: 1px solid var(--border-default);
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-muted);
}
.classify-header span { cursor: pointer; user-select: none; }
.classify-header span:hover { color: var(--text-body); }

.sortable-header {
    cursor: pointer;
    user-select: none;
    display: inline-flex;
    align-items: center;
    gap: 2px;
    transition: color 0.15s, background 0.15s;
}
.sortable-header:hover { color: var(--text-body); background: var(--border-default); border-radius: 4px; }
.classify-row {
    display: grid;
    grid-template-columns: 1fr 360px;
    gap: 0;
    align-items: center;
    padding: 4px 16px;
    border-bottom: 1px solid var(--border-subtle);
    font-size: 0.84rem;
    color: var(--text-secondary);
    transition: background 0.1s;
}
.classify-row:last-child { border-bottom: none; }
.classify-row:hover { background: var(--bg-row-hover); }
.classify-row .q-field { margin: 0; padding: 0; }
.classify-row .q-field--dense .q-field__control { min-height: 36px; height: 36px; }
.classify-row .q-field--dense .q-field__native,
.classify-row .q-field--dense .q-field__append { min-height: 36px; height: 36px; font-size: 0.84rem; }
.classify-row .q-field--dense .q-field__label { display: none; }
.classify-merchant { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.classify-dim { color: var(--text-muted); font-size: 0.8rem; }
.classify-warn { font-size: 0.72rem; color: #d97706; margin-top: 1px; }
.classify-search { margin-bottom: 12px; }

.review-progress {
    height: 8px;
    border-radius: 4px;
    background: var(--border-default);
    overflow: hidden;
}
.review-progress-fill {
    height: 100%;
    border-radius: 4px;
    background: linear-gradient(90deg, #3b82f6, #2563eb);
    transition: width 0.3s;
}


.txn-action-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 20px;
    background: #1e293b;
    border-radius: 12px;
    color: white;
    font-size: 0.85rem;
    font-weight: 500;
    box-shadow: 0 8px 24px rgba(0,0,0,0.15);
}

.txn-checkbox { cursor: pointer; width: 18px; height: 18px; accent-color: #3b82f6; }

.data-table .txn-row-selected td { background: var(--bg-row-selected) !important; }

.report-inline-select .q-field__control { min-height: 28px !important; padding: 0 6px !important; }
.report-inline-select .q-field__native { font-size: 0.8rem !important; padding: 2px 0 !important; min-height: 28px !important; }
.report-inline-select .q-field__append { padding: 0 !important; }
.report-inline-select .q-field__control:before { border: none !important; }
.report-inline-select .q-field__control:after { border: none !important; }

/* ---- Terminal Panel (footer) ---- */

.terminal-wrapper {
    position: fixed;
    bottom: 0;
    left: 170px;
    right: 0;
    z-index: 100;
    height: 260px;
    display: flex;
    flex-direction: column;
    box-shadow: 0 -2px 16px rgba(0,0,0,0.25);
    transition: height 0.25s ease;
}
.terminal-collapsed {
    height: 40px !important;
    overflow: hidden;
}
.terminal-resize {
    height: 5px;
    cursor: ns-resize;
    background: transparent;
    flex-shrink: 0;
    position: relative;
}
.terminal-resize::after {
    content: '';
    position: absolute;
    left: 50%; top: 50%;
    transform: translate(-50%,-50%);
    width: 36px; height: 3px;
    border-radius: 2px;
    background: #475569;
    opacity: 0;
    transition: opacity 0.15s;
}
.terminal-resize:hover::after { opacity: 1; }
.terminal-collapsed .terminal-resize { display: none; }
.terminal-header {
    background: #1e293b;
    padding: 8px 20px;
    display: flex;
    align-items: center;
    gap: 10px;
    cursor: pointer;
    border-top: 1px solid #334155;
    user-select: none;
    flex-shrink: 0;
    min-height: 38px;
    box-sizing: border-box;
}
.terminal-header:hover { background: #253347; }
.terminal-icon { color: #64748b; font-size: 1rem; }
.terminal-title {
    color: #e2e8f0; font-size: 0.8rem; font-weight: 600; letter-spacing: 0.02em;
    flex-shrink: 0;
}

@keyframes terminal-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
.terminal-badge {
    display: none;
    align-items: center;
    gap: 6px;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 0.68rem;
    font-weight: 600;
    background: #1e3a5f;
    color: #60a5fa;
    margin-left: 8px;
}
.terminal-badge-active {
    display: inline-flex;
    animation: terminal-pulse 1.5s ease-in-out infinite;
}
.terminal-badge-dot {
    width: 6px; height: 6px; border-radius: 50%; background: #60a5fa; flex-shrink: 0;
}
.terminal-status {
    color: #64748b; font-size: 0.72rem; margin-left: auto;
    font-family: 'JetBrains Mono', monospace;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.terminal-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }
.terminal-action-btn {
    color: #64748b;
    cursor: pointer;
    font-size: 1rem;
    border-radius: 4px;
    padding: 2px;
    transition: color 0.15s, background 0.15s;
}
.terminal-action-btn:hover { color: #e2e8f0; background: #334155; }
.terminal-toggle {
    transition: transform 0.3s;
}
.terminal-collapsed .terminal-toggle { transform: rotate(180deg); }
.terminal-content {
    background: #0f172a;
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 6px 0;
    font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', monospace;
    font-size: 0.78rem;
    line-height: 1.6;
}
.terminal-collapsed .terminal-content {
    display: none;
}
.terminal-content::-webkit-scrollbar { width: 6px; }
.terminal-content::-webkit-scrollbar-track { background: transparent; }
.terminal-content::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
.terminal-content::-webkit-scrollbar-thumb:hover { background: #475569; }
.terminal-line {
    padding: 1px 20px;
    display: flex;
    gap: 10px;
    align-items: baseline;
}
.terminal-line:hover { background: rgba(255,255,255,0.02); }
.terminal-time {
    color: #475569; flex-shrink: 0; font-size: 0.72rem; min-width: 62px;
}
.terminal-cat {
    flex-shrink: 0; font-weight: 600; font-size: 0.72rem;
    min-width: 58px; text-transform: uppercase; letter-spacing: 0.03em;
}
.terminal-msg { color: #cbd5e1; word-break: break-word; }
.terminal-copy {
    font-family: 'Material Icons'; font-size: 14px; color: #475569;
    cursor: pointer; opacity: 0; transition: opacity 0.15s;
    flex-shrink: 0; margin-left: auto; padding: 0 4px;
    user-select: none;
}
.terminal-line:hover .terminal-copy { opacity: 0.7; }
.terminal-copy:hover { opacity: 1 !important; color: #94a3b8; }

.terminal-llm .terminal-cat { color: #60a5fa; }
.terminal-llm .terminal-msg { color: #93c5fd; }
.terminal-cache .terminal-cat { color: #4ade80; }
.terminal-cache .terminal-msg { color: #86efac; }
.terminal-match .terminal-cat { color: #a78bfa; }
.terminal-match .terminal-msg { color: #c4b5fd; }
.terminal-error .terminal-cat { color: #f87171; }
.terminal-error .terminal-msg { color: #fca5a5; }
.terminal-info .terminal-cat { color: #94a3b8; }
.terminal-info .terminal-msg { color: #cbd5e1; }
.terminal-step .terminal-cat { color: #fbbf24; }
.terminal-step .terminal-msg { color: #fde68a; font-weight: 500; }
.terminal-success .terminal-cat { color: #34d399; }
.terminal-success .terminal-msg { color: #6ee7b7; }

/* ---- Matching row spinner ---- */

@keyframes match-spin { to { transform: rotate(360deg); } }
.row-spinner {
    display: inline-block;
    width: 12px; height: 12px;
    border: 2px solid #60a5fa;
    border-top-color: transparent;
    border-radius: 50%;
    animation: match-spin 0.8s linear infinite;
    vertical-align: middle;
}

/* ---- Dark mode: Quasar component overrides ---- */

body.body--dark .q-page-container, body.body--dark .q-page { background: var(--bg-page) !important; }

body.body--dark .q-dialog .q-card { background: var(--bg-card) !important; color: var(--text-primary) !important; }
body.body--dark .q-dialog .q-card .q-card__section { color: var(--text-primary) !important; }

body.body--dark .q-field__control { background: var(--bg-surface) !important; }
body.body--dark .q-field__control:before { border-color: var(--border-default) !important; }
body.body--dark .q-field__native, body.body--dark .q-field__input { color: var(--text-primary) !important; }
body.body--dark .q-field__label { color: var(--text-muted) !important; }
body.body--dark .q-select__dropdown-icon { color: var(--text-muted) !important; }

body.body--dark .q-menu { background: var(--bg-card) !important; color: var(--text-primary) !important; }
body.body--dark .q-item { color: var(--text-primary) !important; }
body.body--dark .q-item:hover, body.body--dark .q-item--active { background: var(--bg-row-hover) !important; }

body.body--dark .bg-white { background-color: var(--bg-card) !important; }
body.body--dark .bg-slate-50 { background-color: var(--bg-surface) !important; }
body.body--dark .text-slate-900, body.body--dark .text-slate-800 { color: var(--text-primary) !important; }
body.body--dark .text-slate-700, body.body--dark .text-slate-600 { color: var(--text-body) !important; }
body.body--dark .text-slate-500 { color: var(--text-muted) !important; }
body.body--dark .text-blue-600 { color: #60a5fa !important; }
body.body--dark .shadow-sm { box-shadow: 0 1px 3px rgba(0,0,0,0.3) !important; }

body.body--dark ::-webkit-scrollbar-track { background: var(--bg-surface); }
body.body--dark ::-webkit-scrollbar-thumb { background: #475569; border-radius: 3px; }
body.body--dark ::-webkit-scrollbar-thumb:hover { background: #64748b; }
"""


# ---------------------------------------------------------------------------
# Shared layout
# ---------------------------------------------------------------------------

def _badge_html(status: str, confidence: float = 0.0, *, label: str | None = None) -> str:
    cls = f"badge-{status}"
    if label is None:
        label = status.capitalize()
        if status != "unmatched" and confidence:
            label = f"{int(confidence * 100)}% {label}"
    return f'<span class="confidence-badge {cls}">{label}</span>'


def _img_url(path: str) -> str:
    return f"/api/image?path={urllib.parse.quote(path, safe='')}"


def _pdf_thumb_url(path: str) -> str:
    return f"/api/pdf-thumb?path={urllib.parse.quote(path, safe='')}"


def shared_header(nav_drawer=None):
    with ui.header().classes("items-center px-4 ea-header").style(
        "box-shadow: none"
    ):
        with ui.row().classes("items-center gap-3 w-full"):
            if nav_drawer is not None:
                ui.button(
                    icon="menu", on_click=nav_drawer.toggle
                ).props("flat dense round").classes("hamburger-btn header-menu-btn")
            ui.icon("receipt_long").classes("text-2xl").style("color: #3b82f6")
            ui.label("Expense Automator").classes(
                "text-lg font-bold tracking-tight header-title"
            )
            ui.html(_THEME_SWITCHER_HTML).style("margin-left: auto")


def shared_nav(active: str, report_id: str = ""):
    """Fixed left sidebar with compact nav items.

    Returns the drawer element so the header hamburger button can toggle it.
    Quasar's breakpoint prop auto-hides the drawer on narrow viewports.
    """
    _REPORT_NAV_PAGES = {"documents", "transactions", "matching", "submit"}
    drawer = ui.left_drawer(value=True, fixed=True, bordered=False).classes(
        ""
    ).props('width=170 :breakpoint=768')
    with drawer:
        ui.html(
            '<div class="nav-section-title" style="font-size:0.6rem;font-weight:600;'
            'letter-spacing:0.1em;padding:10px 14px 4px">'
            'WORKFLOW</div>'
        )
        items = [
            ("/", "space_dashboard", "Dashboard"),
            ("/documents", "description", "Documents"),
            ("/transactions", "receipt_long", "Transactions"),
            ("/matching", "compare_arrows", "Matching"),
            ("/submit", "send", "Submit"),
        ]
        for href, icon, label in items:
            is_active = label.lower() == active.lower()
            cls = "nav-item active" if is_active else "nav-item"
            nav_href = href
            if report_id and href.lstrip("/") in _REPORT_NAV_PAGES:
                nav_href = f"{href}?report={report_id}"
            with ui.link(target=nav_href).classes("no-underline"):
                ui.html(
                    f'<div class="{cls}">'
                    f'<span class="material-icons" style="font-size:1.05rem;flex-shrink:0">{icon}</span>'
                    f'<span>{label}</span></div>'
                )

        ui.element("div").style("flex:1")

        ui.html(
            '<div class="nav-section-title" style="font-size:0.6rem;font-weight:600;'
            'letter-spacing:0.1em;padding:10px 14px 4px">'
            'SETTINGS</div>'
        )
        settings_items = [
            ("/classification", "category", "Classification"),
            ("/settings", "settings", "Settings"),
        ]
        for href, icon, label in settings_items:
            is_active = label.lower() == active.lower()
            cls = "nav-item active" if is_active else "nav-item"
            with ui.link(target=href).classes("no-underline"):
                ui.html(
                    f'<div class="{cls}">'
                    f'<span class="material-icons" style="font-size:1.05rem;flex-shrink:0">{icon}</span>'
                    f'<span>{label}</span></div>'
                )

        ui.element("div").style("flex:1")

        with ui.element("div").style(
            "padding:8px 14px 12px;font-size:0.65rem;cursor:pointer;"
            "display:flex;align-items:center;gap:4px"
        ).classes("nav-section-title").on("click", lambda: _open_update_check_dialog()):
            ui.icon("info_outline").style("font-size:0.8rem")
            ui.label(f"v{_VERSION}")
    return drawer


def _parse_date_sort_key(d: str) -> str:
    """Return an ISO-style string for date comparison. Falls back to original."""
    if not d:
        return ""
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(d.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return d


_AMOUNT_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")

def _safe_amount_float(s: str) -> float:
    """Parse an amount string to float for sorting, handling commas, currency codes, etc."""
    if not s:
        return 0.0
    m = _AMOUNT_RE.search(s)
    if not m:
        return 0.0
    try:
        return float(m.group().replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _sort_arrow(sort_col: str | None, sort_asc: bool, col: str) -> str:
    if sort_col != col:
        return ""
    return " \u25b2" if sort_asc else " \u25bc"


def _detail_row(label: str, value: str):
    with ui.element("tr"):
        with ui.element("td").classes("doc-detail-label"):
            ui.label(label)
        with ui.element("td").classes("doc-detail-value"):
            ui.label(value)


def report_header_bar(active_page: str, report_id: str = ""):
    """Compact report-selector bar with step status indicators.

    Shown on Documents, Transactions, Matching, and Submit pages.
    Navigates to the same page with ?report=<id> on selection change.
    """
    groups = svc.get_expense_report_groups()
    assigned_count = sum(len(g.line_ids) for g in groups)
    total_txns = len(svc.get_transactions())
    uncategorized_count = max(0, total_txns - assigned_count)

    report_options: dict[str, str] = {"": "All (no filter)"}
    report_options["__uncategorized__"] = f"Uncategorized ({uncategorized_count} items)"
    for g in sorted(groups, key=lambda g: g.name):
        report_options[g.id] = f"{g.name} ({len(g.line_ids)} items)"

    with ui.element("div").classes("report-header-bar"):
        ui.label("Report").style(
            "font-size:0.8rem;font-weight:700;color:var(--text-muted);white-space:nowrap"
        )
        ui.select(
            options=report_options,
            value=report_id if report_id in report_options else "",
            on_change=lambda e: ui.navigate.to(
                f"/{active_page}?report={e.value}" if e.value else f"/{active_page}"
            ),
        ).props("outlined dense").style("min-width:260px")

        def _new_report():
            with ui.dialog() as dlg, ui.card().style(
                "min-width:380px;border-radius:16px;padding:28px"
            ):
                ui.label("New Report").classes("text-lg font-bold text-slate-800 mb-2")
                ui.label("Give this report a name.").classes(
                    "text-sm text-slate-500 mb-4"
                )
                name_input = ui.input(
                    label="Report name", placeholder="e.g. March Travel"
                ).props("outlined dense").classes("w-full mb-4")

                with ui.row().classes("justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                    def _do_create():
                        name = (name_input.value or "").strip()
                        if not name:
                            ui.notify("Enter a name", type="warning")
                            return
                        g = svc.create_expense_report_group(name)
                        dlg.close()
                        ui.notify(f'Created "{name}"', type="positive")
                        ui.navigate.to(f"/{active_page}?report={g.id}")

                    ui.button("Create", on_click=_do_create).props(
                        "color=primary no-caps unelevated"
                    )
            dlg.open()

        ui.button("+ New", on_click=_new_report).props(
            "flat dense no-caps size=sm"
        ).style("font-weight:600;color:#3b82f6")

        def _manage_reports():
            with ui.dialog() as dlg, ui.card().style(
                "min-width:460px;max-width:560px;border-radius:16px;padding:28px"
            ):
                ui.label("Manage Reports").classes("text-lg font-bold text-slate-800 mb-4")
                current_groups = svc.get_expense_report_groups()
                if not current_groups:
                    ui.label("No reports yet. Use \"+ New\" to create one.").classes(
                        "text-sm text-slate-500"
                    )
                else:
                    for grp in sorted(current_groups, key=lambda g: g.name):
                        n = len(grp.line_ids)
                        with ui.row().classes("items-center w-full gap-2").style(
                            "padding:8px 4px;border-bottom:1px solid #e2e8f0"
                        ):
                            ui.label(grp.name).classes("text-sm font-medium").style("flex:1")
                            ui.label(f"{n} item{'s' if n != 1 else ''}").classes(
                                "text-xs text-slate-400"
                            )

                            def _make_rename(g=grp):
                                def _rename():
                                    dlg.close()
                                    with ui.dialog() as rdlg, ui.card().style(
                                        "min-width:380px;border-radius:16px;padding:28px"
                                    ):
                                        ui.label("Rename Report").classes(
                                            "text-lg font-bold text-slate-800 mb-4"
                                        )
                                        name_input = ui.input(
                                            label="Report name", value=g.name
                                        ).props("outlined dense").classes("w-full mb-4")
                                        with ui.row().classes("justify-end gap-3"):
                                            ui.button("Cancel", on_click=rdlg.close).props(
                                                "flat no-caps"
                                            )

                                            def _do_rename():
                                                name = (name_input.value or "").strip()
                                                if not name:
                                                    ui.notify("Enter a name", type="warning")
                                                    return
                                                svc.rename_expense_report_group(g.id, name)
                                                rdlg.close()
                                                ui.notify(f"Renamed to: {name}", type="positive")
                                                ui.navigate.to(f"/{active_page}?report={report_id}" if report_id else f"/{active_page}")

                                            ui.button("Rename", on_click=_do_rename).props(
                                                "color=primary no-caps unelevated"
                                            )
                                    rdlg.open()
                                return _rename

                            def _make_delete(g=grp):
                                def _delete():
                                    dlg.close()
                                    with ui.dialog() as ddlg, ui.card().style(
                                        "min-width:380px;border-radius:16px;padding:28px"
                                    ):
                                        ui.label("Delete Report").classes(
                                            "text-lg font-bold text-slate-800 mb-2"
                                        )
                                        cnt = len(g.line_ids)
                                        ui.label(
                                            f'Delete "{g.name}"? '
                                            f"{'Its ' if cnt else 'No '}"
                                            f"{cnt} transaction{'s' if cnt != 1 else ''} "
                                            f"will become unassigned."
                                        ).classes("text-sm text-slate-600 mb-6")
                                        with ui.row().classes("justify-end gap-3"):
                                            ui.button("Cancel", on_click=ddlg.close).props(
                                                "flat no-caps"
                                            )

                                            def _do_delete():
                                                svc.delete_expense_report_group(g.id)
                                                ddlg.close()
                                                ui.notify(f"Deleted: {g.name}", type="positive")
                                                nav = f"/{active_page}"
                                                if report_id and report_id != g.id:
                                                    nav += f"?report={report_id}"
                                                ui.navigate.to(nav)

                                            ui.button("Delete", icon="delete", on_click=_do_delete).props(
                                                "color=negative no-caps unelevated"
                                            )
                                    ddlg.open()
                                return _delete

                            ui.button(icon="edit", on_click=_make_rename()).props(
                                "flat dense round size=sm"
                            ).tooltip("Rename")
                            ui.button(icon="delete_outline", on_click=_make_delete()).props(
                                "flat dense round size=sm color=negative"
                            ).tooltip("Delete")

                with ui.row().classes("justify-end mt-4"):
                    ui.button("Close", on_click=dlg.close).props("flat no-caps")
            dlg.open()

        ui.button("Manage", on_click=_manage_reports).props(
            "flat dense no-caps size=sm"
        ).style("font-weight:500;color:var(--text-muted)")

        ui.element("div").style("flex:1")

        if report_id and any(g.id == report_id for g in groups):
            r = svc.get_report_readiness(report_id)
            total = r.total_lines or 1
            steps = [
                ("Docs",   r.with_receipt + r.receipt_missing_marked, total),
                ("Trans",  r.total_lines, total),
                ("Match",  r.matched, total),
                ("Submit", 1 if r.submission_status == "Submitted" else 0, 1),
            ]
            for label, current, step_total in steps:
                if step_total > 0 and current >= step_total:
                    cls = "step-complete"
                    icon_name = "check_circle"
                elif current > 0:
                    cls = "step-partial"
                    icon_name = "radio_button_checked"
                else:
                    cls = "step-pending"
                    icon_name = "radio_button_unchecked"
                with ui.element("div").classes(f"report-step-indicator {cls}"):
                    ui.icon(icon_name).style("font-size:0.95rem")
                    ui.label(label)


_REPORT_PAGES = {"Transactions", "Matching", "Submit"}


def _setup_required_overlay():
    """Full-page dialog directing user to Settings when credentials are missing."""
    if svc.credentials_ready():
        return

    missing = svc.missing_credentials()

    with ui.dialog() as setup_dlg, ui.card().style(
        "width:min(560px,calc(100vw - 48px));max-width:560px;"
        "border-radius:20px;padding:36px 40px;"
        "box-shadow:0 25px 50px rgba(0,0,0,0.25);"
    ):
        with ui.column().classes("w-full gap-0 items-stretch"):
            with ui.row().classes("w-full justify-center mb-3"):
                with ui.element("div").style(
                    "width:56px;height:56px;border-radius:14px;"
                    "background:linear-gradient(135deg,#3b82f6,#8b5cf6);"
                    "display:flex;align-items:center;justify-content:center;"
                ):
                    ui.icon("settings").style("color:#fff;font-size:28px")

            ui.label("Setup required").classes(
                "text-h5 font-bold w-full text-center"
            ).style("color:var(--text-primary);letter-spacing:-0.02em")

            ui.html(
                """
                <div style="text-align:left;color:var(--text-body);font-size:0.9rem;
                    line-height:1.5;width:100%;margin:12px 0 16px 0">
                  <ul style="margin:0;padding-left:1.2rem">
                    <li style="margin:0 0 6px 0">Matches receipts to lines and fills your Oracle report using your browser.</li>
                    <li style="margin:0 0 6px 0">Oracle username and password are <b>not</b> stored—you sign in in the browser when needed.</li>
                    <li style="margin:0 0 6px 0">Your data stays on this computer.</li>
                    <li style="margin:0">OpenAI reads receipt images for matching—do <b>not</b> use with receipts you cannot send to OpenAI.</li>
                  </ul>
                </div>
                """
            )

            ui.label(
                "Still needed: " + ", ".join(missing)
            ).classes("text-sm font-semibold w-full text-center").style(
                "color:var(--text-muted);margin-bottom:16px"
            )

            with ui.row().classes("w-full justify-center gap-3"):
                ui.button(
                    "Open Settings",
                    icon="settings",
                    on_click=lambda: ui.navigate.to("/settings"),
                ).props("no-caps unelevated color=primary size=lg").classes("action-btn")

    setup_dlg.props('persistent')
    setup_dlg.open()


_DARK_MODE_JS = """<script>
(function () {
    var PREF_KEY = 'ea-theme';
    var mq = window.matchMedia('(prefers-color-scheme: dark)');

    function getSystemDark() { return mq.matches; }
    function getPref() { return localStorage.getItem(PREF_KEY) || 'system'; }

    function applyTheme(pref) {
        var dark = pref === 'dark' || (pref === 'system' && getSystemDark());
        if (window.Quasar && window.Quasar.Dark) {
            window.Quasar.Dark.set(dark);
        }
        var icon = document.getElementById('ea-theme-icon');
        if (icon) {
            icon.textContent = pref === 'light' ? 'light_mode' : pref === 'dark' ? 'dark_mode' : 'brightness_auto';
        }
        ['light', 'dark', 'system'].forEach(function (t) {
            var el = document.getElementById('ea-theme-check-' + t);
            if (el) el.style.opacity = t === pref ? '1' : '0';
        });
    }

    function setTheme(pref) {
        localStorage.setItem(PREF_KEY, pref);
        applyTheme(pref);
        var menu = document.getElementById('ea-theme-menu');
        if (menu) menu.style.display = 'none';
    }

    function bindUI() {
        var wrap = document.getElementById('ea-theme-btn');
        var menu = document.getElementById('ea-theme-menu');
        var toggler = wrap && wrap.querySelector('button');
        if (!wrap || !menu || !toggler) return false;

        toggler.addEventListener('click', function (e) {
            e.stopPropagation();
            menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
        });
        toggler.addEventListener('mouseenter', function () {
            var isDark = document.body.classList.contains('body--dark');
            toggler.style.background = isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.06)';
        });
        toggler.addEventListener('mouseleave', function () {
            toggler.style.background = 'transparent';
        });

        ['light', 'dark', 'system'].forEach(function (t) {
            var item = document.getElementById('ea-theme-item-' + t);
            if (!item) return;
            item.addEventListener('click', function () { setTheme(t); });
            item.addEventListener('mouseenter', function () {
                var isDark = document.body.classList.contains('body--dark');
                item.style.background = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.04)';
            });
            item.addEventListener('mouseleave', function () { item.style.background = 'transparent'; });
        });

        document.addEventListener('click', function (e) {
            if (!wrap.contains(e.target)) menu.style.display = 'none';
        });
        return true;
    }

    mq.addEventListener('change', function () {
        if (getPref() === 'system') applyTheme('system');
    });

    var attempts = 0;
    function init() {
        if (window.Quasar && window.Quasar.Dark) {
            applyTheme(getPref());
            if (!bindUI() && attempts < 40) {
                attempts++;
                setTimeout(init, 100);
            }
        } else if (attempts++ < 60) {
            setTimeout(init, 100);
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
</script>"""

_THEME_SWITCHER_HTML = """
<div id="ea-theme-btn" style="position:relative">
  <button
    title="Switch theme"
    class="ea-theme-toggle-btn"
    style="background:transparent;border:none;cursor:pointer;width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;transition:background 0.15s,color 0.15s"
  >
    <span id="ea-theme-icon" class="material-icons" style="font-size:20px">brightness_auto</span>
  </button>
  <div id="ea-theme-menu" class="ea-theme-dropdown" style="display:none;position:absolute;right:0;top:calc(100% + 6px);border-radius:10px;padding:4px;min-width:148px;box-shadow:0 8px 28px rgba(0,0,0,0.15);z-index:9999">
    <div id="ea-theme-item-light"
      class="ea-theme-menu-item"
      style="display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:7px;cursor:pointer;font-size:13.5px;font-family:inherit">
      <span class="material-icons" style="font-size:16px;color:#fbbf24">light_mode</span>
      <span>Light</span>
      <span id="ea-theme-check-light" class="material-icons" style="font-size:14px;margin-left:auto;color:#3b82f6;opacity:0">check</span>
    </div>
    <div id="ea-theme-item-dark"
      class="ea-theme-menu-item"
      style="display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:7px;cursor:pointer;font-size:13.5px;font-family:inherit">
      <span class="material-icons" style="font-size:16px;color:#818cf8">dark_mode</span>
      <span>Dark</span>
      <span id="ea-theme-check-dark" class="material-icons" style="font-size:14px;margin-left:auto;color:#3b82f6;opacity:0">check</span>
    </div>
    <div id="ea-theme-item-system"
      class="ea-theme-menu-item"
      style="display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:7px;cursor:pointer;font-size:13.5px;font-family:inherit">
      <span class="material-icons" style="font-size:16px;color:#94a3b8">brightness_auto</span>
      <span>System</span>
      <span id="ea-theme-check-system" class="material-icons" style="font-size:14px;margin-left:auto;color:#3b82f6;opacity:0">check</span>
    </div>
  </div>
</div>
"""


def page_frame(active: str, report_id: str = ""):
    _schedule_keychain_consent_if_needed()
    ui.add_head_html(_GOOGLE_FONTS_HTML)
    ui.add_head_html(f"<style>{CUSTOM_CSS}</style>")
    ui.add_head_html(_DARK_MODE_JS)
    nav_drawer = shared_nav(active, report_id)
    shared_header(nav_drawer)
    _build_terminal()
    if active in _REPORT_PAGES:
        report_header_bar(active.lower(), report_id)
    if active != "Settings" and not svc.credentials_ready():
        ui.navigate.to("/settings")
    _launch_splash()


def _open_update_check_dialog():
    """Opens a dialog that checks for updates and offers upgrade option."""
    global _update_info, _update_checked

    with ui.dialog() as dlg, ui.card().style(
        "min-width:420px;max-width:520px;border-radius:14px;padding:24px 28px"
    ):
        with ui.row().classes("items-center gap-3 w-full mb-3"):
            ui.icon("system_update").classes("text-blue-500").style("font-size:1.4rem")
            ui.label("Software Update").classes("text-lg font-bold")

        status_label = ui.label("Checking for updates\u2026").style(
            "font-size:0.9rem;color:#64748b"
        )
        spinner = ui.spinner(size="sm").classes("mt-1")

        result_container = ui.element("div").style("display:none")

        progress_container = ui.element("div").style("display:none")
        with progress_container:
            progress_label = ui.label("Downloading update\u2026").style(
                "font-size:0.85rem;color:#64748b;margin-bottom:6px"
            )
            progress_bar = ui.linear_progress(value=0, show_value=False).props(
                "color=primary"
            ).style("width:100%")

        btn_row = ui.row().classes("items-center justify-end gap-2 w-full mt-3")
        with btn_row:
            close_btn = ui.button("Close", on_click=dlg.close).props("flat no-caps")

            # Pre-create action buttons (hidden until check result arrives)
            _update_state: dict[str, Any] = {
                "asset_url": "",
                "dl_progress": -1.0,       # -1 = not started
                "dl_done_path": None,       # path when download completes
                "dl_error": None,           # error message if failed
                "applying": False,
                "apply_error": None,
            }

            def _do_update():
                asset_url = _update_state["asset_url"]
                if not asset_url:
                    return
                from web.updater import download_update
                close_btn.disable()
                update_btn.disable()
                progress_container.style("display:block")
                _update_state["dl_progress"] = 0.0
                _dl_poll_timer.activate()

                is_mac = sys.platform == "darwin"

                def _download_and_apply():
                    try:
                        def _on_progress(downloaded, total):
                            if total > 0:
                                _update_state["dl_progress"] = downloaded / total

                        installer_path = download_update(asset_url, on_progress=_on_progress)
                        _update_state["dl_done_path"] = str(installer_path)
                    except Exception as exc:
                        _update_state["dl_error"] = str(exc)

                threading.Thread(target=_download_and_apply, daemon=True).start()

            def _dl_poll():
                """Poll download state from main thread and update UI."""
                pct = _update_state["dl_progress"]
                if pct >= 0:
                    progress_bar.value = pct
                    progress_label.text = f"Downloading\u2026 {int(pct * 100)}%"

                err = _update_state.get("dl_error")
                if err:
                    _dl_poll_timer.deactivate()
                    progress_label.text = f"Download failed: {err}"
                    progress_label.style("color:#dc2626")
                    close_btn.enable()
                    return

                done_path = _update_state.get("dl_done_path")
                if done_path:
                    _dl_poll_timer.deactivate()
                    _apply_update(Path(done_path))

            def _apply_update(installer_path: Path):
                is_mac = sys.platform == "darwin"
                if is_mac:
                    from web.updater import apply_macos_update
                    progress_label.text = "Installing update and restarting\u2026"
                    progress_bar.props("indeterminate")
                    try:
                        apply_macos_update(installer_path)
                        import time; time.sleep(0.5)
                        os._exit(0)
                    except Exception as exc:
                        progress_label.text = f"Update failed: {exc}"
                        progress_label.style("color:#dc2626")
                        close_btn.enable()
                else:
                    from web.updater import apply_windows_update
                    progress_label.text = "Launching installer and closing\u2026"
                    progress_bar.props("indeterminate")
                    try:
                        apply_windows_update(installer_path)
                        import time; time.sleep(0.5)
                        os._exit(0)
                    except Exception as exc:
                        progress_label.text = f"Update failed: {exc}"
                        progress_label.style("color:#dc2626")
                        close_btn.enable()

            _dl_poll_timer = ui.timer(0.3, _dl_poll, active=False)

            update_btn = ui.button(
                "Update & Restart", icon="system_update", on_click=_do_update
            ).props("no-caps unelevated color=primary")
            update_btn.visible = False

            def _open_releases():
                import webbrowser
                webbrowser.open(
                    "https://github.com/elijah286/oracle-expense-automation/releases"
                )
                dlg.close()

            releases_btn = ui.button(
                "View Releases", icon="open_in_new", on_click=_open_releases
            ).props("no-caps unelevated color=primary")
            releases_btn.visible = False

        def _show_result(info):
            spinner.visible = False
            if not info:
                status_label.text = f"You're on the latest version (v{_VERSION})."
                status_label.style("color:#16a34a")
                return

            version = info["version"]
            is_mac = sys.platform == "darwin"
            is_frozen = getattr(sys, "frozen", False)
            asset_url = info.get("macos_url", "") if is_mac else info.get("windows_url", "")
            is_win = sys.platform == "win32"

            if is_frozen and asset_url and (is_mac or is_win):
                status_label.text = f"Version {version} is available!"
                status_label.style("color:#1e40af;font-weight:600")
                _update_state["asset_url"] = asset_url
                update_btn.visible = True
            elif asset_url:
                status_label.text = f"Version {version} is available!"
                status_label.style("color:#1e40af;font-weight:600")
                _update_state["asset_url"] = asset_url
                update_btn.visible = True
            else:
                status_label.text = (
                    f"Version {version} is available! Build is in progress\u2026"
                )
                status_label.style("color:#1e40af;font-weight:600")
                releases_btn.visible = True

            notes = info.get("notes", "").strip()
            changelog = info.get("changelog", [])

            if changelog:
                result_container.style("display:block")
                try:
                    with result_container:
                        with ui.expansion("What's new").classes(
                            "w-full"
                        ).props("dense header-class='text-sm text-slate-600 font-medium'").style(
                            "margin-top:8px;border:1px solid #e2e8f0;border-radius:8px;"
                            "background:#f8fafc"
                        ) as exp:
                            exp.style("padding:0")
                            for entry in changelog:
                                ver = entry.get("version", "")
                                desc = entry.get("description", "")
                                date = entry.get("date", "")
                                if not desc:
                                    continue
                                with ui.element("div").style(
                                    "padding:6px 14px;border-bottom:1px solid #f1f5f9"
                                ):
                                    with ui.row().classes("items-center gap-2"):
                                        if ver:
                                            ui.html(
                                                f'<span style="background:#e0e7ff;color:#3730a3;'
                                                f'padding:1px 8px;border-radius:999px;font-size:0.7rem;'
                                                f'font-weight:600">{_esc(ver)}</span>'
                                            )
                                        ui.label(desc).style(
                                            "font-size:0.82rem;color:#334155;line-height:1.4"
                                        )
                                    if date:
                                        ui.label(date).style(
                                            "font-size:0.7rem;color:#94a3b8;margin-top:1px"
                                        )
                except Exception:
                    pass
            elif notes:
                result_container.style("display:block")
                try:
                    with result_container:
                        with ui.element("div").style(
                            "max-height:160px;overflow-y:auto;font-size:0.82rem;line-height:1.55;"
                            "color:#475569;background:#f8fafc;border-radius:8px;padding:10px 14px;"
                            "margin-top:8px;border:1px solid #e2e8f0"
                        ):
                            ui.html(f"<div style='white-space:pre-wrap'>{notes}</div>")
                except Exception:
                    pass

        _check_done = {"value": False, "info": None}

        def _check():
            # Always do a fresh check so we get the latest asset URLs
            from web.updater import check_for_update
            result = check_for_update(_VERSION)
            with _update_lock:
                global _update_info, _update_checked
                _update_info = result
                _update_checked = True
            _check_done["info"] = result
            _check_done["value"] = True

        def _poll_check():
            if _check_done["value"]:
                poll_timer.deactivate()
                _show_result(_check_done["info"])

        threading.Thread(target=_check, daemon=True).start()
        poll_timer = ui.timer(0.3, _poll_check)

    dlg.open()


def _launch_splash() -> None:
    """Unified splash screen for updates and Chromium setup on launch."""
    from web import startup

    if not startup.splash_active():
        return

    overlay = ui.element("div").style(
        "position:fixed;inset:0;z-index:9999;"
        "display:flex;align-items:center;justify-content:center;"
        "background:rgba(255,255,255,0.97);"
    )
    overlay.classes("launch-splash-overlay")
    ui.add_head_html("""<style>
    body.body--dark .launch-splash-overlay {
        background: rgba(30,30,30,0.97) !important;
    }
    body.body--dark .launch-splash-overlay .splash-title { color: #e2e8f0 !important; }
    body.body--dark .launch-splash-overlay .splash-status { color: #94a3b8 !important; }
    body.body--dark .launch-splash-overlay .splash-hint { color: #475569 !important; }
    </style>""")

    with overlay:
        with ui.column().classes("items-center gap-4").style("text-align:center;max-width:420px"):
            ui.icon("receipt_long").classes("text-blue-500").style("font-size:56px")
            ui.label("Expense Automator").classes("splash-title").style(
                "font-size:1.5rem;font-weight:700;color:#1e293b"
            )
            version_label = ui.label(f"v{_VERSION}").classes("splash-hint").style(
                "font-size:0.75rem;color:#94a3b8;margin-top:-8px"
            )
            status_label = ui.label(startup.current_status()).classes("splash-status").style(
                "font-size:0.95rem;color:#64748b"
            )
            progress_bar = ui.linear_progress(value=0, show_value=False).props(
                "indeterminate color=primary"
            ).style("width:280px")
            hint_label = ui.label("").classes("splash-hint").style(
                "font-size:0.82rem;color:#94a3b8;margin-top:4px"
            )

            def _poll():
                status_label.text = startup.current_status()

                # Show target version when available
                info = startup.update_info()
                if info and info.get("version"):
                    version_label.text = f"v{_VERSION}  →  v{info['version']}"

                # Show determinate progress during update download
                if startup.update_downloading():
                    pct = startup.update_progress()
                    if pct > 0:
                        progress_bar.value = pct
                        try:
                            progress_bar.props(remove="indeterminate")
                        except Exception:
                            pass
                    hint_label.text = ""
                elif startup.chromium_downloading():
                    hint_label.text = "This only happens once and may take a minute."
                    try:
                        progress_bar.props("indeterminate")
                    except Exception:
                        pass
                elif startup.update_applying():
                    hint_label.text = "The app will restart automatically."
                    try:
                        progress_bar.props("indeterminate")
                    except Exception:
                        pass

                # Check for errors
                u_err = startup.update_error()
                c_err = startup.chromium_error()
                if u_err:
                    hint_label.text = f"Update skipped: {u_err}"
                    hint_label.style("color:#d97706")

                # Done?
                if not startup.splash_active():
                    timer.deactivate()
                    overlay.delete()
                    if c_err:
                        ui.notify(f"Browser setup failed: {c_err}", type="negative", timeout=10000)

            timer = ui.timer(0.5, _poll)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@ui.page("/")
def page_dashboard():
    page_frame("Dashboard")

    with ui.element("div").classes("page-container"):
        ui.html('<div class="section-title">Dashboard</div>')
        ui.html('<div class="section-subtitle">Overview of your expense report</div>')

        stats = svc.get_dashboard_stats()

        with ui.row().classes("gap-4 w-full flex-wrap"):
            _stat_card("credit_card", str(stats.total_transactions), "Transactions", "#3b82f6", "/transactions")
            _stat_card("receipt", str(stats.total_receipts), "Receipts", "#8b5cf6", "/documents")
            _stat_card("o_check_circle", str(stats.matched_high), "High Confidence", "#16a34a", "/matching")
            _stat_card("help_outline", str(stats.matched_medium), "Need Review", "#d97706", "/matching")
            _stat_card("error_outline", str(stats.unmatched), "Unmatched", "#dc2626", "/matching")
            _stat_card("task_alt", str(stats.approved), "Approved", "#0891b2", "/submit")

        ui.separator().classes("my-6")

        # Workflow guidance
        with ui.card().classes("w-full").style(
            "border-radius: 16px; padding: 32px; border: 2px solid var(--border-default)"
        ):
            if stats.total_transactions == 0:
                ui.html(
                    '<div style="font-size:1.1rem;font-weight:600;color:var(--text-primary);margin-bottom:8px">'
                    "Get Started</div>"
                )
                ui.label(
                    "No transactions loaded. Use the Scrape Transactions button "
                    "to pull credit card transactions from Oracle, then import your receipts."
                ).classes("text-slate-500")
            elif stats.unmatched > 0:
                ui.html(
                    '<div style="font-size:1.1rem;font-weight:600;color:var(--text-primary);margin-bottom:8px">'
                    f'{stats.unmatched} transaction{"s" if stats.unmatched != 1 else ""} need matching</div>'
                )
                ui.label(
                    "Run the matching pipeline, then review and resolve remaining items."
                ).classes("text-slate-500 mb-4")
                with ui.row().classes("items-center gap-3"):
                    ui.button(
                        "Run Auto-Match",
                        icon="auto_fix_high",
                        on_click=_start_auto_match,
                    ).props("color=primary no-caps unelevated").classes("action-btn")
                    ui.button("Go to Matching", on_click=lambda: ui.navigate.to("/matching")).props(
                        "no-caps outline"
                    ).classes("action-btn")
            elif stats.approved < stats.total_transactions:
                pending = stats.total_transactions - stats.approved
                ui.html(
                    '<div style="font-size:1.1rem;font-weight:600;color:var(--text-primary);margin-bottom:8px">'
                    f"{pending} match{'es' if pending != 1 else ''} pending approval</div>"
                )
                ui.label("Review and approve matches to proceed.").classes(
                    "text-slate-500 mb-4"
                )
                ui.button("Go to Submit", on_click=lambda: ui.navigate.to("/submit")).props(
                    "color=primary no-caps unelevated"
                ).classes("action-btn")
            else:
                ui.html(
                    '<div style="font-size:1.1rem;font-weight:600;color:#16a34a;margin-bottom:8px">'
                    "Ready to Submit</div>"
                )
                ui.label("All transactions are matched and approved.").classes(
                    "text-slate-500 mb-4"
                )
                ui.button("Go to Submit", on_click=lambda: ui.navigate.to("/submit")).props(
                    "color=positive no-caps unelevated"
                ).classes("action-btn")


def _stat_card(icon: str, value: str, label: str, color: str, href: str = "/"):
    with ui.element("div").classes("stat-card flex-1").style("min-width: 180px").on(
        "click", lambda _, h=href: ui.navigate.to(h)
    ):
        with ui.row().classes("items-center gap-3"):
            ui.icon(icon).style(f"color: {color}; font-size: 1.5rem")
            with ui.column().classes("gap-0"):
                ui.html(f'<div class="stat-number" style="color:{color}">{value}</div>')
                ui.html(f'<div class="stat-label">{label}</div>')


# ---------------------------------------------------------------------------
# Documents (receipts)
# ---------------------------------------------------------------------------

@ui.page("/documents")
def page_documents(request: Request):
    report_filter_id = (request.query_params.get("report") or "").strip()
    page_frame("Documents", report_filter_id)

    _doc_actions: dict[str, Callable[[], None]] = {"close": lambda: None}

    doc_detail_drawer = ui.right_drawer(value=False, fixed=True, bordered=True).classes(
        "detail-side-drawer"
    ).props("overlay elevated width=440")
    with doc_detail_drawer:
        with ui.column().classes("w-full").style(
            "height:100%;max-height:100vh;display:flex;flex-direction:column"
        ):
            with ui.row().classes("items-center justify-end w-full").style(
                "flex-shrink:0;padding:6px 8px;border-bottom:1px solid var(--border-subtle)"
            ):
                ui.button(icon="close", on_click=lambda: _doc_actions["close"]()).props(
                    "flat dense round"
                )
            doc_detail_slot = ui.column().classes("w-full").style(
                "flex:1;min-height:0;overflow-y:auto;padding:0 10px 24px"
            )

    with ui.element("div").classes("page-container"):

        state: dict[str, Any] = {
            "selected": set(), "sort_col": None, "sort_asc": True, "search": "",
            "selected_doc": None,
        }

        header_container = ui.element("div")
        search_container = ui.element("div")
        results_container = ui.element("div")

        # Page-level timer for live refresh during receipt analysis
        # (must be outside header_container so it survives re-renders)
        def _analysis_poll():
            if state.get("_analysis_running"):
                _render_documents()

        _analysis_refresh_timer = ui.timer(2.0, _analysis_poll, active=False)

        def _doc_toggle_sort(col: str):
            if state["sort_col"] == col:
                state["sort_asc"] = not state["sort_asc"]
            else:
                state["sort_col"] = col
                state["sort_asc"] = True
            _render_documents()

        def _select_doc(source_file: str, shift: bool = False, ctrl: bool = False):
            if shift and state.get("selected_doc"):
                receipts = svc.get_receipts()
                paths = [r.source_file for r in receipts]
                anchor = state["selected_doc"]
                if anchor in paths and source_file in paths:
                    i_a, i_b = paths.index(anchor), paths.index(source_file)
                    lo, hi = min(i_a, i_b), max(i_a, i_b)
                    state["selected"] = set(paths[lo : hi + 1])
                else:
                    state["selected"] = {source_file}
                    state["selected_doc"] = source_file
            elif ctrl:
                if source_file in state["selected"]:
                    state["selected"].discard(source_file)
                    if state["selected_doc"] == source_file:
                        state["selected_doc"] = next(iter(state["selected"]), None)
                else:
                    state["selected"].add(source_file)
                    state["selected_doc"] = source_file
            else:
                state["selected_doc"] = source_file
                state["selected"] = {source_file}
            _render_documents()

        def _clear_selection():
            state["selected"] = set()
            state["selected_doc"] = None
            _render_documents()

        _doc_actions["close"] = _clear_selection

        def _confirm_remove_selected():
            paths = list(state["selected"])
            n = len(paths)
            if not n:
                return
            with ui.dialog() as dlg, ui.card().style(
                "min-width:400px;border-radius:16px;padding:28px"
            ):
                ui.label("Remove Documents").classes(
                    "text-lg font-bold text-slate-800 mb-2"
                )
                ui.label(
                    f"Remove {n} receipt{'s' if n != 1 else ''} from the documents list? "
                    "Files will remain on disk."
                ).classes("text-sm text-slate-600 mb-6")
                with ui.row().classes("items-center justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                    def _do_remove():
                        svc.remove_receipts(paths)
                        state["selected"] = set()
                        dlg.close()
                        ui.notify(
                            f"Removed {n} receipt{'s' if n != 1 else ''}",
                            type="positive",
                        )
                        _render_documents()

                    ui.button(
                        f"Remove {n}", icon="delete_outline", on_click=_do_remove,
                    ).props("color=negative no-caps unelevated").classes("action-btn")
            dlg.open()

        def _confirm_rescan_selected():
            paths = list(state["selected"])
            n = len(paths)
            if not n:
                return
            with ui.dialog() as dlg, ui.card().style(
                "min-width:400px;border-radius:16px;padding:28px"
            ):
                ui.label("Rescan Documents").classes(
                    "text-lg font-bold text-slate-800 mb-2"
                )
                ui.label(
                    f"Re-analyze {n} receipt{'s' if n != 1 else ''} with the LLM? "
                    "This will replace any existing analysis for these files."
                ).classes("text-sm text-slate-600 mb-6")
                with ui.row().classes("items-center justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                    def _do_rescan():
                        state["selected"] = set()
                        dlg.close()
                        ui.notify(f"Rescanning {n} receipt{'s' if n != 1 else ''}...", type="info")
                        _run_background(
                            "Receipt Rescan",
                            lambda on_status: svc.rescan_receipts(paths, on_status=on_status),
                            f"Finished rescanning {n} receipt{'s' if n != 1 else ''}",
                            on_done=lambda _: _render_documents(),
                        )

                    ui.button(
                        f"Rescan {n}", icon="refresh", on_click=_do_rescan,
                    ).props("no-caps unelevated").classes("action-btn").style(
                        "background:#7c3aed !important;color:white !important"
                    )
            dlg.open()

        def _on_search(e):
            state["search"] = e.args or ""
            _render_documents()

        def _render_documents():
            receipts = svc.get_receipts()
            if report_filter_id == "__uncategorized__":
                unassigned_receipts: set[str] = set()
                for item in svc.get_match_review_queue():
                    t = item.transaction
                    if not t.report_id and t.matched_receipt:
                        unassigned_receipts.add(t.matched_receipt)
                receipts = [r for r in receipts if r.source_file in unassigned_receipts]
            elif report_filter_id:
                report_receipt_paths: set[str] = set()
                for item in svc.get_match_review_queue():
                    t = item.transaction
                    if t.report_id == report_filter_id and t.matched_receipt:
                        report_receipt_paths.add(t.matched_receipt)
                # Also include receipts directly assigned to this report
                assigned = svc.get_receipt_report_assignments()
                for src, rid in assigned.items():
                    if rid == report_filter_id:
                        report_receipt_paths.add(src)
                receipts = [r for r in receipts if r.source_file in report_receipt_paths]
            used_count = sum(1 for r in receipts if r.used)
            unreviewed_count = sum(1 for r in receipts if not r.analyzed)

            header_container.clear()
            with header_container:
                # Header row
                with ui.element("div").classes("page-hero-row"):
                    with ui.element("div").classes("page-hero-title"):
                        with ui.column().classes("gap-0"):
                            ui.html('<div class="section-title">Documents</div>')
                            ui.html(
                                '<div class="section-subtitle" style="margin-bottom:0">'
                                "Imported receipts and their extracted data</div>"
                            )
                    with ui.element("div").classes("page-hero-actions"):
                        if used_count:
                            ui.button(
                                f"Remove Used ({used_count})",
                                icon="delete_sweep",
                                on_click=lambda: _confirm_remove_used(used_count),
                            ).props("color=negative no-caps unelevated").classes("action-btn")

                        def _start_review():
                            cnt = sum(1 for r in svc.get_receipts() if not r.analyzed)
                            if not cnt:
                                ui.notify("All receipts already reviewed", type="info")
                                return
                            ui.notify(f"Starting LLM review of {cnt} receipt(s)...", type="info")
                            # Activate page-level refresh timer
                            state["_analysis_running"] = True
                            _analysis_refresh_timer.activate()

                            def _on_analysis_done(_result):
                                state["_analysis_running"] = False
                                _analysis_refresh_timer.deactivate()
                                _render_documents()

                            _run_background(
                                "Receipt Analysis",
                                lambda on_status: svc.analyze_receipts(on_status=on_status),
                                f"Finished analyzing receipts",
                                on_done=_on_analysis_done,
                            )

                        ui.button(
                            f"Scan Unreviewed ({unreviewed_count})" if unreviewed_count else "Scan Unreviewed",
                            icon="auto_fix_high",
                            on_click=_start_review,
                        ).props(
                            "no-caps unelevated"
                        ).classes("action-btn").style(
                            "background:#7c3aed !important;color:white !important"
                        )

                        _upload_report_id = report_filter_id or ""
                        _upload_url = (
                            f"/api/upload?report={urllib.parse.quote(_upload_report_id, safe='')}"
                            if _upload_report_id else "/api/upload"
                        )
                        ui.button(
                            "Add Files",
                            icon="upload_file",
                        ).props("color=primary no-caps unelevated").classes("action-btn").on(
                            "click",
                            js_handler=f'''() => {{
                                const input = document.createElement("input");
                                input.type = "file";
                                input.multiple = true;
                                input.accept = ".jpg,.jpeg,.png,.gif,.webp,.heic,.tiff,.pdf";
                                input.style.display = "none";
                                document.body.appendChild(input);
                                input.onchange = async () => {{
                                    if (!input.files.length) {{
                                        document.body.removeChild(input);
                                        return;
                                    }}
                                    const fd = new FormData();
                                    for (const f of input.files) fd.append("files", f);
                                    try {{
                                        const resp = await fetch("{_upload_url}", {{method: "POST", body: fd}});
                                        const data = await resp.json();
                                        document.body.removeChild(input);
                                        if (data.count > 0) location.reload();
                                    }} catch (e) {{
                                        document.body.removeChild(input);
                                        console.error("Upload failed:", e);
                                    }}
                                }};
                                input.click();
                            }}''',
                        )

            results_container.clear()

            if not receipts:
                search_container.set_visibility(False)
                with results_container:
                    _empty_state(
                        "description",
                        "No receipts imported",
                        "Import receipt images or PDFs to get started.",
                    )
                _sync_doc_detail_drawer()
                return

            search_container.set_visibility(True)
            total_count = len(receipts)

            with results_container:
                # Filter by search query
                _q = state["search"].strip().lower()
                if _q:
                    def _matches(r: ReceiptDoc) -> bool:
                        haystack = " ".join([
                            r.vendor or "",
                            r.filename or "",
                            f"{r.currency} {r.total}" if r.total else "",
                            r.date or "",
                            r.date_added or "",
                            "not reviewed" if not r.analyzed else "",
                            "used" if r.used else "",
                        ]).lower()
                        return _q in haystack
                    receipts = [r for r in receipts if _matches(r)]

                # Summary strip
                filtered_count = len(receipts)
                with ui.row().classes("items-center gap-4 mb-5"):
                    count_text = (
                        f"{filtered_count} of {total_count} receipt{'s' if total_count != 1 else ''}"
                        if _q else
                        f"{total_count} receipt{'s' if total_count != 1 else ''}"
                    )
                    ui.label(count_text).classes(
                        "text-sm font-semibold text-slate-600"
                    )
                    total_amt = sum(
                        float(r.total) for r in receipts
                        if r.total and r.total.replace(".", "", 1).replace("-", "", 1).isdigit()
                    )
                    if total_amt:
                        ui.label(f"·  Est. total: ${total_amt:,.2f}").classes(
                            "text-sm text-slate-400"
                        )
                    filtered_used = sum(1 for r in receipts if r.used)
                    if filtered_used:
                        ui.html(
                            f'<span class="confidence-badge badge-high">'
                            f'{filtered_used} used</span>'
                        )
                    filtered_matched = sum(1 for r in receipts if r.matched and not r.used)
                    if filtered_matched:
                        ui.html(
                            f'<span style="display:inline-flex;align-items:center;gap:4px;'
                            f'padding:2px 8px;border-radius:12px;background:#dbeafe;'
                            f'color:#1d4ed8;font-size:0.7rem;font-weight:600">'
                            f'{filtered_matched} matched</span>'
                        )
                    filtered_unmatched = sum(1 for r in receipts if r.analyzed and not r.matched and not r.used)
                    if filtered_unmatched:
                        ui.html(
                            f'<span class="confidence-badge badge-unmatched">'
                            f'{filtered_unmatched} unmatched</span>'
                        )
                    filtered_unreviewed = sum(1 for r in receipts if not r.analyzed)
                    if filtered_unreviewed:
                        ui.html(
                            f'<span class="confidence-badge badge-medium">'
                            f'{filtered_unreviewed} not reviewed</span>'
                        )

                # Prune selection to only include files still in the list
                valid_files = {r.source_file for r in receipts}
                state["selected"] = state["selected"] & valid_files

                # Sort receipts
                col = state["sort_col"]
                if col:
                    key_funcs = {
                        "vendor": lambda r: (r.vendor or r.filename).lower(),
                        "amount": lambda r: _safe_amount_float(r.total),
                        "date": lambda r: _parse_date_sort_key(r.date),
                        "added": lambda r: r.date_added or "",
                        "parse": lambda r: r.confidence,
                        "status": lambda r: (1 if r.used else 0),
                    }
                    fn = key_funcs.get(col)
                    if fn:
                        receipts = sorted(receipts, key=fn, reverse=not state["sort_asc"])

                _DOC_GRID_COLS = "28px 72px 1fr 120px 110px 100px 90px 110px 40px"

                # Full-width list; detail opens in a right drawer
                with ui.element("div").style("width:100%;min-width:0;overflow-x:auto"):

                    # Bulk action bar
                    sel_count = len(state.get("selected", set()))
                    if sel_count:
                        with ui.row().classes("items-center gap-3 px-5 py-2").style(
                            "background:#eff6ff;border-radius:8px 8px 0 0;border-bottom:1px solid #bfdbfe;"
                        ):
                            ui.label(f"{sel_count} selected").classes("text-xs font-semibold text-blue-600")
                            ui.button(
                                "Delete", icon="delete_outline", on_click=_confirm_remove_selected,
                            ).props("no-caps outline size=xs dense color=negative").style("font-size:0.7rem")
                            ui.button(
                                "Rescan", icon="refresh", on_click=_confirm_rescan_selected,
                            ).props("no-caps outline size=xs dense").style("font-size:0.7rem")
                            ui.element("div").style("flex:1")
                            ui.button(
                                "Clear", icon="close",
                                on_click=lambda: (
                                    state.update({"selected": set(), "selected_doc": None}),
                                    _render_documents(),
                                ),
                            ).props("no-caps flat size=xs dense").style("font-size:0.7rem;color:#94a3b8")

                    # Table header
                    sc, sa = state["sort_col"], state["sort_asc"]
                    _hdr_radius = "border-radius:0;" if sel_count else "border-radius:8px 8px 0 0;"
                    with ui.element("div").style(
                        f"display:grid;grid-template-columns:{_DOC_GRID_COLS};"
                        "gap:0;padding:8px 20px;font-size:0.7rem;font-weight:700;text-transform:uppercase;"
                        "letter-spacing:0.06em;color:var(--text-muted);align-items:center;"
                        f"background:var(--bg-surface);border-bottom:2px solid var(--border-default);{_hdr_radius}"
                        "position:sticky;top:0;z-index:10;min-width:740px;"
                    ):
                        # Select-all checkbox
                        _all_paths = [r.source_file for r in receipts]
                        _all_sel = bool(_all_paths) and all(p in state.get("selected", set()) for p in _all_paths)
                        _some_sel = bool(state.get("selected", set()) & set(_all_paths)) and not _all_sel

                        def _toggle_select_all_docs(e, all_paths=_all_paths):
                            if e.value:
                                state["selected"] = set(all_paths)
                                state["selected_doc"] = all_paths[0] if all_paths else None
                            else:
                                state["selected"] = set()
                                state["selected_doc"] = None
                            _render_documents()

                        _sa_cb = ui.checkbox("", value=_all_sel, on_change=_toggle_select_all_docs).props(
                            "dense size=xs"
                        ).style("margin:0;padding:0;min-height:0")
                        if _some_sel:
                            _sa_cb.props("indeterminate-value=true model-value=true")

                        ui.element("div")  # thumbnail column
                        for col_key, col_label in [
                            ("vendor", "Vendor / File"), ("amount", "Amount"), ("date", "Date"),
                            ("added", "Added"), ("parse", "Parse"), ("status", "Status"),
                        ]:
                            is_active = sc == col_key
                            lbl = ui.label(f"{col_label}{_sort_arrow(sc, sa, col_key)}")
                            lbl.classes("sortable-header")
                            lbl.style(
                                "cursor:pointer;padding:4px 6px;border-radius:4px;transition:all 0.15s;"
                                "user-select:none;"
                                + ("color:#3b82f6;background:var(--bg-row-selected);" if is_active else "")
                            )
                            lbl.on("click", lambda _, c=col_key: _doc_toggle_sort(c))

                    if not receipts and _q:
                        with ui.element("div").style(
                            "padding:40px 20px;text-align:center;"
                        ):
                            ui.icon("search_off").classes("text-4xl text-slate-300 mb-2")
                            ui.label("No receipts match your search").classes(
                                "text-sm text-slate-400"
                            )
                    else:
                        def _make_remove_single(path):
                            def _do():
                                svc.remove_receipts([path])
                                state["selected"].discard(path)
                                if state["selected_doc"] == path:
                                    state["selected_doc"] = None
                                ui.notify("Removed receipt", type="positive")
                                _render_documents()
                            return _do

                        def _make_rescan_single(path):
                            def _do():
                                ui.notify("Rescanning receipt...", type="info")
                                _run_background(
                                    "Receipt Rescan",
                                    lambda on_status: svc.rescan_receipts([path], on_status=on_status),
                                    "Finished rescanning receipt",
                                    on_done=lambda _: _render_documents(),
                                )
                            return _do

                        def _make_toggle_cb(path):
                            def _toggle(e):
                                if e.value:
                                    state["selected"].add(path)
                                    state["selected_doc"] = path
                                else:
                                    state["selected"].discard(path)
                                    if state["selected_doc"] == path:
                                        state["selected_doc"] = next(iter(state["selected"]), None)
                                _render_documents()
                            return _toggle

                        for r in receipts:
                            is_sel = r.source_file in state["selected"]
                            is_focused = state["selected_doc"] == r.source_file
                            _receipt_row(
                                r,
                                selected=is_sel,
                                focused=is_focused,
                                on_row_click=lambda doc=r: _select_doc(doc.source_file),
                                on_remove=_make_remove_single(r.source_file),
                                on_rescan=_make_rescan_single(r.source_file),
                                on_checkbox=_make_toggle_cb(r.source_file),
                            )

            _sync_doc_detail_drawer()

        def _render_doc_detail_content():
            multi = len(state["selected"]) > 1
            if multi:
                n = len(state["selected"])
                with ui.element("div").classes("detail-panel"):
                    with ui.element("div").classes("detail-panel-header"):
                        ui.label(f"{n} documents selected").classes(
                            "text-lg font-bold text-slate-800"
                        )

                    with ui.element("div").classes("detail-panel-body"):
                        ui.label("Bulk Actions").classes(
                            "text-xs font-semibold text-slate-400 tracking-wider mb-3"
                        )
                        with ui.column().classes("gap-2 w-full"):
                            ui.button(
                                "Rescan selected",
                                icon="refresh",
                                on_click=lambda: _confirm_rescan_selected(),
                            ).props("no-caps outline size=sm").classes("action-btn w-full")

                            ui.button(
                                "Remove selected",
                                icon="delete_outline",
                                on_click=lambda: _confirm_remove_selected(),
                            ).props("no-caps outline size=sm color=negative").classes("action-btn w-full")

                    with ui.element("div").classes("detail-panel-actions"):
                        ui.button(
                            "Clear selection",
                            icon="close",
                            on_click=_clear_selection,
                        ).props("no-caps flat size=sm").classes("text-slate-400")
                return

            doc_path = state.get("selected_doc")
            if not doc_path:
                with ui.element("div").classes("detail-panel"):
                    with ui.element("div").style(
                        "padding:60px 24px;text-align:center;color:#94a3b8;"
                    ):
                        ui.icon("touch_app").classes("text-4xl mb-3")
                        ui.label("Select a document").classes(
                            "text-sm font-semibold text-slate-500 mb-1"
                        )
                        ui.label("Click any row to see receipt details").classes("text-xs")
                return

            doc = svc.get_receipt_by_path(doc_path)
            if not doc:
                return

            raw = doc.raw_analysis or {}

            with ui.element("div").classes("detail-panel"):
                # Color bar
                conf_color = (
                    "#16a34a" if doc.confidence >= 0.85
                    else ("#d97706" if doc.confidence >= 0.6 else "#6b7280")
                )
                ui.element("div").style(f"height:4px;background:{conf_color};width:100%")

                # Header with editable vendor and date
                with ui.element("div").classes("detail-panel-header"):
                    vendor_input = ui.input(
                        label="Vendor",
                        value=doc.vendor or "",
                        placeholder=doc.filename,
                    ).props("dense outlined").classes("w-full").style(
                        "font-size:0.95rem;"
                    )

                    def _save_vendor(sf=doc.source_file):
                        new_val = (vendor_input.value or "").strip()
                        svc.update_receipt_fields(sf, {"vendor": new_val})
                        ui.notify("Vendor updated", type="positive")
                        _render_documents()

                    vendor_input.on("blur", lambda _, cb=_save_vendor: cb())
                    vendor_input.on("keydown.enter", lambda _, cb=_save_vendor: cb())

                    with ui.row().classes("items-center gap-3 mt-2 w-full"):
                        if doc.total:
                            ui.label(f"{doc.currency} {doc.total}").classes(
                                "text-xl font-bold text-slate-900"
                            )
                        date_input = ui.input(
                            label="Date",
                            value=doc.date or "",
                            placeholder="YYYY-MM-DD",
                        ).props("dense outlined").classes("flex-1").style(
                            "max-width:160px;font-size:0.85rem;"
                        )

                        def _save_date(sf=doc.source_file):
                            new_val = (date_input.value or "").strip()
                            svc.update_receipt_fields(sf, {"receipt_date": new_val})
                            ui.notify("Date updated", type="positive")
                            _render_documents()

                        date_input.on("blur", lambda _, cb=_save_date: cb())
                        date_input.on("keydown.enter", lambda _, cb=_save_date: cb())

                    with ui.row().classes("items-center gap-2 mt-3"):
                        if doc.confidence:
                            level = (
                                "high" if doc.confidence >= 0.85
                                else ("medium" if doc.confidence >= 0.6 else "low")
                            )
                            ui.html(_badge_html(level, doc.confidence))
                        if doc.used:
                            ui.html(
                                '<span style="display:inline-flex;align-items:center;gap:3px;'
                                'color:#15803d;font-size:0.75rem;font-weight:600">'
                                '<span class="material-icons" style="font-size:0.95rem">check_circle</span>'
                                'Used</span>'
                            )
                        if not doc.analyzed:
                            ui.html(
                                '<span class="confidence-badge badge-medium">Not Reviewed</span>'
                            )

                with ui.element("div").classes("detail-panel-body"):
                    # Thumbnail
                    if doc.is_image and Path(doc.source_file).is_file():
                        rotation = doc.rotation * 90
                        with ui.element("div").style(
                            "width:100%;height:260px;overflow:hidden;"
                            "border-radius:8px;border:1px solid var(--border-default);cursor:pointer;"
                        ).on("click", lambda _, d=doc: _open_receipt_viewer(d)):
                            img_style = "width:100%;height:100%;object-fit:contain;"
                            if rotation:
                                img_style += f"transform:rotate({rotation}deg);"
                            ui.image(_img_url(doc.source_file)).style(img_style)
                    elif Path(doc.source_file).is_file() and Path(doc.source_file).suffix.lower() == ".pdf":
                        with ui.element("div").style(
                            "width:100%;height:260px;overflow:hidden;"
                            "border-radius:8px;border:1px solid var(--border-default);cursor:pointer;"
                        ).on("click", lambda _, d=doc: _open_receipt_viewer(d)):
                            ui.image(_pdf_thumb_url(doc.source_file)).style(
                                "width:100%;height:100%;object-fit:contain;"
                            )
                    elif Path(doc.source_file).is_file():
                        with ui.element("div").style(
                            "height:100px;background:var(--bg-surface);border-radius:8px;"
                            "border:1px solid var(--border-default);display:flex;align-items:center;"
                            "justify-content:center;gap:8px;cursor:pointer;"
                        ).on("click", lambda _, d=doc: _open_receipt_viewer(d)):
                            ui.icon("picture_as_pdf").classes("text-2xl text-slate-400")
                            ui.label(doc.filename).classes("text-sm text-slate-500")

                    if not doc.analyzed:
                        with ui.element("div").style(
                            "margin-top:12px;padding:12px;background:#fef3c7;"
                            "border-radius:8px;border:1px solid #fde68a;"
                        ):
                            ui.label("This receipt has not been analyzed yet.").classes(
                                "text-xs text-amber-700"
                            )
                        return

                    # Amounts table
                    currency = raw.get("currency", doc.currency) or ""
                    subtotal = raw.get("subtotal")
                    tax = raw.get("tax")
                    total_amount = raw.get("total_amount") or raw.get("matched_amount") or doc.total
                    est_usd = raw.get("estimated_usd_total")
                    fx_note = raw.get("estimated_usd_fx_note", "")
                    card_amt = raw.get("card_charged_amount")
                    card_cur = raw.get("card_charged_currency", "")

                    with ui.element("div").style("margin-top:16px"):
                        ui.label("Receipt Details").classes(
                            "text-xs font-semibold text-slate-400 tracking-wider mb-2"
                        )
                        with ui.element("table").classes("doc-detail-table"):
                            with ui.element("tbody"):
                                _detail_row("Currency", currency or "—")
                                if subtotal is not None and str(subtotal).strip():
                                    _detail_row("Subtotal", f"{currency} {subtotal}")
                                if tax is not None and str(tax).strip():
                                    _detail_row("Tax", f"{currency} {tax}")
                                if total_amount:
                                    _detail_row("Total", f"{currency} {total_amount}")

                                is_foreign = currency and currency.upper() != "USD"

                                if card_amt and card_cur:
                                    _detail_row("Card Charged", f"{card_cur} {card_amt}")

                                if est_usd is not None and str(est_usd).strip() and is_foreign:
                                    _detail_row("Est. USD Total", f"${est_usd}")
                                if fx_note and is_foreign:
                                    _detail_row("FX Rate", str(fx_note))

                    # Line items
                    line_items = raw.get("line_items") or []
                    if line_items and isinstance(line_items, list) and len(line_items) > 0:
                        with ui.element("div").style("margin-top:16px"):
                            ui.label("Itemized Expenses").classes(
                                "text-xs font-semibold text-slate-400 tracking-wider mb-2"
                            )
                            with ui.element("table").classes("doc-detail-table"):
                                is_foreign_items = currency and currency.upper() != "USD"
                                with ui.element("thead"):
                                    with ui.element("tr"):
                                        with ui.element("th"):
                                            ui.label("Description")
                                        with ui.element("th").style("text-align:right"):
                                            ui.label(currency or "Amount")
                                        if is_foreign_items:
                                            with ui.element("th").style("text-align:right"):
                                                ui.label("USD")
                                with ui.element("tbody"):
                                    for item in line_items:
                                        if not isinstance(item, dict):
                                            continue
                                        desc = str(item.get("description", "")).strip() or "—"
                                        amt = item.get("amount", "")
                                        item_cur = item.get("currency", currency) or currency
                                        est_usd_item = item.get("estimated_usd")
                                        with ui.element("tr"):
                                            with ui.element("td"):
                                                ui.label(desc).classes("text-xs")
                                            with ui.element("td").style("text-align:right"):
                                                ui.label(
                                                    f"{item_cur} {amt}" if amt != "" else "—"
                                                ).classes("text-xs font-medium")
                                            if is_foreign_items:
                                                with ui.element("td").style("text-align:right"):
                                                    if est_usd_item is not None and str(est_usd_item).strip():
                                                        ui.label(f"${est_usd_item}").classes(
                                                            "text-xs text-slate-500"
                                                        )
                                                    else:
                                                        ui.label("—").classes("text-xs text-slate-300")

                    # Notes
                    notes = raw.get("notes") or doc.notes
                    if notes and str(notes).strip():
                        with ui.element("div").style(
                            "margin-top:16px;padding:12px;background:var(--bg-surface);"
                            "border-radius:8px;border:1px solid var(--border-subtle);"
                        ):
                            ui.label("Notes").classes(
                                "text-xs font-semibold text-slate-400 tracking-wider mb-1"
                            )
                            ui.label(str(notes)).classes("text-xs text-slate-600 leading-relaxed")

                # Actions
                with ui.element("div").classes("detail-panel-actions"):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.button(
                            "Open Viewer", icon="open_in_new",
                            on_click=lambda d=doc: _open_receipt_viewer(d),
                        ).props("no-caps outline size=sm").classes("action-btn")

                        def _rescan_this(sf=doc.source_file):
                            ui.notify("Rescanning...", type="info")
                            _run_background(
                                "Receipt Rescan",
                                lambda on_status: svc.rescan_receipts([sf], on_status=on_status),
                                "Rescan complete",
                            )

                        ui.button(
                            "Rescan", icon="refresh", on_click=_rescan_this,
                        ).props("no-caps outline size=sm").classes("action-btn")

        def _sync_doc_detail_drawer():
            doc_detail_slot.clear()
            with doc_detail_slot:
                _render_doc_detail_content()
            show = bool(state.get("selected_doc")) or len(state.get("selected", set())) > 1
            doc_detail_drawer.set_value(show)

        def _on_doc_drawer_value(e):
            if e.value:
                return
            if state.get("selected_doc") or len(state.get("selected", set())) > 0:
                state["selected"] = set()
                state["selected_doc"] = None
                _render_documents()

        doc_detail_drawer.on_value_change(_on_doc_drawer_value)

        def _confirm_remove_used(count: int):
            with ui.dialog() as dlg, ui.card().style("min-width:400px;border-radius:16px;padding:28px"):
                ui.label("Remove Used Documents").classes("text-lg font-bold text-slate-800 mb-2")
                ui.label(
                    f"This will remove {count} receipt{'s' if count != 1 else ''} that "
                    f"{'have' if count != 1 else 'has'} been attached to a submitted expense report. "
                    "The files stay on disk but will no longer appear here."
                ).classes("text-sm text-slate-600 mb-6")
                with ui.row().classes("items-center justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")
                    def _do_remove():
                        removed = svc.remove_used_receipts()
                        dlg.close()
                        ui.notify(
                            f"Removed {removed} used receipt{'s' if removed != 1 else ''}",
                            type="positive",
                        )
                        _render_documents()
                    ui.button(
                        f"Remove {count}",
                        icon="delete_sweep",
                        on_click=_do_remove,
                    ).props("color=negative no-caps unelevated").classes("action-btn")
            dlg.open()

        with search_container:
            with ui.element("div").style(
                "margin-bottom:12px;max-width:360px;"
            ):
                _search_input = ui.input(
                    placeholder="Search receipts…",
                    value=state["search"],
                ).props(
                    'dense outlined clearable'
                ).classes("w-full").style(
                    "font-size:0.85rem;"
                )
                _search_input.props('prepend-inner-icon="search"')
                _search_input.on("update:model-value", _on_search)
        search_container.set_visibility(False)

        _render_documents()


def _receipt_row(
    r: ReceiptDoc,
    selected: bool = False,
    focused: bool = False,
    on_toggle=None,
    on_row_click=None,
    on_click=None,
    on_remove=None,
    on_rescan=None,
    on_checkbox=None,
):
    """Compact horizontal card for one receipt — thumbnail + key data."""
    if r.used:
        border_style = "border-left:3px solid #16a34a;"
    elif r.matched:
        border_style = "border-left:3px solid #3b82f6;"
    else:
        border_style = ""
    opacity_style = "opacity:0.65;" if r.used else ""
    sel_bg = "background:var(--bg-row-selected);" if selected else ""
    focus_cls = " receipt-selected" if focused else ""
    row_click = on_row_click or on_click
    row_el = ui.element("div").classes(f"receipt-card{focus_cls}").style(
        "display:grid;grid-template-columns:28px 72px 1fr 120px 110px 100px 90px 110px 40px;"
        f"align-items:center;gap:0;padding:0;margin-bottom:8px;cursor:pointer;"
        f"user-select:none;min-width:740px;{border_style}{opacity_style}{sel_bg}"
    )
    with row_el:
        # Checkbox
        if on_checkbox:
            _cb = ui.checkbox("", value=selected, on_change=on_checkbox).props(
                "dense size=xs"
            ).style("margin:0 0 0 4px;padding:0;min-height:0")
            _cb.on("click.stop", lambda: None)
        else:
            ui.element("div")
        # Right-click context menu
        with ui.menu().props("context-menu") as ctx_menu:
            if on_remove:
                ui.menu_item(
                    "Remove",
                    on_click=lambda: (ctx_menu.close(), on_remove()),
                ).props("dense").style("color:#dc2626")
            if on_rescan:
                ui.menu_item(
                    "Rescan with LLM",
                    on_click=lambda: (ctx_menu.close(), on_rescan()),
                ).props("dense")
        # Thumbnail
        if r.is_image and Path(r.source_file).is_file():
            rotation = r.rotation * 90
            style = (
                "width:72px;height:56px;object-fit:cover;"
                "border-radius:0;cursor:pointer;"
            )
            if rotation:
                style += f"transform:rotate({rotation}deg);"
            ui.image(_img_url(r.source_file)).style(style).on(
                "click", lambda _, cb=row_click: cb() if cb else None,
            )
        elif Path(r.source_file).is_file() and Path(r.source_file).suffix.lower() == ".pdf":
            ui.image(_pdf_thumb_url(r.source_file)).style(
                "width:72px;height:56px;object-fit:cover;"
                "border-radius:0;cursor:pointer;"
            ).on(
                "click", lambda _, cb=row_click: cb() if cb else None,
            )
        else:
            with ui.element("div").style(
                "width:72px;height:56px;background:var(--border-subtle);"
                "display:flex;align-items:center;justify-content:center;"
                "cursor:pointer;"
            ).on("click", lambda _, cb=row_click: cb() if cb else None):
                ui.icon("picture_as_pdf").classes("text-xl text-slate-400")

        # Vendor / filename
        with ui.element("div").style("padding:8px 16px;overflow:hidden;cursor:pointer").on(
            "click", lambda _, cb=row_click: cb() if cb else None,
        ):
            vendor_label = r.vendor or ("Unknown vendor" if r.analyzed else "Not yet reviewed")
            ui.label(vendor_label).classes(
                "font-semibold text-sm " + ("text-slate-800" if r.analyzed else "text-slate-400 italic")
            ).style("white-space:nowrap;overflow:hidden;text-overflow:ellipsis")
            ui.label(r.filename).classes("text-xs text-slate-400").style(
                "white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
            )

        # Amount
        with ui.element("div").style("padding:8px 12px"):
            if r.total:
                ui.label(f"{r.currency} {r.total}").classes(
                    "text-sm font-semibold text-slate-800"
                ).style("font-variant-numeric:tabular-nums")
            else:
                ui.label("—").classes("text-sm text-slate-300")

        # Receipt Date
        with ui.element("div").style("padding:8px 12px"):
            ui.label(r.date or "—").classes("text-sm text-slate-600")

        # Date Added
        with ui.element("div").style("padding:8px 12px"):
            ui.label(r.date_added or "—").classes("text-sm text-slate-500")

        # Parse confidence
        with ui.element("div").style("padding:8px 12px"):
            if not r.analyzed:
                ui.label("—").classes("text-sm text-slate-300")
            elif r.confidence:
                level = (
                    "high" if r.confidence >= 0.85
                    else ("medium" if r.confidence >= 0.6 else "low")
                )
                ui.html(_badge_html(level, r.confidence))
            else:
                ui.label("—").classes("text-sm text-slate-300")

        # Status
        with ui.element("div").style("padding:8px 8px"):
            if not r.analyzed:
                ui.html(
                    '<span style="display:inline-flex;align-items:center;gap:4px;'
                    'padding:2px 8px;border-radius:12px;background:#fef3c7;'
                    'color:#92400e;font-size:0.7rem;font-weight:600">'
                    '<span class="material-icons" style="font-size:0.85rem">hourglass_empty</span>'
                    'Not Reviewed</span>'
                )
            elif r.used:
                n = len(r.used_by_line_ids)
                ui.html(
                    f'<span style="display:inline-flex;align-items:center;gap:4px;'
                    f'padding:2px 8px;border-radius:12px;background:#dcfce7;'
                    f'color:#15803d;font-size:0.7rem;font-weight:600">'
                    f'<span class="material-icons" style="font-size:0.85rem">check_circle</span>'
                    f'Used{f" ({n})" if n > 1 else ""}</span>'
                )
            elif r.matched:
                n = len(r.matched_line_ids)
                ui.html(
                    f'<span style="display:inline-flex;align-items:center;gap:4px;'
                    f'padding:2px 8px;border-radius:12px;background:#dbeafe;'
                    f'color:#1d4ed8;font-size:0.7rem;font-weight:600">'
                    f'<span class="material-icons" style="font-size:0.85rem">link</span>'
                    f'Matched{f" ({n})" if n > 1 else ""}</span>'
                )
            else:
                ui.html(
                    '<span style="display:inline-flex;align-items:center;gap:4px;'
                    'padding:2px 8px;border-radius:12px;background:var(--badge-unmatched-bg);'
                    'color:var(--badge-unmatched-color);font-size:0.7rem;font-weight:600">'
                    '<span class="material-icons" style="font-size:0.85rem">link_off</span>'
                    'Unmatched</span>'
                )

        # Remove button (visible on hover via CSS .receipt-card .remove-doc)
        if on_remove:
            ui.button(
                icon="close",
                on_click=lambda _, cb=on_remove: cb(),
            ).props("flat dense round size=sm").classes("remove-doc").style(
                "min-width:0;padding:2px"
            )
        else:
            ui.element("div")


_VIEWER_JS = """
(function() {
    const ctr = document.getElementById("__CID__");
    if (!ctr) return;
    const img = ctr.querySelector("img");
    if (!img) return;

    let scale = 1, tx = 0, ty = 0;
    let dragging = false, sx = 0, sy = 0, stx = 0, sty = 0;
    const rot = __ROT__;

    function apply() {
        img.style.transform =
            "translate("+tx+"px,"+ty+"px) scale("+scale+") rotate("+rot+"deg)";
    }

    function fit(retries) {
        retries = retries || 0;
        const nw = img.naturalWidth, nh = img.naturalHeight;
        const cw = ctr.clientWidth, ch = ctr.clientHeight;
        if (!nw || !nh || cw < 50 || ch < 50) {
            if (retries < 300) {
                setTimeout(function() { fit(retries + 1); }, 20);
            } else {
                img.style.width = "100%"; img.style.height = "100%";
                img.style.objectFit = "contain";
                img.style.transform = rot ? "rotate("+rot+"deg)" : "";
            }
            return;
        }
        const sw = (rot%180)!==0, ew = sw?nh:nw, eh = sw?nw:nh;
        const s = Math.min((cw-32)/ew, (ch-32)/eh);
        /* If the computed scale would make the image too small, the container
           is still animating/settling — retry instead of locking in a bad scale. */
        if (nw * s < 60 || nh * s < 60) {
            if (retries < 300) {
                setTimeout(function() { fit(retries + 1); }, 20);
            } else {
                img.style.width = "100%"; img.style.height = "100%";
                img.style.objectFit = "contain";
                img.style.transform = rot ? "rotate("+rot+"deg)" : "";
            }
            return;
        }
        img.style.width = "auto"; img.style.height = "auto"; img.style.objectFit = "";
        scale = s;
        tx = (cw - nw*scale)/2;
        ty = (ch - nh*scale)/2;
        apply();
    }

    if (img.complete && img.naturalWidth) fit();
    else img.onload = function() { fit(); };

    /* Refit when container resizes (e.g. dialog open animation) */
    if (typeof ResizeObserver !== "undefined") {
        let prevW = 0, prevH = 0;
        new ResizeObserver(function() {
            const w = ctr.clientWidth, h = ctr.clientHeight;
            if (w >= 50 && h >= 50 && (w !== prevW || h !== prevH)) {
                prevW = w; prevH = h;
                fit();
            }
        }).observe(ctr);
    }

    ctr._vzoom = function(f) {
        const cw=ctr.clientWidth/2, ch=ctr.clientHeight/2;
        tx=cw-f*(cw-tx); ty=ch-f*(ch-ty); scale*=f; apply();
    };
    ctr._vreset = fit;

    ctr.addEventListener("wheel", function(e) {
        e.preventDefault();
        const r=ctr.getBoundingClientRect(), mx=e.clientX-r.left, my=e.clientY-r.top;
        const f = Math.exp(-e.deltaY * 0.0025);
        tx=mx-f*(mx-tx); ty=my-f*(my-ty); scale*=f; apply();
    }, {passive:false});

    ctr.addEventListener("mousedown", function(e) {
        if(e.button!==0)return; dragging=true;
        sx=e.clientX; sy=e.clientY; stx=tx; sty=ty;
        ctr.style.cursor="grabbing";
    });
    window.addEventListener("mousemove", function(e) {
        if(!dragging)return;
        tx=stx+(e.clientX-sx); ty=sty+(e.clientY-sy); apply();
    });
    window.addEventListener("mouseup", function() {
        dragging=false; ctr.style.cursor="grab";
    });
})();
"""

_CARD_PREVIEW_JS = """
(function() {
    const ctr = document.getElementById("__CID__");
    if (!ctr) return;
    const img = ctr.querySelector("img");
    if (!img) return;

    let scale = 1, tx = 0, ty = 0;
    let dragging = false, wasDrag = false, sx = 0, sy = 0, stx = 0, sty = 0;
    const rot = __ROT__;
    let fitted = false;

    function apply() {
        img.style.transform =
            "translate("+tx+"px,"+ty+"px) scale("+scale+") rotate("+rot+"deg)";
    }

    function restoreCss() {
        img.style.width = "100%"; img.style.height = "100%";
        img.style.objectFit = "contain";
        img.style.transform = rot ? "rotate("+rot+"deg)" : "";
    }

    function fit(retries) {
        retries = retries || 0;
        const nw = img.naturalWidth, nh = img.naturalHeight;
        const cw = ctr.clientWidth, ch = ctr.clientHeight;
        if (!nw || !nh || cw < 50 || ch < 50) {
            if (retries < 300) {
                setTimeout(function() { fit(retries + 1); }, 20);
            } else {
                restoreCss();
            }
            return;
        }
        const sw = (rot%180)!==0, ew = sw?nh:nw, eh = sw?nw:nh;
        const s = Math.min(cw/ew, ch/eh);
        /* If the computed scale would make the image too small, the container
           is still animating/settling — retry instead of locking in a bad scale. */
        if (nw * s < 60 || nh * s < 60) {
            if (retries < 300) {
                setTimeout(function() { fit(retries + 1); }, 20);
            } else {
                restoreCss();
            }
            return;
        }
        img.style.width = "auto"; img.style.height = "auto"; img.style.objectFit = "";
        scale = s;
        tx = (cw - nw*scale)/2;
        ty = (ch - nh*scale)/2;
        apply();
        fitted = true;
    }

    if (img.complete && img.naturalWidth) fit();
    else img.onload = function() { fit(); };

    /* Refit when container resizes (e.g. drawer open animation) */
    if (typeof ResizeObserver !== "undefined") {
        let prevW = 0, prevH = 0;
        new ResizeObserver(function() {
            const w = ctr.clientWidth, h = ctr.clientHeight;
            if (w >= 50 && h >= 50 && (w !== prevW || h !== prevH)) {
                prevW = w; prevH = h;
                fit();
            }
        }).observe(ctr);
    }

    ctr.addEventListener("wheel", function(e) {
        e.preventDefault();
        const r = ctr.getBoundingClientRect(), mx = e.clientX-r.left, my = e.clientY-r.top;
        const f = Math.exp(-e.deltaY * 0.0025);
        tx = mx-f*(mx-tx); ty = my-f*(my-ty); scale *= f; apply();
    }, {passive:false});

    ctr.addEventListener("mousedown", function(e) {
        if (e.button!==0) return;
        dragging = true; wasDrag = false;
        sx = e.clientX; sy = e.clientY; stx = tx; sty = ty;
        ctr.style.cursor = "grabbing";
    });
    window.addEventListener("mousemove", function(e) {
        if (!dragging) return;
        if (Math.abs(e.clientX-sx)>3 || Math.abs(e.clientY-sy)>3) wasDrag = true;
        tx = stx+(e.clientX-sx); ty = sty+(e.clientY-sy); apply();
    });
    window.addEventListener("mouseup", function() {
        if (!dragging) return;
        dragging = false; ctr.style.cursor = "grab";
    });

    ctr.addEventListener("click", function(e) {
        if (wasDrag) { e.stopPropagation(); wasDrag = false; }
    }, false);

    let pinch0 = null, pinchDist0 = 0, pinchScale0 = 1, pinchTx0 = 0, pinchTy0 = 0;
    let wasTouchMove = false;

    function tdist(a, b) {
        const dx = a.clientX-b.clientX, dy = a.clientY-b.clientY;
        return Math.sqrt(dx*dx+dy*dy);
    }

    ctr.addEventListener("touchstart", function(e) {
        wasTouchMove = false;
        if (e.touches.length===2) {
            e.preventDefault();
            pinch0 = Array.from(e.touches);
            pinchDist0 = tdist(pinch0[0], pinch0[1]);
            pinchScale0 = scale; pinchTx0 = tx; pinchTy0 = ty;
        } else if (e.touches.length===1) {
            sx = e.touches[0].clientX; sy = e.touches[0].clientY;
            stx = tx; sty = ty;
        }
    }, {passive:false});

    ctr.addEventListener("touchmove", function(e) {
        e.preventDefault();
        if (e.touches.length===2 && pinch0) {
            wasTouchMove = true;
            const d = tdist(e.touches[0], e.touches[1]);
            const f = d/pinchDist0;
            const mx = (e.touches[0].clientX+e.touches[1].clientX)/2;
            const my = (e.touches[0].clientY+e.touches[1].clientY)/2;
            const r = ctr.getBoundingClientRect();
            scale = pinchScale0*f;
            tx = (mx-r.left) - f*((mx-r.left) - pinchTx0);
            ty = (my-r.top) - f*((my-r.top) - pinchTy0);
            apply();
        } else if (e.touches.length===1) {
            if (Math.abs(e.touches[0].clientX-sx)>3 || Math.abs(e.touches[0].clientY-sy)>3)
                wasTouchMove = true;
            tx = stx+(e.touches[0].clientX-sx);
            ty = sty+(e.touches[0].clientY-sy);
            apply();
        }
    }, {passive:false});

    ctr.addEventListener("touchend", function(e) {
        pinch0 = null;
        if (wasTouchMove && e.touches.length===0) {
            ctr.addEventListener("click", function b(ce) {
                ce.stopPropagation(); ce.preventDefault();
                ctr.removeEventListener("click", b, true);
            }, {capture:true, once:true});
        }
    });

    ctr.addEventListener("dblclick", function(e) {
        e.stopPropagation(); fit();
    });
})();
"""


def _open_receipt_viewer(doc: ReceiptDoc):
    rotation = doc.rotation * 90 if doc.is_image else 0

    with ui.dialog().props("maximized") as dlg, ui.card().classes("w-full h-full").style(
        "display:flex;flex-direction:column;overflow:hidden"
    ):
        # Toolbar
        with ui.row().classes("items-center justify-between w-full px-4 py-3 bg-slate-50").style(
            "flex-shrink:0;border-bottom:1px solid var(--border-default)"
        ):
            ui.label(doc.vendor or doc.filename).classes("text-lg font-semibold text-slate-800")
            with ui.row().classes("items-center gap-2"):
                if doc.total:
                    ui.label(f"{doc.currency} {doc.total}").classes("text-sm font-medium text-slate-600")
                if doc.date:
                    ui.label(doc.date).classes("text-sm text-slate-500")
                ui.button(icon="close", on_click=dlg.close).props("flat round dense size=sm")

        # Image area
        if doc.is_image and Path(doc.source_file).is_file():
            img_src = _img_url(doc.source_file)
            container_id = f"rv{id(doc)}"

            with ui.element("div").style("flex:1 1 0;min-height:0;position:relative;width:100%"):
                ui.html(
                    f'<div id="{container_id}" style="position:absolute;top:0;left:0;right:0;bottom:0;overflow:hidden;'
                    f'cursor:grab;background:var(--bg-surface);">'
                    f'<img src="{img_src}" style="transform-origin:0 0;position:absolute;'
                    f'top:0;left:0;width:100%;height:100%;object-fit:contain;'
                    f'user-select:none;pointer-events:none;" />'
                    f'</div>'
                ).style("position:absolute;top:0;left:0;right:0;bottom:0")

                with ui.row().style(
                    "position:absolute;top:8px;left:8px;z-index:10;gap:4px;"
                ):
                    zoom_in_btn = ui.button(icon="zoom_in").props("flat round dense size=sm outline")
                    zoom_out_btn = ui.button(icon="zoom_out").props("flat round dense size=sm outline")
                    fit_btn = ui.button(icon="fit_screen").props("flat round dense size=sm outline")

            js = _VIEWER_JS.replace("__CID__", container_id).replace("__ROT__", str(rotation))

            zoom_in_btn.on("click", lambda: ui.run_javascript(
                f'document.getElementById("{container_id}")?._vzoom(1.2)'
            ))
            zoom_out_btn.on("click", lambda: ui.run_javascript(
                f'document.getElementById("{container_id}")?._vzoom(1/1.2)'
            ))
            fit_btn.on("click", lambda: ui.run_javascript(
                f'document.getElementById("{container_id}")?._vreset()'
            ))

            async def _init_viewer():
                await ui.run_javascript(js)

            dlg.on("show", _init_viewer)
        elif Path(doc.source_file).is_file() and Path(doc.source_file).suffix.lower() == ".pdf":
            img_src = _pdf_thumb_url(doc.source_file)
            container_id = f"rv{id(doc)}"

            with ui.element("div").style("flex:1 1 0;min-height:0;position:relative;width:100%"):
                ui.html(
                    f'<div id="{container_id}" style="position:absolute;top:0;left:0;right:0;bottom:0;overflow:hidden;'
                    f'cursor:grab;background:var(--bg-surface);">'
                    f'<img src="{img_src}" style="transform-origin:0 0;position:absolute;'
                    f'top:0;left:0;width:100%;height:100%;object-fit:contain;'
                    f'user-select:none;pointer-events:none;" />'
                    f'</div>'
                ).style("position:absolute;top:0;left:0;right:0;bottom:0")

                with ui.row().style(
                    "position:absolute;top:8px;left:8px;z-index:10;gap:4px;"
                ):
                    zoom_in_btn = ui.button(icon="zoom_in").props("flat round dense size=sm outline")
                    zoom_out_btn = ui.button(icon="zoom_out").props("flat round dense size=sm outline")
                    fit_btn = ui.button(icon="fit_screen").props("flat round dense size=sm outline")

            js = _VIEWER_JS.replace("__CID__", container_id).replace("__ROT__", "0")

            zoom_in_btn.on("click", lambda: ui.run_javascript(
                f'document.getElementById("{container_id}")?._vzoom(1.2)'
            ))
            zoom_out_btn.on("click", lambda: ui.run_javascript(
                f'document.getElementById("{container_id}")?._vzoom(1/1.2)'
            ))
            fit_btn.on("click", lambda: ui.run_javascript(
                f'document.getElementById("{container_id}")?._vreset()'
            ))

            async def _init_viewer():
                await ui.run_javascript(js)

            dlg.on("show", _init_viewer)
        else:
            with ui.element("div").classes("text-center py-20").style("flex:1 1 0"):
                ui.icon("picture_as_pdf").classes("text-6xl text-slate-400")
                ui.label(doc.filename).classes("mt-4 text-slate-600")

        if doc.raw_analysis:
            import json as _json
            raw_json = _json.dumps(doc.raw_analysis, indent=2, ensure_ascii=False)
            with ui.expansion("LLM Response", icon="smart_toy").classes("w-full").style(
                "flex-shrink:0;border-top:1px solid var(--border-default)"
            ).props("dense header-class='text-xs font-semibold text-slate-500 bg-slate-50 px-6 py-1'"):
                ui.html(
                    f'<pre style="margin:0;padding:12px 24px;font-size:0.78rem;line-height:1.5;'
                    f'color:var(--text-body);background:var(--bg-surface);overflow-x:auto;white-space:pre-wrap;'
                    f'word-break:break-word;max-height:300px;overflow-y:auto">'
                    f'{raw_json}</pre>'
                )
        elif doc.notes:
            with ui.element("div").classes("px-6 py-4 bg-slate-50 border-t").style("flex-shrink:0"):
                ui.label("Notes").classes("text-xs font-semibold text-slate-500 mb-1")
                ui.label(doc.notes).classes("text-sm text-slate-600")
    dlg.open()


def _empty_state(icon: str, title: str, desc: str):
    ui.html(
        f'<div class="empty-state">'
        f'<div class="icon"><span class="material-icons">{icon}</span></div>'
        f'<div class="title">{title}</div>'
        f'<div class="desc">{desc}</div></div>'
    )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@ui.page("/transactions")
def page_transactions(request: Request):
    report_filter_id = (request.query_params.get("report") or "").strip()
    page_frame("Transactions", report_filter_id)

    with ui.element("div").classes("page-container"):

        all_txns = svc.get_transactions()
        groups = svc.get_expense_report_groups()

        state: dict[str, Any] = {
            "selected": set(),
            "status_filter": "all",
            "report_filter": "__unassigned__" if report_filter_id == "__uncategorized__" else (report_filter_id if report_filter_id else "__all__"),
            "sort_col": None,
            "sort_asc": True,
            "search": "",
        }

        # ---- Header with title + scrape button + report management ----
        with ui.element("div").classes("page-hero-row"):
            with ui.element("div").classes("page-hero-title"):
                with ui.column().classes("gap-0"):
                    ui.html('<div class="section-title">Transactions</div>')
                    ui.html(
                        '<div class="section-subtitle" style="margin-bottom:0">'
                        "Scraped from Oracle expense portal — organize into expense reports</div>"
                    )
            with ui.element("div").classes("page-hero-actions column-end is-stack"):
                def _start_scrape():
                    def _run_after_notice():
                        def _do_scrape(on_status):
                            return svc.run_scrape(on_status=on_status)

                        def _on_scrape_done(result):
                            if isinstance(result, dict):
                                scraped = result.get("scraped", 0)
                                new = result.get("new", 0)
                                total = result.get("after", scraped)
                            else:
                                scraped = int(result) if isinstance(result, (int, float)) else 0
                                new = scraped
                                total = scraped

                            with ui.dialog() as done_dlg, ui.card().style(
                                "min-width:380px;max-width:460px;border-radius:14px;padding:24px 28px"
                            ):
                                with ui.row().classes("items-center gap-3 w-full mb-3"):
                                    ui.icon("check_circle").classes("text-green-500").style("font-size:1.6rem")
                                    ui.label("Scrape Complete").classes("text-lg font-bold text-slate-800")
                                with ui.element("div").style(
                                    "background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;"
                                    "padding:16px 20px;margin-bottom:4px"
                                ):
                                    with ui.row().classes("items-center gap-3"):
                                        ui.html(
                                            f'<span style="font-size:1.8rem;font-weight:700;color:#16a34a">{scraped}</span>'
                                        )
                                        ui.label(
                                            f"transaction{'s' if scraped != 1 else ''} scraped from Oracle"
                                        ).style("color:#15803d;font-size:0.9rem")
                                    if new > 0 and new != scraped:
                                        ui.label(
                                            f"{new} new transaction{'s' if new != 1 else ''} added"
                                        ).style("color:#16a34a;font-size:0.82rem;margin-top:4px;font-weight:600")
                                    elif new == 0 and total > 0:
                                        ui.label(
                                            "No new transactions — all were already imported."
                                        ).style("color:#64748b;font-size:0.82rem;margin-top:4px")
                                    ui.label(
                                        f"{total} total transaction{'s' if total != 1 else ''} in library"
                                    ).style("color:#64748b;font-size:0.78rem;margin-top:2px")
                                with ui.row().classes("items-center justify-end gap-2 w-full mt-3"):
                                    def _close_and_reload():
                                        done_dlg.close()
                                        ui.navigate.to("/transactions", new_tab=False)
                                    ui.button("OK", on_click=_close_and_reload).props(
                                        "no-caps unelevated color=primary"
                                    ).classes("action-btn")
                            done_dlg.open()

                        _run_background(
                            "Scrape Transactions",
                            _do_scrape,
                            "Transaction scrape complete.",
                            on_done=_on_scrape_done,
                        )

                    _open_oracle_manual_login_dialog(_run_after_notice)

                ui.button(
                    "Scrape Transactions",
                    icon="cloud_download",
                    on_click=_start_scrape,
                ).props("no-caps unelevated color=primary").classes("action-btn")
                with ui.element("div").style(
                    "border-radius:10px;padding:12px 16px;background:var(--bg-surface);"
                    "border:1px solid var(--border-default);display:flex;align-items:center;gap:10px"
                ):
                    ui.icon("vpn_lock").classes("text-xl text-amber-600")
                    with ui.column().classes("gap-0"):
                        ui.label("VPN Required").classes(
                            "font-semibold text-slate-700 text-xs"
                        )
                        ui.label(
                            "Turn VPN on before scraping."
                        ).classes("text-xs text-slate-400")

        if not all_txns and not _is_task_running("Scrape Transactions"):
            _empty_state(
                "receipt_long",
                "No transactions loaded",
                "Click Scrape Transactions above to load credit card rows from Oracle.",
            )
            return

        def _on_txn_search(e):
            state["search"] = e.args or ""
            _render_table()

        search_container = ui.element("div")
        table_container = ui.element("div")
        action_bar_container = ui.element("div")

        # Poll timer for live refresh during transaction scraping
        def _scrape_poll():
            nonlocal all_txns, groups
            if _is_task_running("Scrape Transactions"):
                fresh = svc.get_transactions()
                if len(fresh) != len(all_txns):
                    all_txns = fresh
                    groups = svc.get_expense_report_groups()
                    _render_table()

        _scrape_timer = ui.timer(2.0, _scrape_poll)

        def _refresh_all():
            nonlocal all_txns, groups
            all_txns = svc.get_transactions()
            groups = svc.get_expense_report_groups()
            state["selected"] = set()
            _render_table()
            _render_action_bar()

        def _filtered_txns() -> list[TransactionRow]:
            visible = list(all_txns)
            rf = state["report_filter"]
            if rf == "__unassigned__":
                visible = [t for t in visible if not t.report_id]
            elif rf != "__all__":
                visible = [t for t in visible if t.report_id == rf]
            sf = state["status_filter"]
            if sf != "all":
                visible = [t for t in visible if t.match_status == sf]
            _q = state.get("search", "").strip().lower()
            if _q:
                def _txn_matches(t: TransactionRow) -> bool:
                    haystack = " ".join([
                        t.merchant, t.date, str(t.amount),
                        t.report_name or "", t.expense_type or "",
                        t.match_status, t.currency or "",
                    ]).lower()
                    return _q in haystack
                visible = [t for t in visible if _txn_matches(t)]
            col = state["sort_col"]
            if col:
                key_funcs = {
                    "merchant": lambda t: t.merchant.lower(),
                    "date": lambda t: _parse_date_sort_key(t.date),
                    "amount": lambda t: _safe_amount_float(t.amount),
                    "match": lambda t: t.match_confidence,
                    "report": lambda t: (t.report_name or "").lower(),
                    "type": lambda t: (t.expense_type or "").lower(),
                }
                fn = key_funcs.get(col)
                if fn:
                    visible.sort(key=fn, reverse=not state["sort_asc"])
            return visible

        def _toggle_select(line_id: str, checked: bool):
            if checked:
                state["selected"].add(line_id)
            else:
                state["selected"].discard(line_id)
            _render_action_bar()

        def _toggle_select_all(txns: list[TransactionRow], checked: bool):
            if checked:
                state["selected"] = {t.line_id for t in txns}
            else:
                state["selected"] = set()
            _render_table()
            _render_action_bar()

        def _change_report_inline(line_id: str, target_id: str):
            if target_id == "__new__":
                _open_create_report_dialog(preselected_line_ids=[line_id])
                return
            report_id = None if target_id == "__unassigned__" else target_id
            svc.assign_transactions_to_report([line_id], report_id)
            target_name = "Unassigned"
            if report_id:
                grp = next((g for g in groups if g.id == report_id), None)
                if grp:
                    target_name = grp.name
            ui.notify(f"Assigned to {target_name}", type="positive")
            _refresh_all()

        def _txn_toggle_sort(col: str):
            if state["sort_col"] == col:
                state["sort_asc"] = not state["sort_asc"]
            else:
                state["sort_col"] = col
                state["sort_asc"] = True
            _render_table()

        def _render_table():
            table_container.clear()
            visible = _filtered_txns()

            with table_container:
                total_count = len(list(all_txns))
                filtered_count = len(visible)
                _q = state.get("search", "").strip()

                # Summary strip with count + badges (Documents style)
                with ui.row().classes("items-center gap-4 mb-5"):
                    count_text = (
                        f"{filtered_count} of {total_count} transaction{'s' if total_count != 1 else ''}"
                        if _q or state["status_filter"] != "all" or state["report_filter"] != "__all__"
                        else f"{total_count} transaction{'s' if total_count != 1 else ''}"
                    )
                    ui.label(count_text).classes("text-sm font-semibold text-slate-600")

                    total_amt = sum(
                        _safe_amount_float(t.amount) for t in visible
                    )
                    if total_amt:
                        ui.label(f"·  Total: ${total_amt:,.2f}").classes(
                            "text-sm text-slate-400"
                        )

                    n_high = sum(1 for t in visible if t.match_status == "high")
                    if n_high:
                        ui.html(
                            f'<span class="confidence-badge badge-high">'
                            f'{n_high} high</span>'
                        )
                    n_medium = sum(1 for t in visible if t.match_status == "medium")
                    if n_medium:
                        ui.html(
                            f'<span class="confidence-badge badge-medium">'
                            f'{n_medium} medium</span>'
                        )
                    n_low = sum(1 for t in visible if t.match_status == "low")
                    if n_low:
                        ui.html(
                            f'<span class="confidence-badge badge-low">'
                            f'{n_low} low</span>'
                        )
                    n_unmatched = sum(1 for t in visible if t.match_status == "unmatched")
                    if n_unmatched:
                        ui.html(
                            f'<span class="confidence-badge badge-unmatched">'
                            f'{n_unmatched} unmatched</span>'
                        )

                # Filter row: report dropdown + status chips
                with ui.row().classes("items-center gap-2 mb-4 flex-wrap"):
                    report_options: dict[str, str] = {"__all__": "All Reports", "__unassigned__": "Unassigned"}
                    for g in groups:
                        report_options[g.id] = g.name
                    ui.select(
                        options=report_options,
                        value=state["report_filter"],
                        on_change=lambda e: _apply_report_filter(e.value),
                    ).props("dense outlined").style("min-width:170px").classes("text-sm")

                    ui.element("div").style("flex:1")

                    for fval, flabel in [
                        ("all", "All"),
                        ("unmatched", "Unmatched"),
                        ("low", "Low"),
                        ("medium", "Medium"),
                        ("high", "High"),
                    ]:
                        is_active = state["status_filter"] == fval
                        btn = ui.button(
                            flabel,
                            on_click=lambda _, f=fval: _apply_status_filter(f),
                        ).props(
                            ("no-caps unelevated size=sm" if is_active else "no-caps flat size=sm")
                            + (" color=primary" if is_active else "")
                        ).classes("text-xs")

                if not visible:
                    _empty_state(
                        "filter_list",
                        "No matching transactions",
                        "Try a different filter or scrape new transactions.",
                    )
                    return

                all_selected = all(t.line_id in state["selected"] for t in visible)

                report_select_options: dict[str, str] = {"__unassigned__": "—"}
                for g in groups:
                    report_select_options[g.id] = g.name
                report_select_options["__new__"] = "+ New Report"

                with ui.element("div").style(
                    "background:var(--bg-card);border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.08);overflow:hidden"
                ):
                    with ui.element("div").style(
                        "display:grid;grid-template-columns:40px 2fr 100px 120px 120px 160px 100px 40px;"
                        "gap:0;padding:12px 20px;background:var(--bg-surface);border-bottom:1px solid var(--border-default);"
                        "font-size:0.75rem;font-weight:600;text-transform:uppercase;"
                        "letter-spacing:0.05em;color:var(--text-muted);"
                    ):
                        ui.checkbox(
                            value=all_selected,
                            on_change=lambda e, vt=visible: _toggle_select_all(vt, e.value),
                        ).props("dense").style("margin:0;padding:0")
                        sc, sa = state["sort_col"], state["sort_asc"]
                        for col_key, col_label in [
                            ("merchant", "Merchant"), ("date", "Date"), ("amount", "Amount"),
                            ("match", "Match"), ("report", "Report"), ("type", "Type"),
                        ]:
                            ui.label(f"{col_label}{_sort_arrow(sc, sa, col_key)}").classes(
                                "sortable-header"
                            ).on("click", lambda _, c=col_key: _txn_toggle_sort(c))
                        ui.label("")

                    for t in visible:
                        is_sel = t.line_id in state["selected"]
                        bg = "background:var(--bg-row-selected);" if is_sel else ""
                        with ui.element("div").style(
                            f"display:grid;grid-template-columns:40px 2fr 100px 120px 120px 160px 100px 40px;"
                            f"gap:0;padding:10px 20px;border-bottom:1px solid var(--border-subtle);"
                            f"align-items:center;font-size:0.875rem;color:var(--text-secondary);"
                            f"transition:background 0.1s;{bg}"
                        ):
                            ui.checkbox(
                                value=is_sel,
                                on_change=lambda e, lid=t.line_id: _toggle_select(lid, e.value),
                            ).props("dense").style("margin:0;padding:0")
                            with ui.element("div"):
                                ui.label(t.merchant).classes("font-semibold text-slate-800 text-sm").style(
                                    "white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
                                )
                            ui.label(t.date).classes("text-sm text-slate-600")
                            with ui.element("div").style("font-variant-numeric:tabular-nums"):
                                ui.label(t.amount).classes("text-sm font-medium")
                                if t.currency and t.currency != "USD":
                                    ui.label(t.currency).classes("text-xs text-slate-400")
                            ui.html(_badge_html(t.match_status, t.match_confidence))
                            with ui.element("div"):
                                current_val = t.report_id if t.report_id else "__unassigned__"
                                ui.select(
                                    options=report_select_options,
                                    value=current_val,
                                    on_change=lambda e, lid=t.line_id: _change_report_inline(lid, e.value),
                                ).props("dense borderless").style(
                                    "min-width:130px;font-size:0.8rem"
                                ).classes("report-inline-select")
                            with ui.element("div"):
                                if t.expense_type:
                                    ui.label(t.expense_type).classes("text-xs text-slate-600").style(
                                        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis"
                                    )
                                else:
                                    ui.label("—").classes("text-xs text-slate-300")
                            with ui.element("div"):
                                if t.approved:
                                    ui.icon("verified").classes("text-green-600 text-base")

        def _apply_status_filter(f: str):
            state["status_filter"] = f
            state["selected"] = set()
            _render_table()
            _render_action_bar()

        def _apply_report_filter(f: str):
            state["report_filter"] = f
            state["selected"] = set()
            _render_table()
            _render_action_bar()

        def _render_action_bar():
            action_bar_container.clear()
            selected = state["selected"]
            if not selected:
                return
            with action_bar_container:
                with ui.element("div").classes("txn-action-bar").style(
                    "position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:100"
                ):
                    ui.label(f"{len(selected)} selected").style("font-weight:600")

                    move_options: dict[str, str] = {"__unassigned__": "Unassigned"}
                    for g in groups:
                        move_options[g.id] = g.name
                    move_options["__new__"] = "+ New Report"

                    ui.select(
                        options=move_options,
                        label="Assign to...",
                        on_change=lambda e: _do_move(e.value),
                    ).props("dense outlined dark").style(
                        "min-width:180px;color:white"
                    ).classes("text-white")

                    ui.button(
                        "Delete",
                        icon="delete_outline",
                        on_click=lambda: _confirm_delete_selected(),
                    ).props("flat no-caps size=sm color=red-4")

                    ui.button(
                        "Deselect",
                        icon="close",
                        on_click=lambda: _clear_selection(),
                    ).props("flat no-caps size=sm color=white")

        def _do_move(target_id: str):
            if not state["selected"]:
                return
            line_ids = list(state["selected"])
            if target_id == "__new__":
                _open_create_report_dialog(preselected_line_ids=line_ids)
                return
            report_id = None if target_id == "__unassigned__" else target_id
            svc.assign_transactions_to_report(line_ids, report_id)
            target_name = "Unassigned"
            if report_id:
                grp = next((g for g in groups if g.id == report_id), None)
                if grp:
                    target_name = grp.name
            ui.notify(
                f"Assigned {len(line_ids)} transaction{'s' if len(line_ids) != 1 else ''} to {target_name}",
                type="positive",
            )
            _refresh_all()

        def _confirm_delete_selected():
            line_ids = list(state["selected"])
            n = len(line_ids)
            if not n:
                return
            with ui.dialog() as dlg, ui.card().style(
                "min-width:400px;border-radius:16px;padding:28px"
            ):
                ui.label("Delete Transactions").classes(
                    "text-lg font-bold text-slate-800 mb-2"
                )
                ui.label(
                    f"Delete {n} transaction{'s' if n != 1 else ''}? "
                    "This also removes any associated matches and approvals."
                ).classes("text-sm text-slate-600 mb-6")
                with ui.row().classes("items-center justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                    def _do_delete():
                        removed = svc.delete_transactions(line_ids)
                        state["selected"] = set()
                        dlg.close()
                        ui.notify(
                            f"Deleted {removed} transaction{'s' if removed != 1 else ''}",
                            type="positive",
                        )
                        _refresh_all()

                    ui.button(
                        f"Delete {n}", icon="delete_outline", on_click=_do_delete,
                    ).props("color=negative no-caps unelevated").classes("action-btn")
            dlg.open()

        def _clear_selection():
            state["selected"] = set()
            _render_table()
            _render_action_bar()

        def _open_create_report_dialog(preselected_line_ids: list[str] | None = None):
            with ui.dialog() as dlg, ui.card().style(
                "min-width:400px;border-radius:16px;padding:28px"
            ):
                ui.label("Create Expense Report").classes(
                    "text-lg font-bold text-slate-800 mb-2"
                )
                n = len(preselected_line_ids) if preselected_line_ids else 0
                subtitle = "Give this report a descriptive name (e.g., 'March Travel', 'Q1 Client Meals')."
                if n:
                    subtitle += f" {n} transaction{'s' if n != 1 else ''} will be assigned."
                ui.label(subtitle).classes("text-sm text-slate-600 mb-4")
                name_input = ui.input(
                    label="Report name", placeholder="e.g., March Travel"
                ).props("outlined dense").classes("w-full mb-4")
                with ui.row().classes("items-center justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                    def _do_create():
                        name = name_input.value.strip() if name_input.value else ""
                        if not name:
                            ui.notify("Please enter a name", type="warning")
                            return
                        new_group = svc.create_expense_report_group(name)
                        if preselected_line_ids:
                            svc.assign_transactions_to_report(preselected_line_ids, new_group.id)
                        dlg.close()
                        ui.notify(f"Created report: {name}", type="positive")
                        _refresh_all()

                    ui.button("Create", icon="add", on_click=_do_create).props(
                        "color=primary no-caps unelevated"
                    ).classes("action-btn")
            dlg.open()

        def _open_rename_dialog(grp: ExpenseReportGroup):
            with ui.dialog() as dlg, ui.card().style(
                "min-width:400px;border-radius:16px;padding:28px"
            ):
                ui.label("Rename Report").classes(
                    "text-lg font-bold text-slate-800 mb-4"
                )
                name_input = ui.input(
                    label="Report name", value=grp.name
                ).props("outlined dense").classes("w-full mb-4")
                with ui.row().classes("items-center justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                    def _do_rename():
                        name = name_input.value.strip() if name_input.value else ""
                        if not name:
                            ui.notify("Please enter a name", type="warning")
                            return
                        svc.rename_expense_report_group(grp.id, name)
                        dlg.close()
                        ui.notify(f"Renamed to: {name}", type="positive")
                        _refresh_all()

                    ui.button("Rename", on_click=_do_rename).props(
                        "color=primary no-caps unelevated"
                    ).classes("action-btn")
            dlg.open()

        def _open_delete_dialog(grp: ExpenseReportGroup):
            with ui.dialog() as dlg, ui.card().style(
                "min-width:400px;border-radius:16px;padding:28px"
            ):
                ui.label("Delete Report").classes(
                    "text-lg font-bold text-slate-800 mb-2"
                )
                n = len(grp.line_ids)
                ui.label(
                    f"Delete \"{grp.name}\"? "
                    f"{'Its' if n else 'No'} "
                    f"{n} transaction{'s' if n != 1 else ''} "
                    f"{'will become' if n else 'to become'} unassigned."
                ).classes("text-sm text-slate-600 mb-6")
                with ui.row().classes("items-center justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                    def _do_delete():
                        svc.delete_expense_report_group(grp.id)
                        dlg.close()
                        ui.notify(f"Deleted: {grp.name}", type="positive")
                        if state["report_filter"] == grp.id:
                            state["report_filter"] = "__all__"
                        _refresh_all()

                    ui.button("Delete", icon="delete", on_click=_do_delete).props(
                        "color=negative no-caps unelevated"
                    ).classes("action-btn")
            dlg.open()

        # ---- Search input ----
        with search_container:
            with ui.element("div").style(
                "margin-bottom:12px;max-width:360px;"
            ):
                _txn_search_input = ui.input(
                    placeholder="Search transactions\u2026",
                    value=state["search"],
                ).props(
                    'dense outlined clearable'
                ).classes("w-full").style(
                    "font-size:0.85rem;"
                )
                _txn_search_input.props('prepend-inner-icon="search"')
                _txn_search_input.on("update:model-value", _on_txn_search)

        # ---- Table + action bar ----
        _render_table()
        _render_action_bar()


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------------------
# Matching — master-detail table layout
# ---------------------------------------------------------------------------

@ui.page("/matching")
def page_matching(request: Request):
    report_filter_id: str = ""
    try:
        report_filter_id = (request.query_params.get("report") or "").strip()
    except Exception:
        pass
    page_frame("Matching", report_filter_id)

    report_filter_name: str = ""
    if report_filter_id == "__uncategorized__":
        report_filter_name = "Uncategorized"
    elif report_filter_id:
        for g in svc.get_expense_report_groups():
            if g.id == report_filter_id:
                report_filter_name = g.name
                break

    full_queue = svc.get_match_review_queue()

    if report_filter_id == "__uncategorized__":
        queue = [q for q in full_queue if not q.transaction.report_id]
    elif report_filter_id:
        queue = [q for q in full_queue if q.transaction.report_id == report_filter_id]
    else:
        queue = full_queue
    total = len(queue)

    if total == 0:
        with ui.element("div").classes("page-container"):
            if report_filter_id and report_filter_name:
                _empty_state(
                    "compare_arrows",
                    f"No transactions in '{report_filter_name}'",
                    "This report has no transactions assigned yet." if report_filter_id != "__uncategorized__" else "All transactions have been assigned to a report.",
                )
                with ui.element("div").classes("text-center mt-4"):
                    ui.button(
                        "Show All Transactions",
                        on_click=lambda: ui.navigate.to("/matching"),
                    ).props("no-caps outlined").classes("action-btn")
            else:
                _empty_state("compare_arrows", "No transactions to match", "Load transactions and receipts first.")
        return

    _match_actions: dict[str, Callable[[], None]] = {"close": lambda: None}

    match_detail_drawer = ui.right_drawer(value=False, fixed=True, bordered=True).classes(
        "detail-side-drawer"
    ).props("overlay elevated width=440")
    with match_detail_drawer:
        with ui.column().classes("w-full").style(
            "height:100%;max-height:100vh;display:flex;flex-direction:column"
        ):
            with ui.row().classes("items-center justify-end w-full").style(
                "flex-shrink:0;padding:6px 8px;border-bottom:1px solid var(--border-subtle)"
            ):
                ui.button(icon="close", on_click=lambda: _match_actions["close"]()).props(
                    "flat dense round"
                )
            match_detail_slot = ui.column().classes("w-full").style(
                "flex:1;min-height:0;overflow-y:auto;padding:0 10px 24px"
            )

    with ui.element("div").classes("page-container"):

        all_receipts = svc.get_receipts()
        state: dict[str, Any] = {
            "selected_lid": None,
            "selected_lids": set(),
            "filter": "all",
            "sort_col": None,
            "sort_asc": True,
            "search": "",
        }

        # --- Header row ---
        with ui.element("div").classes("page-hero-row"):
            with ui.element("div").classes("page-hero-title"):
                with ui.column().classes("gap-0"):
                    ui.html('<div class="section-title">Matching</div>')
                    ui.html(
                        '<div class="section-subtitle" style="margin-bottom:0">'
                        "Review receipt matches for every line item</div>"
                    )
            with ui.element("div").classes("page-hero-actions"):
                def _do_approve_all():
                    count = svc.approve_all_high_confidence()
                    if count:
                        ui.notify(f"Approved {count} high-confidence matches", type="positive")
                    else:
                        ui.notify("No new high-confidence matches to approve", type="info")
                    ui.navigate.to("/matching")

                high_count = sum(1 for q in queue if q.transaction.match_status == "high" and not q.transaction.approved)
                if high_count:
                    ui.button(
                        f"Accept All High ({high_count})",
                        on_click=_do_approve_all,
                    ).props("color=positive no-caps unelevated size=sm").classes("action-btn")

                ui.button(
                    "Run Auto-Match",
                    icon="auto_fix_high",
                    on_click=_start_auto_match,
                ).props("no-caps outline size=sm").classes("action-btn")

        def _on_match_search(e):
            state["search"] = e.args or ""
            _render_all()

        # --- Search input ---
        match_search_container = ui.element("div")
        with match_search_container:
            with ui.element("div").style(
                "margin-bottom:12px;max-width:360px;"
            ):
                _match_search_input = ui.input(
                    placeholder="Search matches\u2026",
                    value=state["search"],
                ).props(
                    'dense outlined clearable'
                ).classes("w-full").style(
                    "font-size:0.85rem;"
                )
                _match_search_input.props('prepend-inner-icon="search"')
                _match_search_input.on("update:model-value", _on_match_search)

        # --- Summary strip ---
        n_review = sum(1 for q in queue if q.transaction.match_status in ("unmatched", "low", "medium"))
        n_high = sum(1 for q in queue if q.transaction.match_status == "high")
        n_approved = sum(1 for q in queue if q.transaction.approved)
        n_unmatched = sum(1 for q in queue if q.transaction.match_status == "unmatched")
        reviewed = n_approved
        progress_pct = int((reviewed / total) * 100) if total else 0

        def _refresh_queue():
            nonlocal queue, total, n_review, n_high, n_approved, n_unmatched, all_receipts
            fresh = svc.get_match_review_queue()
            if report_filter_id == "__uncategorized__":
                queue = [q for q in fresh if not q.transaction.report_id]
            elif report_filter_id:
                queue = [q for q in fresh if q.transaction.report_id == report_filter_id]
            else:
                queue = fresh
            total = len(queue)
            n_review = sum(1 for q in queue if q.transaction.match_status in ("unmatched", "low", "medium"))
            n_high = sum(1 for q in queue if q.transaction.match_status == "high")
            n_approved = sum(1 for q in queue if q.transaction.approved)
            n_unmatched = sum(1 for q in queue if q.transaction.match_status == "unmatched")
            all_receipts = svc.get_receipts()

        with ui.row().classes("items-center gap-4 mb-5"):
            ui.label(
                f"{total} transaction{'s' if total != 1 else ''}"
            ).classes("text-sm font-semibold text-slate-600")

            total_amt = sum(
                _safe_amount_float(q.transaction.amount) for q in queue
            )
            if total_amt:
                ui.label(f"·  Total: ${total_amt:,.2f}").classes(
                    "text-sm text-slate-400"
                )

            if n_approved:
                ui.html(
                    f'<span class="confidence-badge badge-high">'
                    f'{n_approved} approved</span>'
                )
            if n_high:
                ui.html(
                    f'<span style="display:inline-flex;align-items:center;gap:4px;'
                    f'padding:2px 8px;border-radius:12px;background:#dbeafe;'
                    f'color:#1d4ed8;font-size:0.7rem;font-weight:600">'
                    f'{n_high} high</span>'
                )
            if n_review:
                ui.html(
                    f'<span class="confidence-badge badge-medium">'
                    f'{n_review} needs review</span>'
                )
            if n_unmatched:
                ui.html(
                    f'<span class="confidence-badge badge-unmatched">'
                    f'{n_unmatched} unmatched</span>'
                )

        # --- Progress bar ---
        with ui.row().classes("items-center gap-3 w-full mb-4"):
            ui.html(
                f'<div class="review-progress" style="flex:1">'
                f'<div class="review-progress-fill" style="width:{progress_pct}%"></div></div>'
            )
            ui.label(f"{reviewed}/{total} approved").classes("text-sm font-medium text-slate-500")

        # --- Filter chips ---
        filter_chips_container = ui.row().classes("items-center gap-2 mb-5")

        layout_container = ui.column().classes("w-full")

        def _filtered_queue():
            f = state["filter"]
            if f == "all":
                result = list(queue)
            elif f == "review":
                result = [q for q in queue if q.transaction.match_status in ("unmatched", "low", "medium")]
            elif f == "high":
                result = [q for q in queue if q.transaction.match_status == "high"]
            elif f == "approved":
                result = [q for q in queue if q.transaction.approved]
            elif f == "unmatched":
                result = [q for q in queue if q.transaction.match_status == "unmatched"]
            else:
                result = list(queue)
            _q = state.get("search", "").strip().lower()
            if _q:
                def _item_matches(item: MatchReviewItem) -> bool:
                    t = item.transaction
                    r = item.receipt
                    haystack = " ".join([
                        t.merchant, t.date, str(t.amount),
                        t.expense_type or "", t.match_status,
                        (r.vendor or r.filename) if r else "",
                    ]).lower()
                    return _q in haystack
                result = [q for q in result if _item_matches(q)]
            col = state["sort_col"]
            if col:
                key_funcs = {
                    "merchant": lambda q: q.transaction.merchant.lower(),
                    "date": lambda q: _parse_date_sort_key(q.transaction.date),
                    "amount": lambda q: _safe_amount_float(q.transaction.amount),
                    "match": lambda q: q.transaction.match_confidence,
                    "type": lambda q: (q.transaction.expense_type or "").lower(),
                    "receipt": lambda q: ((q.receipt.vendor or q.receipt.filename) if q.receipt else "").lower(),
                }
                fn = key_funcs.get(col)
                if fn:
                    result.sort(key=fn, reverse=not state["sort_asc"])
            return result

        def _set_filter(f: str):
            state["filter"] = f
            state["selected_lid"] = None
            state["selected_lids"] = set()
            _render_all()

        def _match_toggle_sort(col: str):
            if state["sort_col"] == col:
                state["sort_asc"] = not state["sort_asc"]
            else:
                state["sort_col"] = col
                state["sort_asc"] = True
            _render_all()

        def _render_all():
            _render_filter_chips()
            layout_container.clear()
            with layout_container:
                with ui.element("div").style("width:100%;min-width:0;overflow-x:auto"):
                    _render_table()
            match_detail_slot.clear()
            with match_detail_slot:
                _render_detail()
            show = bool(state.get("selected_lid")) or len(state.get("selected_lids", set())) > 1
            match_detail_drawer.set_value(show)

        def _render_filter_chips():
            filter_chips_container.clear()
            with filter_chips_container:
                for fval, flabel, fcount in [
                    ("all", "All", total),
                    ("review", "Needs Review", n_review),
                    ("unmatched", "Unmatched", n_unmatched),
                    ("high", "High Confidence", n_high),
                    ("approved", "Approved", n_approved),
                ]:
                    is_active = state["filter"] == fval
                    props = "no-caps unelevated size=sm" if is_active else "no-caps flat size=sm"
                    color = "color=primary" if is_active else ""
                    ui.button(
                        f"{flabel} ({fcount})",
                        on_click=lambda _, fv=fval: _set_filter(fv),
                    ).props(f"{props} {color}").classes("text-xs")

        def _select_item(lid: str, shift: bool = False, ctrl: bool = False):
            """Handle row click with optional multi-select modifiers."""
            if shift and state.get("selected_lid"):
                items = _filtered_queue()
                lids = [it.transaction.line_id for it in items]
                anchor = state["selected_lid"]
                if anchor in lids and lid in lids:
                    i_a, i_b = lids.index(anchor), lids.index(lid)
                    lo, hi = min(i_a, i_b), max(i_a, i_b)
                    state["selected_lids"] = set(lids[lo : hi + 1])
                else:
                    state["selected_lids"] = {lid}
                    state["selected_lid"] = lid
            elif ctrl:
                if lid in state["selected_lids"]:
                    state["selected_lids"].discard(lid)
                    if state["selected_lid"] == lid:
                        state["selected_lid"] = next(iter(state["selected_lids"]), None)
                else:
                    state["selected_lids"].add(lid)
                    state["selected_lid"] = lid
            else:
                state["selected_lid"] = lid
                state["selected_lids"] = {lid}
            _render_all()

        def _clear_match_selection():
            state["selected_lid"] = None
            state["selected_lids"] = set()
            _render_all()

        _match_actions["close"] = _clear_match_selection

        def _toggle_receipt_missing(lid: str, is_currently_missing: bool):
            if is_currently_missing:
                svc.unmark_receipt_missing(lid)
                ui.notify("Receipt missing flag removed", type="info")
            else:
                svc.mark_receipt_missing(lid)
                ui.notify("Marked as receipt missing", type="positive")
            _refresh_queue()
            _render_all()

        def _do_rescan_selected():
            sel = list(state["selected_lids"])
            if not sel:
                ui.notify("Select one or more items first", type="warning")
                return
            _run_background(
                "Matching",
                lambda on_status: svc.rescan_lines_for_match(sel, on_status=on_status),
                "Rescan complete",
            )
            _was_matching[0] = True

        def _do_remove_from_report():
            sel = list(state["selected_lids"])
            if not sel:
                ui.notify("Select one or more items first", type="warning")
                return
            if not report_filter_id:
                ui.notify("Select a specific report first", type="warning")
                return
            svc.assign_transactions_to_report(sel, None)
            ui.notify(f"Removed {len(sel)} item(s) from report", type="positive")
            _refresh_queue()
            _render_all()


        _MATCH_GRID_COLS = "28px 32px 2fr 100px 100px 110px 1fr 1.5fr 36px"

        def _render_table():
            items = _filtered_queue()
            if not items:
                _empty_state("filter_list", "No items match this filter", "Try a different filter.")
                return

            _q = state.get("search", "").strip()
            filtered_count = len(items)

            with ui.element("div").style(
                "background:var(--bg-card);border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.08);"
                "overflow-x:auto;min-width:0"
            ):
                with ui.row().classes("items-center gap-4 px-5 pt-3 pb-2"):
                    count_text = (
                        f"{filtered_count} of {total}"
                        if _q else str(total)
                    ) + f" match{'es' if total != 1 else ''}"
                    ui.label(count_text).classes("text-sm font-semibold text-slate-600")

                    # Bulk action bar — visible when items are selected
                    sel_count = len(state.get("selected_lids", set()))
                    if sel_count:
                        ui.element("div").style("flex:1")
                        ui.label(f"{sel_count} selected").classes("text-xs font-semibold text-blue-600")
                        ui.button(
                            "Rescan", icon="refresh", on_click=_do_rescan_selected,
                        ).props("no-caps outline size=xs dense").style("font-size:0.7rem")
                        ui.button(
                            "Mark Missing", icon="do_not_disturb", on_click=_do_bulk_mark_missing,
                        ).props("no-caps outline size=xs dense color=amber").style("font-size:0.7rem")
                        ui.button(
                            "Clear", icon="close",
                            on_click=lambda: (
                                state.update({"selected_lids": set(), "selected_lid": None}),
                                _render_all(),
                            ),
                        ).props("no-caps flat size=xs dense").style("font-size:0.7rem;color:#94a3b8")

                sc, sa = state["sort_col"], state["sort_asc"]
                with ui.element("div").style(
                    f"display:grid;grid-template-columns:{_MATCH_GRID_COLS};"
                    "gap:0;padding:8px 20px;font-size:0.7rem;font-weight:700;text-transform:uppercase;"
                    "letter-spacing:0.06em;color:var(--text-muted);align-items:center;"
                    "background:var(--bg-surface);border-bottom:2px solid var(--border-default);border-radius:8px 8px 0 0;"
                    "position:sticky;top:0;z-index:10;min-width:860px;"
                ):
                    # Select-all checkbox
                    _all_lids = [it.transaction.line_id for it in items]
                    _all_selected = bool(_all_lids) and all(lid in state.get("selected_lids", set()) for lid in _all_lids)
                    _some_selected = bool(state.get("selected_lids", set()) & set(_all_lids)) and not _all_selected

                    def _toggle_select_all(e, all_lids=_all_lids):
                        if e.value:
                            state["selected_lids"] = set(all_lids)
                            state["selected_lid"] = all_lids[0] if all_lids else None
                        else:
                            state["selected_lids"] = set()
                            state["selected_lid"] = None
                        _render_all()

                    _sa_cb = ui.checkbox("", value=_all_selected, on_change=_toggle_select_all).props(
                        "dense size=xs"
                    ).style("margin:0;padding:0;min-height:0")
                    if _some_selected:
                        _sa_cb.props("indeterminate-value=true model-value=true")

                    ui.element("div")  # status dot column
                    for col_key, col_label in [
                        ("merchant", "Merchant"), ("date", "Date"), ("amount", "Amount"),
                        ("match", "Match"), ("type", "Type"), ("receipt", "Receipt"),
                    ]:
                        is_active = sc == col_key
                        lbl = ui.label(f"{col_label}{_sort_arrow(sc, sa, col_key)}")
                        lbl.classes("sortable-header")
                        lbl.style(
                            "cursor:pointer;padding:4px 6px;border-radius:4px;transition:all 0.15s;"
                            "user-select:none;"
                            + ("color:#3b82f6;background:var(--bg-row-selected);" if is_active else "")
                        )
                        lbl.on("click", lambda _, c=col_key: _match_toggle_sort(c))
                    ui.icon("do_not_disturb").style(
                        "font-size:0.85rem;color:#94a3b8"
                    ).tooltip("Receipt Missing")

                for item in items:
                    t = item.transaction
                    is_active = t.line_id in state.get("selected_lids", set())
                    status_colors = {
                        "high": "var(--color-high)", "medium": "var(--color-medium)",
                        "low": "var(--color-low)", "unmatched": "var(--color-unmatched)",
                    }
                    left_accent = f"box-shadow:inset 3px 0 0 {status_colors.get(t.match_status, 'var(--border-default)')};"
                    bg = "background:var(--bg-row-selected);" if is_active else ""
                    row_el = ui.element("div").style(
                        f"display:grid;grid-template-columns:{_MATCH_GRID_COLS};"
                        f"gap:0;padding:10px 20px;border-bottom:1px solid var(--border-subtle);"
                        f"align-items:center;font-size:0.875rem;color:var(--text-secondary);"
                        f"cursor:pointer;transition:background 0.1s;user-select:none;min-width:860px;{bg}{left_accent}"
                    )
                    row_el.on(
                        "click",
                        lambda e, lid=t.line_id: _select_item(
                            lid,
                            shift=e.args.get("shiftKey", False) if isinstance(e.args, dict) else False,
                            ctrl=e.args.get("ctrlKey", False) or e.args.get("metaKey", False)
                            if isinstance(e.args, dict) else False,
                        ),
                        ["shiftKey", "ctrlKey", "metaKey"],
                    )
                    with row_el:
                        def _toggle_row_cb(e, lid=t.line_id):
                            if e.value:
                                state["selected_lids"].add(lid)
                                state["selected_lid"] = lid
                            else:
                                state["selected_lids"].discard(lid)
                                if state["selected_lid"] == lid:
                                    state["selected_lid"] = next(iter(state["selected_lids"]), None)
                            _render_all()

                        _row_cb = ui.checkbox("", value=is_active, on_change=_toggle_row_cb).props(
                            "dense size=xs"
                        ).style("margin:0;padding:0;min-height:0")
                        _row_cb.on("click.stop", lambda: None)
                        ui.html(
                            f'<span class="status-dot status-dot-{t.match_status}"></span>'
                        )

                        with ui.element("div").style(
                            "overflow:hidden;white-space:nowrap;text-overflow:ellipsis"
                        ):
                            merchant_lbl = ui.label(t.merchant).classes("font-semibold text-slate-800 text-sm")
                            if t.translated_merchant_name:
                                merchant_lbl.tooltip(t.translated_merchant_name)

                        ui.label(t.date).classes("text-slate-500 text-sm")

                        with ui.element("div"):
                            ui.label(t.amount).classes("text-sm font-medium").style(
                                "font-variant-numeric:tabular-nums"
                            )
                            if t.currency and t.currency != "USD":
                                ui.label(t.currency).classes("text-xs text-slate-400")

                        ui.html(_badge_html(t.match_status, t.match_confidence))

                        with ui.element("div").style(
                            "overflow:hidden;white-space:nowrap;text-overflow:ellipsis;"
                            "color:var(--text-muted);font-size:0.75rem"
                        ):
                            ui.label(t.expense_type or "\u2014").classes(
                                "text-slate-500" if t.expense_type else "text-slate-300"
                            )

                        with ui.element("div").style(
                            "overflow:hidden;white-space:nowrap;text-overflow:ellipsis;"
                            "color:var(--text-muted);font-size:0.78rem"
                        ):
                            if item.receipt:
                                if t.approved:
                                    ui.html(
                                        '<span class="material-icons" style="font-size:0.9rem;color:#16a34a;'
                                        'vertical-align:middle;margin-right:3px">verified</span>'
                                    )
                                ui.label(item.receipt.vendor or item.receipt.filename)
                            else:
                                ui.label("\u2014").classes("text-slate-300")

                        with ui.element("div").style("text-align:center"):
                            is_missing = "receipt missing" in t.match_reason.lower()
                            if is_missing:
                                ui.icon("do_not_disturb").style(
                                    "font-size:1rem;color:#d97706"
                                ).tooltip("Receipt marked missing")

        def _do_bulk_mark_missing():
            for lid in list(state["selected_lids"]):
                svc.mark_receipt_missing(lid)
            ui.notify(f"Marked {len(state['selected_lids'])} item(s) as receipt missing", type="positive")
            _refresh_queue()
            _render_all()

        def _render_detail():
            multi_sel = state.get("selected_lids", set())
            if len(multi_sel) > 1:
                with ui.element("div").classes("detail-panel"):
                    with ui.element("div").classes("detail-panel-header"):
                        ui.label(f"{len(multi_sel)} transactions selected").classes(
                            "text-lg font-bold text-slate-800"
                        )
                        statuses = {}
                        for q in queue:
                            if q.transaction.line_id in multi_sel:
                                s = q.transaction.match_status
                                statuses[s] = statuses.get(s, 0) + 1
                        with ui.row().classes("items-center gap-2 mt-2 flex-wrap"):
                            for s, cnt in statuses.items():
                                ui.html(_badge_html(s, None, label=f"{cnt} {s}"))

                    with ui.element("div").classes("detail-panel-body"):
                        ui.label("Bulk Actions").classes(
                            "text-xs font-semibold text-slate-400 tracking-wider mb-3"
                        )
                        with ui.column().classes("gap-2 w-full"):
                            ui.button(
                                "Rescan selected",
                                icon="refresh",
                                on_click=_do_rescan_selected,
                            ).props("no-caps outline size=sm").classes("action-btn w-full")

                            ui.button(
                                "Mark selected as receipt missing",
                                icon="do_not_disturb",
                                on_click=_do_bulk_mark_missing,
                            ).props("no-caps outline size=sm color=amber").classes("action-btn w-full")

                            if report_filter_id:
                                ui.button(
                                    "Remove selected from report",
                                    icon="remove_circle_outline",
                                    on_click=_do_remove_from_report,
                                ).props("no-caps outline size=sm color=negative").classes("action-btn w-full")

                    with ui.element("div").classes("detail-panel-actions"):
                        ui.button(
                            "Clear selection",
                            icon="close",
                            on_click=lambda: (
                                state.update({"selected_lids": set(), "selected_lid": None}),
                                _render_all(),
                            ),
                        ).props("no-caps flat size=sm").classes("text-slate-400")
                return

            selected_lid = state.get("selected_lid")
            if not selected_lid:
                with ui.element("div").classes("detail-panel"):
                    with ui.element("div").style(
                        "padding:60px 24px;text-align:center;color:#94a3b8;"
                    ):
                        ui.icon("touch_app").classes("text-4xl mb-3")
                        ui.label("Select a transaction").classes("text-sm font-semibold text-slate-500 mb-1")
                        ui.label("Click any row to see match details").classes("text-xs")
                return

            item = None
            for q in queue:
                if q.transaction.line_id == selected_lid:
                    item = q
                    break
            if not item:
                return

            t = item.transaction
            status_color = {
                "high": "#16a34a", "medium": "#d97706", "low": "#dc2626", "unmatched": "#6b7280"
            }.get(t.match_status, "#6b7280")

            with ui.element("div").classes("detail-panel"):
                ui.element("div").style(f"height:4px;background:{status_color};width:100%")

                # Transaction header
                with ui.element("div").classes("detail-panel-header"):
                    ui.label(t.merchant).classes("text-lg font-bold text-slate-800")
                    if t.translated_merchant_name:
                        ui.label(t.translated_merchant_name).classes("text-sm text-slate-500 -mt-1 italic")
                    with ui.row().classes("items-center gap-3 mt-1"):
                        ui.label(f"${t.amount}").classes("text-xl font-bold text-slate-900")
                        if t.currency and t.currency != "USD":
                            ui.label(f"({t.currency} {t.amount})").classes("text-sm text-slate-500")
                        ui.label(t.date).classes("text-sm text-slate-500")
                    with ui.row().classes("items-center gap-2 mt-3"):
                        ui.html(_badge_html(t.match_status, t.match_confidence))
                        if t.approved:
                            ui.html(
                                '<span style="display:inline-flex;align-items:center;gap:3px;'
                                'color:#15803d;font-size:0.75rem;font-weight:600">'
                                '<span class="material-icons" style="font-size:0.95rem">verified</span>'
                                'Approved</span>'
                            )
                        if t.expense_type:
                            ui.html(
                                f'<span class="confidence-badge" style="background:#f0f9ff;color:#0369a1">'
                                f'{_esc(t.expense_type)}</span>'
                            )

                with ui.element("div").classes("detail-panel-body"):
                    # Receipt preview
                    if item.receipt:
                        r = item.receipt
                        with ui.row().classes("items-center gap-2 mb-3"):
                            ui.icon("description").classes("text-sm text-slate-400")
                            ui.label(r.vendor or r.filename).classes("text-sm font-semibold text-slate-700")
                        if r.total:
                            with ui.row().classes("items-center gap-2 mb-2"):
                                ui.label(f"Receipt: {r.currency} {r.total}").classes("text-xs text-slate-500")
                                if r.date:
                                    ui.label(f"· {r.date}").classes("text-xs text-slate-400")

                        if r.is_image and Path(r.source_file).is_file():
                            rotation = r.rotation * 90
                            preview_cid = f"pv{id(r)}"
                            with ui.element("div").style(
                                "width:100%;height:380px;position:relative;"
                                "border-radius:8px;border:1px solid var(--border-default);overflow:hidden;"
                                "background:var(--bg-surface);"
                            ).on("click", lambda _, d=r: _open_receipt_viewer(d)):
                                ui.html(
                                    f'<div id="{preview_cid}" style="position:absolute;top:0;left:0;right:0;bottom:0;'
                                    f'overflow:hidden;cursor:grab;touch-action:none;background:var(--bg-surface);">'
                                    f'<img src="{_img_url(r.source_file)}" style="transform-origin:0 0;'
                                    f'position:absolute;top:0;left:0;width:100%;height:100%;object-fit:contain;'
                                    f'user-select:none;pointer-events:none;" />'
                                    f'</div>'
                                    f'<div style="position:absolute;bottom:8px;right:8px;z-index:10;'
                                    f'background:rgba(255,255,255,0.9);border-radius:50%;width:28px;height:28px;'
                                    f'display:flex;align-items:center;justify-content:center;'
                                    f'box-shadow:0 1px 3px rgba(0,0,0,0.15);pointer-events:none;">'
                                    f'<span class="material-icons" style="font-size:16px;color:#475569;">open_in_full</span>'
                                    f'</div>'
                                ).style("position:absolute;top:0;left:0;right:0;bottom:0")
                            _pjs = _CARD_PREVIEW_JS.replace("__CID__", preview_cid).replace("__ROT__", str(rotation))
                            ui.timer(0.5, lambda _js=_pjs: ui.run_javascript(_js), once=True)
                        elif Path(r.source_file).is_file() and Path(r.source_file).suffix.lower() == ".pdf":
                            with ui.element("div").style(
                                "width:100%;height:380px;position:relative;"
                                "border-radius:8px;border:1px solid var(--border-default);overflow:hidden;"
                                "background:var(--bg-surface);cursor:pointer;"
                            ).on("click", lambda _, d=r: _open_receipt_viewer(d)):
                                ui.image(_pdf_thumb_url(r.source_file)).style(
                                    "width:100%;height:100%;object-fit:contain;"
                                )
                        else:
                            with ui.element("div").style(
                                "height:120px;background:var(--bg-surface);border-radius:8px;"
                                "border:1px solid var(--border-default);display:flex;align-items:center;"
                                "justify-content:center;gap:8px;cursor:pointer;"
                            ).on("click", lambda _, d=r: _open_receipt_viewer(d)):
                                ui.icon("picture_as_pdf").classes("text-2xl text-slate-400")
                                ui.label(r.filename).classes("text-sm text-slate-500")
                    else:
                        detail_is_missing = "receipt missing" in t.match_reason.lower()
                        if detail_is_missing:
                            with ui.element("div").style(
                                "height:100px;background:#fffbeb;border-radius:8px;"
                                "border:2px dashed #fde68a;display:flex;flex-direction:column;"
                                "align-items:center;justify-content:center;gap:6px;"
                            ):
                                ui.icon("do_not_disturb").classes("text-2xl text-amber-400")
                                ui.label("Receipt Missing").classes("text-xs text-amber-600 font-medium")
                        else:
                            with ui.element("div").style(
                                "height:100px;background:#fef2f2;border-radius:8px;"
                                "border:2px dashed #fca5a5;display:flex;flex-direction:column;"
                                "align-items:center;justify-content:center;gap:6px;"
                            ):
                                ui.icon("image_not_supported").classes("text-2xl text-red-300")
                                ui.label("No receipt matched").classes("text-xs text-red-400 font-medium")

                    # Reasoning
                    if t.match_reason:
                        with ui.element("div").style(
                            "margin-top:16px;padding:12px;background:var(--bg-surface);"
                            "border-radius:8px;border:1px solid var(--border-subtle);"
                        ):
                            ui.label("Reasoning").classes("text-xs font-semibold text-slate-400 tracking-wider mb-1")
                            ui.label(t.match_reason).classes("text-xs text-slate-600 leading-relaxed")

                # Action buttons
                with ui.element("div").classes("detail-panel-actions"):
                    lid = t.line_id

                    def _accept(lid=lid):
                        svc.approve_match(lid)
                        ui.notify("Match approved", type="positive")
                        _advance_selection(lid)

                    def _reject(lid=lid):
                        svc.reject_match(lid)
                        ui.notify("Match rejected", type="warning")
                        url = f"/matching?report={report_filter_id}" if report_filter_id else "/matching"
                        ui.navigate.to(url)

                    def _manual_pick(lid=lid):
                        _open_manual_pick_dialog(lid, all_receipts, lambda: ui.navigate.to("/matching"))

                    def _rescan_single(lid=lid):
                        _run_background(
                            "Matching",
                            lambda on_status, _lid=lid: svc.rescan_lines_for_match([_lid], on_status=on_status),
                            "Rescan complete",
                        )
                        _was_matching[0] = True

                    def _remove_single_from_report(lid=lid):
                        if not report_filter_id:
                            ui.notify("Select a report first to remove items", type="warning")
                            return
                        svc.assign_transactions_to_report([lid], None)
                        ui.notify("Removed from report", type="positive")
                        ui.navigate.to(f"/matching?report={report_filter_id}")

                    is_missing = "receipt missing" in t.match_reason.lower()

                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        if item.receipt:
                            ui.button("Accept", icon="check", on_click=_accept).props(
                                "color=positive no-caps unelevated size=sm"
                            ).classes("action-btn")
                            ui.button("Reject", icon="close", on_click=_reject).props(
                                "color=negative no-caps unelevated size=sm"
                            ).classes("action-btn")

                        if not item.receipt and not is_missing:
                            ui.button(
                                "Receipt Missing",
                                icon="do_not_disturb",
                                on_click=lambda _, lid=lid: _toggle_receipt_missing(lid, False),
                            ).props("no-caps outline size=sm color=amber").classes("action-btn")
                        elif is_missing:
                            ui.button(
                                "Undo Receipt Missing",
                                icon="undo",
                                on_click=lambda _, lid=lid: _toggle_receipt_missing(lid, True),
                            ).props("no-caps outline size=sm").classes("action-btn")

                        ui.button("Manual Pick", icon="image_search", on_click=_manual_pick).props(
                            "no-caps outline size=sm"
                        ).classes("action-btn")

                        ui.button("Rescan", icon="refresh", on_click=_rescan_single).props(
                            "no-caps outline size=sm"
                        ).classes("action-btn")

                        if report_filter_id:
                            ui.button("Remove from report", icon="remove_circle_outline", on_click=_remove_single_from_report).props(
                                "no-caps outline size=sm color=negative"
                            ).classes("action-btn")

        def _on_match_drawer_value(e):
            if e.value:
                return
            if state.get("selected_lid") or len(state.get("selected_lids", set())) > 0:
                state["selected_lid"] = None
                state["selected_lids"] = set()
                _render_all()

        match_detail_drawer.on_value_change(_on_match_drawer_value)

        def _advance_selection(current_lid: str):
            """After approving, move selection to next unreviewed item."""
            items = _filtered_queue()
            current_idx = None
            for i, q in enumerate(items):
                if q.transaction.line_id == current_lid:
                    current_idx = i
                    break
            if current_idx is not None and current_idx + 1 < len(items):
                state["selected_lid"] = items[current_idx + 1].transaction.line_id
            url = f"/matching?report={report_filter_id}" if report_filter_id else "/matching"
            ui.navigate.to(url)

        _render_all()

        # Keyboard shortcuts
        def _on_key(e):
            if not e.action.keydown:
                return
            key = e.key.name
            selected_lid = state.get("selected_lid")
            if not selected_lid:
                return
            item = None
            for q in queue:
                if q.transaction.line_id == selected_lid:
                    item = q
                    break
            if not item:
                return

            if key == "a" and item.receipt:
                svc.approve_match(item.transaction.line_id)
                ui.notify("Match approved", type="positive")
                _advance_selection(item.transaction.line_id)
            elif key == "r":
                svc.reject_match(item.transaction.line_id)
                ui.notify("Match rejected", type="warning")
                url = f"/matching?report={report_filter_id}" if report_filter_id else "/matching"
                ui.navigate.to(url)
            elif e.key.arrow_down or key == "j":
                items = _filtered_queue()
                for i, q in enumerate(items):
                    if q.transaction.line_id == selected_lid and i + 1 < len(items):
                        nid = items[i + 1].transaction.line_id
                        state["selected_lid"] = nid
                        state["selected_lids"] = {nid}
                        _render_all()
                        break
            elif e.key.arrow_up or key == "k":
                items = _filtered_queue()
                for i, q in enumerate(items):
                    if q.transaction.line_id == selected_lid and i > 0:
                        nid = items[i - 1].transaction.line_id
                        state["selected_lid"] = nid
                        state["selected_lids"] = {nid}
                        _render_all()
                        break

        ui.keyboard(on_key=_on_key)

        _was_matching = [activity_log.get_state().get("active_task") == "Matching"]

        def _check_matching_done():
            log_state = activity_log.get_state()
            is_matching = log_state.get("active_task") == "Matching"
            if _was_matching[0] and not is_matching:
                _was_matching[0] = False
                _refresh_queue()
                _render_all()
            elif is_matching:
                _was_matching[0] = True

        ui.timer(2.0, _check_matching_done)


def _open_manual_pick_dialog(line_id: str, receipts: list[ReceiptDoc], on_done):
    with ui.dialog().props("maximized") as dlg, ui.card().classes("w-full h-full"):
        with ui.row().classes("items-center justify-between w-full px-6 py-4 bg-slate-50 border-b"):
            ui.label("Select a Receipt").classes("text-lg font-semibold text-slate-800")
            ui.button(icon="close", on_click=dlg.close).props("flat round")

        with ui.scroll_area().classes("w-full flex-grow p-6"):
            with ui.element("div").style(
                "display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;"
            ):
                for r in receipts:
                    with ui.card().classes("cursor-pointer hover:shadow-lg transition-shadow").on(
                        "click",
                        lambda _, rid=r.source_file: (
                            svc.set_manual_match(line_id, rid),
                            ui.notify("Receipt assigned", type="positive"),
                            dlg.close(),
                            on_done(),
                        ),
                    ):
                        if r.is_image and Path(r.source_file).is_file():
                            ui.image(_img_url(r.source_file)).style(
                                "width:100%;height:160px;object-fit:cover;border-radius:8px"
                            )
                        elif Path(r.source_file).is_file() and Path(r.source_file).suffix.lower() == ".pdf":
                            ui.image(_pdf_thumb_url(r.source_file)).style(
                                "width:100%;height:160px;object-fit:cover;border-radius:8px"
                            )
                        else:
                            with ui.element("div").style(
                                "width:100%;height:160px;background:#f1f5f9;border-radius:8px;"
                                "display:flex;align-items:center;justify-content:center;"
                            ):
                                ui.icon("picture_as_pdf").classes("text-3xl text-slate-400")
                        ui.label(r.vendor or r.filename).classes("text-sm font-semibold text-slate-700 mt-2")
                        with ui.row().classes("items-center gap-2"):
                            if r.total:
                                ui.label(f"{r.currency} {r.total}").classes("text-xs text-slate-500")
                            if r.date:
                                ui.label(r.date).classes("text-xs text-slate-400")
    dlg.open()


# ---------------------------------------------------------------------------
# Vendor Classification
# ---------------------------------------------------------------------------

@ui.page("/classification")
def page_classification():
    page_frame("Vendor Classification")

    with ui.element("div").classes("page-container"):
        ui.html('<div class="section-title">Vendor Classification</div>')
        ui.html(
            '<div class="section-subtitle">'
            "Define the Expense Type to use whenever a merchant name matches exactly. "
            "New merchants are added automatically and classified by the LLM."
            "</div>"
        )

        vendors = svc.get_vendor_classifications()
        if not vendors:
            _empty_state("category", "No vendors", "Load transactions first to populate vendor list.")
            return

        sort_state = {"col": "merchant_key", "asc": True}
        search_state = {"term": ""}

        def _on_search(e):
            search_state["term"] = e.value or ""
            _build_table()

        ui.input(
            placeholder="Search merchants or expense types...",
            on_change=_on_search,
        ).props("dense outlined clearable").classes("classify-search").style("width:100%;max-width:480px")

        table_container = ui.element("div")

        def _filtered_sorted() -> list[dict[str, str]]:
            fresh = svc.get_vendor_classifications()
            term = search_state["term"].lower()
            if term:
                fresh = [
                    v for v in fresh
                    if term in v["merchant_key"].lower()
                    or term in v["expense_type"].lower()
                ]
            col = sort_state["col"]
            fresh.sort(key=lambda v: v[col].lower(), reverse=not sort_state["asc"])
            return fresh

        def _build_table():
            table_container.clear()
            rows = _filtered_sorted()
            col = sort_state["col"]
            asc = sort_state["asc"]
            m_arrow = " \u25b2" if (col == "merchant_key" and asc) else (" \u25bc" if col == "merchant_key" else "")
            t_arrow = " \u25b2" if (col == "expense_type" and asc) else (" \u25bc" if col == "expense_type" else "")

            with table_container:
                with ui.element("div").classes("classify-table"):
                    with ui.element("div").classes("classify-header"):
                        ui.html(
                            f'<span id="sort-merchant">Merchant{m_arrow}</span>'
                        ).on("click", lambda: _toggle_sort("merchant_key"))
                        ui.html(
                            f'<span id="sort-type">Expense Type{t_arrow}</span>'
                        ).on("click", lambda: _toggle_sort("expense_type"))

                    for v in rows:
                        mk = v["merchant_key"]
                        current_type = v["expense_type"]
                        opts = get_expense_type_options()
                        with ui.element("div").classes("classify-row"):
                            ui.label(mk).classes("classify-merchant")
                            ui.select(
                                options=opts,
                                value=current_type if current_type in opts else None,
                                on_change=lambda e, key=mk: (
                                    svc.set_vendor_classification(key, e.value),
                                    ui.notify(f"Set {key} \u2192 {e.value}", type="positive"),
                                ),
                            ).props("dense outlined hide-bottom-space").style("width:100%")

        def _toggle_sort(col: str):
            if sort_state["col"] == col:
                sort_state["asc"] = not sort_state["asc"]
            else:
                sort_state["col"] = col
                sort_state["asc"] = True
            _build_table()

        _build_table()


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

def _submit_mini_stat(label: str, current: int, total: int, color: str):
    pct = int((current / total) * 100) if total else 0
    with ui.element("div").style("flex:1;min-width:120px"):
        ui.html(
            f'<div style="font-size:0.65rem;font-weight:600;color:var(--text-muted);'
            f'text-transform:uppercase;letter-spacing:0.05em;margin-bottom:2px">{label}</div>'
            f'<div style="font-size:1rem;font-weight:700;color:{color}">{current}/{total}</div>'
            f'<div class="review-progress" style="margin-top:4px;height:4px">'
            f'<div class="review-progress-fill" style="width:{pct}%;background:{color}"></div>'
            f"</div>"
        )


@ui.page("/submit")
def page_submit(request: Request):
    svc.purge_expired()

    selected_report_id: str = ""
    try:
        selected_report_id = (request.query_params.get("report") or "").strip()
    except Exception:
        pass

    groups = svc.get_expense_report_groups()
    if not selected_report_id or not any(g.id == selected_report_id for g in groups):
        selected_report_id = groups[0].id if groups else ""

    page_frame("Submit", selected_report_id)

    with ui.element("div").classes("page-container"):

        ui.html('<div class="section-title">Submit</div>')
        ui.html('<div class="section-subtitle">Select a report to submit via Oracle automation</div>')

        if not groups:
            _empty_state(
                "send",
                "No reports created",
                "Go to Transactions to create expense reports and assign transactions.",
            )
            return

        # Find the selected report and show its details
        g = None
        for grp in groups:
            if grp.id == selected_report_id:
                g = grp
                break
        if not g:
            return

        r = svc.get_report_readiness(g.id)
        created = g.created_at[:10] if len(g.created_at) >= 10 else g.created_at
        pending_sub = svc.get_pending_submission(g.id)

        # ---- Incomplete submission banner ----
        if pending_sub:
            with ui.element("div").classes("w-full mb-4").style(
                "background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;"
                "padding:14px 18px"
            ):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("replay").classes("text-amber-600").style(
                        "font-size:22px"
                    )
                    with ui.column().classes("gap-1"):
                        ui.label(
                            "Previous submission did not complete"
                        ).classes("text-sm font-semibold text-amber-900")
                        ui.label(
                            "The browser may still be open. Click Submit "
                            "to resume the automation from where it stopped. "
                            "If the browser window was closed, the automation "
                            "will restart from the beginning."
                        ).classes("text-xs text-amber-700")

        with ui.card().classes("w-full mb-5").style(
            "border-radius:14px;padding:24px 28px"
        ):
            # ---- Header row: icon + name + status badge ----
            with ui.row().classes("items-center justify-between w-full"):
                with ui.row().classes("items-center gap-4 flex-grow"):
                    if r.submission_status == "Submitted":
                        ui.icon("check_circle").classes("text-2xl text-green-600")
                    elif pending_sub:
                        ui.icon("replay").classes("text-2xl text-amber-500")
                    elif r.ready and r.total_lines > 0:
                        ui.icon("send").classes("text-2xl text-blue-600")
                    elif r.total_lines == 0:
                        ui.icon("folder_open").classes("text-2xl text-slate-400")
                    else:
                        ui.icon("error_outline").classes("text-2xl text-amber-500")

                    with ui.column().classes("gap-0"):
                        ui.label(g.name).classes("font-semibold text-slate-800 text-base")
                        with ui.row().classes("items-center gap-3"):
                            ui.label(f"{r.total_lines} line{'s' if r.total_lines != 1 else ''}").classes(
                                "text-xs text-slate-500"
                            )
                            if created:
                                ui.label(f"Created {created}").classes("text-xs text-slate-400")

                with ui.row().classes("items-center gap-2"):
                    if r.submission_status == "Submitted":
                        ui.html(
                            '<span style="display:inline-flex;align-items:center;gap:4px;'
                            'background:#dcfce7;color:#166534;padding:4px 12px;border-radius:999px;'
                            'font-size:0.75rem;font-weight:600">'
                            '<span class="material-icons" style="font-size:14px">check_circle</span>'
                            "Submitted</span>"
                        )
                    elif pending_sub:
                        ui.html(
                            '<span style="display:inline-flex;align-items:center;gap:4px;'
                            'background:#fff7ed;color:#9a3412;padding:4px 12px;border-radius:999px;'
                            'font-size:0.75rem;font-weight:600">'
                            '<span class="material-icons" style="font-size:14px">replay</span>'
                            "Incomplete</span>"
                        )
                    elif r.submission_status == "Partial":
                        ui.html(
                            '<span style="display:inline-flex;align-items:center;gap:4px;'
                            'background:#fef3c7;color:#92400e;padding:4px 12px;border-radius:999px;'
                            'font-size:0.75rem;font-weight:600">'
                            '<span class="material-icons" style="font-size:14px">pending</span>'
                            "Partial</span>"
                        )

            # ---- Review summary stats (mini progress bars) ----
            if r.total_lines > 0:
                with ui.row().classes("gap-4 w-full flex-wrap mt-4"):
                    _submit_mini_stat(
                        "Matched", r.matched, r.total_lines,
                        "#16a34a" if r.matched == r.total_lines else "#d97706",
                    )
                    _submit_mini_stat(
                        "Approved", r.approved, r.total_lines,
                        "#16a34a" if r.approved == r.total_lines else "#d97706",
                    )
                    _submit_mini_stat(
                        "Classified", r.classified, r.total_lines,
                        "#16a34a" if r.classified == r.total_lines else "#64748b",
                    )

            # ---- Ready: show detail breakdown ----
            if r.total_lines > 0 and r.ready:
                detail_parts: list[str] = []
                if r.with_receipt:
                    detail_parts.append(f"{r.with_receipt} with receipt")
                if r.receipt_missing_marked:
                    detail_parts.append(f"{r.receipt_missing_marked} marked receipt missing")
                if detail_parts:
                    with ui.element("div").classes("w-full mt-3"):
                        ui.label(" · ".join(detail_parts)).classes("text-xs text-slate-500")

            # ---- Not ready: warning + attention items table ----
            if r.total_lines > 0 and not r.ready:
                with ui.element("div").classes("w-full mt-3").style(
                    "background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:12px 16px"
                ):
                    with ui.row().classes("items-center gap-2 mb-1"):
                        ui.icon("warning").classes("text-amber-500").style("font-size:18px")
                        ui.label("Not Ready").classes("text-sm font-semibold text-amber-800")
                    ui.label(
                        f"{r.needs_fix} transaction{'s' if r.needs_fix != 1 else ''} "
                        f"{'have' if r.needs_fix != 1 else 'has'} no receipt and "
                        f"{'are' if r.needs_fix != 1 else 'is'} not marked as 'receipt missing'. "
                        "Fix matches or mark items as receipt missing before submitting."
                    ).classes("text-xs text-amber-700")

                if r.attention_items:
                    ui.label("Items Needing Attention").classes(
                        "text-sm font-semibold text-slate-700 mt-4 mb-2"
                    )
                    tbl = '<table class="data-table"><thead><tr>'
                    for col in ["Merchant", "Date", "Amount", "Issue"]:
                        tbl += f"<th>{col}</th>"
                    tbl += "</tr></thead><tbody>"
                    for item in r.attention_items:
                        _curr = (
                            f' <span style="color:#94a3b8;font-size:0.75rem"> '
                            f"{_esc(item.currency)}</span>"
                            if item.currency and item.currency != "USD"
                            else ""
                        )
                        tbl += (
                            f"<tr>"
                            f"<td><strong>{_esc(item.merchant)}</strong></td>"
                            f"<td>{_esc(item.date)}</td>"
                            f"<td>{_esc(item.amount)}{_curr}</td>"
                            f"<td><span style='color:#dc2626;font-weight:500'>"
                            f"{_esc(item.issue)}</span></td>"
                            f"</tr>"
                        )
                    tbl += "</tbody></table>"
                    ui.html(tbl)

            # ---- Action buttons ----
            with ui.row().classes("items-center gap-2 mt-4"):
                if pending_sub and r.total_lines > 0:
                    ui.button(
                        "Resume Submission",
                        icon="replay",
                        on_click=lambda _, gid=g.id, gname=g.name: _do_submit(gid, gname),
                    ).props("color=warning no-caps unelevated size=sm").classes("action-btn")
                elif r.ready and r.total_lines > 0:
                    ui.button(
                        "Submit Report",
                        icon="send",
                        on_click=lambda _, gid=g.id, gname=g.name: _do_submit(gid, gname),
                    ).props("color=positive no-caps unelevated size=sm").classes("action-btn")
                elif r.total_lines > 0 and not r.ready:
                    def _mark_all_receipt_missing(report_id=g.id, lids=r.needs_fix_line_ids):
                        for lid in lids:
                            svc.mark_receipt_missing(lid)
                        ui.notify(
                            f"Marked {len(lids)} item{'s' if len(lids) != 1 else ''} as receipt missing",
                            type="positive",
                        )
                        ui.navigate.to(f"/submit?report={report_id}")

                    ui.button(
                        "Mark All Receipt Missing",
                        icon="do_not_disturb",
                        on_click=lambda _: _mark_all_receipt_missing(),
                    ).props("no-caps unelevated size=sm color=amber").classes("action-btn")
                    ui.button(
                        "Fix Matches",
                        icon="build",
                        on_click=lambda _, gid=g.id: ui.navigate.to(f"/matching?report={gid}"),
                    ).props("color=warning no-caps unelevated size=sm").classes("action-btn")

                ui.button(
                    "Mark Report as Submitted",
                    icon="check_circle_outline",
                    on_click=lambda _, gid=g.id, gname=g.name, n=r.total_lines: _open_mark_submitted_dialog(gid, gname, n),
                ).props("color=negative no-caps flat size=sm")
                ui.label(
                    "Transactions and attached documents will be removed from this view "
                    "and permanently deleted after 5 days."
                ).classes("text-xs text-slate-400").style("max-width:340px")

        def _do_submit(report_id: str, report_name: str):
            result = svc.prepare_report_for_submission(report_id)
            if "error" in result:
                ui.notify(result["error"], type="negative")
                return
            n = result.get("approved", 0)

            def _continue_submit() -> None:
                ui.notify(
                    f"Report '{report_name}' prepared — {n} receipt(s) approved. "
                    "Launching browser automation\u2026",
                    type="positive",
                    timeout=6000,
                )
                _trigger_submission_automation(report_id, report_name)

            _open_oracle_manual_login_dialog(_continue_submit)

        def _trigger_submission_automation(report_id: str, report_name: str):
            activity_log.emit("step", f"Submitting report: {report_name}")
            ui.notify(
                "Browser automation triggered. The Chromium browser will open and "
                "navigate through the Oracle wizard. Check the terminal for progress.",
                type="info",
                timeout=8000,
            )

            def _do_submission(on_status):
                return svc.run_submission(report_id, on_status=on_status)

            _run_background(
                "Report Submission",
                _do_submission,
                f"Report '{report_name}' submission complete.",
            )

        def _open_mark_submitted_dialog(report_id: str, report_name: str, n_lines: int):
            with ui.dialog() as dlg, ui.card().style(
                "min-width:450px;border-radius:16px;padding:28px"
            ):
                ui.label("Mark Report as Submitted").classes("text-lg font-bold text-slate-800 mb-2")
                with ui.column().classes("gap-2 mb-6"):
                    ui.label(
                        f'Mark "{report_name}" as submitted?'
                    ).classes("text-sm text-slate-700")
                    with ui.element("div").style(
                        "background:#fef9e7;border-radius:8px;padding:12px 16px"
                    ):
                        ui.label("What will happen:").classes(
                            "text-xs font-semibold text-amber-800 mb-1"
                        )
                        ui.label(
                            f"• {n_lines} transaction(s) will be removed from this view"
                        ).classes("text-xs text-amber-700")
                        ui.label("• Attached receipt images / documents will be hidden").classes(
                            "text-xs text-amber-700"
                        )
                        ui.label(
                            "• All data will be permanently deleted after 5 days"
                        ).classes("text-xs text-amber-700")
                    ui.label(
                        "You can restore the report from Settings before the 5-day window expires."
                    ).classes("text-xs text-slate-500 italic")

                with ui.row().classes("items-center justify-end gap-3"):
                    ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                    def _do_mark():
                        result = svc.mark_report_submitted(report_id)
                        dlg.close()
                        if "error" in result:
                            ui.notify(f"Failed: {result['error']}", type="negative")
                        else:
                            n = result.get("line_count", 0)
                            ui.notify(
                                f"'{report_name}' marked as submitted — {n} transaction(s) "
                                f"scheduled for deletion in 5 days.",
                                type="positive",
                            )
                            ui.navigate.to("/submit")

                    ui.button("Mark as Submitted", icon="check_circle", on_click=_do_mark).props(
                        "color=primary no-caps unelevated"
                    ).classes("action-btn")
            dlg.open()

        with ui.card().classes("w-full mt-6").style(
            "border-radius:12px;padding:20px;background:var(--bg-surface);border:1px solid var(--border-default)"
        ):
            with ui.row().classes("items-center gap-3"):
                ui.icon("vpn_lock").classes("text-2xl text-amber-600")
                with ui.column().classes("gap-1"):
                    ui.label("VPN Required").classes("font-semibold text-slate-700 text-sm")
                    ui.label(
                        "Turn VPN on before submitting. The Oracle portal requires "
                        "network access through VPN to create expense reports."
                    ).classes("text-xs text-slate-500")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@ui.page("/settings")
def page_settings():
    page_frame("Settings")

    with ui.element("div").classes("page-container"):
        current = svc.get_settings()
        missing = svc.missing_credentials()

        if missing:
            missing_html = ", ".join(f"<b>{m}</b>" for m in missing)
            ui.html(f"""
            <div style="
                background:linear-gradient(135deg,#eff6ff,#f5f3ff);
                border:2px solid #3b82f6;border-radius:16px;
                padding:24px 28px;margin-bottom:24px;max-width:640px;
            ">
              <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
                <span class="material-icons" style="color:#3b82f6;font-size:1.5rem">info</span>
                <span style="font-size:1.05rem;font-weight:700;color:#1e3a5f">
                  Welcome! Enter your credentials to get started.
                </span>
              </div>
              <div style="color:#475569;font-size:0.9rem;line-height:1.6;padding-left:36px">
                Fill in the fields below and click <b>Save Settings</b>.
                Still needed: {missing_html}
              </div>
            </div>
            """)

        ui.html('<div class="section-title">Settings</div>')
        ui.html('<div class="section-subtitle">Credentials and configuration</div>')

        with ui.card().classes("w-full mb-6").style("border-radius:16px;padding:32px;max-width:640px"):
            ui.label("OpenAI").classes("text-base font-bold text-slate-800 mb-4")

            with ui.column().classes("w-full mb-3 gap-2"):
                if current["openai_key_set"]:
                    with ui.row().classes("items-center gap-2 w-full").style(
                        "background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;"
                        "padding:8px 12px;margin-bottom:4px"
                    ):
                        ui.icon("check_circle").style("color:#16a34a;font-size:1.1rem")
                        ui.label(f"API key saved ({current['openai_key_hint']})").style(
                            "color:#15803d;font-size:0.85rem;font-weight:500"
                        )
                    openai_key_input = ui.input(
                        label="Replace API Key",
                        password=True,
                        password_toggle_button=True,
                        placeholder="Enter a new key to replace the saved one",
                    ).classes("w-full").props('outlined dense clearable')
                else:
                    with ui.row().classes("items-center gap-2 w-full").style(
                        "background:#fef2f2;border:1px solid #fecaca;border-radius:8px;"
                        "padding:8px 12px;margin-bottom:4px"
                    ):
                        ui.icon("warning").style("color:#dc2626;font-size:1.1rem")
                        ui.label("No API key saved — required for receipt analysis").style(
                            "color:#991b1b;font-size:0.85rem;font-weight:500"
                        )
                    openai_key_input = ui.input(
                        label="API Key",
                        password=True,
                        password_toggle_button=True,
                        placeholder="sk-...",
                    ).classes("w-full").props('outlined dense clearable')
                openai_key_input.style("pointer-events:auto")
                ui.label("Get your API key →").style(
                    "font-size:0.8rem;color:#3b82f6;text-decoration:underline;cursor:pointer"
                ).on("click", lambda: webbrowser.open("https://platform.openai.com/api-keys"))

            openai_model_input = ui.input(
                label="Model",
                value=current["openai_model"],
            ).classes("w-full").props('outlined dense clearable')
            openai_model_input.style("pointer-events:auto")

        with ui.card().classes("w-full mb-6").style("border-radius:16px;padding:32px;max-width:640px"):
            ui.label("Oracle Expense Portal").classes("text-base font-bold text-slate-800 mb-4")

            oracle_url_input = ui.input(
                label="Portal URL",
                value=current["oracle_url"],
            ).classes("w-full mb-3").props('outlined dense clearable')
            oracle_url_input.style("pointer-events:auto")

            approver_input = ui.input(
                label="Approver (display name in Oracle)",
                value=current.get("approver", ""),
                placeholder='e.g. John Richard Smith → "Smith, John Richard"',
            ).classes("w-full mb-3").props('outlined dense clearable')
            approver_input.style("pointer-events:auto")

            nav_label_input = ui.input(
                label="Navigator menu label (iExpenses folder name)",
                value=current.get("nav_menu_label", ""),
                placeholder='e.g. "NIC iExpenses" — leave blank for default',
            ).classes("w-full").props('outlined dense clearable')
            nav_label_input.style("pointer-events:auto")

            ui.label(
                "Oracle username and password are never saved. You sign in manually in the "
                "browser whenever scraping or submission opens Chromium."
            ).classes("text-xs text-slate-500 mt-2")

        def _save():
            warnings = svc.save_settings(
                oracle_url=oracle_url_input.value,
                approver=approver_input.value,
                openai_key=openai_key_input.value or None,
                openai_model=openai_model_input.value,
                nav_menu_label=nav_label_input.value,
            )
            if warnings:
                for w in warnings:
                    ui.notify(w, type="warning", timeout=8000)
            ui.notify("Settings saved", type="positive")
            # Reload the settings page so the API key status indicator updates
            ui.timer(0.5, lambda: ui.navigate.to("/settings"), once=True)

        ui.button("Save Settings", icon="save", on_click=_save).props(
            "no-caps unelevated color=primary"
        ).classes("action-btn")

        # ---- Factory reset ----
        with ui.card().classes("w-full mt-8").style(
            "border-radius:16px;padding:32px;max-width:640px"
        ):
            with ui.row().classes("items-center gap-3 mb-2"):
                ui.icon("restart_alt").classes("text-xl text-red-600")
                ui.label("Factory Reset").classes(
                    "text-base font-bold text-slate-800"
                )
            ui.label(
                "Delete all documents, transactions, matches, settings, and "
                "cached data. Your OpenAI API key will also be removed from "
                "the system keychain. This cannot be undone."
            ).classes("text-xs text-slate-500 mb-4")

            def _confirm_reset():
                with ui.dialog() as confirm_dlg, ui.card().style(
                    "min-width:400px;max-width:480px;border-radius:16px;padding:28px"
                ):
                    ui.label("Reset everything?").classes(
                        "text-lg font-bold text-slate-800 mb-2"
                    )
                    ui.label(
                        "All data will be permanently deleted and the app "
                        "will return to its initial state. This action cannot "
                        "be undone."
                    ).classes("text-sm text-slate-600 mb-4")
                    with ui.row().classes("items-center justify-end gap-2 w-full"):
                        ui.button("Cancel", on_click=confirm_dlg.close).props(
                            "flat no-caps"
                        )

                        def _do_reset():
                            confirm_dlg.close()
                            svc.reset_all_data()
                            ui.notify(
                                "All data has been reset.",
                                type="positive",
                                timeout=4000,
                            )
                            ui.timer(
                                0.5,
                                lambda: ui.navigate.to("/settings"),
                                once=True,
                            )

                        ui.button(
                            "Reset Everything",
                            icon="delete_forever",
                            on_click=_do_reset,
                        ).props("no-caps unelevated color=negative")
                confirm_dlg.open()

            ui.button(
                "Reset to Defaults",
                icon="restart_alt",
                on_click=_confirm_reset,
            ).props("no-caps outline color=negative")

        # ---- Pending deletions ----
        svc.purge_expired()
        pending = svc.get_pending_deletions()

        with ui.card().classes("w-full mt-8").style(
            "border-radius:16px;padding:32px;max-width:800px"
        ):
            with ui.row().classes("items-center gap-3 mb-4"):
                ui.icon("schedule").classes("text-xl text-amber-600")
                ui.label("Reports Pending Deletion").classes(
                    "text-base font-bold text-slate-800"
                )

            if not pending:
                with ui.element("div").style(
                    "background:var(--bg-surface);border-radius:8px;padding:20px;text-align:center"
                ):
                    ui.label("No reports pending deletion.").classes(
                        "text-sm text-slate-400"
                    )
            else:
                ui.label(
                    "These reports have been marked as submitted. They will be "
                    "permanently deleted when the countdown expires. You can "
                    "restore them before that."
                ).classes("text-xs text-slate-500 mb-4")

                pending_container = ui.column().classes("w-full gap-3")

                def _render_pending():
                    pending_container.clear()
                    current_pending = svc.get_pending_deletions()
                    if not current_pending:
                        with pending_container:
                            with ui.element("div").style(
                                "background:var(--bg-surface);border-radius:8px;padding:20px;text-align:center"
                            ):
                                ui.label("No reports pending deletion.").classes(
                                    "text-sm text-slate-400"
                                )
                        return

                    with pending_container:
                        for item in current_pending:
                            rid = item.get("report_id", "")
                            name = item.get("report_name", "Untitled")
                            n_lines = len(item.get("line_ids", []))
                            n_files = len(item.get("receipt_files", []))
                            marked_at = item.get("marked_at", "")[:10]
                            delete_after = item.get("delete_after", "")[:10]

                            try:
                                da = datetime.fromisoformat(item.get("delete_after", ""))
                                remaining = da - datetime.now(timezone.utc)
                                days_left = max(0, remaining.days)
                                hours_left = max(0, remaining.seconds // 3600)
                                if days_left > 0:
                                    time_str = f"{days_left}d {hours_left}h remaining"
                                elif hours_left > 0:
                                    time_str = f"{hours_left}h remaining"
                                else:
                                    time_str = "Expiring soon"
                            except (ValueError, TypeError):
                                time_str = ""

                            with ui.element("div").style(
                                "background:#fffbeb;border:1px solid #fde68a;"
                                "border-radius:10px;padding:14px 18px"
                            ):
                                with ui.row().classes(
                                    "items-center justify-between w-full"
                                ):
                                    with ui.column().classes("gap-1"):
                                        ui.label(name).classes(
                                            "font-semibold text-slate-800 text-sm"
                                        )
                                        with ui.row().classes("items-center gap-3"):
                                            ui.label(
                                                f"{n_lines} transaction{'s' if n_lines != 1 else ''}"
                                            ).classes("text-xs text-slate-500")
                                            ui.label(
                                                f"{n_files} document{'s' if n_files != 1 else ''}"
                                            ).classes("text-xs text-slate-500")
                                            ui.label(f"Marked {marked_at}").classes(
                                                "text-xs text-slate-400"
                                            )
                                        if time_str:
                                            ui.html(
                                                f'<span style="display:inline-flex;align-items:center;gap:4px;'
                                                f'background:#fef3c7;color:#92400e;padding:2px 10px;border-radius:999px;'
                                                f'font-size:0.7rem;font-weight:600;margin-top:2px">'
                                                f'<span class="material-icons" style="font-size:12px">schedule</span>'
                                                f"{_esc(time_str)}</span>"
                                            )

                                    def _do_restore(report_id=rid, report_name=name):
                                        result = svc.restore_report(report_id)
                                        if "error" in result:
                                            ui.notify(
                                                f"Restore failed: {result['error']}",
                                                type="negative",
                                            )
                                        else:
                                            ui.notify(
                                                f"'{report_name}' restored successfully.",
                                                type="positive",
                                            )
                                            _render_pending()

                                    ui.button(
                                        "Restore",
                                        icon="restore",
                                        on_click=lambda _, r=rid, n=name: _do_restore(r, n),
                                    ).props(
                                        "no-caps unelevated color=primary size=sm"
                                    ).classes("action-btn")

                _render_pending()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _kill_existing_on_port(port: int) -> None:
    """Kill any process already listening on *port* so we can bind cleanly."""
    import signal
    import subprocess

    try:
        if sys.platform == "win32":
            _cflags = subprocess.CREATE_NO_WINDOW
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=5,
                creationflags=_cflags,
            )
            pids: set[int] = set()
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        try:
                            pids.add(int(parts[-1]))
                        except ValueError:
                            pass
            pids.discard(os.getpid())
            for pid in pids:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True, timeout=5,
                        creationflags=_cflags,
                    )
                except Exception:
                    pass
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            )
            pids = {int(p) for p in result.stdout.split() if p.strip().isdigit()}
            pids.discard(os.getpid())
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except Exception:
        pass


if __name__ == "__main__":
    _kill_existing_on_port(8080)

if __name__ in {"__main__", "__mp_main__"}:
    from web.macos_single_process_webview import (
        patch_nicegui_server_run,
        patch_nicegui_skip_process_pool_on_frozen_macos,
        use_embedded_webview,
    )

    patch_nicegui_skip_process_pool_on_frozen_macos()
    patch_nicegui_server_run()
    _native = os.environ.get("EXPENSE_AUTOMATOR_NATIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    _kw = {
        "title": "Expense Automator",
        "port": 8080,
        "reload": False,
        "favicon": "💰",
    }
    if use_embedded_webview():
        ui.run(**_kw, show=False, native=False, host="127.0.0.1")
    elif _native:
        ui.run(**_kw, native=True, window_size=(1280, 800))
    else:
        ui.run(**_kw, show=True)
