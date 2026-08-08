from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys

from gui_runtime_guard import ensure_gui_runtime_ok

ensure_gui_runtime_ok()

import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from datetime import datetime
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = None  # type: ignore[misc, assignment]
    ImageOps = None  # type: ignore[misc, assignment]
    ImageTk = None  # type: ignore[misc, assignment]

import keychain_credentials
from dotenv import load_dotenv
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from browser.reliability import RetryPolicy, execute_with_retry, is_transient_error
from browser.runtime import ensure_chromium_executable
from browser_automation import (
    analyze_receipts_with_llm,
    build_openai_client,
    normalize_currency_code,
    openai_tls_troubleshooting_hint,
    receipt_local_amount_display,
    receipt_usd_amount_display,
    write_analysis_report,
)
from orchestration.events import JsonlAutomationEventSink
from expense_lines_cache import (
    approved_match_path,
    delete_report_with_data,
    expense_lines_cache_path,
    get_report_submission_status,
    load_analyses_snapshot,
    load_approved_matches,
    load_expense_lines_cache,
    load_expense_report_groups,
    load_receipt_line_matches,
    persist_expense_line_derived_fields,
    prune_receipt_sidecars_after_step2_scrape,
    receipt_analyses_snapshot_path,
    receipt_line_match_path,
    record_submitted_receipt,
    remove_expense_lines_by_ids,
    save_analyses_snapshot,
    save_approved_matches,
    save_expense_report_groups,
    save_expense_lines_cache,
    save_receipt_line_matches,
    validate_approved_for_attach,
    validate_lines_cache_for_match,
)
from expense_match_normalize import (
    _parse_amount_currency_token,
    format_date_for_ui,
    signature_from_cached_line,
    signature_from_step6_row,
)
from llm_query_cache import (
    expense_type_prompt,
    expense_type_query_id,
    load_document,
    new_empty_document,
    pending_expense_type_ids,
    register_expense_type_query,
    response_expense_type,
    save_document,
    set_response_expense_type,
    validate_replay_ready,
    llm_pending_file,
)
from classification.service import classify_transactions
from matching.pipeline import match_transactions_to_receipts
from portal_expense_types import PORTAL_EXPENSE_TYPE_OPTIONS, get_expense_type_options
from receipt_matching import (
    REVIEW_CONFIDENCE_THRESHOLD,
    match_one_expense_line_to_receipts,
)


APP_DIR = Path.home() / ".expense-automator"
CHROMIUM_USER_DATA = APP_DIR / "chromium-profile"
SETTINGS_FILE = APP_DIR / "settings.json"
STATE_FILE = APP_DIR / "state.json"
UI_LAYOUT_FILE = APP_DIR / "ui_layout.json"
VENDOR_EXPENSE_CACHE_FILE = APP_DIR / "vendor_expense_types.json"
ENV_OPENAI_KEY = "OPENAI_API_KEY"
SETTINGS_OPENAI_KEY = "openai_api_key"
DEBUG_LOG_PATH = APP_DIR / "debug.log"
DEBUG_SESSION_ID = "debug"
INCLUDE_CHECKED = "[x]"
INCLUDE_UNCHECKED = "[ ]"
AUTO_INCLUDE_CONFIDENCE_THRESHOLD = REVIEW_CONFIDENCE_THRESHOLD

load_dotenv()

try:
    keychain_credentials.warm_up()
except Exception:
    pass

# Ordered resume anchors for Step 3 (create expense report). Keys must match _execute_populate_from phases.
POPULATE_RESUME_STEPS: list[tuple[str, str]] = [
    ("nic_iexpenses", "Step 3: Navigate to Expenses Home (iExpenses Navigator)"),
    ("create_report", "Step 4.0: Open Create Expense Report (new report path)"),
    ("wait_step1", "Step 4.1 (Oracle Step 1 of 6): Verify purpose and approver"),
    ("select_template", "Step 4.1 internal: verify Travel template"),
    ("fill_purpose", "Step 4.1 internal: verify/set Purpose"),
    ("fill_approver", "Step 4.1 internal: verify/set Approver"),
    ("save_step1", "Step 4.1 internal: Save General Information"),
    ("next_from_step1", "Step 4.1 internal: Next to Step 2"),
    ("wait_step2", "Step 4.2 (Oracle Step 2 of 6): Verify Credit Card Transactions page"),
    ("credit_card_transactions", "Step 4.2: Select/verify report transactions across pages"),
    (
        "complete_report_step2",
        "Step 4.3 preflight: rewind Step 2, then continue Step 3 workflow",
    ),
    (
        "step3_autofill",
        "Step 4.3.1/4.3.2/4.3.3 (Oracle Step 3 of 6): types, missing receipts, validation fixes",
    ),
    ("step4_no_action_next", "Step 4.4 (Oracle Step 4 of 6): no action, click Next"),
    ("step5_no_action_next", "Step 4.5 (Oracle Step 5 of 6): no action, click Next"),
    ("step6_attach_files", "Step 4.6 (Oracle Step 6 of 6): Attach documents"),
]
POPULATE_RESUME_KEYS = [key for key, _ in POPULATE_RESUME_STEPS]
RESUME_DIALOG_STEPS: list[tuple[str, str]] = [
    ("nic_iexpenses", "Step 3: Navigate to Expenses Home"),
    ("create_report", "Step 4.0: Open Create Expense Report"),
    ("wait_step1", "Step 4.1 / Oracle 1 of 6: Verify purpose + approver"),
    ("wait_step2", "Step 4.2 / Oracle 2 of 6: Verify selected transactions"),
    ("credit_card_transactions", "Step 4.2: Select/verify transactions and save"),
    (
        "complete_report_step2",
        "Step 4.3 preflight: Rewind Step 2 then move into Step 3",
    ),
    (
        "step3_autofill",
        "Step 4.3.1-4.3.3 / Oracle 3 of 6: Type, justification, missing receipts, fix errors",
    ),
    ("step4_no_action_next", "Step 4.4 / Oracle 4 of 6: No action, click Next"),
    ("step5_no_action_next", "Step 4.5 / Oracle 5 of 6: No action, click Next"),
    ("step6_attach_files", "Step 4.6 / Oracle 6 of 6: Attach documents"),
]
RESUME_DIALOG_KEYS = [key for key, _ in RESUME_DIALOG_STEPS]
RESUME_DIALOG_LABEL_BY_KEY = dict(RESUME_DIALOG_STEPS)
AUTO_DETECT_RESUME_CHOICE = "__auto_detect_resume__"
# Returned from _ask_resume_populate_step when user chooses full crash recovery (relaunch + in-progress report).
CRASH_RESUME_DIALOG_CHOICE = "__crash_resume__"

# Table pagination only (e.g. "Next 10", "Next 3", "Next one") — not the wizard button named exactly "Next".
_EXPENSE_TABLE_NEXT_NAME = re.compile(
    r"^\s*Next\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s*$",
    re.IGNORECASE,
)
_EXPENSE_TABLE_PREV_NAME = re.compile(
    r"^\s*Previous\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s*$",
    re.IGNORECASE,
)
# Oracle sometimes exposes extra characters in the accessible name; allow substring-style match.
_EXPENSE_TABLE_NEXT_NAME_LOOSE = re.compile(
    r"Next\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
    re.IGNORECASE,
)
_EXPENSE_TABLE_PREV_NAME_LOOSE = re.compile(
    r"Previous\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
    re.IGNORECASE,
)

# Step 3 yellow banner when receipt/reimbursement currency validation fails (Oracle iExpenses).
_STEP3_CURRENCY_ERROR_MARKERS = (
    "Exchange Rate will default to 1",
    "Receipt Currency is the same as the Reimbursement Currency",
)

def _step3_banner_chunk_is_currency_exchange_error(chunk: str) -> bool:
    """True if the banner text after ``Line N Error -`` describes receipt/reimbursement currency (not Expense Type)."""
    c = (chunk or "").lower()
    if not c.strip():
        return False
    if "expense type" in c and "please enter" in c:
        return False
    return ("exchange rate will default" in c) or (
        "receipt currency is the same as the reimbursement currency" in c
    )


def _step3_row_throttle_ms() -> int:
    """Pause between Step 3 grid row entries. Default 250ms; ``0`` disables. Set ``AUTOMATED_EXPENSES_STEP3_ROW_THROTTLE_MS``."""
    raw = (os.environ.get("AUTOMATED_EXPENSES_STEP3_ROW_THROTTLE_MS") or "250").strip()
    try:
        return max(0, min(int(raw), 5000))
    except ValueError:
        return 250


def _step3_details_flow_throttle_ms() -> int:
    """Pause around Step 3 line Details / Original Receipt Missing. Default 300ms; ``0`` disables. Set ``AUTOMATED_EXPENSES_STEP3_DETAILS_THROTTLE_MS``."""
    raw = (os.environ.get("AUTOMATED_EXPENSES_STEP3_DETAILS_THROTTLE_MS") or "300").strip()
    try:
        return max(0, min(int(raw), 5000))
    except ValueError:
        return 300


def _chromium_breather_ms() -> int:
    """Extra pause after CDP-heavy operations so the renderer can catch up. Default 100ms; ``AUTOMATED_EXPENSES_CHROMIUM_BREATHER_MS``; ``0`` disables."""
    raw = (os.environ.get("AUTOMATED_EXPENSES_CHROMIUM_BREATHER_MS") or "100").strip()
    try:
        return max(0, min(int(raw), 3000))
    except ValueError:
        return 100


def _step3_post_mutation_settle_ms() -> int:
    """Fixed delay after Step 3 mutations (after breather). ``AUTOMATED_EXPENSES_STEP3_POST_MUTATION_MS``; default 50ms."""
    raw = (os.environ.get("AUTOMATED_EXPENSES_STEP3_POST_MUTATION_MS") or "50").strip()
    try:
        return max(0, min(int(raw), 2000))
    except ValueError:
        return 50


def _step3_post_return_modal_delay_ms() -> int:
    """Pause right after Step 3 Return before modal interaction. ``AUTOMATED_EXPENSES_STEP3_POST_RETURN_MODAL_DELAY_MS``; default 700ms."""
    raw = (
        os.environ.get("AUTOMATED_EXPENSES_STEP3_POST_RETURN_MODAL_DELAY_MS") or "700"
    ).strip()
    try:
        return max(0, min(int(raw), 5000))
    except ValueError:
        return 700


def _step3_ready_state_wait_cap_ms() -> int:
    """Max time to wait for ``document.readyState === 'complete'`` on the main frame after a mutation. ``AUTOMATED_EXPENSES_STEP3_READYSTATE_WAIT_MS``; default 1500ms; ``0`` skips."""
    raw = (os.environ.get("AUTOMATED_EXPENSES_STEP3_READYSTATE_WAIT_MS") or "1500").strip()
    try:
        return max(0, min(int(raw), 15000))
    except ValueError:
        return 1500


def _blob_shows_wizard_step(blob: str, step: int, total: int = 6) -> bool:
    """Match Oracle wizard progress: 'Step 3 of 6', '3 of 6', and 5- or 6-step wizards."""
    if not blob or step < 1:
        return False
    for t in sorted({total, 5, 6}):
        if step > t:
            continue
        if f"Step {step} of {t}" in blob:
            return True
        compact = re.sub(r"\s+", "", blob)
        if f"Step{step}of{t}" in compact:
            return True
        if re.search(rf"(?<![0-9]){step}\s+of\s+{t}(?![0-9])", blob):
            return True
    return False


def _frame_inner_text_has_approver_label(blob: str) -> bool:
    """True if body innerText mentions an approver field.

    Oracle pages may use different casing or wording; requiring the exact substring ``Approver``
    caused every iframe to be skipped (up to ~35s of retries) even when the form was visible.
    """
    return bool(blob and re.search(r"\bapprovers?\b", blob, re.IGNORECASE))


# Main-window activity track: Step 1–2 labels plus Step 3 resume anchors (order matters).
ACTIVITY_UI_STEPS: list[tuple[str, str]] = [
    ("step1", "Step 1: Import & analyze receipts"),
    ("step2", "Step 2: Expense portal (browser login)"),
    *POPULATE_RESUME_STEPS,
]

# Short labels for header checklist (done = ✓, not done = ·)
WORKFLOW_CHECKLIST: list[tuple[str, str]] = [
    ("browser", "Browser"),
    ("scrape", "Scrape"),
    ("receipts", "Files"),
    ("parsed", "Parse"),
    ("types", "Types"),
    ("match", "Match"),
    ("approve", "OK'd"),
]


class AutomationCancelled(Exception):
    """User requested stop between Step 3 phases."""


def _normalize_vendor_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _match_label_to_options(label: str, options: list[str]) -> str | None:
    """Return the canonical option string if label matches one of the dropdown options."""
    if not label or not options:
        return None
    label_stripped = label.strip()
    exact = {opt.lower(): opt for opt in options}
    if label_stripped.lower() in exact:
        return exact[label_stripped.lower()]
    lowered = label_stripped.lower()
    for option in options:
        ol = option.lower()
        if ol in lowered or lowered in ol:
            return option
    return None


@dataclass
class AppSettings:
    legacy_url: str = ""
    approver: str = ""
    openai_model: str = "gpt-4.1-mini"
    openai_http_verify: str = ""
    photos_limit: int = 5
    photos_export_dir: str = "./photos-exports"
    nav_menu_label: str = ""


# Preview pane: keep source bounded so pan/zoom stays responsive; sharpen after gestures settle.
_PREVIEW_SOURCE_MAX_EDGE = 2200
_PREVIEW_HQ_DEBOUNCE_MS = 110
_PREVIEW_WHEEL_COALESCE_MS = 10


class ReceiptAutomationUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Expense automator")
        self.root.geometry("1100x720")
        self.root.minsize(900, 600)

        self.settings = self.load_settings()
        self.vendor_expense_cache: dict[str, str] = self._load_vendor_expense_cache()
        self._expense_types_tree_iid_meta: dict[str, dict] = {}
        self.receipt_paths: list[str] = []
        self.analyses: list[dict] = []
        self.assignment_map: dict[str, str] = {}
        self._activity_log_max_lines = 500
        self._run_id = f"run-{int(time.time())}"
        self._event_sink = JsonlAutomationEventSink(APP_DIR / "automation-events.ndjson")
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.browser_context: BrowserContext | None = None
        self.browser_page: Page | None = None
        self._openai_client = None
        self._openai_key_cache = ""
        self._bootstrap_openai_key()
        self._last_populate_step: str = POPULATE_RESUME_KEYS[0]
        self._crash_resume_anchor: str | None = None
        self.resume_after_crash_btn: ttk.Button | None = None
        self._automation_cancel = threading.Event()
        self._step3_automation_active = False
        self._populate_ui_current: str | None = None
        self._receipt_llm_worker_active = False
        self._receipt_llm_cancel = threading.Event()
        # Cached disk reads for Documents table columns (line / approved); invalidate when match files change.
        self._receipt_table_ma_cache: tuple[dict[str, dict], set[str]] | None = None
        self._activity_stopped_at_key: str | None = None
        self._populate_flow_completed = False
        # "standard" | "vpn_collect" | "vpn_replay" — vpn_collect stops after Step 2 scrape; replay/fill use Step 3.
        self._step3_vpn_mode: str = "standard"
        self._skip_match_vpn_prompt_until: float = 0.0
        self._llm_replay_document: dict | None = None
        self._llm_resolve_worker_active = False
        self._expense_types_scan_worker_active = False
        self._match_receipts_worker_active = False
        self._scraped_expense_lines: list[dict] = []
        self._run_step6_file_attach: bool = False
        self._chromium_proc: subprocess.Popen[bytes] | None = None
        self._cdp_http_url: str | None = None
        self._cdp_unreachable_streak: int = 0
        self._pending_release_browser: bool = False
        self._workflow_chk_labels: dict[str, ttk.Label] = {}
        # Progress row: only mark Scrape / Files / Parse after doing them this session (not from disk).
        self._session_progress_scrape_done: bool = False
        self._session_progress_receipts_done: bool = False
        self._session_progress_parsed_done: bool = False
        # Browser column: ✓ live link, ✗ user closed window after connect, · never / released.
        self._progress_browser_had_live_link: bool = False
        self._progress_browser_released_by_user: bool = False
        self._workflow_poll_after_id: str | None = None
        self._workflow_poll_prev_usable: bool | None = None
        self._chromium_stderr_log_fp: object | None = None
        self._preview_target_canvas: tk.Canvas | None = None
        self._step2_credit_card_frame: Frame | None = None
        self._assign_row_paths: dict[str, str] = {}
        self._assign_row_include: dict[str, bool] = {}
        self._assign_row_llm_reason_raw: dict[str, str] = {}
        self._preview_raw_exif_image: object | None = None
        self._preview_display_path: str = ""
        self._preview_llm_quarter_turns: int = 0
        self._preview_session_quarter_turns: int = 0
        self._preview_user_zoom: float = 1.0
        self._preview_pan_x: float = 0.0
        self._preview_pan_y: float = 0.0
        self._preview_drag_last: tuple[int, int] | None = None
        self._preview_last_scale: float = 1.0
        self._preview_photo: object | None = None
        self._preview_hq_timer: str | None = None
        self._preview_wheel_coalesce_timer: str | None = None
        self._preview_use_fast_resample: bool = False
        self._preview_macos_pinch_keepalive: list[object] = []
        self._preview_pinch_canvas_ids: set[int] = set()
        self._run_status_phase: str = "Idle"
        self._run_status_progress_pct: int = 0
        self._run_status_attention: str = "None"
        self._run_status_message: str = "Waiting for next action."
        self._expense_report_attention_only: bool = False

        self._build_layout()
        self._apply_ui_layout()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._load_persisted_state()
        if not self.analyses:
            snap = load_analyses_snapshot(APP_DIR)
            if snap:
                self.analyses = list(snap)
        elif not receipt_analyses_snapshot_path(APP_DIR).exists() and self.analyses:
            try:
                save_analyses_snapshot(APP_DIR, list(self.analyses))
            except Exception:
                pass
        if not self.receipt_paths and self.analyses:
            paths: list[str] = []
            seen: set[str] = set()
            for a in self.analyses:
                p = str(a.get("source_file", "") or "").strip()
                if p and p not in seen:
                    seen.add(p)
                    paths.append(p)
            if paths:
                self.receipt_paths = paths
        self._workflow_poll_prev_usable = self._controlled_browser_usable()
        self._start_workflow_state_polling()
        self._backfill_expense_line_derived_if_needed()
        self.refresh_all_tabs()
        self._setup_ui_layout_persistence()
        self._set_run_status()

    def _build_layout(self) -> None:
        from ui.shell import build_main_shell

        build_main_shell(self)

    def _on_notebook_tab_changed(self, event: tk.Event) -> None:
        w = event.widget
        if w is not getattr(self, "main_notebook", None):
            return
        try:
            name = w.tab(w.index("current"), "text")
        except tk.TclError:
            return
        if name == "Settings":
            self._sync_settings_tab_vars()
        elif name == "Workflow":
            self.refresh_workflow_views()
        elif name == "Vendor Classification":
            self.refresh_expense_types_tab()
        elif name == "Expense types":
            self.refresh_expense_types_tab()
        elif name == "Expense report":
            self.refresh_expense_report_tab()
        elif name == "Documents":
            self._documents_update_preview_from_selection()

    def focus_settings_tab(self) -> None:
        if hasattr(self, "_frame_settings"):
            self.main_notebook.select(self._frame_settings)
            self._sync_settings_tab_vars()

    def show_workflow_stage(self, stage_key: str) -> None:
        frames = getattr(self, "_workflow_stage_frames", {})
        if not isinstance(frames, dict) or stage_key not in frames:
            return
        for key, frame in frames.items():
            if key == stage_key:
                frame.tkraise()
        self._workflow_stage_key = stage_key
        btns = getattr(self, "_workflow_stage_buttons", {})
        if isinstance(btns, dict):
            for key, btn in btns.items():
                try:
                    btn.configure(state=(tk.DISABLED if key == stage_key else tk.NORMAL))
                except tk.TclError:
                    pass
        if hasattr(self, "_frame_workflow"):
            self.main_notebook.select(self._frame_workflow)
        self.refresh_workflow_views()

    def focus_expense_report_tab(self) -> None:
        if hasattr(self, "_workflow_stage_frames"):
            self.show_workflow_stage("matching")
            return
        if hasattr(self, "_frame_expense_report"):
            self.main_notebook.select(self._frame_expense_report)
            self.refresh_expense_report_tab()

    def focus_expense_types_tab(self) -> None:
        if hasattr(self, "_workflow_stage_frames"):
            self.show_workflow_stage("classification")
            return
        if hasattr(self, "_frame_expense_types"):
            self.main_notebook.select(self._frame_expense_types)
            self.refresh_expense_types_tab()

    def refresh_oracle_transactions_view(self) -> None:
        tree = getattr(self, "oracle_transactions_tree", None)
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        lines, _ = load_expense_lines_cache(APP_DIR)
        matches = load_receipt_line_matches(APP_DIR)
        report_filter = self._get_selected_report_line_ids()
        if report_filter is not None:
            lines = [l for l in lines if str(l.get("line_id", "") or "").strip() in report_filter]
        for line in lines:
            lid = str(line.get("line_id", "") or "").strip()
            if not lid:
                continue
            block = matches.get(lid) or {}
            best = str(block.get("best_receipt") or "").strip()
            match_status = "Matched" if best else "Unmatched"
            et = self._expense_type_cell_for_line(line)
            class_status = "Classified" if et and et != "—" else "Pending"
            tree.insert(
                "",
                tk.END,
                iid=lid,
                values=(
                    lid,
                    str(line.get("merchant_name", "") or "")[:120],
                    format_date_for_ui(str(line.get("transaction_date", "") or ""))[:24],
                    str(line.get("amount", "") or "")[:16],
                    str(line.get("currency", "") or "")[:8],
                    match_status,
                    class_status,
                ),
            )
        matched = sum(
            1
            for line in lines
            if str((matches.get(str(line.get("line_id", "") or "").strip()) or {}).get("best_receipt") or "").strip()
        )
        summary = getattr(self, "_oracle_stage_summary_var", None)
        if summary is not None:
            summary.set(
                f"Scraped {len(lines)} transaction(s) · matched {matched} · unmatched {max(0, len(lines) - matched)}"
            )

    def refresh_final_review_view(self) -> None:
        readiness = getattr(self, "_review_readiness_var", None)
        summary = getattr(self, "_review_summary_var", None)
        blockers_list = getattr(self, "_review_blockers_list", None)
        if readiness is None or summary is None or blockers_list is None:
            return
        lines, _ = load_expense_lines_cache(APP_DIR)
        matches = load_receipt_line_matches(APP_DIR)
        approved = load_approved_matches(APP_DIR)
        report_filter = self._get_selected_report_line_ids()
        if report_filter is not None:
            lines = [l for l in lines if str(l.get("line_id", "") or "").strip() in report_filter]
        low_conf = 0
        missing = 0
        blockers: list[str] = []
        blocker_line_ids: list[str] = []
        for line in lines:
            lid = str(line.get("line_id", "") or "").strip()
            if not lid:
                continue
            m = matches.get(lid) or {}
            best = str(m.get("best_receipt") or "").strip()
            try:
                cf = float(m.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                cf = 0.0
            if cf < REVIEW_CONFIDENCE_THRESHOLD:
                low_conf += 1
            if not best:
                missing += 1
                blockers.append(f"{lid}: missing receipt match.")
                blocker_line_ids.append(lid)
            elif not Path(best).expanduser().is_file():
                missing += 1
                blockers.append(f"{lid}: matched file not found on disk.")
                blocker_line_ids.append(lid)
            if lid not in approved:
                blockers.append(f"{lid}: not approved for attachment.")
                blocker_line_ids.append(lid)
        matched = max(0, len(lines) - missing)
        summary.set(
            f"Matched: {matched} | Missing receipts: {missing} | Low confidence: {low_conf} | Total: {len(lines)}"
        )
        ready = len(blockers) == 0 and len(lines) > 0
        readiness.set("READY to submit" if ready else "NOT READY")
        blockers_list.delete(0, tk.END)
        if not blockers:
            blockers_list.insert(tk.END, "No blockers found.")
        else:
            for b in blockers[:500]:
                blockers_list.insert(tk.END, b)
        self._review_blocker_line_ids = blocker_line_ids[:500]

    def on_final_review_fix_selected(self) -> None:
        lb = getattr(self, "_review_blockers_list", None)
        if lb is None:
            self.on_focus_attention_items()
            return
        sel = lb.curselection()
        if not sel:
            self.on_focus_attention_items()
            return
        idx = int(sel[0])
        lids = getattr(self, "_review_blocker_line_ids", [])
        lid = lids[idx] if idx < len(lids) else ""
        self.focus_expense_report_tab()
        tree = getattr(self, "expense_report_tree", None)
        if tree is None or not lid or lid not in tree.get_children():
            self.on_focus_attention_items()
            return
        tree.selection_set(lid)
        tree.focus(lid)
        tree.see(lid)
        self._assignments_show_preview_for_line_id(lid)
        self._matching_workspace_update_for_line(lid)
        self.set_status(f"Final review fix: jumped to blocker line {lid}.")

    def refresh_submission_timeline(self) -> None:
        widget = getattr(self, "_submission_timeline_text", None)
        if widget is None:
            return
        path = APP_DIR / "automation-events.ndjson"
        lines: list[str] = []
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8").splitlines()
                lines = raw[-80:]
            except Exception:
                lines = []
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        if not lines:
            widget.insert(tk.END, "No automation timeline events yet.")
        else:
            for ln in lines:
                try:
                    payload = json.loads(ln)
                    msg = str(payload.get("message", "") or "").strip()
                    kind = str(payload.get("kind", "") or "").strip()
                    phase = str(payload.get("phase", "") or "").strip()
                    widget.insert(tk.END, f"[{phase or '-'}] {kind}: {msg}\n")
                except Exception:
                    widget.insert(tk.END, ln + "\n")
        widget.configure(state=tk.DISABLED)
        status = getattr(self, "_submission_status_var", None)
        if status is not None:
            status.set("Submission timeline refreshed.")

    def refresh_submit_reports_table(self) -> None:
        tree = getattr(self, "_submit_reports_tree", None)
        if tree is None:
            return
        for item in tree.get_children():
            tree.delete(item)
        reports = load_expense_report_groups(APP_DIR)
        if not reports:
            return
        selected_rid = self._matching_report_id_map.get(self._matching_report_var.get())
        for rid, data in sorted(reports.items(), key=lambda kv: kv[1].get("created_at", "")):
            if selected_rid is not None and rid != selected_rid:
                continue
            name = str(data.get("name", "")).strip() or "Untitled"
            line_ids = data.get("line_ids", [])
            n_lines = len(line_ids) if isinstance(line_ids, list) else 0
            created = str(data.get("created_at", "")).strip()
            if created and len(created) >= 10:
                created = created[:10]
            status = get_report_submission_status(APP_DIR, rid)
            tree.insert("", tk.END, iid=rid, values=(name, n_lines, created, status))

    def _get_selected_submit_report_id(self) -> str | None:
        tree = getattr(self, "_submit_reports_tree", None)
        if tree is None:
            return None
        sel = tree.selection()
        if not sel:
            self.set_status("Select a report from the table first.")
            return None
        return str(sel[0])

    def on_submit_selected_report(self) -> None:
        """Load the selected report's lines into the matching workspace, approve them, then run automation."""
        rid = self._get_selected_submit_report_id()
        if not rid:
            return

        reports = load_expense_report_groups(APP_DIR)
        report = reports.get(rid)
        if not report:
            self.set_status("Report not found.")
            return

        line_ids = report.get("line_ids", [])
        if not line_ids:
            self.set_status(f"Report '{report.get('name', 'Untitled')}' has no transactions assigned.")
            return

        line_id_set = {str(lid).strip() for lid in line_ids if str(lid).strip()}
        matches = load_receipt_line_matches(APP_DIR)
        approved: dict[str, dict] = {}
        missing_receipt: list[str] = []

        for lid in line_id_set:
            m = matches.get(lid, {})
            best = str(m.get("best_receipt") or "").strip()
            if best and Path(best).expanduser().is_file():
                approved[lid] = {"source_file": best, "approved": True}
            else:
                missing_receipt.append(lid)

        if not approved:
            self.set_status(
                f"Submit blocked: none of the {len(line_id_set)} lines in "
                f"'{report.get('name', 'Untitled')}' have a receipt file on disk."
            )
            return

        if missing_receipt:
            self.set_status(
                f"Warning: {len(missing_receipt)} line(s) have no receipt — "
                f"submitting {len(approved)} line(s) that do."
            )

        save_approved_matches(APP_DIR, approved)

        if hasattr(self, "expense_report_tree"):
            tree = self.expense_report_tree
            for lid in tree.get_children():
                self._assign_row_include[lid] = lid in approved
            self._invalidate_receipt_table_match_cache()

        if self._step3_automation_active:
            self.set_status("Submit blocked: stop the running automation first.")
            return

        if not load_analyses_snapshot(APP_DIR) and not self.receipt_paths:
            self.set_status("Submit blocked: import receipts or run matching first.")
            return

        ok_m, err_m = validate_approved_for_attach(APP_DIR)
        if not ok_m:
            self.set_status(f"Submit blocked (approvals): {err_m}")
            return

        if not self._prepare_complete_report_llm_mode():
            return

        if not self._controlled_browser_usable():
            self.set_status(
                f"Submitting '{report.get('name', 'Untitled')}': opening Chromium — "
                "complete login or 2FA in the browser if prompted."
            )
            self.on_step_login()

        if not self.browser_page:
            self.set_status(
                "Submit blocked: Chromium not connected. "
                "Use Open Oracle to sign in, then try Submit again."
            )
            return

        self._run_step6_file_attach = True
        self._submit_report_name = str(report.get("name", "")).strip() or "Expense Report"
        self.set_status(
            f"Submitting report '{self._submit_report_name}' ({len(approved)} lines): "
            "Step 1 open browser → Step 2 login → Step 3 Expenses Home → "
            "Step 4.1 to 4.6 (Oracle wizard) …"
        )
        self._run_populate_expense_report_flow(start_from="nic_iexpenses")

    def on_delete_selected_report(self) -> None:
        """Delete the selected report, its transactions, and associated receipt documents."""
        rid = self._get_selected_submit_report_id()
        if not rid:
            return

        reports = load_expense_report_groups(APP_DIR)
        report = reports.get(rid)
        if not report:
            self.set_status("Report not found.")
            return

        report_name = str(report.get("name", "")).strip() or "Untitled"
        n_lines = len(report.get("line_ids", []))

        confirm = messagebox.askyesno(
            "Delete report",
            f"Delete report '{report_name}' and its {n_lines} transaction(s) "
            f"plus attached receipt images?\n\n"
            f"Merchant classifications will be preserved for future reports.",
            parent=self.root,
        )
        if not confirm:
            return

        result = delete_report_with_data(APP_DIR, rid)
        if "error" in result:
            self.set_status(f"Delete failed: {result['error']}")
            return

        self.refresh_submit_reports_table()
        self.refresh_all_tabs()
        self.set_status(
            f"Deleted report '{report_name}': "
            f"{result.get('removed_lines', 0)} transactions, "
            f"{result.get('deleted_files', 0)} receipt files removed."
        )

    def refresh_workflow_dashboard(self) -> None:
        kpi = getattr(self, "_workflow_dashboard_kpi_var", None)
        ready = getattr(self, "_workflow_dashboard_ready_var", None)
        nxt = getattr(self, "_workflow_dashboard_next_var", None)
        if kpi is None or ready is None or nxt is None:
            return
        lines, _ = load_expense_lines_cache(APP_DIR)
        matches = load_receipt_line_matches(APP_DIR)
        approved = load_approved_matches(APP_DIR)
        report_filter = self._get_selected_report_line_ids()
        if report_filter is not None:
            lines = [l for l in lines if str(l.get("line_id", "") or "").strip() in report_filter]
            line_id_set = report_filter
        else:
            line_id_set = {str(l.get("line_id", "") or "").strip() for l in lines}
        matched = 0
        low_conf = 0
        for lid, block in matches.items():
            if lid not in line_id_set:
                continue
            if str(block.get("best_receipt") or "").strip():
                matched += 1
            try:
                if float(block.get("confidence", 0.0) or 0.0) < REVIEW_CONFIDENCE_THRESHOLD:
                    low_conf += 1
            except (TypeError, ValueError):
                low_conf += 1
        unmatched = max(0, len(lines) - matched)
        kpi.set(
            f"Transactions: {len(lines)} | Matched: {matched} | Unmatched: {unmatched} | Low confidence: {low_conf}"
        )
        ready_to_submit = len(lines) > 0 and len(approved) > 0 and unmatched == 0
        ready.set("Readiness: READY TO SUBMIT" if ready_to_submit else "Readiness: Needs review")
        if len(lines) == 0:
            nxt.set("Next step: Scrape Oracle transactions.")
        elif not self.receipt_paths:
            nxt.set("Next step: Import documents.")
        elif not matches:
            nxt.set("Next step: Run matching.")
        elif self._expense_report_attention_line_ids():
            nxt.set("Next step: Review attention items.")
        else:
            nxt.set("Next step: Final Review and Submission.")

    def refresh_workflow_views(self) -> None:
        self._populate_matching_report_combo()
        self._refresh_report_header_status()
        self.refresh_workflow_dashboard()
        self.refresh_oracle_transactions_view()
        self.refresh_classification_transactions_view()
        self.refresh_final_review_view()
        self.refresh_submit_reports_table()
        self.refresh_submission_timeline()

    def on_workflow_resume(self) -> None:
        lines, _ = load_expense_lines_cache(APP_DIR)
        matches = load_receipt_line_matches(APP_DIR)
        if not lines:
            self.show_workflow_stage("oracle")
            self.set_status("Resume workflow: scrape Oracle transactions first.")
            return
        if not self.receipt_paths:
            self.show_workflow_stage("documents")
            self.set_status("Resume workflow: import receipt documents.")
            return
        if not matches:
            self.show_workflow_stage("matching")
            self.set_status("Resume workflow: run transaction ↔ receipt matching.")
            return
        if self._expense_report_attention_line_ids():
            self.show_workflow_stage("matching")
            self.set_status("Resume workflow: resolve attention items in matching workspace.")
            return
        self.show_workflow_stage("review")
        self.set_status("Resume workflow: final review is next.")

    def refresh_classification_transactions_view(self) -> None:
        tree = getattr(self, "classification_transactions_tree", None)
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        lines, _ = load_expense_lines_cache(APP_DIR)
        cache = dict(self._load_vendor_expense_cache())
        suggestions = classify_transactions(lines, user_memory=cache)
        by_id = {str(r.get("transaction_id", "") or "").strip(): r for r in suggestions if isinstance(r, dict)}
        combo = getattr(self, "_classification_type_combo", None)
        if combo is not None:
            combo.configure(values=list(get_expense_type_options()))
        for line in lines:
            lid = str(line.get("line_id", "") or "").strip()
            if not lid:
                continue
            s = by_id.get(lid) or {}
            et = str(s.get("type") or self._expense_type_cell_for_line(line) or "Uncategorized")
            conf = s.get("confidence", 0.0)
            try:
                conf_txt = f"{float(conf):.2f}"
            except (TypeError, ValueError):
                conf_txt = str(conf)
            tree.insert(
                "",
                tk.END,
                iid=lid,
                values=(
                    lid,
                    str(line.get("merchant_name", "") or "")[:120],
                    et[:120],
                    conf_txt,
                    str(s.get("source") or "rule"),
                    str(s.get("justification") or "")[:240],
                ),
            )

    def _on_classification_row_select(self, _event: object | None = None) -> None:
        tree = getattr(self, "classification_transactions_tree", None)
        if tree is None:
            return
        sel = list(tree.selection())
        if not sel:
            return
        lid = sel[0]
        vals = tree.item(lid, "values") or ()
        lbl = getattr(self, "_classification_selected_line_var", None)
        if lbl is not None:
            lbl.set(f"Line: {lid}")
        tvar = getattr(self, "_classification_type_var", None)
        if tvar is not None and len(vals) > 2:
            tvar.set(str(vals[2]))
        jvar = getattr(self, "_classification_justification_var", None)
        if jvar is not None and len(vals) > 5:
            jvar.set(str(vals[5]))

    def on_classification_apply_selected(self) -> None:
        tree = getattr(self, "classification_transactions_tree", None)
        if tree is None:
            return
        sel = list(tree.selection())
        if not sel:
            self.set_status("Classification: select a transaction row first.")
            return
        lid = sel[0]
        new_type = str(getattr(self, "_classification_type_var", tk.StringVar()).get() or "").strip()
        if not new_type:
            self.set_status("Classification: choose an expense type first.")
            return
        lines, _ = load_expense_lines_cache(APP_DIR)
        line = next((ln for ln in lines if str(ln.get("line_id", "") or "").strip() == lid), None)
        if not isinstance(line, dict):
            self.set_status("Classification: selected row is no longer in cache.")
            return
        vk = _normalize_vendor_key(str(line.get("merchant_name", "") or ""))
        if not vk:
            self.set_status("Classification: missing merchant key for selected row.")
            return
        ok, err = self._expense_types_commit_mapping(None, vk, new_type)
        if not ok:
            self.set_status(f"Classification blocked: {err}")
            return
        self.refresh_workflow_views()
        self.set_status(f'Classification saved: "{vk}" -> "{new_type}".')

    def on_classification_apply_to_similar(self) -> None:
        tree = getattr(self, "classification_transactions_tree", None)
        if tree is None:
            return
        sel = list(tree.selection())
        if not sel:
            self.set_status("Apply to similar: select a transaction row first.")
            return
        lid = sel[0]
        new_type = str(getattr(self, "_classification_type_var", tk.StringVar()).get() or "").strip()
        if not new_type:
            self.set_status("Apply to similar: choose an expense type first.")
            return
        vals = tree.item(lid, "values") or ()
        merchant = str(vals[1] if len(vals) > 1 else "").strip()
        if not merchant:
            self.set_status("Apply to similar: selected row has no merchant.")
            return
        vk = _normalize_vendor_key(merchant)
        ok, err = self._expense_types_commit_mapping(None, vk, new_type)
        if not ok:
            self.set_status(f"Apply to similar blocked: {err}")
            return
        self.refresh_workflow_views()
        self.set_status(f'Applied "{new_type}" to similar merchant rows for "{vk}".')

    def on_expense_report_show_attention_only(self) -> None:
        self._expense_report_attention_only = True
        self.refresh_expense_report_tab()
        self.set_status("Expense report filter: showing only low-confidence or unmatched rows.")

    def on_expense_report_show_all_rows(self) -> None:
        self._expense_report_attention_only = False
        self.refresh_expense_report_tab()
        self.set_status("Expense report filter: showing all rows.")

    def _expense_report_attention_line_ids(self) -> list[str]:
        tree = getattr(self, "expense_report_tree", None)
        if tree is None:
            return []
        out: list[str] = []
        for lid in tree.get_children():
            try:
                values = tree.item(lid, "values") or ()
                tags = set(tree.item(lid, "tags") or ())
            except tk.TclError:
                continue
            file_val = str(values[7] if len(values) > 7 else "").strip()
            conf_val = str(values[9] if len(values) > 9 else "").strip()
            no_file = (not self._assign_row_paths.get(str(lid), "").strip()) or file_val in {"", "—"}
            low_conf = "low_confidence" in tags
            try:
                conf_f = float(conf_val) if conf_val else None
            except (TypeError, ValueError):
                conf_f = None
            if conf_f is not None and conf_f < REVIEW_CONFIDENCE_THRESHOLD:
                low_conf = True
            if no_file or low_conf:
                out.append(str(lid))
        return out

    def on_focus_attention_items(self) -> None:
        self.focus_expense_report_tab()
        tree = getattr(self, "expense_report_tree", None)
        if tree is None:
            self.set_status("Attention review unavailable: expense report table is not ready.")
            return
        ids = self._expense_report_attention_line_ids()
        if not ids:
            self.set_status("No attention items found: all rows have files and acceptable confidence.")
            self._set_run_status(attention="None", message="No low-confidence or unmatched rows need review.")
            return
        first = ids[0]
        try:
            tree.selection_set(ids)
            tree.focus(first)
            tree.see(first)
        except tk.TclError:
            pass
        self._assignments_show_preview_for_line_id(first)
        self.set_status(
            f"Attention focus: selected {len(ids)} low-confidence/unmatched row(s) in Expense report."
        )
        self._set_run_status(
            attention=f"{len(ids)} item(s) need review",
            message="Review selected low-confidence/unmatched rows in Expense report.",
        )

    def _sync_settings_tab_vars(self) -> None:
        if not hasattr(self, "_settings_url_var"):
            return
        self._settings_url_var.set(self.settings.legacy_url)
        self._settings_approver_var.set(self.settings.approver)
        self._settings_model_var.set(self.settings.openai_model)
        self._settings_limit_var.set(str(self.settings.photos_limit))
        self._settings_export_var.set(self.settings.photos_export_dir)
        self._settings_api_var.set(self.get_openai_key())
        self._settings_tls_var.set(self.settings.openai_http_verify)

    def _expense_type_cell_for_line(self, line: dict, *, vendor_cache: dict[str, str] | None = None) -> str:
        m = str(line.get("merchant_name", "") or "").strip()
        vk = _normalize_vendor_key(m)
        cache = vendor_cache if vendor_cache is not None else self.vendor_expense_cache
        if vk:
            t = str(cache.get(vk, "") or "").strip()
            if t:
                return t
        t2 = str(line.get("cached_expense_type", "") or "").strip()
        return t2 if t2 else "—"

    def _replace_vendor_cache_and_persist(self, cache: dict[str, str]) -> None:
        self.vendor_expense_cache = dict(cache)
        self._persist_vendor_expense_cache()

    @staticmethod
    def _include_cell_text(included: bool) -> str:
        return INCLUDE_CHECKED if included else INCLUDE_UNCHECKED

    @staticmethod
    def _compose_llm_note_with_match_name(path_str: str, reason_text: str) -> str:
        name = Path(str(path_str or "").strip()).name
        reason = str(reason_text or "").strip()
        if name and reason:
            return f"{name} — {reason}"
        if name:
            return name
        return reason

    def _expense_report_set_selected_note_detail(self, text: str) -> None:
        widget = getattr(self, "_expense_report_llm_note_text", None)
        if widget is None:
            return
        try:
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert("1.0", text if str(text).strip() else "No LLM note for this line.")
            widget.configure(state=tk.DISABLED)
        except tk.TclError:
            return

    def _expense_report_copy_selected_note(self) -> None:
        if not hasattr(self, "expense_report_tree"):
            self.set_status("Copy note: expense report table is not ready.")
            return
        lid = self._treeview_primary_selection_iid(self.expense_report_tree)
        if not lid:
            self.set_status("Copy note: select a row first.")
            return
        path_str = self._assign_row_paths.get(lid, "").strip()
        reason_raw = self._assign_row_llm_reason_raw.get(lid, "")
        text = self._compose_llm_note_with_match_name(path_str, reason_raw).strip()
        if not text:
            self.set_status("Copy note: selected row has no LLM note text.")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
        except tk.TclError as exc:
            self.set_status(f"Copy note failed: {exc}")
            return
        line_label = str(lid).strip() or "(unknown line)"
        self.set_status(f"Copied full LLM note for line {line_label} to clipboard.")

    def _refill_expense_report_tree_rows(self, lines: list[dict], *, scraping: bool) -> None:
        tree = self.expense_report_tree
        for item in tree.get_children():
            tree.delete(item)
        self._assign_row_paths.clear()
        self._assign_row_include.clear()
        self._assign_row_llm_reason_raw.clear()
        self.vendor_expense_cache = self._load_vendor_expense_cache()
        vcache = self.vendor_expense_cache

        if scraping:
            llm_matches: dict = {}
            approved_prev: dict = {}
        else:
            llm_matches = load_receipt_line_matches(APP_DIR)
            approved_prev = load_approved_matches(APP_DIR)

        attention_total = 0
        shown_count = 0

        for line in lines:
            lid = str(line.get("line_id", "") or "").strip()
            if not lid:
                continue
            if scraping:
                et = self._expense_type_cell_for_line(line, vendor_cache=vcache)
                vals = (
                    self._include_cell_text(False),
                    lid,
                    str(line.get("merchant_name", "") or "")[:80],
                    format_date_for_ui(str(line.get("transaction_date", "") or ""))[:24],
                    str(line.get("amount", "") or "")[:16],
                    str(line.get("currency", "") or "")[:8],
                    et[:80] if et != "—" else "—",
                    "—",
                    "\u2713",
                    "",
                    "",
                )
                self._assign_row_paths[lid] = ""
                self._assign_row_include[lid] = False
                self._assign_row_llm_reason_raw[lid] = ""
            else:
                block = dict(llm_matches.get(lid) or {})
                # Do not substitute embedded cache when this line has an explicit match record (even if best_receipt is empty).
                has_saved_match = lid in llm_matches
                if (
                    not str(block.get("best_receipt") or "").strip()
                    and not has_saved_match
                ):
                    cbr = str(line.get("cached_best_receipt") or "").strip()
                    if cbr:
                        block["best_receipt"] = cbr
                        if block.get("confidence") is None or str(block.get("confidence")).strip() == "":
                            block["confidence"] = line.get("cached_match_confidence", "")
                        if not str(block.get("reason") or "").strip():
                            block["reason"] = str(line.get("cached_match_reason") or "")
                best = str(block.get("best_receipt") or "").strip()
                conf = block.get("confidence", "")
                reason_raw = str(block.get("reason", "") or "").strip()
                prev = approved_prev.get(lid)
                approved_override_mismatch = False
                has_explicit_llm_match = lid in llm_matches
                use_approved_override = bool(prev and str(prev.get("source_file") or "").strip()) and (
                    not has_explicit_llm_match
                )
                if use_approved_override:
                    use = True
                    rpath = str(prev.get("source_file")).strip()
                    if best and Path(best).expanduser() != Path(rpath).expanduser():
                        approved_override_mismatch = True
                        # region agent log
                        self._debug_log(
                            hypothesis_id="H1",
                            location="receipt_automation_ui.py:_refill_expense_report_tree_rows",
                            message="Approved receipt overrides LLM best_receipt for line",
                            data={
                                "line_id": lid,
                                "approved_source_file": rpath,
                                "llm_best_receipt": best,
                                "approved_present": True,
                            },
                            run_id="pairing_drift_probe",
                        )
                        # endregion
                else:
                    try:
                        cf = float(conf) if conf is not None and str(conf).strip() != "" else 0.0
                    except (TypeError, ValueError):
                        cf = 0.0
                    if cf <= 0.0:
                        best = ""
                    use = bool(best) and cf >= AUTO_INCLUDE_CONFIDENCE_THRESHOLD
                    rpath = best
                if not rpath and not reason_raw:
                    reason_raw = "Receipt missing."
                if approved_override_mismatch:
                    llm_file = Path(best).name if best else "(unknown)"
                    approved_file = Path(rpath).name if rpath else "(none)"
                    reason_raw = (
                        f"Approved file override is active: showing {approved_file}. "
                        f"LLM reason below refers to {llm_file}. "
                        f"{reason_raw}"
                    ).strip()
                self._assign_row_paths[lid] = rpath
                self._assign_row_include[lid] = use
                self._assign_row_llm_reason_raw[lid] = reason_raw
                inc = self._include_cell_text(use)
                note_full = self._compose_llm_note_with_match_name(rpath, reason_raw)
                et = self._expense_type_cell_for_line(line, vendor_cache=vcache)
                receipt_missing_mark = "\u2713" if not rpath else ""
                vals = (
                    inc,
                    lid,
                    str(line.get("merchant_name", "") or "")[:80],
                    format_date_for_ui(str(line.get("transaction_date", "") or ""))[:24],
                    str(line.get("amount", "") or "")[:16],
                    str(line.get("currency", "") or "")[:8],
                    et[:80] if et != "—" else "—",
                    Path(rpath).name if rpath else "—",
                    receipt_missing_mark,
                    str(conf)[:8] if conf is not None else "",
                    note_full[:120],
                )
            needs_attention = False
            row_tags: tuple[str, ...] = ()
            if not scraping:
                try:
                    cf_row = float(conf) if conf is not None and str(conf).strip() != "" else None
                except (TypeError, ValueError):
                    cf_row = None
                if cf_row is not None and cf_row < REVIEW_CONFIDENCE_THRESHOLD:
                    row_tags = ("low_confidence",)
                no_receipt = not bool(self._assign_row_paths.get(lid, "").strip())
                needs_attention = no_receipt or (cf_row is not None and cf_row < REVIEW_CONFIDENCE_THRESHOLD)
                if needs_attention:
                    attention_total += 1
                if self._expense_report_attention_only and not needs_attention:
                    continue
            tree.insert("", tk.END, iid=lid, values=vals, tags=row_tags)
            shown_count += 1

        ch0 = tree.get_children()
        if ch0 and not scraping:
            tree.selection_set(ch0[0])
            tree.focus(ch0[0])
            self._assignments_show_preview_for_line_id(ch0[0])
            self._matching_workspace_update_for_line(ch0[0])
        elif not ch0:
            self._matching_workspace_update_for_line("")
        flabel = getattr(self, "_expense_report_filter_label", None)
        if flabel is not None:
            if scraping:
                flabel.configure(text="Filter: disabled while scraping")
            elif self._expense_report_attention_only:
                flabel.configure(text=f"Filter: attention only ({shown_count}/{len(lines)} shown)")
            else:
                flabel.configure(text=f"Attention items: {attention_total} of {len(lines)}")
        self._expense_report_sync_remove_all_button()

    def _populate_matching_report_combo(self) -> None:
        """Refresh the report dropdown with current report groups."""
        reports = load_expense_report_groups(APP_DIR)
        id_map: dict[str, str | None] = {"All (no filter)": None}
        display_list = ["All (no filter)"]
        for rid, data in sorted(reports.items(), key=lambda kv: str(kv[1].get("name", ""))):
            name = str(data.get("name", "")).strip() or "Untitled"
            count = len(data.get("line_ids", []))
            label = f"{name} ({count} items)"
            display_list.append(label)
            id_map[label] = rid
        self._matching_report_id_map = id_map
        combo = getattr(self, "_matching_report_combo", None)
        if combo is not None:
            combo["values"] = display_list
            current = self._matching_report_var.get()
            if current not in id_map:
                self._matching_report_var.set("All (no filter)")

    def _get_selected_report_line_ids(self) -> set[str] | None:
        """Return the set of line_ids for the selected report, or None if 'All'."""
        label = self._matching_report_var.get()
        rid = self._matching_report_id_map.get(label)
        if rid is None:
            return None
        reports = load_expense_report_groups(APP_DIR)
        report = reports.get(rid)
        if not report:
            return set()
        return {str(lid).strip() for lid in report.get("line_ids", []) if str(lid).strip()}

    def _on_matching_report_selected(self) -> None:
        """Handle report dropdown selection change — refreshes all filtered views."""
        self._receipt_table_ma_cache = None
        self.refresh_expense_report_tab()
        self.refresh_oracle_transactions_view()
        self.refresh_receipt_table()
        self.refresh_final_review_view()
        self.refresh_workflow_dashboard()
        self.refresh_submit_reports_table()
        self._refresh_report_header_status()

    def on_create_new_report(self) -> None:
        """Create a new empty report group via dialog."""
        import uuid as _uuid
        from tkinter import simpledialog

        name = simpledialog.askstring("New Report", "Report name:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        rid = str(_uuid.uuid4())[:8]
        reports = load_expense_report_groups(APP_DIR)
        reports[rid] = {
            "name": name,
            "created_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "line_ids": [],
        }
        save_expense_report_groups(APP_DIR, reports)
        self._populate_matching_report_combo()
        for label, r_id in self._matching_report_id_map.items():
            if r_id == rid:
                self._matching_report_var.set(label)
                break
        self._on_matching_report_selected()
        self.set_status(f'Created new report "{name}".')

    def _refresh_report_header_status(self) -> None:
        """Update the status indicator dots in the report header bar."""
        dots = getattr(self, "_report_header_status_dots", {})
        if not dots:
            return

        report_line_ids = self._get_selected_report_line_ids()
        lines, _ = load_expense_lines_cache(APP_DIR)
        matches = load_receipt_line_matches(APP_DIR)

        if report_line_ids is not None:
            lines = [l for l in lines if str(l.get("line_id", "") or "").strip() in report_line_ids]

        n_lines = len(lines)

        trans_status = "complete" if n_lines > 0 else "pending"

        n_with_file = 0
        n_matched = 0
        for line in lines:
            lid = str(line.get("line_id", "") or "").strip()
            m = matches.get(lid) or {}
            best = str(m.get("best_receipt") or "").strip()
            if best and Path(best).expanduser().is_file():
                n_with_file += 1
            if best:
                n_matched += 1

        if n_lines == 0:
            docs_status = "pending"
        elif n_with_file == n_lines:
            docs_status = "complete"
        elif n_with_file > 0:
            docs_status = "partial"
        else:
            docs_status = "pending"

        if n_lines == 0:
            match_status = "pending"
        elif n_matched == n_lines:
            match_status = "complete"
        elif n_matched > 0:
            match_status = "partial"
        else:
            match_status = "pending"

        rid = self._matching_report_id_map.get(self._matching_report_var.get())
        if rid:
            sub = get_report_submission_status(APP_DIR, rid)
            if sub == "Submitted":
                submit_status = "complete"
            elif sub == "Partial":
                submit_status = "partial"
            else:
                submit_status = "pending"
        else:
            submit_status = "pending"

        status_map = {
            "docs": docs_status,
            "trans": trans_status,
            "match": match_status,
            "submit": submit_status,
        }
        for key, dot_label in dots.items():
            status = status_map.get(key, "pending")
            if status == "complete":
                dot_label.configure(text="✓", foreground="#34a853")
            elif status == "partial":
                dot_label.configure(text="◐", foreground="#f9ab00")
            else:
                dot_label.configure(text="○", foreground="#bbb")

    def _on_matching_remove_from_report(self) -> None:
        """Remove selected line(s) from the current report, moving them back to unassigned."""
        sel = list(self.expense_report_tree.selection())
        if not sel:
            self.set_status("Remove from report: select one or more rows first.")
            return
        label = self._matching_report_var.get()
        rid = self._matching_report_id_map.get(label)
        if rid is None:
            self.set_status("Remove from report: select a specific report first (not 'All').")
            return
        line_ids = [str(x).strip() for x in sel if str(x).strip()]
        if not line_ids:
            return
        reports = load_expense_report_groups(APP_DIR)
        report = reports.get(rid)
        if not report:
            self.set_status("Remove from report: report not found.")
            return
        ids_set = set(line_ids)
        existing = report.get("line_ids", [])
        report["line_ids"] = [x for x in existing if str(x).strip() not in ids_set]
        save_expense_report_groups(APP_DIR, reports)
        self.refresh_all_tabs()
        self._update_activity_recommendation_hint()
        self._refresh_workflow_checklist()
        self.set_status(f"Removed {len(line_ids)} item(s) from report back to unassigned.")

    def refresh_expense_report_tab(self, *, progress_lines: list[dict] | None = None) -> None:
        if not hasattr(self, "expense_report_tree"):
            return
        self._populate_matching_report_combo()
        path = expense_lines_cache_path(APP_DIR)
        self._expense_report_raw_path.configure(text=str(path))
        if progress_lines is not None:
            self._refill_expense_report_tree_rows(progress_lines, scraping=True)
            self._expense_report_summary.configure(
                text=(
                    f"Scraping in progress — {len(progress_lines)} line(s) in the table so far. "
                    "The cache file is updated when scraping finishes."
                )
            )
            self._expense_report_preview_blank_message("Select a row")
            return

        lines, meta = load_expense_lines_cache(APP_DIR)
        if not lines:
            for item in self.expense_report_tree.get_children():
                self.expense_report_tree.delete(item)
            self._assign_row_paths.clear()
            self._assign_row_include.clear()
            self._assign_row_llm_reason_raw.clear()
            self._expense_report_summary.configure(
                text=(
                    "No scraped lines yet. With VPN on, use \u201cLaunch browser & scrape expenses\u201d above, "
                    "or Open Oracle on the Activity tab then Scrape Step 2."
                )
            )
            self._expense_report_preview_blank_message("Select a row")
            self._expense_report_sync_remove_all_button()
            return

        report_filter = self._get_selected_report_line_ids()
        if report_filter is not None:
            lines = [l for l in lines if str(l.get("line_id", "") or "").strip() in report_filter]

        updated = str(meta.get("updated_at", "") or "")
        src = str(meta.get("source", "") or "")
        em = "\u2014"
        self._expense_report_summary.configure(
            text=f"{len(lines)} line(s) \u00b7 updated {updated or em} \u00b7 source {src or em}"
        )
        self._refill_expense_report_tree_rows(lines, scraping=False)

    def refresh_expense_types_tab(self) -> None:
        if hasattr(self, "_expense_types_refill_tree"):
            self._expense_types_refill_tree()
        if hasattr(self, "_expense_types_llm_label"):
            try:
                doc = load_document(llm_pending_file(APP_DIR))
                pend = pending_expense_type_ids(doc)
                nq = len(doc.get("queries") or {})
                nr = len(doc.get("responses") or {})
                path = llm_pending_file(APP_DIR)
                self._expense_types_llm_label.configure(
                    text=(
                        f"LLM expense-type queue: {path.name} — {nq} query record(s), "
                        f"{len(pend)} pending resolution, {nr} response block(s). "
                        f"VPN off → Activity → Resolve types when pending > 0."
                    )
                )
            except Exception:
                self._expense_types_llm_label.configure(text="")

    def _gather_expense_type_tab_rows(self) -> list[dict]:
        """Union of merchants from vendor cache, scraped expense lines, and Step 3 LLM queue."""
        doc = load_document(llm_pending_file(APP_DIR))
        queries = doc.get("queries") or {}
        by_vendor: dict[str, dict] = {}

        def ensure(vk: str) -> dict:
            if vk not in by_vendor:
                by_vendor[vk] = {"options": [], "qids": []}
            return by_vendor[vk]

        if isinstance(queries, dict):
            for qid, q in queries.items():
                if not isinstance(q, dict) or str(q.get("kind")) != "expense_type":
                    continue
                pl = q.get("payload")
                if not isinstance(pl, dict):
                    continue
                m = str(pl.get("merchant_name", "")).strip()
                opts = pl.get("options")
                opt_list: list[str] = []
                if isinstance(opts, list):
                    opt_list = [str(o).strip() for o in opts if str(o).strip()]
                vk = _normalize_vendor_key(m)
                if not vk:
                    continue
                row = ensure(vk)
                seen = set(row["options"])
                for o in opt_list:
                    if o not in seen:
                        row["options"].append(o)
                        seen.add(o)
                row["qids"].append(str(qid))

        try:
            lines, _ = load_expense_lines_cache(APP_DIR)
            for line in lines:
                m = str(line.get("merchant_name", "") or "").strip()
                vk = _normalize_vendor_key(m)
                if vk:
                    ensure(vk)
        except Exception:
            pass

        for vk in self.vendor_expense_cache:
            ensure(vk)

        rows_out: list[dict] = []
        for vk in sorted(by_vendor.keys()):
            row = by_vendor[vk]
            cached = self.vendor_expense_cache.get(vk, "").strip()
            resolved = ""
            if not cached:
                for qid in row["qids"]:
                    r = response_expense_type(doc, qid)
                    if r:
                        resolved = r.strip()
                        break
            effective = cached or resolved
            if cached or resolved:
                display = cached or resolved
            elif row["qids"]:
                display = "(pending LLM)"
            else:
                display = "—"
            rows_out.append(
                {
                    "vendor_key": vk,
                    "display_type": display,
                    "effective_type": effective,
                    "options": list(row["options"]),
                    "qids": list(row["qids"]),
                }
            )
        return rows_out

    def _refill_expense_types_tree(self) -> None:
        if not hasattr(self, "expense_types_tree"):
            return
        self.vendor_expense_cache = self._load_vendor_expense_cache()
        rows = self._gather_expense_type_tab_rows()

        search_term = ""
        if hasattr(self, "_expense_types_search_var"):
            search_term = self._expense_types_search_var.get().strip().lower()
        if search_term:
            rows = [
                r for r in rows
                if search_term in r["vendor_key"].lower()
                or search_term in r["display_type"].lower()
            ]

        sort_col = getattr(self, "_expense_types_sort_col", "vendor_key")
        sort_asc = getattr(self, "_expense_types_sort_asc", True)
        sort_key = "vendor_key" if sort_col == "vendor_key" else "display_type"
        rows.sort(key=lambda r: r[sort_key].lower(), reverse=not sort_asc)

        tree = self.expense_types_tree
        for item in tree.get_children():
            tree.delete(item)
        self._expense_types_tree_iid_meta = {}
        for i, row in enumerate(rows):
            iid = str(i)
            self._expense_types_tree_iid_meta[iid] = row
            tree.insert(
                "",
                tk.END,
                iid=iid,
                values=(row["vendor_key"], row["display_type"]),
            )

    def _expense_types_sync_form_from_meta(self, iid: str) -> None:
        if not hasattr(self, "_expense_types_type_combo"):
            return
        meta = self._expense_types_tree_iid_meta.get(iid)
        if not meta:
            return
        self._expense_types_vendor_var.set(meta["vendor_key"])
        eff = str(meta.get("effective_type") or "").strip()
        opts = [str(o) for o in (meta.get("options") or []) if str(o).strip()]
        combo = self._expense_types_type_combo
        if opts:
            vals = list(opts)
            if eff and eff not in vals:
                vals.insert(0, eff)
            combo.configure(values=vals)
            in_opts = eff in opts
            combo.configure(state=("readonly" if in_opts or not eff else "normal"))
            combo.set(eff or "")
        else:
            vals = list(get_expense_type_options())
            if eff and eff not in vals:
                vals = [eff] + vals
            combo.configure(values=vals, state="normal")
            combo.set(eff or "")

    def _merchant_label_for_vendor_key(self, vendor_key: str) -> str:
        """Human-readable merchant name for LLM prompts (from scraped lines if possible)."""
        vk = _normalize_vendor_key(vendor_key)
        if not vk:
            return vendor_key
        try:
            lines, _ = load_expense_lines_cache(APP_DIR)
            for line in lines:
                if not isinstance(line, dict):
                    continue
                m = str(line.get("merchant_name", "") or "").strip()
                if m and _normalize_vendor_key(m) == vk:
                    return m
        except Exception:
            pass
        return vendor_key

    def scan_new_vendors_expense_types(self) -> None:
        """
        For each vendor in the Expense types table with no mapped type, call the LLM to pick
        one of PORTAL_EXPENSE_TYPE_OPTIONS and save to vendor_expense_types.json (report fill uses this cache).
        """
        if self._expense_types_scan_worker_active:
            self.set_status("Scan new vendors is already running.")
            return
        api_key = self.get_openai_key()
        if not api_key:
            messagebox.showwarning(
                "OpenAI API key",
                "Add an API key in Settings (or OPENAI_API_KEY) to scan vendors.",
                parent=self.root,
            )
            return
        rows = self._gather_expense_type_tab_rows()
        targets = [r for r in rows if not str(r.get("effective_type") or "").strip()]
        if not targets:
            self.set_status("No vendors without an expense type — nothing to scan.")
            return
        opts = list(get_expense_type_options())
        total = len(targets)
        self._expense_types_scan_worker_active = True
        self.set_status(f"Scanning {total} vendor(s) with LLM…")
        self.log_event("llm", f"Scan new vendors: {total} merchant(s) → portal expense type list.")

        def worker() -> None:
            completed_ok = True
            try:
                for idx, row in enumerate(targets, start=1):
                    vk = str(row.get("vendor_key") or "").strip()
                    if not vk:
                        continue
                    merchant = self._merchant_label_for_vendor_key(vk)
                    short = merchant if len(merchant) <= 64 else merchant[:61] + "..."

                    def log_start(i=idx, n=total, s=short):
                        self.log_event("llm", f"Scan vendors: {i}/{n} — {s}")

                    self.root.after(0, log_start)
                    try:
                        chosen = self._choose_expense_type_with_llm(
                            api_key=api_key,
                            merchant_name=merchant,
                            options=opts,
                        )
                    except Exception as exc:
                        completed_ok = False

                        def on_err(e=exc):
                            self.set_status(f"Scan new vendors stopped: {e}")
                            self.log_event("err", f"Scan new vendors failed: {e}")

                        self.root.after(0, on_err)
                        break

                    def apply_choice(k=vk, c=chosen, i=idx, n=total):
                        self.vendor_expense_cache[k] = c
                        self._persist_vendor_expense_cache()
                        self.refresh_expense_types_tab()
                        self.set_status(f"Scan vendors: saved {i}/{n} — {k} → {c}")

                    self.root.after(0, apply_choice)
            finally:

                def end(success: bool = completed_ok):
                    self._expense_types_scan_worker_active = False
                    if success:
                        self.set_status("Scan new vendors finished.")
                        self.log_event("llm", "Scan new vendors: complete.")

                self.root.after(0, end)

        threading.Thread(target=worker, daemon=True).start()

    def _expense_types_commit_mapping(
        self,
        prior_vendor_key: str | None,
        vendor_key: str,
        expense_type: str,
        *,
        portal_options: list[str] | None = None,
    ) -> tuple[bool, str]:
        type_clean = expense_type.strip()
        if not type_clean:
            return False, "Select or enter an expense type (must match the portal dropdown when options are listed)."
        if not vendor_key:
            return False, "Enter a vendor or merchant name."

        opts: list[str] = list(portal_options) if portal_options else []
        if not opts:
            rows = self._gather_expense_type_tab_rows()
            for r in rows:
                if r["vendor_key"] == vendor_key:
                    opts = list(r["options"])
                    break
        if opts:
            matched = _match_label_to_options(type_clean, opts)
            if not matched:
                return (
                    False,
                    "Expense type must be one of the portal options for this merchant. "
                    "Pick from the dropdown or type an exact option label.",
                )
            type_clean = matched

        if prior_vendor_key and prior_vendor_key != vendor_key:
            self.vendor_expense_cache.pop(prior_vendor_key, None)
        self.vendor_expense_cache[vendor_key] = type_clean
        self._persist_vendor_expense_cache()
        try:
            persist_expense_line_derived_fields(
                APP_DIR,
                load_receipt_line_matches(APP_DIR),
                self._load_vendor_expense_cache(),
            )
        except Exception:
            pass

        path = llm_pending_file(APP_DIR)
        doc = load_document(path)
        qmap = doc.get("queries") or {}
        changed = False
        if isinstance(qmap, dict):
            for qid, q in qmap.items():
                if not isinstance(q, dict) or str(q.get("kind")) != "expense_type":
                    continue
                pl = q.get("payload")
                if not isinstance(pl, dict):
                    continue
                m = str(pl.get("merchant_name", "")).strip()
                if _normalize_vendor_key(m) != vendor_key:
                    continue
                set_response_expense_type(doc, str(qid), type_clean)
                changed = True
        if changed:
            save_document(path, doc)

        self.set_status(f'Vendor expense cache: saved "{vendor_key}" -> "{type_clean}".')
        self.refresh_all_tabs()
        for iid, meta in self._expense_types_tree_iid_meta.items():
            if meta["vendor_key"] == vendor_key:
                self.expense_types_tree.selection_set(iid)
                self.expense_types_tree.see(iid)
                self._expense_types_sync_form_from_meta(iid)
                break
        return True, ""

    def refresh_all_tabs(self) -> None:
        self.refresh_receipt_table()
        self.refresh_expense_report_tab()
        self.refresh_expense_types_tab()
        self.refresh_workflow_views()

    def _update_activity_recommendation_hint(self) -> None:
        if not hasattr(self, "_activity_hint"):
            return
        lines_ok, _ = validate_lines_cache_for_match(APP_DIR)
        n_lines = len(load_expense_lines_cache(APP_DIR)[0]) if lines_ok else 0
        has_files = bool(self.receipt_paths)
        parsed = bool(self.analyses)
        has_match = bool(load_receipt_line_matches(APP_DIR))
        try:
            doc = load_document(llm_pending_file(APP_DIR))
            n_pend = len(pending_expense_type_ids(doc))
        except Exception:
            n_pend = 0
        parts: list[str] = []
        if not lines_ok or n_lines == 0:
            parts.append("Scrape expense lines (VPN on).")
        if not has_files:
            parts.append("Add receipt files on the Documents tab (VPN off).")
        elif not parsed:
            parts.append("Run LLM parse on receipts (Documents tab: Add files or Rescan).")
        if n_pend > 0:
            parts.append("Resolve expense types (VPN off).")
        if lines_ok and has_files and parsed and not has_match:
            parts.append("Run Match lines (VPN off).")
        if has_match:
            parts.append("Review Expense report tab, then Create report (VPN on).")
        if not parts:
            self._activity_hint.configure(text="All major steps look satisfied — use tabs to adjust details.")
        else:
            self._activity_hint.configure(text="Suggested next: " + " ".join(parts))

    @staticmethod
    def _receipt_preview_apply_cw_quarter_turns(im: object, quarters: int) -> object:
        if Image is None or quarters % 4 == 0:
            return im
        try:
            trans = Image.Transpose
        except AttributeError:
            return im
        q = quarters % 4
        op = {
            1: trans.ROTATE_270,
            2: trans.ROTATE_180,
            3: trans.ROTATE_90,
        }
        return im.transpose(op[q])  # type: ignore[union-attr, no-any-return]

    @staticmethod
    def _preview_downscale_source_for_canvas(im: object) -> object:
        if Image is None:
            return im
        w, h = im.size  # type: ignore[union-attr]
        cap = _PREVIEW_SOURCE_MAX_EDGE
        m = max(int(w), int(h))
        if m <= cap:
            return im
        s = cap / float(m)
        nw, nh = max(1, int(float(w) * s)), max(1, int(float(h) * s))
        rs = getattr(getattr(Image, "Resampling", Image), "BILINEAR", Image.BILINEAR)
        return im.resize((nw, nh), rs)  # type: ignore[union-attr, no-any-return]

    def _analysis_for_receipt_path(self, path_str: str) -> dict:
        key = str(path_str).strip()
        if not key:
            return {}
        for row in self.analyses:
            if str(row.get("source_file", "") or "").strip() == key:
                return row if isinstance(row, dict) else {}
        for row in load_analyses_snapshot(APP_DIR):
            if isinstance(row, dict) and str(row.get("source_file", "") or "").strip() == key:
                return row
        return {}

    def _parse_llm_rotation_quarters(self, block: dict) -> int:
        if not block:
            return 0
        q = block.get("display_rotation_quarter_turns")
        if q is not None and str(q).strip() != "":
            try:
                return int(float(q)) % 4
            except (TypeError, ValueError):
                pass
        deg = block.get("image_rotation_degrees")
        if deg is not None and str(deg).strip() != "":
            try:
                return (int(round(float(deg) / 90.0)) % 4 + 4) % 4
            except (TypeError, ValueError):
                pass
        return 0

    def _preview_working_pil(self) -> object | None:
        if self._preview_raw_exif_image is None or Image is None:
            return None
        q = (self._preview_llm_quarter_turns + self._preview_session_quarter_turns) % 4
        return self._receipt_preview_apply_cw_quarter_turns(self._preview_raw_exif_image, q)

    def _preview_canvas(self) -> tk.Canvas | None:
        if self._preview_target_canvas is not None:
            return self._preview_target_canvas
        return getattr(self, "_assignments_preview_canvas", None)

    def _preview_cancel_redraw_timers(self) -> None:
        for attr in ("_preview_hq_timer", "_preview_wheel_coalesce_timer"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.root.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, attr, None)

    def _preview_cancel_hq_timer_only(self) -> None:
        if self._preview_hq_timer is not None:
            try:
                self.root.after_cancel(self._preview_hq_timer)
            except (tk.TclError, ValueError):
                pass
            self._preview_hq_timer = None

    def _preview_schedule_hq_redraw(self) -> None:
        self._preview_cancel_hq_timer_only()

        def hq() -> None:
            self._preview_hq_timer = None
            if self._preview_raw_exif_image is None:
                return
            self._preview_use_fast_resample = False
            self._expense_report_preview_redraw(force_hq=True)

        self._preview_hq_timer = self.root.after(_PREVIEW_HQ_DEBOUNCE_MS, hq)

    def _preview_request_wheel_redraw(self) -> None:
        if self._preview_wheel_coalesce_timer is not None:
            try:
                self.root.after_cancel(self._preview_wheel_coalesce_timer)
            except (tk.TclError, ValueError):
                pass

        def flush() -> None:
            self._preview_wheel_coalesce_timer = None
            if self._preview_raw_exif_image is not None:
                self._expense_report_preview_redraw()

        self._preview_wheel_coalesce_timer = self.root.after(_PREVIEW_WHEEL_COALESCE_MS, flush)

    def _receipt_preview_show_blank(self, msg: str) -> None:
        self._preview_cancel_redraw_timers()
        self._preview_use_fast_resample = False
        self._preview_raw_exif_image = None
        self._preview_display_path = ""
        self._preview_llm_quarter_turns = 0
        self._preview_session_quarter_turns = 0
        self._preview_user_zoom = 1.0
        self._preview_pan_x = 0.0
        self._preview_pan_y = 0.0
        self._preview_drag_last = None
        self._preview_photo = None
        c = self._preview_canvas()
        if c is None:
            return
        c.delete("all")
        w = max(int(c.winfo_width()), 280)
        h = max(int(c.winfo_height()), 260)
        if w <= 1 or h <= 1:
            w, h = 300, 360
        c.create_text(
            w // 2,
            h // 2,
            text=msg,
            fill="#b0b0b0",
            width=max(w - 24, 40),
            justify=tk.CENTER,
        )

    def _expense_report_preview_blank_message(self, msg: str) -> None:
        self._preview_target_canvas = getattr(self, "_assignments_preview_canvas", None)
        self._receipt_preview_show_blank(msg)
        self._expense_report_set_selected_note_detail("")

    def _register_receipt_preview_canvas(self, canvas: tk.Canvas) -> None:
        canvas.bind("<Configure>", self._preview_on_canvas_configure)
        canvas.bind("<Enter>", self._preview_canvas_enter_focus)
        canvas.bind("<ButtonPress-1>", self._preview_on_button1_press)
        canvas.bind("<B1-Motion>", self._preview_on_b1_motion)
        canvas.bind("<ButtonRelease-1>", self._preview_on_button1_release)
        canvas.bind("<MouseWheel>", self._preview_on_mousewheel)
        canvas.bind("<Button-4>", self._preview_on_mousewheel)
        canvas.bind("<Button-5>", self._preview_on_mousewheel)
        try:
            canvas.bind("<Magnify>", self._preview_on_tk_magnify)
        except tk.TclError:
            pass
        if sys.platform == "darwin":
            for seq in ("<Command-MouseWheel>", "<Option-MouseWheel>", "<Mod4-MouseWheel>"):
                try:
                    canvas.bind(seq, self._preview_on_mousewheel_mac_zoom)
                except tk.TclError:
                    pass
            cid = id(canvas)
            if cid not in self._preview_pinch_canvas_ids:
                try:
                    from ui.macos_preview_pinch import attach_canvas_pinch_zoom

                    hooks = attach_canvas_pinch_zoom(canvas, self._preview_queue_pinch_native)
                    if hooks:
                        self._preview_macos_pinch_keepalive.extend(hooks)
                        self._preview_pinch_canvas_ids.add(cid)
                except Exception:
                    pass

    def _expense_report_preview_setup_interaction(self) -> None:
        c = getattr(self, "_assignments_preview_canvas", None)
        if c is None:
            return
        self._register_receipt_preview_canvas(c)

    def _preview_on_canvas_configure(self, event: tk.Event) -> None:
        if event.widget is not self._preview_target_canvas:
            return
        if self._preview_raw_exif_image is None:
            return
        self._preview_use_fast_resample = True
        self._expense_report_preview_redraw()
        self._preview_schedule_hq_redraw()

    def _preview_canvas_enter_focus(self, event: tk.Event) -> None:
        w = event.widget
        if hasattr(w, "focus_set"):
            try:
                w.focus_set()
            except tk.TclError:
                pass

    def _preview_on_button1_press(self, event: tk.Event) -> None:
        w = event.widget
        if hasattr(w, "focus_set"):
            w.focus_set()
        self._preview_cancel_hq_timer_only()
        self._preview_use_fast_resample = True
        self._preview_drag_last = (event.x, event.y)

    def _preview_on_b1_motion(self, event: tk.Event) -> None:
        if self._preview_drag_last is None or self._preview_raw_exif_image is None:
            return
        dx = event.x - self._preview_drag_last[0]
        dy = event.y - self._preview_drag_last[1]
        self._preview_drag_last = (event.x, event.y)
        scale = max(self._preview_last_scale, 1e-6)
        self._preview_pan_x -= dx / scale
        self._preview_pan_y -= dy / scale
        self._expense_report_preview_redraw()

    def _preview_on_button1_release(self, _event: tk.Event) -> None:
        self._preview_drag_last = None
        self._preview_schedule_hq_redraw()

    @staticmethod
    def _preview_wheel_delta_raw(event: tk.Event) -> float:
        d = float(getattr(event, "delta", 0) or 0)
        if getattr(event, "num", None) == 4:
            return 120.0
        if getattr(event, "num", None) == 5:
            return -120.0
        return d

    def _preview_wheel_apply(self, event: tk.Event, *, zoom_only: bool) -> str | None:
        if self._preview_raw_exif_image is None:
            return None
        delta = self._preview_wheel_delta_raw(event)
        if abs(delta) < 0.001:
            return None

        state = int(getattr(event, "state", 0) or 0)
        shift = bool(state & 0x0001)
        ctrl = bool(state & 0x0004)
        scale = max(self._preview_last_scale, 1e-6)
        d_pan = max(-400.0, min(400.0, delta))
        pan_step = (2.4 * d_pan) / max(scale, 0.06)

        self._preview_cancel_hq_timer_only()
        self._preview_use_fast_resample = True

        do_zoom = zoom_only or ctrl
        if do_zoom:
            d_zoom = max(-200.0, min(200.0, delta))
            raw_mul = 1.0 + (d_zoom / 900.0)
            mul = max(0.9, min(1.1, raw_mul))
            self._preview_user_zoom = max(0.16, min(16.0, self._preview_user_zoom * mul))
        elif shift:
            self._preview_pan_x -= pan_step
        else:
            self._preview_pan_y -= pan_step

        self._preview_request_wheel_redraw()
        self._preview_schedule_hq_redraw()
        return "break"

    def _preview_on_mousewheel(self, event: tk.Event) -> str | None:
        return self._preview_wheel_apply(event, zoom_only=False)

    def _preview_on_mousewheel_mac_zoom(self, event: tk.Event) -> str | None:
        return self._preview_wheel_apply(event, zoom_only=True)

    def _preview_queue_pinch_native(self, magnification: float) -> None:
        self.root.after(0, lambda m=magnification: self._preview_apply_pinch_magnification(m))

    def _preview_apply_pinch_magnification(self, m: float) -> None:
        if self._preview_raw_exif_image is None:
            return
        if abs(m) < 1e-9:
            return
        self._preview_cancel_hq_timer_only()
        self._preview_use_fast_resample = True
        # Cocoa: positive magnification = fingers apart → zoom in
        mul = max(0.92, min(1.08, 1.0 + m * 2.2))
        self._preview_user_zoom = max(0.16, min(16.0, self._preview_user_zoom * mul))
        self._preview_request_wheel_redraw()
        self._preview_schedule_hq_redraw()

    def _preview_on_tk_magnify(self, event: tk.Event) -> str | None:
        if self._preview_raw_exif_image is None:
            return None
        try:
            m = float(getattr(event, "delta", 0) or 0)
        except (TypeError, ValueError):
            return None
        self._preview_apply_pinch_magnification(m)
        return "break"

    def _center_preview_pan(self) -> None:
        working = self._preview_working_pil()
        if working is None or Image is None:
            return
        c = self._preview_canvas()
        if c is None:
            return
        iw, ih = working.size  # type: ignore[union-attr]
        cw = max(int(c.winfo_width()), 280)
        ch = max(int(c.winfo_height()), 260)
        fit = min(cw / max(iw, 1), ch / max(ih, 1))
        scale = max(fit * self._preview_user_zoom, 1e-6)
        vw = cw / scale
        vh = ch / scale
        self._preview_pan_x = max(0.0, (float(iw) - vw) / 2.0)
        self._preview_pan_y = max(0.0, (float(ih) - vh) / 2.0)

    def _expense_report_preview_redraw(self, *, force_hq: bool = False) -> None:
        c = self._preview_canvas()
        if c is None or Image is None or ImageTk is None:
            return
        working = self._preview_working_pil()
        if working is None:
            return
        if force_hq:
            self._preview_use_fast_resample = False
        use_fast = self._preview_use_fast_resample and not force_hq
        rs_mod = getattr(Image, "Resampling", Image)
        resample = (
            getattr(rs_mod, "BILINEAR", Image.BILINEAR)
            if use_fast
            else getattr(rs_mod, "LANCZOS", Image.LANCZOS)
        )

        c.delete("all")
        iw, ih = working.size  # type: ignore[union-attr]
        cw = max(int(c.winfo_width()), 280)
        ch = max(int(c.winfo_height()), 260)
        if cw <= 1 or ch <= 1:
            cw, ch = 300, 360
        fit = min(cw / max(iw, 1), ch / max(ih, 1))
        actual_scale = max(fit * self._preview_user_zoom, 1e-6)
        self._preview_last_scale = actual_scale
        vw = cw / actual_scale
        vh = ch / actual_scale
        px = max(0.0, min(float(self._preview_pan_x), max(0.0, float(iw) - vw)))
        py = max(0.0, min(float(self._preview_pan_y), max(0.0, float(ih) - vh)))
        self._preview_pan_x, self._preview_pan_y = px, py
        x1 = int(px)
        y1 = int(py)
        x2 = int(min(px + vw, float(iw)))
        y2 = int(min(py + vh, float(ih)))
        if x2 <= x1 or y2 <= y1:
            return
        try:
            crop = working.crop((x1, y1, x2, y2))  # type: ignore[union-attr]
            crop_r = crop.resize((cw, ch), resample)
            photo = ImageTk.PhotoImage(crop_r)
            self._preview_photo = photo
            c.create_image(0, 0, anchor=tk.NW, image=photo)
        except Exception:
            c.delete("all")
            c.create_text(cw // 2, ch // 2, text="Preview failed", fill="#e07070")

    def _expense_report_preview_zoom_in(self) -> None:
        if self._preview_raw_exif_image is None:
            return
        self._preview_cancel_hq_timer_only()
        self._preview_user_zoom = min(16.0, self._preview_user_zoom * 1.2)
        self._preview_use_fast_resample = False
        self._expense_report_preview_redraw(force_hq=True)

    def _expense_report_preview_zoom_out(self) -> None:
        if self._preview_raw_exif_image is None:
            return
        self._preview_cancel_hq_timer_only()
        self._preview_user_zoom = max(0.16, self._preview_user_zoom / 1.2)
        self._preview_use_fast_resample = False
        self._expense_report_preview_redraw(force_hq=True)

    def _expense_report_preview_reset_view(self) -> None:
        if self._preview_raw_exif_image is None:
            return
        self._preview_cancel_hq_timer_only()
        self._preview_user_zoom = 1.0
        self._center_preview_pan()
        self._preview_use_fast_resample = False
        self._expense_report_preview_redraw(force_hq=True)

    def _expense_report_preview_rotate_90_cw(self) -> None:
        if self._preview_raw_exif_image is None:
            return
        self._preview_cancel_hq_timer_only()
        self._preview_session_quarter_turns = (self._preview_session_quarter_turns + 1) % 4
        self._center_preview_pan()
        self._preview_use_fast_resample = False
        self._expense_report_preview_redraw(force_hq=True)

    def _treeview_primary_selection_iid(self, tree: ttk.Treeview) -> str | None:
        """Row that should drive preview and other single-row actions in extended selection mode.

        ``Treeview.selection()`` order follows tree order, not last click. The keyboard focus item
        (the row the user activated) is the correct anchor when it is still selected.
        """
        sel = tree.selection()
        if not sel:
            return None
        try:
            focus = tree.focus()
        except tk.TclError:
            focus = ""
        if focus and focus in sel:
            return str(focus)
        return str(sel[0])

    def _assignments_show_preview_for_line_id(self, lid: str) -> None:
        self._preview_target_canvas = getattr(self, "_assignments_preview_canvas", None)
        path_str = self._assign_row_paths.get(lid, "").strip()
        reason_raw = self._assign_row_llm_reason_raw.get(lid, "")
        self._expense_report_set_selected_note_detail(self._compose_llm_note_with_match_name(path_str, reason_raw))
        if not path_str:
            self._receipt_preview_show_blank("No file for this line")
            return
        self._show_receipt_preview_for_path(path_str)

    def _show_receipt_preview_for_path(self, path_str: str) -> None:
        p = Path(path_str).expanduser()
        if not p.is_file():
            self._receipt_preview_show_blank(f"Missing:\n{p.name}")
            return
        if Image is None or ImageTk is None:
            self._receipt_preview_show_blank("Install Pillow for image preview")
            return
        suf = p.suffix.lower()
        if suf == ".pdf":
            self._receipt_preview_show_blank(f"{p.name}\n(PDF — open file to view)")
            return
        try:
            im = Image.open(p)
            if ImageOps is not None:
                try:
                    im = ImageOps.exif_transpose(im)
                except Exception:
                    pass
            im = im.convert("RGB")
            im = self._preview_downscale_source_for_canvas(im)
        except Exception:
            self._receipt_preview_show_blank(f"{p.name}\n(preview failed)")
            return

        self._preview_raw_exif_image = im
        self._preview_display_path = str(p)
        self._preview_session_quarter_turns = 0
        block = self._analysis_for_receipt_path(str(p))
        self._preview_llm_quarter_turns = self._parse_llm_rotation_quarters(block)
        self._preview_user_zoom = 1.0
        self._preview_use_fast_resample = False
        self._center_preview_pan()
        self._expense_report_preview_redraw(force_hq=True)

    def _documents_on_receipt_select(self, _event: object | None = None) -> None:
        doc_c = getattr(self, "_documents_preview_canvas", None)
        if doc_c is None:
            return
        self._preview_target_canvas = doc_c
        primary = self._treeview_primary_selection_iid(self.table)
        if not primary:
            self._receipt_preview_show_blank("Select a row")
            return
        self._show_receipt_preview_for_path(primary)

    def _documents_is_current_tab(self) -> bool:
        if not hasattr(self, "_frame_documents") or not hasattr(self, "main_notebook"):
            return False
        try:
            return self.main_notebook.index(self.main_notebook.select()) == self.main_notebook.index(
                self._frame_documents
            )
        except tk.TclError:
            return False

    def _documents_update_preview_from_selection(self) -> None:
        if not hasattr(self, "_documents_preview_canvas"):
            return
        if not self._documents_is_current_tab():
            return
        self._preview_target_canvas = self._documents_preview_canvas
        if not self.receipt_paths:
            self._receipt_preview_show_blank("Add receipt files to preview")
            return
        primary = self._treeview_primary_selection_iid(self.table)
        if not primary:
            self._receipt_preview_show_blank("Select a row")
            return
        self._show_receipt_preview_for_path(primary)

    def _assignments_on_select(self) -> None:
        primary = self._treeview_primary_selection_iid(self.expense_report_tree)
        if primary:
            self._assignments_show_preview_for_line_id(primary)
            self._matching_workspace_update_for_line(primary)
        else:
            self._expense_report_preview_blank_message("Select a row")
            self._matching_workspace_update_for_line("")
        self._expense_report_sync_remove_all_button()

    def _matching_workspace_update_for_line(self, lid: str) -> None:
        line_var = getattr(self, "_matching_line_var", None)
        conf_var = getattr(self, "_matching_conf_var", None)
        receipt_var = getattr(self, "_matching_receipt_var", None)
        reason_widget = getattr(self, "_matching_reason_text", None)
        if line_var is None or conf_var is None or receipt_var is None or reason_widget is None:
            return
        if not lid:
            line_var.set("Line: —")
            conf_var.set("Confidence: —")
            receipt_var.set("Suggested receipt: —")
            reason_widget.configure(state=tk.NORMAL)
            reason_widget.delete("1.0", tk.END)
            reason_widget.insert("1.0", "Select a row to inspect match rationale.")
            reason_widget.configure(state=tk.DISABLED)
            return
        path_str = self._assign_row_paths.get(lid, "").strip()
        llm = load_receipt_line_matches(APP_DIR).get(lid) or {}
        raw_conf = llm.get("confidence", "")
        try:
            conf_f = float(raw_conf) if raw_conf is not None and str(raw_conf).strip() != "" else 0.0
            conf_txt = f"{conf_f:.2f}"
        except (TypeError, ValueError):
            conf_txt = str(raw_conf) if raw_conf is not None else "—"
        line_var.set(f"Line: {lid}")
        conf_var.set(f"Confidence: {conf_txt}")
        receipt_var.set(f"Suggested receipt: {Path(path_str).name if path_str else 'None'}")
        reason = self._assign_row_llm_reason_raw.get(lid, "").strip() or "No rationale available."
        reason_widget.configure(state=tk.NORMAL)
        reason_widget.delete("1.0", tk.END)
        reason_widget.insert("1.0", reason)
        reason_widget.configure(state=tk.DISABLED)

    def on_matching_accept_selected(self) -> None:
        tree = getattr(self, "expense_report_tree", None)
        if tree is None:
            return
        sel = list(tree.selection())
        if not sel:
            self.set_status("Accept selected: choose one or more lines first.")
            return
        n = 0
        for lid in sel:
            path = str(self._assign_row_paths.get(lid, "") or "").strip()
            if not path:
                continue
            self._assign_row_include[lid] = True
            vals = list(tree.item(lid, "values"))
            vals[0] = self._include_cell_text(True)
            tree.item(lid, values=vals)
            n += 1
        self.set_status(f"Accepted {n} selected match(es).")

    def on_matching_reject_selected(self) -> None:
        self._assignments_mark_receipt_missing_selected()

    def on_matching_manual_pick(self) -> None:
        self._assignments_pick_file()
        primary = self._treeview_primary_selection_iid(self.expense_report_tree)
        if primary:
            self._matching_workspace_update_for_line(primary)

    def on_matching_accept_all_high_confidence(self) -> None:
        self._assignments_approve_suggested()
        self.set_status("Accepted all high-confidence suggestions.")

    def _matching_workspace_on_keypress(self, event: tk.Event) -> str | None:
        k = (event.keysym or "").lower()
        tree = getattr(self, "expense_report_tree", None)
        if tree is None:
            return None
        rows = list(tree.get_children())
        primary = self._treeview_primary_selection_iid(tree)
        if k == "a":
            self.on_matching_accept_selected()
            return "break"
        if k == "r":
            self.on_matching_reject_selected()
            return "break"
        if k == "m":
            self.on_matching_manual_pick()
            return "break"
        if k in {"n", "p"} and rows:
            if primary in rows:
                idx = rows.index(primary)
            else:
                idx = 0
            idx = min(len(rows) - 1, idx + 1) if k == "n" else max(0, idx - 1)
            target = rows[idx]
            tree.selection_set(target)
            tree.focus(target)
            tree.see(target)
            self._assignments_show_preview_for_line_id(target)
            self._matching_workspace_update_for_line(target)
            return "break"
        return None

    def _expense_report_sync_remove_all_button(self) -> None:
        if not hasattr(self, "_expense_report_remove_all_btn"):
            return
        btn = self._expense_report_remove_all_btn
        if not hasattr(self, "expense_report_tree"):
            btn.configure(text="Remove all lines")
            return
        if self.expense_report_tree.selection():
            btn.configure(text="Clear selection")
        else:
            btn.configure(text="Remove all lines")

    def _expense_report_select_all(self, event: tk.Event | None = None) -> str:
        tree = self.expense_report_tree
        items = tree.get_children("")
        if items:
            tree.selection_set(*items)
            tree.focus(items[0])
            self._assignments_show_preview_for_line_id(items[0])
        self._expense_report_sync_remove_all_button()
        return "break"

    def on_expense_report_remove_all_or_clear_selection(self) -> None:
        tree = self.expense_report_tree
        sel = tree.selection()
        if sel:
            tree.selection_remove(*sel)
            self._expense_report_preview_blank_message("Select a row")
            self._expense_report_sync_remove_all_button()
            return
        self.on_expense_report_remove_all()

    def _assignments_toggle_include(self, event: tk.Event) -> None:
        tree = self.expense_report_tree
        if tree.identify_column(event.x) != "#1":
            return
        row = tree.identify_row(event.y)
        if not row:
            return
        self._assign_row_include[row] = not self._assign_row_include.get(row, False)
        vals = list(tree.item(row, "values"))
        vals[0] = self._include_cell_text(self._assign_row_include[row])
        tree.item(row, values=vals)

    def _assignments_pick_file(self) -> None:
        tree = self.expense_report_tree
        sel = tree.selection()
        if not sel:
            self.set_status("Choose file: select a line first.")
            return
        if len(sel) > 1:
            self.set_status("Choose file: select only one row (Cmd/Ctrl-click to clear extras).")
            return
        lid = sel[0]
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Choose receipt file",
            filetypes=[
                ("Images / PDF", "*.jpg *.jpeg *.png *.heic *.tiff *.pdf"),
                ("All", "*.*"),
            ],
        )
        if not path:
            return
        self._assign_row_paths[lid] = path
        vals = list(tree.item(lid, "values"))
        vals[7] = Path(path).name
        vals[8] = "" if path else "\u2713"
        reason_raw = self._assign_row_llm_reason_raw.get(lid, "")
        vals[10] = self._compose_llm_note_with_match_name(path, reason_raw)[:120]
        tree.item(lid, values=vals)
        self._assignments_show_preview_for_line_id(lid)

    def _assignments_mark_receipt_missing_selected(self) -> None:
        tree = self.expense_report_tree
        sel = list(tree.selection())
        if not sel:
            self.set_status("Mark receipt missing: select one or more lines first.")
            return
        line_ids = [str(x).strip() for x in sel if str(x).strip()]
        if not line_ids:
            self.set_status("Mark receipt missing: select one or more lines first.")
            return

        matches = load_receipt_line_matches(APP_DIR)
        approved = load_approved_matches(APP_DIR)
        for lid in line_ids:
            matches[lid] = {
                "best_receipt": None,
                "confidence": 0.0,
                "reason": "Receipt missing (manually marked).",
            }
            approved.pop(lid, None)

        save_receipt_line_matches(APP_DIR, matches)
        save_approved_matches(APP_DIR, approved)
        self._invalidate_receipt_table_match_cache()
        try:
            persist_expense_line_derived_fields(
                APP_DIR,
                matches,
                self._load_vendor_expense_cache(),
            )
        except Exception:
            pass
        self.refresh_all_tabs()
        self.set_status(
            f"Marked {len(line_ids)} line(s) as receipt missing and cleared document links."
        )

    def _assignments_approve_suggested(self) -> None:
        tree = self.expense_report_tree
        llm_matches = load_receipt_line_matches(APP_DIR)
        for lid in tree.get_children():
            block = llm_matches.get(lid) or {}
            best = str(block.get("best_receipt") or "").strip()
            try:
                cf = float(block.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                cf = 0.0
            if best and cf >= AUTO_INCLUDE_CONFIDENCE_THRESHOLD:
                self._assign_row_include[lid] = True
                vals = list(tree.item(lid, "values"))
                vals[0] = self._include_cell_text(True)
                tree.item(lid, values=vals)

    def _checked_rows_with_existing_receipt_file_count(self) -> int:
        """Count checked rows whose receipt path currently exists on disk."""
        tree = self.expense_report_tree
        n = 0
        for lid in tree.get_children():
            if not self._assign_row_include.get(lid, False):
                continue
            p = (self._assign_row_paths.get(lid) or "").strip()
            ok = bool(p) and Path(p).expanduser().is_file()
            if ok:
                n += 1
        return n

    def on_create_report(self) -> None:
        """
        Use checked Include rows that have real receipt files, save approvals, then run the full
        portal automation aligned to Oracle wizard naming:
        open browser, login, navigate to Expenses Home, then Create Expense Report and continue
        through Step 4.1 to Step 4.6 (attachments). Opens Chromium and runs saved login when no
        controlled browser is connected.
        """
        if not hasattr(self, "expense_report_tree"):
            return
        n = self._checked_rows_with_existing_receipt_file_count()
        if n == 0:
            self.set_status(
                "Create report: no checked rows have a receipt file on disk. "
                "Check the Include boxes, then run Analyze line items (VPN off) or right-click a row to choose a file."
            )
            return
        if not self._assignments_save(quiet_success=True):
            return
        if self._step3_automation_active:
            self.set_status("Create report blocked: stop the running automation first.")
            return
        if not self.receipt_paths and not load_analyses_snapshot(APP_DIR):
            self.set_status(
                "Create report blocked: import receipts (VPN off) or run matching once (analyses snapshot)."
            )
            return
        ok_m, err_m = validate_approved_for_attach(APP_DIR)
        if not ok_m:
            self.set_status(f"Create report blocked (approvals): {err_m}")
            return
        if not self._prepare_complete_report_llm_mode():
            return
        if not self._controlled_browser_usable():
            self.set_status(
                "Create report: opening Chromium — complete login or 2FA in the browser if prompted, "
                "then automation continues from the Navigator."
            )
            self.on_step_login()
        if not self.browser_page:
            self.set_status(
                "Create report blocked: Chromium not connected. "
                "Use Activity → Open Oracle (or Step 2), sign in, then try Create report again."
            )
            return
        self._run_step6_file_attach = True
        selected_label = self._matching_report_var.get()
        rid = self._matching_report_id_map.get(selected_label)
        if rid:
            reports = load_expense_report_groups(APP_DIR)
            rpt = reports.get(rid)
            self._submit_report_name = str((rpt or {}).get("name", "")).strip() or "Expense Report"
        else:
            self._submit_report_name = "Expense Report"
        self.set_status(
            "Create report (VPN on): Step 1 open browser → Step 2 login → Step 3 Expenses Home → "
            "Step 4.1 to 4.6 (Oracle wizard) …"
        )
        self._run_populate_expense_report_flow(start_from="nic_iexpenses")

    def _assignments_clear_all(self) -> None:
        tree = self.expense_report_tree
        for lid in tree.get_children():
            self._assign_row_include[lid] = False
            vals = list(tree.item(lid, "values"))
            vals[0] = self._include_cell_text(False)
            tree.item(lid, values=vals)

    def _assignments_save(self, *, quiet_success: bool = False) -> bool:
        tree = self.expense_report_tree
        out: dict[str, dict] = {}
        for lid in tree.get_children():
            if not self._assign_row_include.get(lid, False):
                continue
            p = (self._assign_row_paths.get(lid) or "").strip()
            if not p:
                continue
            if not Path(p).expanduser().is_file():
                self.set_status(f"Create report blocked: line {lid} file missing: {p}")
                return False
            out[lid] = {"source_file": p, "approved": True}
        save_approved_matches(APP_DIR, out)
        self._invalidate_receipt_table_match_cache()
        self.refresh_all_tabs()
        if not quiet_success:
            self.set_status(
                f"Saved {len(out)} approved attachment(s) to {approved_match_path(APP_DIR)}."
            )
        return True

    def _expense_report_context_menu(self, event: tk.Event) -> None:
        tree = self.expense_report_tree
        row = tree.identify_row(event.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(
            label="Choose receipt file for selected row…",
            command=self._assignments_pick_file,
        )
        m.add_command(
            label="Rescan selected for match",
            command=self.on_expense_report_match_selected_lines,
        )
        m.add_command(
            label="Mark receipt missing (clear matched document)",
            command=self._assignments_mark_receipt_missing_selected,
        )
        m.add_separator()
        m.add_command(
            label="Remove selected from report (back to unassigned)",
            command=self._on_matching_remove_from_report,
        )
        m.add_command(label="Delete selected lines from cache…", command=self.on_expense_report_remove_selected)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def on_expense_report_remove_selected(self) -> None:
        sel = list(self.expense_report_tree.selection())
        if not sel:
            self.set_status("Remove lines: select one or more rows first.")
            return
        self._apply_remove_expense_line_ids(set(sel))

    def on_expense_report_remove_all(self) -> None:
        ids = list(self.expense_report_tree.get_children())
        if not ids:
            self.set_status("Remove all: the report table is already empty.")
            return
        self._apply_remove_expense_line_ids(set(ids))

    def _apply_remove_expense_line_ids(self, line_ids: set[str]) -> None:
        removed, remaining = remove_expense_lines_by_ids(APP_DIR, line_ids)
        self._invalidate_receipt_table_match_cache()
        if self._scraped_expense_lines:
            drop = {str(x).strip() for x in line_ids if str(x).strip()}
            self._scraped_expense_lines = [
                row
                for row in self._scraped_expense_lines
                if str(row.get("line_id", "") or "").strip() not in drop
            ]
        self.refresh_all_tabs()
        self._update_activity_recommendation_hint()
        self._refresh_workflow_checklist()
        self.set_status(f"Removed {removed} line(s) from expense report cache; {remaining} remaining.")

    def _append_activity_log_line(self, text: str, tag: str | None = None) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {text}\n"
        self.activity_log.configure(state=tk.NORMAL)
        start = self.activity_log.index(tk.END)
        self.activity_log.insert(tk.END, line)
        end = self.activity_log.index(tk.END)
        if tag:
            self.activity_log.tag_add(tag, start, end)
        while True:
            end_idx = self.activity_log.index("end-1c")
            line_count = int(end_idx.split(".")[0])
            if line_count <= self._activity_log_max_lines:
                break
            self.activity_log.delete("1.0", "2.0")
        self.activity_log.see(tk.END)
        self.activity_log.configure(state=tk.DISABLED)

    def _debug_log(
        self,
        *,
        hypothesis_id: str,
        location: str,
        message: str,
        data: dict | None = None,
        run_id: str = "unified_path_probe",
    ) -> None:
        try:
            payload = {
                "sessionId": DEBUG_SESSION_ID,
                "id": f"log_{time.time_ns()}",
                "timestamp": int(time.time() * 1000),
                "location": location,
                "message": message,
                "data": data or {},
                "runId": run_id,
                "hypothesisId": hypothesis_id,
            }
            with DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=True) + "\n")
        except Exception:
            pass

    def _clear_activity_log(self) -> None:
        self.activity_log.configure(state=tk.NORMAL)
        self.activity_log.delete("1.0", tk.END)
        self.activity_log.configure(state=tk.DISABLED)

    def set_busy_status(self, text: str) -> None:
        """Update the bottom status line only (no activity log line). Safe from worker threads."""

        def _apply() -> None:
            if hasattr(self, "_status_bar"):
                self._status_bar.configure(text=text)
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass

        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            self.root.after(0, _apply)

    def set_status(self, text: str, *, log_tag: str | None = None) -> None:
        def _apply() -> None:
            self._append_activity_log_line(text, tag=log_tag)
            if hasattr(self, "_status_bar"):
                self._status_bar.configure(text=text)
            # Avoid root.update() here — it processes all Tk events and can re-enter handlers while
            # Playwright is mid-call; update_idletasks is enough to refresh the log widget.
            try:
                self.root.update_idletasks()
            except tk.TclError:
                pass

        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            self.root.after(0, _apply)

    def log_event(self, category: str, message: str) -> None:
        mapping: dict[str, tuple[str | None, str]] = {
            "llm": ("log_llm", "[LLM] "),
            "cache": ("log_cache", "[CACHE] "),
            "browser": ("log_browser", "[BROWSER] "),
            "net": ("log_net", "[NET] "),
            "step": ("log_step", "[STEP] "),
            "warn": ("log_warn", "[WARN] "),
            "err": ("log_err", "[ERR] "),
            "info": (None, ""),
        }
        tag, prefix = mapping.get(category, (None, ""))
        self.set_status(f"{prefix}{message}", log_tag=tag)
        self._emit_automation_event(
            kind=f"log.{category}",
            message=message,
            data={"category": category},
        )

    def _emit_automation_event(
        self, *, kind: str, message: str, data: dict | None = None, phase: str | None = None
    ) -> None:
        self._update_run_status_from_event(kind=kind, message=message, phase=phase, data=data)
        try:
            self._event_sink.emit(
                kind=kind,
                message=message,
                phase=phase,
                run_id=self._run_id,
                data=dict(data or {}),
            )
        except Exception:
            # Event telemetry must never break automation.
            pass

    @staticmethod
    def _phase_default_progress(phase: str) -> int:
        return {
            "DocumentIngestion": 10,
            "OracleScraping": 35,
            "Matching": 55,
            "Classification": 70,
            "UserReview": 85,
            "Submission": 95,
            "Completed": 100,
            "Recovery": 0,
        }.get(phase, 0)

    def _set_run_status(
        self,
        *,
        phase: str | None = None,
        progress: int | None = None,
        attention: str | None = None,
        message: str | None = None,
    ) -> None:
        def _apply() -> None:
            if phase is not None:
                self._run_status_phase = str(phase).strip() or self._run_status_phase
            if progress is not None:
                self._run_status_progress_pct = max(0, min(100, int(progress)))
            if attention is not None:
                self._run_status_attention = str(attention).strip() or "None"
            if message is not None:
                self._run_status_message = str(message).strip() or self._run_status_message
            if hasattr(self, "_run_status_phase_var"):
                self._run_status_phase_var.set(f"Phase: {self._run_status_phase}")
            if hasattr(self, "_run_status_progress_var"):
                self._run_status_progress_var.set(f"Progress: {self._run_status_progress_pct}%")
            if hasattr(self, "_run_status_attention_var"):
                self._run_status_attention_var.set(f"Attention: {self._run_status_attention}")
            if hasattr(self, "_run_status_message_var"):
                self._run_status_message_var.set(self._run_status_message)
            if hasattr(self, "_global_run_phase_var"):
                self._global_run_phase_var.set(f"Phase: {self._run_status_phase}")
            if hasattr(self, "_global_run_progress_var"):
                self._global_run_progress_var.set(f"Progress: {self._run_status_progress_pct}%")
            if hasattr(self, "_global_run_attention_var"):
                self._global_run_attention_var.set(f"Attention: {self._run_status_attention}")
            if hasattr(self, "_global_run_message_var"):
                self._global_run_message_var.set(self._run_status_message)
            bar = getattr(self, "_run_status_progress", None)
            if bar is not None:
                try:
                    bar.configure(value=self._run_status_progress_pct)
                except tk.TclError:
                    pass
            global_bar = getattr(self, "_global_run_progress", None)
            if global_bar is not None:
                try:
                    global_bar.configure(value=self._run_status_progress_pct)
                except tk.TclError:
                    pass

        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            self.root.after(0, _apply)

    def _update_run_status_from_event(
        self,
        *,
        kind: str,
        message: str,
        phase: str | None,
        data: dict | None,
    ) -> None:
        phase_text = phase or self._run_status_phase
        progress = self._run_status_progress_pct
        attention = self._run_status_attention
        kind_l = str(kind or "").lower()
        if phase:
            progress = max(progress, self._phase_default_progress(phase_text))
        if "retry" in kind_l:
            attention = "Automatic retry in progress"
        elif "failed" in kind_l or "recovery" in kind_l:
            attention = "Action needed"
        elif kind_l.endswith(".complete") or kind_l.endswith(".ok"):
            attention = "None"
            if phase_text == "Submission":
                progress = max(progress, 100)
        elif kind_l.endswith(".start"):
            attention = "None"

        if kind_l == "matching.start":
            message = "Matching transactions to receipts…"
        elif kind_l == "matching.complete":
            message = "Matching complete. Review exceptions before submit."
        elif kind_l == "scrape.page.retry":
            page_idx = int((data or {}).get("page_index") or 0)
            if page_idx > 0:
                message = f"Retrying page {page_idx} due to slow pagination."
        elif kind_l == "submission.recovery_needed":
            message = "Recovered from automation interruption. Resume guidance is available."

        self._set_run_status(
            phase=phase_text,
            progress=progress,
            attention=attention,
            message=message,
        )

    def _pump_ui_and_check_cancel(self) -> None:
        self.root.update_idletasks()
        self.root.update()
        if self._automation_cancel.is_set():
            raise AutomationCancelled()

    def _populate_key_index(self, key: str) -> int:
        try:
            return POPULATE_RESUME_KEYS.index(key)
        except ValueError:
            return 0

    def _refresh_workflow_checklist(self) -> None:
        """Header · / ✓ / ✗ from this session + cache files (types/match/approve use disk)."""
        if not self._workflow_chk_labels:
            return
        try:
            doc = load_document(llm_pending_file(APP_DIR))
            pend = pending_expense_type_ids(doc)
            has_q = bool(doc.get("queries"))
        except Exception:
            pend, has_q = [], False
        try:
            lines_cnt = len(load_expense_lines_cache(APP_DIR)[0])
        except Exception:
            lines_cnt = 0

        usable = self._controlled_browser_usable()
        if usable:
            browser_text, browser_fg = "✓", "#1a7f37"
        elif self._progress_browser_released_by_user:
            browser_text, browser_fg = "·", "#cccccc"
        elif self._progress_browser_had_live_link:
            browser_text, browser_fg = "✗", "#c62828"
        else:
            browser_text, browser_fg = "·", "#cccccc"

        step_ok = {
            "scrape": self._session_progress_scrape_done and lines_cnt > 0,
            "receipts": self._session_progress_receipts_done and bool(self.receipt_paths),
            "parsed": self._session_progress_parsed_done and len(self.analyses) > 0,
            "types": has_q and len(pend) == 0,
            "match": bool(load_receipt_line_matches(APP_DIR)),
            "approve": bool(load_approved_matches(APP_DIR)),
        }
        for key, lb in self._workflow_chk_labels.items():
            if key == "browser":
                text, fg = browser_text, browser_fg
            else:
                ok = step_ok.get(key, False)
                text, fg = ("✓", "#1a7f37") if ok else ("·", "#cccccc")
            lb.configure(text=text, foreground=fg)

    def _start_workflow_state_polling(self) -> None:
        """Detect browser disconnect (user closed window) so the header updates without other UI events."""
        self._workflow_poll_after_id = self.root.after(1500, self._workflow_poll_tick)

    def _workflow_poll_tick(self) -> None:
        self._workflow_poll_after_id = None
        try:
            now_ok = self._controlled_browser_usable()
        except tk.TclError:
            return
        prev = self._workflow_poll_prev_usable
        self._workflow_poll_prev_usable = now_ok
        if prev is not None and prev != now_ok:
            self._refresh_activity_panel()
        else:
            self._refresh_workflow_checklist()
        self._update_activity_recommendation_hint()
        try:
            self._workflow_poll_after_id = self.root.after(1500, self._workflow_poll_tick)
        except tk.TclError:
            pass

    def _refresh_activity_panel(self) -> None:
        for item in self.activity_table.get_children():
            self.activity_table.delete(item)

        step1_status: str
        if self._receipt_llm_worker_active:
            step1_status = "Running…"
            step1_tag = "run"
        elif self.receipt_paths:
            step1_status = "Done"
            step1_tag = "done"
        else:
            step1_status = "Pending"
            step1_tag = "pending"

        step2_status: str
        step2_tag: str
        if self._controlled_browser_usable():
            step2_status = "Done"
            step2_tag = "done"
        elif self._chromium_proc is not None and self._chromium_proc.poll() is None:
            step2_status = "Manual (reconnect Step 2)"
            step2_tag = "stopped"
        else:
            step2_status = "Pending"
            step2_tag = "pending"

        last_idx = self._populate_key_index(self._last_populate_step)
        stopped_key = self._activity_stopped_at_key
        running = self._step3_automation_active

        for key, label in ACTIVITY_UI_STEPS:
            if key == "step1":
                status, tag = step1_status, step1_tag
            elif key == "step2":
                status, tag = step2_status, step2_tag
            else:
                p_idx = self._populate_key_index(key)
                if running:
                    cur = self._populate_ui_current or self._last_populate_step
                    cur_idx = self._populate_key_index(cur)
                    if p_idx < cur_idx:
                        status, tag = "Done", "done"
                    elif p_idx == cur_idx:
                        status, tag = "Running…", "current"
                    else:
                        status, tag = "Pending", "pending"
                elif self._populate_flow_completed:
                    status, tag = "Done", "done"
                elif stopped_key:
                    si = self._populate_key_index(stopped_key)
                    if p_idx < si:
                        status, tag = "Done", "done"
                    elif p_idx == si:
                        status, tag = "Stopped here", "stopped"
                    else:
                        status, tag = "Pending", "pending"
                else:
                    if p_idx < last_idx:
                        status, tag = "Done", "done"
                    elif p_idx == last_idx:
                        status, tag = "Last checkpoint", "current"
                    else:
                        status, tag = "Pending", "pending"

            self.activity_table.insert("", tk.END, iid=key, values=(label, status), tags=(tag,))

        if self._step3_automation_active:
            self.stop_automation_btn.configure(state=tk.NORMAL)
        else:
            self.stop_automation_btn.configure(state=tk.DISABLED)

        stop_llm_btn = getattr(self, "stop_receipt_llm_btn", None)
        if stop_llm_btn is not None:
            if self._receipt_llm_worker_active:
                stop_llm_btn.configure(state=tk.NORMAL)
            else:
                stop_llm_btn.configure(state=tk.DISABLED)

        if self._step3_automation_active or self._controlled_browser_usable():
            self.stop_release_browser_btn.configure(state=tk.NORMAL)
            self.workflow_take_browser_btn.configure(state=tk.NORMAL)
        else:
            self.stop_release_browser_btn.configure(state=tk.DISABLED)
            self.workflow_take_browser_btn.configure(state=tk.DISABLED)

        attention_btn = getattr(self, "_run_status_attention_btn", None)
        if attention_btn is not None:
            try:
                has_attention_rows = bool(self._expense_report_attention_line_ids())
            except Exception:
                has_attention_rows = False
            attention_btn.configure(state=(tk.NORMAL if has_attention_rows else tk.DISABLED))

        if self._step3_automation_active:
            self._set_run_status(phase="Submission", progress=max(self._phase_default_progress("Submission"), 90))
        elif self._match_receipts_worker_active:
            self._set_run_status(phase="Matching", progress=self._phase_default_progress("Matching"))
        elif self._llm_resolve_worker_active or self._expense_types_scan_worker_active:
            self._set_run_status(
                phase="Classification",
                progress=self._phase_default_progress("Classification"),
            )
        elif self._receipt_llm_worker_active:
            self._set_run_status(
                phase="DocumentIngestion",
                progress=self._phase_default_progress("DocumentIngestion"),
            )
        elif self._populate_flow_completed:
            self._set_run_status(
                phase="Completed",
                progress=100,
                attention="None",
                message="Automation sequence finished. Review results and submit if ready.",
            )

        self._refresh_workflow_checklist()

    def on_stop_step3_automation(self) -> None:
        if self._step3_automation_active:
            self._pending_release_browser = False
            self._automation_cancel.set()
            self.set_status("Stop requested — will take effect after the current browser action finishes.")

    def on_stop_sequence_release_browser(self) -> None:
        if self._step3_automation_active:
            self._automation_cancel.set()
            self._pending_release_browser = True
            self.set_status(
                "Stop & take browser: canceling automation — Playwright disconnects after the current "
                "browser action. Chromium stays open for you to use manually."
            )
            return
        if self._controlled_browser_usable():
            self._pending_release_browser = False
            self._disconnect_playwright_keep_chrome()
            self.set_status(
                "You have the browser — automation disconnected. Use Chromium normally; "
                "click “Open Oracle” when you want automation attached again."
            )
            self._refresh_activity_panel()
            return
        self.set_status("No automation-linked browser to release — open Chromium with “Open Oracle” first.")

    def _disconnect_playwright_keep_chrome(self) -> None:
        """Close the Playwright CDP session; leave the Chromium process running."""
        self._pending_release_browser = False
        self._progress_browser_had_live_link = False
        self._progress_browser_released_by_user = True
        br = self.browser
        self.browser_context = None
        self.browser_page = None
        self.browser = None
        if br is not None:
            try:
                br.close()
            except Exception:
                pass
        self._refresh_activity_panel()

    def _begin_receipt_llm_worker_ui(self) -> None:
        self._receipt_llm_worker_active = True
        self._refresh_activity_panel()

    def _end_receipt_llm_worker_ui(self) -> None:
        self._receipt_llm_worker_active = False
        self._receipt_llm_cancel.clear()
        self._refresh_activity_panel()

    def _invalidate_receipt_table_match_cache(self) -> None:
        self._receipt_table_ma_cache = None

    def on_stop_receipt_llm_parse(self) -> None:
        if not self._receipt_llm_worker_active:
            self.set_status("No receipt LLM work is running.")
            return
        self._receipt_llm_cancel.set()
        self.set_status(
            "Stop receipt LLM: cancel requested — will stop after the current file finishes "
            "(API call cannot be interrupted mid-flight)."
        )

    def _receipt_llm_worker_guard_or_notify(self) -> bool:
        """Return True if a receipt LLM worker is already running (and notify user)."""
        if self._receipt_llm_worker_active:
            self.set_status(
                "Receipt LLM parse is already running. Use “Stop receipt LLM” on the Activity tab to cancel, "
                "or wait until it finishes."
            )
            return True
        return False

    def on_restart_from_selected_activity(self) -> None:
        sel = self.activity_table.selection()
        if not sel:
            self.set_status("Select a row in the activity sequence, then click Restart from selected step.")
            return
        key = sel[0]
        if key in ("step1", "step2"):
            self.set_status(
                "Steps 1–2 are manual: use the workflow buttons above for receipts and browser login."
            )
            return
        if key not in POPULATE_RESUME_KEYS:
            self.set_status("Unknown activity key; cannot restart.")
            return
        if self._step3_automation_active:
            self.set_status("Stop the running automation first, then restart from a step.")
            return
        if not self.browser_page:
            self.set_status("Open Step 2 (browser) before restarting Step 3 phases.")
            return
        if not self.receipt_paths:
            self.set_status("Import receipts (Step 1) before restarting Step 3 phases.")
            return
        self._activity_stopped_at_key = None
        self.set_status(f"Restarting expense automation from: {key}…")
        self._run_populate_expense_report_flow(start_from=key)

    def load_settings(self) -> AppSettings:
        data = self._read_settings_payload()
        if not data:
            return AppSettings()
        try:
            return AppSettings(
                legacy_url=data.get("legacy_url", AppSettings.legacy_url),
                approver=str(data.get("approver", "") or ""),
                openai_model=data.get("openai_model", AppSettings.openai_model),
                openai_http_verify=str(data.get("openai_http_verify", "") or ""),
                photos_limit=int(data.get("photos_limit", AppSettings.photos_limit)),
                photos_export_dir=data.get("photos_export_dir", AppSettings.photos_export_dir),
                nav_menu_label=data.get("nav_menu_label", AppSettings.nav_menu_label),
            )
        except Exception:
            return AppSettings()

    def save_settings(self, settings: AppSettings) -> None:
        existing = self._read_settings_payload()
        if SETTINGS_OPENAI_KEY in existing:
            # Keep API key fallback persistence untouched by non-key setting saves.
            payload = {**asdict(settings), SETTINGS_OPENAI_KEY: existing[SETTINGS_OPENAI_KEY]}
        else:
            payload = asdict(settings)
        payload.pop("expense_username", None)
        self._write_settings_payload(payload)
        self.settings = settings

    def _read_settings_payload(self) -> dict:
        if not SETTINGS_FILE.exists():
            return {}
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _backfill_expense_line_derived_if_needed(self) -> None:
        """Older runs only wrote sidecar JSON; embed summaries onto lines once so relaunch repopulates."""
        lines, _ = load_expense_lines_cache(APP_DIR)
        if not lines:
            return
        if any(
            str(ln.get("cached_analysis_at", "")).strip()
            for ln in lines
            if isinstance(ln, dict)
        ):
            return
        if not load_receipt_line_matches(APP_DIR):
            return
        try:
            persist_expense_line_derived_fields(
                APP_DIR,
                load_receipt_line_matches(APP_DIR),
                self._load_vendor_expense_cache(),
            )
        except Exception:
            pass

    def _read_state_payload(self) -> dict:
        if not STATE_FILE.exists():
            return {}
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state_payload(self, payload: dict) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _persist_runtime_state(self) -> None:
        payload = {
            "receipt_paths": self.receipt_paths,
            "analyses": self.analyses,
            "assignment_map": self.assignment_map,
        }
        self._write_state_payload(payload)
        if self.analyses:
            try:
                save_analyses_snapshot(APP_DIR, list(self.analyses))
            except Exception:
                pass

    def _load_persisted_state(self) -> None:
        payload = self._read_state_payload()
        raw_receipts = payload.get("receipt_paths", [])
        raw_analyses = payload.get("analyses", [])
        raw_assignments = payload.get("assignment_map", {})

        if isinstance(raw_receipts, list):
            self.receipt_paths = [str(path) for path in raw_receipts if str(path).strip()]
        if isinstance(raw_analyses, list):
            self.analyses = [item for item in raw_analyses if isinstance(item, dict)]
        if isinstance(raw_assignments, dict):
            self.assignment_map = {
                str(k): str(v)
                for k, v in raw_assignments.items()
                if str(k).strip() and str(v).strip()
            }

    def _read_ui_layout(self) -> dict:
        if not UI_LAYOUT_FILE.exists():
            return {}
        try:
            raw = json.loads(UI_LAYOUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_ui_layout(self, payload: dict) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        UI_LAYOUT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @staticmethod
    def _tree_column_ids(tree: ttk.Treeview) -> tuple[str, ...]:
        raw = tree.cget("columns")
        if isinstance(raw, (tuple, list)):
            return tuple(str(x) for x in raw)
        if isinstance(raw, str):
            return tuple(tree.tk.splitlist(raw))
        return ()

    def _collect_tree_column_widths(self, tree: ttk.Treeview | None) -> dict[str, int]:
        if tree is None:
            return {}
        out: dict[str, int] = {}
        for col in self._tree_column_ids(tree):
            try:
                w = tree.column(col, "width")
                out[col] = int(w)
            except (tk.TclError, TypeError, ValueError):
                continue
        return out

    def _apply_tree_column_widths(self, tree: ttk.Treeview | None, widths: dict) -> None:
        if tree is None or not isinstance(widths, dict):
            return
        for col, w in widths.items():
            if col not in self._tree_column_ids(tree):
                continue
            try:
                iw = int(w)
                if iw >= 12:
                    tree.column(col, width=iw)
            except (TypeError, ValueError, tk.TclError):
                continue

    def _safe_paned_sash(self, paned: ttk.PanedWindow | None, idx: int, pos: int) -> None:
        if paned is None:
            return
        try:
            paned.update_idletasks()
            w = int(paned.winfo_width())
            if w < 60:
                return
            p = max(60, min(int(pos), w - 60))
            paned.sashpos(idx, p)
        except (tk.TclError, TypeError, ValueError):
            pass

    def _collect_ui_layout(self) -> dict:
        win_geom = ""
        try:
            win_geom = self.root.geometry()
        except tk.TclError:
            pass
        try:
            win_state = str(self.root.state())
        except tk.TclError:
            win_state = "normal"

        nb_idx: int | None = None
        if hasattr(self, "main_notebook"):
            try:
                nb_idx = int(self.main_notebook.index("current"))
            except (tk.TclError, ValueError):
                nb_idx = None

        paned: dict[str, int] = {}
        for key, attr in (
            ("documents", "_documents_paned"),
            ("expense_report", "_expense_report_paned"),
            ("activity", "_activity_paned"),
        ):
            p = getattr(self, attr, None)
            if p is None:
                continue
            try:
                paned[key] = int(p.sashpos(0))
            except (tk.TclError, TypeError, ValueError):
                pass

        columns: dict[str, dict[str, int]] = {
            "documents_table": self._collect_tree_column_widths(getattr(self, "table", None)),
            "expense_report_tree": self._collect_tree_column_widths(
                getattr(self, "expense_report_tree", None)
            ),
            "activity_table": self._collect_tree_column_widths(getattr(self, "activity_table", None)),
            "expense_types_tree": self._collect_tree_column_widths(
                getattr(self, "expense_types_tree", None)
            ),
        }
        columns = {k: v for k, v in columns.items() if v}

        return {
            "window": {"geometry": win_geom, "state": win_state},
            "notebook_index": nb_idx,
            "paned": paned,
            "columns": columns,
        }

    def _save_ui_layout_now(self) -> None:
        try:
            self._write_ui_layout(self._collect_ui_layout())
        except Exception:
            pass

    def _apply_ui_layout(self) -> None:
        data = self._read_ui_layout()
        if not data:
            return
        win = data.get("window")
        if isinstance(win, dict):
            geom = win.get("geometry")
            if isinstance(geom, str) and geom.strip() and "x" in geom:
                try:
                    self.root.geometry(geom.strip())
                except tk.TclError:
                    pass
            state = win.get("state")
            if state == "zoomed":
                try:
                    self.root.state("zoomed")
                except tk.TclError:
                    pass
            elif state == "iconic":
                try:
                    self.root.iconify()
                except tk.TclError:
                    pass

        cols_all = data.get("columns")
        if isinstance(cols_all, dict):
            self._apply_tree_column_widths(getattr(self, "table", None), cols_all.get("documents_table") or {})
            self._apply_tree_column_widths(
                getattr(self, "expense_report_tree", None),
                cols_all.get("expense_report_tree") or {},
            )
            self._apply_tree_column_widths(
                getattr(self, "activity_table", None),
                cols_all.get("activity_table") or {},
            )
            self._apply_tree_column_widths(
                getattr(self, "expense_types_tree", None),
                cols_all.get("expense_types_tree") or {},
            )

        nb_idx = data.get("notebook_index")
        try:
            nb_i = int(nb_idx) if nb_idx is not None else None
        except (TypeError, ValueError):
            nb_i = None
        if nb_i is not None and hasattr(self, "main_notebook"):
            try:
                n = int(self.main_notebook.index("end"))
                if 0 <= nb_i < n:
                    self.main_notebook.select(nb_i)
            except (tk.TclError, ValueError):
                pass

        paned_data = data.get("paned")
        if isinstance(paned_data, dict):

            def apply_sashes() -> None:
                if not isinstance(paned_data, dict):
                    return
                for key, attr in (
                    ("documents", "_documents_paned"),
                    ("expense_report", "_expense_report_paned"),
                    ("activity", "_activity_paned"),
                ):
                    raw = paned_data.get(key)
                    if raw is None:
                        continue
                    try:
                        pos = int(raw)
                    except (TypeError, ValueError):
                        continue
                    self._safe_paned_sash(getattr(self, attr, None), 0, pos)

            self.root.after_idle(lambda: self.root.after(120, apply_sashes))
            self.root.after_idle(lambda: self.root.after(400, apply_sashes))

    def _schedule_ui_layout_save(self, event: tk.Event | None = None) -> None:
        # `bind('<Configure>', …)` on the root also receives Configure for descendants; ignore those.
        if event is not None and event.widget is not self.root:
            if getattr(event, "type", None) == 22:
                return
        tid = getattr(self, "_ui_layout_save_after_id", None)
        if tid is not None:
            try:
                self.root.after_cancel(tid)
            except tk.TclError:
                pass
        self._ui_layout_save_after_id = self.root.after(500, self._flush_ui_layout_save)

    def _flush_ui_layout_save(self) -> None:
        self._ui_layout_save_after_id = None
        self._save_ui_layout_now()

    def _setup_ui_layout_persistence(self) -> None:
        self._ui_layout_save_after_id: str | None = None
        self.root.bind("<Configure>", self._schedule_ui_layout_save, add="+")
        for attr in ("_documents_paned", "_expense_report_paned", "_activity_paned"):
            p = getattr(self, attr, None)
            if p is not None:
                p.bind("<<PanedWindowMoved>>", self._schedule_ui_layout_save, add="+")
        if hasattr(self, "main_notebook"):
            self.main_notebook.bind("<<NotebookTabChanged>>", self._schedule_ui_layout_save, add="+")
        for attr in ("table", "expense_report_tree", "activity_table", "expense_types_tree"):
            t = getattr(self, attr, None)
            if t is not None:
                t.bind("<ButtonRelease-1>", self._schedule_ui_layout_save, add="+")

    def _write_settings_payload(self, payload: dict) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_vendor_expense_cache(self) -> dict[str, str]:
        if not VENDOR_EXPENSE_CACHE_FILE.exists():
            return {}
        try:
            raw = json.loads(VENDOR_EXPENSE_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        inner = raw.get("vendors") if isinstance(raw.get("vendors"), dict) else raw
        out: dict[str, str] = {}
        for k, v in inner.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            key = _normalize_vendor_key(k)
            val = v.strip()
            if key and val:
                out[key] = val
        return out

    def _persist_vendor_expense_cache(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"vendors": dict(sorted(self.vendor_expense_cache.items()))}
        VENDOR_EXPENSE_CACHE_FILE.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _bootstrap_openai_key(self) -> None:
        key = self.get_openai_key()
        if key:
            self._persist_openai_key_fallback(key)
            self._try_set_keyring_value(key)

    def _persist_openai_key_fallback(self, api_key: str) -> None:
        payload = self._read_settings_payload()
        if api_key:
            payload[SETTINGS_OPENAI_KEY] = api_key
        else:
            payload.pop(SETTINGS_OPENAI_KEY, None)
        self._write_settings_payload(payload)

    def _try_set_keyring_value(self, api_key: str) -> str | None:
        try:
            if api_key:
                return keychain_credentials.set_keychain_openai_key(api_key)
            keychain_credentials.delete_keychain_openai_key()
            return None
        except Exception as exc:
            return str(exc)

    def get_openai_key(self) -> str:
        if self._openai_key_cache:
            return self._openai_key_cache

        try:
            keychain_value = keychain_credentials.get_keychain_openai_key() or ""
            if keychain_value:
                self._openai_key_cache = keychain_value.strip()
                return self._openai_key_cache
        except Exception:
            pass

        settings_value = str(self._read_settings_payload().get(SETTINGS_OPENAI_KEY, "")).strip()
        if settings_value:
            self._openai_key_cache = settings_value
            return self._openai_key_cache

        env_value = os.getenv(ENV_OPENAI_KEY, "").strip()
        if env_value:
            self._openai_key_cache = env_value
            return self._openai_key_cache

        return ""

    def set_openai_key(self, api_key: str) -> str | None:
        normalized = api_key.strip()
        self._openai_key_cache = normalized
        self._persist_openai_key_fallback(normalized)
        keyring_error = self._try_set_keyring_value(normalized)
        if keyring_error:
            return (
                "OpenAI key was saved in local app settings but not macOS keychain: "
                f"{keyring_error}"
            )
        return None

    def open_settings_dialog(self) -> None:
        """Backward-compatible entry point: opens the Settings tab."""
        self.focus_settings_tab()

    def open_vendor_expense_cache_dialog(self) -> None:
        """Backward-compatible entry point: opens the Expense types tab."""
        self.focus_expense_types_tab()

    def _prepare_receipt_files_for_import(
        self,
        raw_paths: list[str],
        *,
        target_bytes: int = 980_000,
    ) -> tuple[list[str], int, int, dict[str, str]]:
        """
        Normalize selected receipt files into stable local paths.
        - PDFs are copied only when source is temporary.
        - Images from temporary sources are copied into app storage.
        - Large images are downscaled/compressed toward target_bytes.
        Returns (prepared_paths, copied_count, optimized_count, old_to_new_path_map).
        """
        out: list[str] = []
        copied = 0
        optimized = 0
        seen: set[str] = set()
        old_to_new: dict[str, str] = {}
        import_dir = APP_DIR / "receipt-imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        type_counts: dict[str, int] = {}
        action_counts: dict[str, int] = {"optimized": 0, "copied": 0, "unchanged": 0, "missing": 0}
        already_in_unified = 0

        def _safe_name(name: str) -> str:
            s = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "").strip("._")
            return s or "receipt"

        def _is_temp_source(path_obj: Path) -> bool:
            s = str(path_obj)
            if "com.apple.Photos.NSItemProvider" in s:
                return True
            return s.startswith("/private/var/folders/") and "/T/" in s

        image_exts = {".jpg", ".jpeg", ".png", ".heic", ".tiff", ".tif", ".webp"}

        def _try_optimize_image(src: Path, dest_base: str) -> Path | None:
            if Image is None:
                return None
            try:
                with Image.open(src) as im0:
                    im = ImageOps.exif_transpose(im0) if ImageOps is not None else im0.copy()
                    orig_w, orig_h = im.size
                    if orig_w <= 0 or orig_h <= 0:
                        return None
                    orig_size = src.stat().st_size
                    ext = src.suffix.lower()
                    keep_png = ext == ".png"

                    best_bytes: bytes | None = None
                    best_ext = ".jpg"
                    best_size = 1 << 60

                    scales = [1.0, 0.9, 0.8, 0.7, 0.6, 0.5]
                    if max(orig_w, orig_h) > 4000:
                        scales = [0.8, 0.7, 0.6, 0.5, 0.4]

                    for scale in scales:
                        w = max(900, int(orig_w * scale))
                        h = max(900, int(orig_h * scale))
                        if w > orig_w:
                            w = orig_w
                        if h > orig_h:
                            h = orig_h
                        if (w, h) == im.size:
                            cand = im
                        else:
                            cand = im.resize((w, h), Image.LANCZOS)

                        if keep_png and cand.mode in ("RGBA", "LA"):
                            b = io.BytesIO()
                            cand.save(b, format="PNG", optimize=True)
                            data = b.getvalue()
                            sz = len(data)
                            if sz < best_size:
                                best_bytes, best_ext, best_size = data, ".png", sz
                            if sz <= target_bytes:
                                break
                            continue

                        if cand.mode not in ("RGB", "L"):
                            cand = cand.convert("RGB")
                        elif cand.mode == "L":
                            cand = cand.convert("RGB")

                        for q in (88, 82, 76, 70, 64, 58):
                            b = io.BytesIO()
                            cand.save(b, format="JPEG", quality=q, optimize=True, progressive=True)
                            data = b.getvalue()
                            sz = len(data)
                            if sz < best_size:
                                best_bytes, best_ext, best_size = data, ".jpg", sz
                            if sz <= target_bytes:
                                break
                        if best_size <= target_bytes:
                            break

                    if best_bytes is None:
                        return None
                    if best_size >= orig_size and not _is_temp_source(src):
                        return None

                    digest = hashlib.sha1(src.as_posix().encode("utf-8")).hexdigest()[:10]
                    out_path = import_dir / f"{dest_base}-{digest}-opt{best_ext}"
                    out_path.write_bytes(best_bytes)
                    return out_path
            except Exception:
                return None

        for raw in raw_paths:
            p = Path(str(raw or "").strip()).expanduser()
            if not p.is_file():
                action_counts["missing"] = action_counts.get("missing", 0) + 1
                continue
            ext = p.suffix.lower()
            type_counts[ext or "<none>"] = type_counts.get(ext or "<none>", 0) + 1
            is_image = ext in image_exts
            is_temp = _is_temp_source(p)
            over_target = is_image and p.stat().st_size > target_bytes
            try:
                p.relative_to(import_dir)
                in_unified = True
            except Exception:
                in_unified = False
            if in_unified:
                already_in_unified += 1

            final_path = p
            if is_image and (is_temp or over_target):
                base = _safe_name(p.stem)
                opt = _try_optimize_image(p, base)
                if opt and opt.is_file():
                    final_path = opt
                    optimized += 1
                    action_counts["optimized"] = action_counts.get("optimized", 0) + 1
                    if is_temp:
                        copied += 1
                        action_counts["copied"] = action_counts.get("copied", 0) + 1
                elif is_temp:
                    digest = hashlib.sha1(p.as_posix().encode("utf-8")).hexdigest()[:10]
                    dest = import_dir / f"{base}-{digest}{ext or '.bin'}"
                    try:
                        shutil.copy2(p, dest)
                        final_path = dest
                        copied += 1
                        action_counts["copied"] = action_counts.get("copied", 0) + 1
                    except OSError:
                        final_path = p
                        action_counts["unchanged"] = action_counts.get("unchanged", 0) + 1
                else:
                    action_counts["unchanged"] = action_counts.get("unchanged", 0) + 1
            elif is_temp:
                base = _safe_name(p.stem)
                digest = hashlib.sha1(p.as_posix().encode("utf-8")).hexdigest()[:10]
                dest = import_dir / f"{base}-{digest}{ext or '.bin'}"
                try:
                    shutil.copy2(p, dest)
                    final_path = dest
                    copied += 1
                    action_counts["copied"] = action_counts.get("copied", 0) + 1
                except OSError:
                    final_path = p
                    action_counts["unchanged"] = action_counts.get("unchanged", 0) + 1
            else:
                action_counts["unchanged"] = action_counts.get("unchanged", 0) + 1

            fp = str(final_path)
            old_to_new[str(p)] = fp
            if fp and fp not in seen:
                seen.add(fp)
                out.append(fp)

        # region agent log
        self._debug_log(
            hypothesis_id="HX1",
            location="receipt_automation_ui.py:_prepare_receipt_files_for_import",
            message="Prepare receipts summary by type/action",
            data={
                "input_count": len(raw_paths),
                "prepared_count": len(out),
                "already_in_unified": already_in_unified,
                "type_counts": type_counts,
                "action_counts": action_counts,
            },
            run_id="export_downconvert_probe",
        )
        # endregion
        return out, copied, optimized, old_to_new

    def on_step_select_receipts(self) -> None:
        api_key = self.get_openai_key().strip()
        selected_paths, precomputed_analyses = self.open_receipt_picker_dialog(api_key=api_key)
        if not selected_paths:
            self.set_status("No images selected.")
            return

        if not api_key:
            existing_receipts = list(self.receipt_paths)
            self.receipt_paths = list(dict.fromkeys(existing_receipts + selected_paths))
            self._session_progress_receipts_done = True
            self.refresh_receipt_table()
            self._persist_runtime_state()
            self.set_status("Images imported. Set OpenAI key in Settings to run LLM review.")
            return

        existing_receipts = list(self.receipt_paths)
        self.receipt_paths = list(dict.fromkeys(existing_receipts + selected_paths))
        self._session_progress_receipts_done = True
        analysis_by_source = {item.get("source_file", ""): item for item in precomputed_analyses}
        existing_analyses = {item.get("source_file", ""): item for item in self.analyses}
        existing_analyses.update(analysis_by_source)
        self.analyses = list(existing_analyses.values())
        self.refresh_receipt_table()
        self._persist_runtime_state()

        pending_paths = [path for path in selected_paths if path not in existing_analyses]
        if not pending_paths:
            self._session_progress_parsed_done = bool(self.analyses)
            write_analysis_report(self.analyses, Path(self.settings.photos_export_dir).expanduser())
            self._persist_runtime_state()
            self.set_status(f"Imported {len(selected_paths)} receipt(s) and completed LLM review.")
            return

        if self._receipt_llm_worker_guard_or_notify():
            return

        self._receipt_llm_cancel.clear()
        self._begin_receipt_llm_worker_ui()

        def worker() -> None:
            t0 = time.monotonic()
            cancelled = False
            try:
                self.root.after(
                    0,
                    lambda: self.set_status(
                        f"Analyzing {len(pending_paths)} remaining file(s) with LLM..."
                    ),
                )
                total = len(pending_paths)
                for idx, source_path in enumerate(pending_paths, start=1):
                    if self._receipt_llm_cancel.is_set():
                        cancelled = True
                        break
                    analyses = analyze_receipts_with_llm(
                        receipt_paths=[source_path],
                        model=self.settings.openai_model,
                        api_key=api_key,
                        on_status=self.set_status,
                        http_verify_preferred=self.settings.openai_http_verify,
                    )
                    if analyses:
                        analysis = analyses[0]
                        self.root.after(0, lambda item=analysis: self._apply_single_analysis_result(item))
                    elapsed = int(time.monotonic() - t0)
                    self.root.after(
                        0,
                        lambda i=idx, t=total, e=elapsed: self.set_status(
                            f"Step 1 progress: analyzed {i}/{t} file(s) ({e}s elapsed)."
                        ),
                    )

                self.root.after(
                    0,
                    lambda: write_analysis_report(
                        self.analyses, Path(self.settings.photos_export_dir).expanduser()
                    ),
                )
                if cancelled:
                    self.root.after(
                        0,
                        lambda: self.set_status(
                            f"Step 1: receipt LLM stopped after partial parse "
                            f"({len(self.analyses)} file(s) in memory). Remaining files were skipped."
                        ),
                    )
                else:
                    self.root.after(
                        0,
                        lambda: self.set_status(
                            f"Imported {len(selected_paths)} receipt(s) and completed LLM review."
                        ),
                    )
                self.root.after(
                    0,
                    lambda: setattr(self, "_session_progress_parsed_done", bool(self.analyses)),
                )
            except Exception as exc:
                self.root.after(0, lambda e=exc: self.set_status(f"Step 1 failed: {e}"))
            finally:
                self.root.after(0, self._end_receipt_llm_worker_ui)

        threading.Thread(target=worker, daemon=True).start()

    def add_more_receipt_files(self) -> None:
        """Append receipts from disk (Documents tab). Parses with LLM when an API key is set."""
        file_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="Add receipt images or PDFs",
            filetypes=[
                ("Image/PDF files", "*.jpg *.jpeg *.png *.heic *.tiff *.pdf"),
                ("All files", "*.*"),
            ],
        )
        if not file_paths:
            return
        selected_paths, copied_count, optimized_count, _ = self._prepare_receipt_files_for_import(list(file_paths))
        if not selected_paths:
            self.set_status("Add files: selected items were not readable files.")
            return
        existing_before = set(self.receipt_paths)
        self.receipt_paths = list(dict.fromkeys(list(self.receipt_paths) + selected_paths))
        self._session_progress_receipts_done = True
        n_new = sum(1 for p in selected_paths if p not in existing_before)

        api_key = self.get_openai_key().strip()
        if not api_key:
            self.refresh_receipt_table()
            self._persist_runtime_state()
            self.set_status(
                f"Added {n_new} new file(s). Prepared {optimized_count} optimized / {copied_count} copied from temp sources. "
                "Set OpenAI key in Settings to run LLM review."
                if n_new
                else "Those files are already in the list."
            )
            return

        existing_analyses = {item.get("source_file", ""): item for item in self.analyses}
        pending_paths = [path for path in selected_paths if path not in existing_analyses]
        self.refresh_receipt_table()
        self._persist_runtime_state()

        if not pending_paths:
            self._session_progress_parsed_done = bool(self.analyses)
            write_analysis_report(self.analyses, Path(self.settings.photos_export_dir).expanduser())
            self._persist_runtime_state()
            self.set_status(
                f"Added {n_new} new file(s); parse data already present for all. "
                f"Prepared {optimized_count} optimized / {copied_count} copied from temp sources."
                if n_new
                else "Those files are already in the list."
            )
            return

        if self._receipt_llm_worker_guard_or_notify():
            return

        self._receipt_llm_cancel.clear()
        self._begin_receipt_llm_worker_ui()

        def worker() -> None:
            t0 = time.monotonic()
            cancelled = False
            try:
                self.root.after(
                    0,
                    lambda: self.set_status(
                        f"Analyzing {len(pending_paths)} new file(s) with LLM..."
                    ),
                )
                total = len(pending_paths)
                for idx, source_path in enumerate(pending_paths, start=1):
                    if self._receipt_llm_cancel.is_set():
                        cancelled = True
                        break
                    analyses = analyze_receipts_with_llm(
                        receipt_paths=[source_path],
                        model=self.settings.openai_model,
                        api_key=api_key,
                        on_status=self.set_status,
                        http_verify_preferred=self.settings.openai_http_verify,
                    )
                    if analyses:
                        analysis = analyses[0]
                        self.root.after(0, lambda item=analysis: self._apply_single_analysis_result(item))
                    elapsed = int(time.monotonic() - t0)
                    self.root.after(
                        0,
                        lambda i=idx, t=total, e=elapsed: self.set_status(
                            f"Add files: analyzed {i}/{t} ({e}s elapsed)."
                        ),
                    )

                self.root.after(
                    0,
                    lambda: write_analysis_report(
                        self.analyses, Path(self.settings.photos_export_dir).expanduser()
                    ),
                )
                if cancelled:
                    self.root.after(
                        0,
                        lambda: self.set_status(
                            "Add files: receipt LLM stopped — partial results kept; "
                            "run Rescan on skipped files when ready."
                        ),
                    )
                else:
                    self.root.after(
                        0,
                        lambda: self.set_status(
                            f"Added {n_new} file(s) and finished LLM review for new items."
                        ),
                    )
                self.root.after(
                    0,
                    lambda: setattr(self, "_session_progress_parsed_done", bool(self.analyses)),
                )
            except Exception as exc:
                self.root.after(0, lambda e=exc: self.set_status(f"Add files / LLM failed: {e}"))
            finally:
                self.root.after(0, self._end_receipt_llm_worker_ui)

        threading.Thread(target=worker, daemon=True).start()

    def open_receipt_picker_dialog(self, api_key: str) -> tuple[list[str], list[dict]]:
        dialog = tk.Toplevel(self.root)
        dialog.title("Select Receipts")
        dialog.geometry("860x420")
        dialog.transient(self.root)

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text=(
                "Add receipt images/PDFs. LLM parsing runs in the background; you can use the main window "
                "while this dialog is open. Close with OK (keep list) or Cancel."
            ),
        ).pack(anchor="w", pady=(0, 8))

        table_container = ttk.Frame(frame)
        table_container.pack(fill=tk.BOTH, expand=True)
        columns = ("file", "status", "local", "usd", "confidence")
        table = ttk.Treeview(table_container, columns=columns, show="headings", height=11)
        table.heading("file", text="File")
        table.heading("status", text="Status")
        table.heading("local", text="Local")
        table.heading("usd", text="USD")
        table.heading("confidence", text="Confidence")
        table.column("file", width=260)
        table.column("status", width=100, anchor=tk.CENTER)
        table.column("local", width=200, anchor=tk.W)
        table.column("usd", width=120, anchor=tk.W)
        table.column("confidence", width=72, anchor=tk.CENTER)
        table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        yscroll = ttk.Scrollbar(table_container, orient=tk.VERTICAL, command=table.yview)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        table.configure(yscrollcommand=yscroll.set)

        candidate_paths: list[str] = []
        result_paths: list[str] = []
        analysis_by_source: dict[str, dict] = {
            str(item.get("source_file", "")): item
            for item in self.analyses
            if str(item.get("source_file", "")).strip()
        }
        parsing_status: dict[str, str] = {}
        parse_queue: queue.Queue[str] = queue.Queue()
        queued_paths: set[str] = set()
        stop_event = threading.Event()
        dialog_status = tk.StringVar(
            value="Add files to begin. Parsing starts automatically."
            if api_key
            else "Add files to begin. Set OpenAI key in Settings to enable LLM parsing."
        )

        def refresh_table() -> None:
            for item in table.get_children():
                table.delete(item)
            for path_str in candidate_paths:
                analysis = analysis_by_source.get(path_str, {})
                status = parsing_status.get(path_str, "queued")
                table.insert(
                    "",
                    tk.END,
                    values=(
                        Path(path_str).name,
                        status,
                        receipt_local_amount_display(analysis),
                        receipt_usd_amount_display(analysis),
                        analysis.get("confidence", ""),
                    ),
                )

        def parse_worker() -> None:
            while True:
                if stop_event.is_set() and parse_queue.empty():
                    break
                try:
                    path_str = parse_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                if stop_event.is_set():
                    parsing_status[path_str] = "skipped"
                    parse_queue.task_done()
                    continue

                parsing_status[path_str] = "parsing"

                try:
                    if stop_event.is_set():
                        parsing_status[path_str] = "skipped"
                    else:
                        analyses = analyze_receipts_with_llm(
                            receipt_paths=[path_str],
                            model=self.settings.openai_model,
                            api_key=api_key,
                            on_status=self.set_status,
                            http_verify_preferred=self.settings.openai_http_verify,
                        )
                        if analyses:
                            analysis_by_source[path_str] = analyses[0]
                            parsing_status[path_str] = "done"
                        else:
                            parsing_status[path_str] = "skipped"
                except Exception:
                    parsing_status[path_str] = "error"
                finally:
                    parse_queue.task_done()

        def poll_ui_updates() -> None:
            refresh_table()
            done_count = sum(1 for v in parsing_status.values() if v == "done")
            cached_count = sum(1 for v in parsing_status.values() if v == "cached")
            parsing_count = sum(1 for v in parsing_status.values() if v == "parsing")
            if parsing_count > 0:
                dialog_status.set(
                    f"Parsing {parsing_count} file(s)... Parsed {done_count + cached_count}/{len(candidate_paths)}."
                )
            elif candidate_paths:
                dialog_status.set(
                    f"Parsed {done_count + cached_count}/{len(candidate_paths)} file(s)."
                )
            if not stop_event.is_set():
                dialog.after(300, poll_ui_updates)

        if api_key:
            threading.Thread(target=parse_worker, daemon=True).start()
            dialog.after(300, poll_ui_updates)

        def add_files() -> None:
            file_paths = filedialog.askopenfilenames(
                parent=dialog,
                title="Select receipt images",
                filetypes=[
                    ("Image/PDF files", "*.jpg *.jpeg *.png *.heic *.tiff *.pdf"),
                    ("All files", "*.*"),
                ],
            )
            if not file_paths:
                return
            prepared_paths, copied_count, optimized_count, _ = self._prepare_receipt_files_for_import(list(file_paths))
            if not prepared_paths:
                dialog_status.set("No readable files were selected.")
                self.set_status("Step 1: selected files were not readable.")
                return
            existing = set(candidate_paths)
            new_paths: list[str] = []
            for p in prepared_paths:
                if p not in existing:
                    candidate_paths.append(p)
                    new_paths.append(p)
                    parsing_status[p] = "cached" if p in analysis_by_source else "queued"
            refresh_table()
            self.set_status(
                f"Added {len(new_paths)} file(s) manually. "
                f"Prepared {optimized_count} optimized / {copied_count} copied from temp sources."
            )
            dialog_status.set(
                f"Added {len(new_paths)} new file(s) "
                f"({optimized_count} optimized, {copied_count} copied)."
            )

            if api_key:
                for path_str in new_paths:
                    if path_str in analysis_by_source:
                        continue
                    if path_str in queued_paths:
                        continue
                    queued_paths.add(path_str)
                    parse_queue.put(path_str)

        def confirm_selection() -> None:
            if not candidate_paths:
                dialog_status.set("Add at least one image or PDF.")
                self.set_status("Step 1: add at least one image or PDF before continuing.")
                return
            result_paths.extend(candidate_paths)
            stop_event.set()
            dialog.destroy()

        def cancel_dialog() -> None:
            stop_event.set()
            dialog.destroy()

        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(button_row, text="Add Files...", command=add_files).pack(side=tk.LEFT, padx=8)
        ttk.Button(button_row, text="Cancel", command=cancel_dialog).pack(side=tk.RIGHT, padx=6)
        ttk.Button(button_row, text="OK", command=confirm_selection).pack(side=tk.RIGHT)

        ttk.Label(frame, textvariable=dialog_status, anchor="w").pack(fill=tk.X, pady=(8, 0))
        dialog.protocol("WM_DELETE_WINDOW", cancel_dialog)

        self.root.wait_window(dialog)
        computed_analyses = [analysis_by_source[path] for path in result_paths if path in analysis_by_source]
        return result_paths, computed_analyses

    def on_step_login(self) -> None:
        try:
            from tkinter import messagebox

            messagebox.showinfo(
                "Oracle sign-in",
                "Your Oracle username and password are not stored in this app.\n\n"
                "Sign in manually in the Chromium window (including 2FA if required). "
                "When you run scrape or expense-report automation, the app will wait until "
                "you are logged in, then continue automatically.",
            )
        except Exception:
            pass
        try:
            self.open_controlled_browser(self.settings.legacy_url)
            self._refresh_activity_panel()
        except Exception as exc:
            self.set_status(f"Step 2 failed: could not open controlled browser ({exc}).")
            return

        self.set_status(
            "Opened Chromium to the expense portal. Sign in manually, then continue with the workflow steps."
        )
        self._refresh_activity_panel()

    def relaunch_controlled_browser_for_resume(self) -> None:
        """Close and reopen Chromium to the expense portal; retry auto-login. Used from Resume dialog after a crash."""
        try:
            self.close_controlled_browser()
        except Exception:
            pass
        try:
            self.open_controlled_browser(self.settings.legacy_url)
        except Exception as exc:
            self.set_status(f"Relaunch failed: {exc}")
            self.log_event("err", f"Resume relaunch: could not open browser ({exc}).")
            return
        self.set_status(
            "Browser relaunched — sign in manually in Chromium, then Continue automation."
        )
        self._refresh_activity_panel()

    def _post_login_wait_seconds(self) -> float:
        """Max time to wait for SSO / 2FA / manual login after relaunch. ``AUTOMATED_EXPENSES_POST_LOGIN_WAIT_S`` (default 600)."""
        raw = (os.environ.get("AUTOMATED_EXPENSES_POST_LOGIN_WAIT_S") or "").strip()
        if not raw:
            return 600.0
        try:
            return max(60.0, float(raw))
        except ValueError:
            return 600.0

    def _expense_portal_shell_visible(self) -> bool:
        """True when the main iExpenses shell is visible — past the username/password login screen."""
        if not self.browser_page:
            return False
        markers = [
            "Update Expense Reports",
            "Create Expense Report",
            "Expenses Home",
            "Track Submitted",
            "Logged In As",
        ]
        nav_label = getattr(self.settings, "nav_menu_label", "") or ""
        if nav_label:
            markers.append(nav_label)
        return any(self._body_contains_text(m) for m in markers)

    def _wait_for_expense_portal_logged_in(self, *, context: str = "resume") -> None:
        """After relaunch, block until login/SSO finishes and the expenses UI appears (then automation can open in-progress reports)."""
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        deadline = time.monotonic() + self._post_login_wait_seconds()
        started = time.monotonic()
        last_log = 0.0
        did_retry_goto = False
        legacy = (self.settings.legacy_url or "").strip()
        self.set_status(
            "Waiting for expense portal after login — complete SSO, 2FA, or sign-in in Chromium when prompted…"
        )
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            if self._expense_portal_shell_visible():
                self.log_event("browser", f"{context}: expense portal home detected (past login).")
                return
            now = time.monotonic()
            if legacy and not did_retry_goto and (now - started) >= 40.0:
                did_retry_goto = True
                try:
                    self.browser_page.goto(legacy, wait_until="domcontentloaded", timeout=90000)
                    self.browser_page.wait_for_timeout(600)
                except Exception as exc:
                    self.log_event("warn", f"{context}: could not reload legacy URL during login wait: {exc}")
            if now - last_log >= 20.0:
                last_log = now
                self.log_event(
                    "browser",
                    f"{context}: still on login or redirect — finish signing in; looking for portal shell "
                    "(Expenses Home, Logged In As, Create/Update Expense Reports, …)…",
                )
                self.set_status(
                    "Still waiting — finish login in the Chromium window (SSO/2FA). "
                    "Automation continues when you are past the login page (e.g. Expenses Home or navigator visible)."
                )
            self.browser_page.wait_for_timeout(450)
        raise RuntimeError(
            "Timed out waiting for the expense portal after login. "
            "Sign in until the logged-in shell appears (e.g. Expenses Home or Create Expense Report), "
            "then try Resume again."
        )

    def _navigate_to_expenses_home_for_resume(self) -> None:
        """Resume path: iExpenses Navigator → Expenses Home tab, same as manual flow (not *Create Expense Report*)."""
        if not self.browser_page:
            return
        if self._body_contains_text("Update Expense Reports"):
            self.log_event("browser", "Resume: Update Expense Reports already visible.")
            return
        self.set_status("Resume: iExpenses Navigator → Expenses Home (same as manual — then Update pencil)…")
        self.log_event("browser", "Resume: expanding iExpenses Navigator; opening Expenses Home (not Create Expense Report).")
        self._oracle_expand_nic_iexpenses_menu()
        self.browser_page.wait_for_timeout(800)
        clicked = False
        for frame in self.browser_page.frames:
            try:
                for pat in (
                    re.compile(r"^\s*expenses\s*home\s*$", re.I),
                    re.compile(r"^\s*expenses\s+home\s*$", re.I),
                ):
                    tab = frame.get_by_role("tab", name=pat)
                    if tab.count() > 0:
                        tab.first.click(timeout=12000)
                        clicked = True
                        self.browser_page.wait_for_timeout(600)
                        break
                if clicked:
                    break
            except Exception:
                continue
        if not clicked:
            if self.click_text_in_any_frame("Expenses Home"):
                self.browser_page.wait_for_timeout(600)
                clicked = True
        legacy = (self.settings.legacy_url or "").strip()
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            if self._body_contains_text("Update Expense Reports"):
                return
            self.browser_page.wait_for_timeout(350)
        if legacy:
            try:
                self.browser_page.goto(legacy, wait_until="domcontentloaded", timeout=90000)
                self.browser_page.wait_for_timeout(1000)
            except Exception as exc:
                self.log_event("warn", f"Resume: goto legacy URL after NIC navigation: {exc}")
        for _ in range(40):
            self._pump_ui_and_check_cancel()
            if self._body_contains_text("Update Expense Reports"):
                return
            self.browser_page.wait_for_timeout(400)
        self.log_event(
            "warn",
            "Resume: “Update Expense Reports” not found after iExpenses Navigator / Expenses Home — try manual navigation.",
        )

    def _enable_crash_resume_button(self) -> None:
        btn = self.resume_after_crash_btn
        if btn is not None:
            try:
                btn.configure(state=tk.NORMAL)
            except tk.TclError:
                pass

    def _disable_crash_resume_button(self) -> None:
        btn = self.resume_after_crash_btn
        if btn is not None:
            try:
                btn.configure(state=tk.DISABLED)
            except tk.TclError:
                pass

    def _click_update_expense_reports_pencil_for_in_progress(self) -> bool:
        """On Expenses Home, *Update Expense Reports* table: click the yellow pencil in the **Update** column (In Progress row)."""
        if not self.browser_page:
            return False
        legacy = (self.settings.legacy_url or "").strip()
        if legacy and not self._body_contains_text("Update Expense Reports"):
            try:
                self.browser_page.goto(legacy, wait_until="domcontentloaded", timeout=90000)
                self.browser_page.wait_for_timeout(800)
            except Exception:
                pass

        # Oracle ADF: scope to the Update Expense Reports section; use tr.cells for colspan; click <a> wrapping the icon.
        js_column = r"""
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const vis = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 1 && r.height > 1;
  };
  const clickCell = (td) => {
    if (!td) return false;
    const as = Array.from(td.querySelectorAll('a')).filter(vis);
    for (const a of as) {
      a.click();
      return true;
    }
    for (const img of td.querySelectorAll('img')) {
      if (!vis(img)) continue;
      const wrap = img.closest('a');
      if (wrap && vis(wrap)) {
        wrap.click();
        return true;
      }
      img.click();
      return true;
    }
    const clickable = td.querySelector('[onclick], [role="link"], span');
    if (clickable && vis(clickable)) {
      clickable.click();
      return true;
    }
    td.click();
    return true;
  };
  const tableInUpdateExpenseReportsSection = (table) => {
    let p = table;
    for (let i = 0; i < 45 && p; i++) {
      const chunk = (p.innerText || '').slice(0, 900);
      if (/update\s+expense\s+reports/i.test(chunk)) return true;
      if (/click\s+an\s+update\s+icon/i.test(chunk)) return true;
      p = p.parentElement;
    }
    let sib = table.previousElementSibling;
    for (let j = 0; j < 35 && sib; j++) {
      const t = (sib.innerText || '').slice(0, 400);
      if (/update\s+expense\s+reports/i.test(t)) return true;
      if (/click\s+an\s+update\s+icon/i.test(t)) return true;
      sib = sib.previousElementSibling;
    }
    return false;
  };
  const tables = document.querySelectorAll('table');
  for (const table of tables) {
    if (!tableInUpdateExpenseReportsSection(table)) continue;
    const headerRow = table.querySelector('thead tr') || (table.rows && table.rows[0]);
    if (!headerRow) continue;
    const hc = headerRow.cells ? headerRow.cells.length : 0;
    const headers = [];
    for (let i = 0; i < hc; i++) {
      headers.push(norm(headerRow.cells[i].textContent || ''));
    }
    let updateIdx = headers.findIndex((h) => h === 'update');
    if (updateIdx < 0) {
      updateIdx = headers.findIndex((h) => h === 'update expense reports');
    }
    if (updateIdx < 0) {
      updateIdx = headers.findIndex((h) => h.includes('update') && !h.includes('duplicate') && !h.includes('delete'));
    }
    if (updateIdx < 0) continue;

    const rowList = [];
    if (table.tBodies && table.tBodies.length) {
      for (const tb of table.tBodies) {
        rowList.push(...Array.from(tb.rows));
      }
    } else {
      rowList.push(...Array.from(table.rows).slice(1));
    }
    for (const tr of rowList) {
      if (!/in\s+progress/i.test(tr.innerText || '')) continue;
      if (!tr.cells || tr.cells.length <= updateIdx) continue;
      const td = tr.cells[updateIdx];
      if (clickCell(td)) return true;
    }
  }
  for (const table of tables) {
    if (tableInUpdateExpenseReportsSection(table)) continue;
    const headerRow = table.querySelector('thead tr') || (table.rows && table.rows[0]);
    if (!headerRow) continue;
    const hc = headerRow.cells ? headerRow.cells.length : 0;
    const headers = [];
    for (let i = 0; i < hc; i++) {
      headers.push(norm(headerRow.cells[i].textContent || ''));
    }
    let updateIdx = headers.findIndex((h) => h === 'update');
    if (updateIdx < 0) {
      updateIdx = headers.findIndex((h) => h.includes('update') && !h.includes('duplicate') && !h.includes('delete'));
    }
    if (updateIdx < 0) continue;
    const rowList = [];
    if (table.tBodies && table.tBodies.length) {
      for (const tb of table.tBodies) {
        rowList.push(...Array.from(tb.rows));
      }
    } else {
      rowList.push(...Array.from(table.rows).slice(1));
    }
    for (const tr of rowList) {
      if (!/in\s+progress/i.test(tr.innerText || '')) continue;
      if (!tr.cells || tr.cells.length <= updateIdx) continue;
      const td = tr.cells[updateIdx];
      if (clickCell(td)) return true;
    }
  }
  return false;
}
"""
        js_fallback = r"""
() => {
  const vis = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 1 && r.height > 1;
  };
  const rows = Array.from(document.querySelectorAll('tr'));
  for (const tr of rows) {
    if (!/in\s+progress/i.test(tr.innerText || '')) continue;
    if (!tr.cells || tr.cells.length < 4) continue;
    const n = tr.cells.length;
    for (const idx of [n - 3, n - 4]) {
      if (idx < 0) continue;
      const td = tr.cells[idx];
      const as = td.querySelectorAll('a');
      for (const a of as) {
        if (!vis(a)) continue;
        a.click();
        return true;
      }
      for (const img of td.querySelectorAll('img')) {
        if (!vis(img)) continue;
        const w = img.closest('a');
        if (w && vis(w)) { w.click(); return true; }
        img.click();
        return true;
      }
    }
  }
  return false;
}
"""

        for frame in self.browser_page.frames:
            try:
                if frame.evaluate(js_column):
                    return True
            except Exception:
                continue
        for frame in self.browser_page.frames:
            try:
                if frame.evaluate(js_fallback):
                    return True
            except Exception:
                continue

        for frame in self.browser_page.frames:
            try:
                row = frame.locator("tr").filter(has_text=re.compile(r"In\s+Progress", re.I)).last
                if row.count() == 0:
                    continue
                tds = row.locator("td")
                tc = tds.count()
                if tc < 4:
                    continue
                # Typical layout: … | Purpose | Update | Duplicate | Delete → Update is third from end.
                cell = tds.nth(tc - 3)
                cell.scroll_into_view_if_needed(timeout=8000)
                for part in ("a", "img"):
                    pl = cell.locator(part).first
                    if pl.count() > 0:
                        pl.click(timeout=15000)
                        return True
            except Exception:
                continue

        for frame in self.browser_page.frames:
            try:
                link = frame.get_by_role("link", name=re.compile(r"^\s*update\s*$", re.I))
                if link.count() > 0:
                    tr_h = frame.locator("tr", has_text=re.compile(r"in\s+progress", re.I))
                    if tr_h.count() > 0:
                        row = tr_h.first
                        cell = row.get_by_role("link", name=re.compile(r"update", re.I))
                        if cell.count() > 0:
                            cell.first.click(timeout=15000)
                            return True
            except Exception:
                continue
        return False

    def _crash_resume_catch_up_stop_phase(self, anchor: str) -> str:
        """First pass of crash resume stops before this phase. Maps complete-report preamble to the linear step before it."""
        if anchor == "complete_report_step2":
            return "step3_autofill"
        return anchor

    def _execute_resume_in_progress_and_continue(self, continue_from: str) -> None:
        """Open the saved in-progress report, re-run portal steps from Step 1 through the phase before the saved anchor, then continue the normal sequence from ``continue_from``."""
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        self._pump_ui_and_check_cancel()
        self._wait_for_expense_portal_logged_in(context="resume_after_crash")
        self._pump_ui_and_check_cancel()
        self._navigate_to_expenses_home_for_resume()
        self._pump_ui_and_check_cancel()
        self.set_status("Opening in-progress report (Update column → pencil, not Create Expense Report)…")
        self.log_event("browser", "Resume: click pencil in Update column for an In Progress row.")
        if not self._click_update_expense_reports_pencil_for_in_progress():
            raise RuntimeError(
                "Could not click the Update pencil for an In Progress report. "
                "Confirm you are on the expenses homepage with “Update Expense Reports”, then try again."
            )
        self.browser_page.wait_for_timeout(1200)

        anchor = continue_from.strip()
        if anchor in ("nic_iexpenses", "create_report"):
            anchor = "wait_step1"

        if anchor == "wait_step1":
            self.log_event("step", "Crash resume: full sequence from Step 1 (General Information).")
            self._execute_populate_from("wait_step1")
            return

        catch_up_stop = self._crash_resume_catch_up_stop_phase(anchor)
        self.set_status(
            f"Resume after crash: replaying portal steps up to your saved point, then continuing ({anchor})…"
        )
        self.log_event(
            "step",
            f"Crash resume: catch-up from wait_step1 (stop before {catch_up_stop}), then full flow from {anchor}.",
        )
        self._execute_populate_from("wait_step1", stop_before_phase=catch_up_stop)
        if anchor == "complete_report_step2":
            self._execute_populate_from("complete_report_step2")
        else:
            self._execute_populate_from(anchor)

    def _cdp_http_reachable(self, http_base: str, timeout: float = 0.45) -> bool:
        """Cheap check via DevTools HTTP — avoids browser.is_connected() which can block on a crashed browser."""
        try:
            version_url = http_base.rstrip("/") + "/json/version"
            with urllib.request.urlopen(version_url, timeout=timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _step3_pause_between_row_entries(self) -> None:
        ms = _step3_row_throttle_ms()
        if ms <= 0 or not self.browser_page:
            return
        self.browser_page.wait_for_timeout(ms)

    def _step3_pause_details_flow(self) -> None:
        ms = _step3_details_flow_throttle_ms()
        if ms <= 0 or not self.browser_page:
            return
        self.browser_page.wait_for_timeout(ms)

    def _chromium_interaction_gap(self) -> None:
        """Yield to the UI thread and pause briefly between Playwright/CDP calls (reduces Chromium load)."""
        if not self.browser_page:
            return
        self._pump_ui_and_check_cancel()
        ms = _chromium_breather_ms()
        if ms > 0:
            self.browser_page.wait_for_timeout(ms)

    def _step3_wait_main_ready_state_complete(self) -> None:
        """Light acknowledgement: main document reached ``complete`` (Oracle may still run AJAX in iframes)."""
        if not self.browser_page:
            return
        cap = _step3_ready_state_wait_cap_ms()
        if cap <= 0:
            return
        try:
            self.browser_page.wait_for_function(
                "() => document.readyState === 'complete'",
                timeout=min(cap, 12000),
            )
        except Exception:
            pass

    def _step3_after_playwright_mutation(self) -> None:
        """After a Step 3 DOM change: pump UI, breather, optional settle delay, then readyState check."""
        if not self.browser_page:
            return
        try:
            if self.browser_page.is_closed():
                return
        except Exception:
            return
        self._chromium_interaction_gap()
        extra = _step3_post_mutation_settle_ms()
        if extra > 0:
            self.browser_page.wait_for_timeout(extra)
        self._step3_wait_main_ready_state_complete()

    def _clear_cdp_session_after_disconnect(self, *, kill_proc: bool, reason: str = "") -> None:
        """Drop Playwright handles after Chromium died or CDP stopped responding (must run on main thread)."""
        br = self.browser
        self._cdp_unreachable_streak = 0
        self.browser_context = None
        self.browser_page = None
        self.browser = None
        self._cdp_http_url = None
        proc = self._chromium_proc
        if br is not None:
            try:
                br.close()
            except Exception:
                pass
        if kill_proc and proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._chromium_proc = None
        self._close_chromium_stderr_log()

    def _controlled_browser_usable(self) -> bool:
        """False if Chromium/CDP is gone or the page was closed. Avoids blocking WebSocket calls on a dead browser."""
        if not self.browser_page or not self.browser:
            return False
        proc = self._chromium_proc
        if proc is not None and proc.poll() is not None:
            self._clear_cdp_session_after_disconnect(kill_proc=False, reason="proc_exited")
            return False
        cdp = self._cdp_http_url
        if cdp:
            if not self._cdp_http_reachable(cdp, timeout=0.45):
                self._cdp_unreachable_streak += 1
                if self._cdp_unreachable_streak >= 3:
                    if self._step3_automation_active:
                        self._cdp_unreachable_streak = 0
                        try:
                            return not self.browser_page.is_closed()
                        except Exception:
                            self._clear_cdp_session_after_disconnect(
                                kill_proc=True, reason="page_is_closed_exception"
                            )
                            return False
                    self._clear_cdp_session_after_disconnect(kill_proc=True, reason="cdp_unreachable")
                    return False
            else:
                self._cdp_unreachable_streak = 0
        try:
            return not self.browser_page.is_closed()
        except Exception:
            self._clear_cdp_session_after_disconnect(kill_proc=True, reason="page_is_closed_exception")
            return False

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    def _wait_cdp_http_ready(self, http_base: str, timeout: float = 45.0, proc: subprocess.Popen | None = None) -> None:
        version_url = http_base.rstrip("/") + "/json/version"
        deadline = time.monotonic() + timeout
        last_exc: BaseException | None = None
        while time.monotonic() < deadline:
            # Fail fast if the Chromium process crashed
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"Chromium process exited with code {proc.returncode} "
                    f"before CDP became ready at {http_base}"
                )
            try:
                with urllib.request.urlopen(version_url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
            time.sleep(0.2)
        raise RuntimeError(f"Chromium CDP did not become ready at {http_base} ({last_exc})")

    def _spawn_chromium_cdp_subprocess(self) -> tuple[str, subprocess.Popen[bytes]]:
        CHROMIUM_USER_DATA.mkdir(parents=True, exist_ok=True)
        # Clean up stale profile locks from crashed instances
        self._cleanup_stale_chromium_profile()
        port = self._find_free_port()
        http_base = f"http://127.0.0.1:{port}"
        if not self.playwright:
            self.playwright = sync_playwright().start()
        exe = ensure_chromium_executable(self.playwright.chromium.executable_path)
        chromium_log = (os.environ.get("AUTOMATED_EXPENSES_CHROMIUM_LOG") or "").strip()
        stderr_dest = subprocess.DEVNULL
        log_fp = None
        if chromium_log:
            try:
                log_fp = open(chromium_log, "ab", buffering=0)
                stderr_dest = log_fp
            except OSError:
                log_fp = None

        args = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={CHROMIUM_USER_DATA}",
            "--no-first-run",
            "--no-default-browser-check",
            # Avoid macOS blocking on "Chromium Safe Storage" keychain prompts (automation cannot click them).
            "--use-mock-keychain",
            # Throttle/backgrounding can make automation flaky.
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            # Fewer moving parts than extension background pages (debug runs still saw CDP loss either way).
            "--disable-extensions",
            *(
                ()
                if (os.environ.get("AUTOMATED_EXPENSES_CHROMIUM_ENABLE_GPU", "").strip() in ("1", "true", "yes"))
                else ("--disable-gpu",)
            ),
            "about:blank",
        ]
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=stderr_dest,
            )
        except Exception:
            if log_fp is not None:
                log_fp.close()
            raise
        if log_fp is not None:
            self._chromium_stderr_log_fp = log_fp
        self._wait_cdp_http_ready(http_base, proc=proc)
        return http_base, proc

    def _cleanup_stale_chromium_profile(self) -> None:
        """Remove stale lock files and kill zombie Chromium processes using our profile."""
        import signal as _signal

        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock = CHROMIUM_USER_DATA / lock_name
            if not lock.exists():
                continue
            pid = self._pid_from_singleton_lock(lock)
            if pid is not None and self._process_alive(pid):
                try:
                    os.kill(pid, _signal.SIGTERM)
                    for _ in range(20):
                        if not self._process_alive(pid):
                            break
                        time.sleep(0.1)
                    else:
                        os.kill(pid, _signal.SIGKILL)
                except OSError:
                    pass
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _pid_from_singleton_lock(lock: Path) -> int | None:
        try:
            target = os.readlink(lock)
            parts = target.rsplit("-", 1)
            if len(parts) == 2:
                return int(parts[1])
        except (OSError, ValueError):
            pass
        return None

    @staticmethod
    def _process_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _close_other_pages(self, keep: Page) -> None:
        """Leave a single tab; session restore / reconnect can leave many pages open."""
        ctx = self.browser_context
        if ctx is None:
            return
        for p in list(ctx.pages):
            if p != keep:
                try:
                    if not p.is_closed():
                        p.close()
                except Exception:
                    pass

    def _attach_playwright_to_cdp(self, http_base: str, target_url: str) -> None:
        if not self.playwright:
            self.playwright = sync_playwright().start()
        self._emit_automation_event(
            kind="browser.attach.start",
            message="Attaching Playwright to Chromium CDP.",
            phase="OracleScraping",
            data={"http_base": http_base},
        )

        def _connect() -> Browser:
            assert self.playwright is not None
            return self.playwright.chromium.connect_over_cdp(http_base, slow_mo=0, is_local=True)

        self.browser = execute_with_retry(
            _connect,
            policy=RetryPolicy(max_attempts=4, initial_backoff_s=0.5, max_backoff_s=3.0),
            transient_predicate=is_transient_error,
            on_retry=lambda attempt, exc: self.set_status(
                f"Browser connection unstable — recovering session (attempt {attempt + 1}/4): {exc}"
            ),
        )
        contexts = self.browser.contexts
        if contexts:
            self.browser_context = contexts[0]
        else:
            self.browser_context = self.browser.new_context()
        pages = self.browser_context.pages
        if pages:
            self.browser_page = pages[0]
        else:
            self.browser_page = self.browser_context.new_page()
        self._close_other_pages(self.browser_page)

        def _goto() -> None:
            assert self.browser_page is not None
            self.browser_page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

        execute_with_retry(
            _goto,
            policy=RetryPolicy(max_attempts=3, initial_backoff_s=0.5, max_backoff_s=2.5),
            transient_predicate=is_transient_error,
            on_retry=lambda attempt, exc: self.set_status(
                f"Retrying Oracle page load (attempt {attempt + 1}/3): {exc}"
            ),
        )
        self._cdp_http_url = http_base
        self._progress_browser_had_live_link = True
        self._progress_browser_released_by_user = False
        self._emit_automation_event(
            kind="browser.attach.ok",
            message="Playwright attached and page loaded.",
            phase="OracleScraping",
            data={"target_url": target_url},
        )

    def open_controlled_browser(self, url: str) -> None:
        if self._chromium_proc is not None and self._chromium_proc.poll() is not None:
            self._chromium_proc = None
            self._cdp_http_url = None

        if self._controlled_browser_usable():
            self._close_other_pages(self.browser_page)
            self.browser_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return

        if (
            self._cdp_http_url
            and self._chromium_proc is not None
            and self._chromium_proc.poll() is None
            and not self.browser
        ):
            self._attach_playwright_to_cdp(self._cdp_http_url, url)
            return

        self.close_controlled_browser()

        http_base, proc = self._spawn_chromium_cdp_subprocess()
        self._chromium_proc = proc
        self._attach_playwright_to_cdp(http_base, url)

    def _close_chromium_stderr_log(self) -> None:
        fp = self._chromium_stderr_log_fp
        if fp is None:
            return
        self._chromium_stderr_log_fp = None
        try:
            close = getattr(fp, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    def close_controlled_browser(self) -> None:
        self._progress_browser_had_live_link = False
        self._progress_browser_released_by_user = False
        br = self.browser
        pw = self.playwright
        proc = self._chromium_proc
        self.browser_context = None
        self.browser_page = None
        self.browser = None
        self._chromium_proc = None
        self._cdp_http_url = None
        if br is not None:
            try:
                br.close()
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._close_chromium_stderr_log()
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        self.playwright = None

    def on_close(self) -> None:
        self._preview_cancel_redraw_timers()
        if self._workflow_poll_after_id is not None:
            try:
                self.root.after_cancel(self._workflow_poll_after_id)
            except tk.TclError:
                pass
            self._workflow_poll_after_id = None
        tid_layout = getattr(self, "_ui_layout_save_after_id", None)
        if tid_layout is not None:
            try:
                self.root.after_cancel(tid_layout)
            except tk.TclError:
                pass
            self._ui_layout_save_after_id = None
        try:
            self._save_ui_layout_now()
            self._persist_runtime_state()
            self._persist_vendor_expense_cache()
        except Exception:
            pass
        try:
            self.close_controlled_browser()
        finally:
            self.root.destroy()

    def _schedule_log_event(self, category: str, message: str) -> None:
        """Marshal log lines onto the Tk main thread (e.g. from background LLM worker)."""
        self.root.after(0, lambda c=category, m=message: self.log_event(c, m))

    def on_step_populate_expense_report(self) -> None:
        if not self.browser_page:
            self.set_status("Step 3 blocked: open Step 2 first (controlled browser required).")
            return
        if not self.receipt_paths:
            self.set_status("Step 3 blocked: no imported receipts yet. Run Step 1 first.")
            return
        if self._step3_automation_active:
            self.set_status("Step 3 is already running — use Stop automation or wait for it to finish.")
            return
        self._step3_vpn_mode = "standard"
        self.set_status("Step 3: populating expense report from imported receipts...")
        self._run_populate_expense_report_flow()

    def on_vpn_collect_llm_prompts(self) -> None:
        if not self.browser_page:
            self.set_status("VPN collect blocked: open Step 2 first (controlled browser required).")
            return
        if self._step3_automation_active:
            self.set_status("Stop the running automation before starting VPN collect.")
            return
        self._step3_vpn_mode = "vpn_collect"
        self._skip_match_vpn_prompt_until = 0.0
        self.set_status(
            "VPN: full portal flow through Step 2 (2 of 6); scrapes card table + pages, then VPN off for matching."
        )
        self._run_populate_expense_report_flow()

    def on_expense_report_launch_browser_and_scrape(self) -> None:
        """Open Chromium if needed, then run the VPN-on scrape through Step 2 (credit card lines)."""
        if self._step3_automation_active:
            self.set_status("Stop the running automation before starting a new scrape.")
            return
        if not self._controlled_browser_usable():
            self.on_step_login()
        if not self.browser_page:
            self.set_status(
                "Expense scrape cancelled: Chromium not connected — use Activity → Open Oracle, then retry."
            )
            return
        self._step3_vpn_mode = "vpn_collect"
        self._skip_match_vpn_prompt_until = 0.0
        self.set_status(
            "VPN on: navigating the portal and scraping credit card line items into the table…"
        )
        self._run_populate_expense_report_flow()

    def _spawn_expense_type_llm_resolver(
        self,
        path: Path,
        pending_ids: list[str],
        api_key: str,
        *,
        on_success: Callable[[int], None],
    ) -> None:
        """Background thread: resolve pending expense_type queries via OpenAI; UI callbacks on main thread."""
        if self._llm_resolve_worker_active:
            self.set_status("LLM cache resolution is already running.")
            return

        def worker() -> None:
            total = len(pending_ids)
            try:
                doc = load_document(path)
                for i, qid in enumerate(pending_ids, start=1):
                    block = doc.get("queries", {}).get(qid)
                    if not isinstance(block, dict) or str(block.get("kind")) != "expense_type":
                        continue
                    payload = block.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    merchant = str(payload.get("merchant_name", "")).strip()
                    options = payload.get("options")
                    if not merchant or not isinstance(options, list):
                        continue
                    opt_list = [str(o).strip() for o in options if str(o).strip()]
                    if not opt_list:
                        continue
                    self._schedule_log_event(
                        "llm",
                        f"Resolving cache {i}/{total}: {merchant[:48]}…",
                    )
                    chosen = self._choose_expense_type_with_llm(
                        api_key,
                        merchant,
                        opt_list,
                        log_event_fn=self._schedule_log_event,
                    )
                    set_response_expense_type(doc, qid, chosen)
                    save_document(path, doc)
                    ck = _normalize_vendor_key(merchant)
                    self.vendor_expense_cache[ck] = chosen
                    self._persist_vendor_expense_cache()

                def _done_ok() -> None:
                    self._llm_resolve_worker_active = False
                    self.refresh_all_tabs()
                    self._emit_automation_event(
                        kind="classification.complete",
                        message="Expense type resolution finished.",
                        phase="Classification",
                        data={"resolved_count": total},
                    )
                    on_success(total)

                self.root.after(0, _done_ok)
            except Exception as exc:
                def _done_err() -> None:
                    self._llm_resolve_worker_active = False
                    self.refresh_all_tabs()
                    self.set_status(f"LLM cache resolution failed: {exc}")
                    self._emit_automation_event(
                        kind="classification.failed",
                        message="Expense type resolution failed.",
                        phase="Classification",
                        data={"error": str(exc)},
                    )

                self.root.after(0, _done_err)

        self._llm_resolve_worker_active = True
        self._emit_automation_event(
            kind="classification.start",
            message="Expense type resolution started.",
            phase="Classification",
            data={"pending_count": len(pending_ids)},
        )
        self.set_status(f"Resolving {len(pending_ids)} cached LLM prompt(s) (VPN should be off)...")
        threading.Thread(target=worker, daemon=True).start()

    def _on_llm_resolve_complete_button(self, total: int) -> None:
        path = llm_pending_file(APP_DIR)
        self.set_status(
            f"LLM cache resolved: {total} prompt(s); file {path}. "
            "Reconnect VPN, then use Create report on the Expense report tab to fill the portal and attach receipts."
        )

    def _continue_after_vpn_collect_scrape(self) -> None:
        """After Step 2 scrape on VPN: confirm VPN off, then user runs receipt matching (OpenAI)."""
        api_key = self.get_openai_key().strip()
        if not api_key:
            self.set_status(
                "OpenAI key required — set it in Settings, then Match lines (VPN off) to pair receipts with card lines."
            )
            return
        self._skip_match_vpn_prompt_until = time.monotonic() + 600.0
        self.set_status(
            "Scraped card lines saved. Activity → Match lines (VPN off), then Expense report → Create report (VPN on)."
        )

    def _finish_vpn_collect_after_step2(self, line_count: int) -> None:
        """Step 2 (2 of 6) fully paged and scraped; keep browser open; prompt VPN off before matching."""
        self.log_event(
            "step",
            f"VPN collect: Step 2 of 6 complete — {line_count} card line(s) scraped (all table pages).",
        )
        self.log_event(
            "step",
            f"Step 2 scrape complete — {line_count} line(s) in cache. "
            f"Wizard Cancel + OK on discard prompt; Chromium left open. Match lines with VPN off when ready.",
        )
        self.refresh_all_tabs()
        self._continue_after_vpn_collect_scrape()

    def on_vpn_resolve_llm_cache(self) -> None:
        if self._step3_automation_active:
            self.set_status("Stop Step 3 browser automation before resolving the LLM cache.")
            return
        if self._llm_resolve_worker_active:
            self.set_status("LLM cache resolution is already running.")
            return
        api_key = self.get_openai_key().strip()
        if not api_key:
            self.set_status("Resolve types blocked: set OpenAI API key in Settings first.")
            return
        path = llm_pending_file(APP_DIR)
        doc = load_document(path)
        pending = pending_expense_type_ids(doc)
        if not pending:
            self.set_status(
                f"No pending expense-type prompts in {path}. "
                "Scrape Step 2 feeds Match lines; Resolve types is for queued llm_query prompts."
            )
            return

        self._spawn_expense_type_llm_resolver(
            path, pending, api_key, on_success=self._on_llm_resolve_complete_button
        )

    def _prompt_disconnect_vpn_for_openai(self) -> bool:
        """Previously a modal VPN reminder; always proceed (status bar carries operational hints)."""
        return True

    def _expense_types_resolve_for_line_list_in_worker(
        self, line_list: list[dict], api_key: str
    ) -> int:
        """
        Worker thread: for each unique merchant on lines without a vendor-cache expense type,
        call the LLM (portal option list) and persist. Refreshes report / Expense types tabs on UI thread.
        """
        opts = list(get_expense_type_options())
        cache = dict(self._load_vendor_expense_cache())
        # Fast path: deterministic/user-memory classification suggestions before spending LLM calls.
        classifications = classify_transactions(line_list, user_memory=cache)
        by_txn_id = {
            str(row.get("transaction_id", "") or "").strip(): row
            for row in classifications
            if isinstance(row, dict)
        }
        seen: set[str] = set()
        need: list[tuple[str, str]] = []
        resolved_without_llm = 0
        for line in line_list:
            lid = str(line.get("line_id", "") or "").strip()
            m = str(line.get("merchant_name", "") or "").strip()
            vk = _normalize_vendor_key(m)
            if not vk or vk in seen:
                continue
            seen.add(vk)
            if str(cache.get(vk, "") or "").strip():
                continue
            suggestion = by_txn_id.get(lid) or {}
            source = str(suggestion.get("source", "") or "").strip().lower()
            suggested_type = str(suggestion.get("type", "") or "").strip()
            matched_type = _match_label_to_options(suggested_type, opts) if suggested_type else ""
            if source in {"user", "rule"} and matched_type:
                cache[vk] = matched_type
                resolved_without_llm += 1
                self._schedule_log_event(
                    "cache",
                    f'Expense types: "{m}" -> "{matched_type}" ({source} suggestion; no LLM call).',
                )
                continue
            need.append((vk, m))
        if not need:
            self._schedule_log_event(
                "llm",
                "Expense types: all merchants resolved via cache/rules — skipping LLM.",
            )
            if resolved_without_llm:
                self._schedule_log_event(
                    "step",
                    f"Expense types: resolved {resolved_without_llm} merchant(s) without LLM.",
                )
            self.root.after(0, self.refresh_expense_report_tab)
            self.root.after(0, self.refresh_expense_types_tab)
            return resolved_without_llm
        self._schedule_log_event(
            "llm",
            f"Expense types: asking LLM for {len(need)} new merchant(s) (portal category list).",
        )
        n = len(need)
        for idx, (vk, m) in enumerate(need, start=1):

            def busy(i=idx, total=n, key=vk):
                self.set_busy_status(f"Expense types (Analyze): {i}/{total} — {key}…")

            self.root.after(0, busy)
            chosen = self._choose_expense_type_with_llm(api_key=api_key, merchant_name=m, options=opts)
            cache[vk] = chosen

        try:
            VENDOR_EXPENSE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            VENDOR_EXPENSE_CACHE_FILE.write_text(
                json.dumps({"vendors": dict(sorted(cache.items()))}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        self.root.after(0, lambda s=dict(cache): setattr(self, "vendor_expense_cache", dict(s)))

        def after_types() -> None:
            self.refresh_expense_report_tab()
            self.refresh_expense_types_tab()

        self.root.after(0, after_types)
        return resolved_without_llm + n

    def _expense_line_missing_receipt_file(
        self,
        line: dict,
        *,
        llm_matches: dict[str, dict],
        approved_prev: dict[str, dict],
    ) -> bool:
        """True when the line has no usable receipt path on disk (same rules as the Expense report grid)."""
        lid = str(line.get("line_id", "") or "").strip()
        if not lid:
            return False
        block = dict(llm_matches.get(lid) or {})
        has_saved_match = lid in llm_matches
        if not str(block.get("best_receipt") or "").strip() and not has_saved_match:
            cbr = str(line.get("cached_best_receipt") or "").strip()
            if cbr:
                block["best_receipt"] = cbr
                if block.get("confidence") is None or str(block.get("confidence")).strip() == "":
                    block["confidence"] = line.get("cached_match_confidence", "")
                if not str(block.get("reason") or "").strip():
                    block["reason"] = str(line.get("cached_match_reason") or "")
        best = str(block.get("best_receipt") or "").strip()
        prev = approved_prev.get(lid)
        if prev and str(prev.get("source_file") or "").strip() and lid not in llm_matches:
            rpath = str(prev.get("source_file")).strip()
        else:
            rpath = best
        if not rpath:
            return True
        return not Path(rpath).expanduser().is_file()

    def on_match_receipts_off_vpn(
        self,
        *,
        skip_vpn_prompt: bool = False,
        also_resolve_expense_types: bool = False,
        only_without_receipt_file: bool = False,
        only_line_ids: frozenset[str] | None = None,
    ) -> None:
        if self._step3_automation_active:
            self.set_status("Stop Step 3 browser automation before matching receipts.")
            return
        if self._match_receipts_worker_active:
            self.set_status("Receipt matching is already running.")
            return
        api_key = self.get_openai_key().strip()
        if not api_key:
            self.set_status("Match lines blocked: set OpenAI API key in Settings first.")
            return
        ok, err = validate_lines_cache_for_match(APP_DIR)
        if not ok:
            self.set_status(f"Match lines blocked: {err}")
            return
        lines, _ = load_expense_lines_cache(APP_DIR)
        analyses_src = self.analyses if self.analyses else load_analyses_snapshot(APP_DIR)
        if not analyses_src:
            self.set_status(
                f"Match lines blocked: add receipts on Documents (VPN off) or ensure "
                f"{receipt_analyses_snapshot_path(APP_DIR)} exists."
            )
            return
        if not skip_vpn_prompt:
            now = time.monotonic()
            skip_vpn = now < self._skip_match_vpn_prompt_until
            if skip_vpn:
                self._skip_match_vpn_prompt_until = 0.0
            if not skip_vpn and not self._prompt_disconnect_vpn_for_openai():
                return

        if only_line_ids is not None:
            want = {str(x).strip() for x in only_line_ids if str(x).strip()}
            if not want:
                self.set_status("Analyze selected blocked: select one or more expense lines first.")
                return
            line_list_full_pre = [ln for ln in lines if str(ln.get("line_id", "") or "").strip()]
            matching = [
                ln
                for ln in line_list_full_pre
                if str(ln.get("line_id", "") or "").strip() in want
            ]
            if not matching:
                self.set_status(
                    "Analyze selected blocked: selected lines are not in the scraped expense cache."
                )
                return

        if only_without_receipt_file:
            existing_m = load_receipt_line_matches(APP_DIR)
            approved_prev_pre = load_approved_matches(APP_DIR)
            line_list_full_pre = [ln for ln in lines if str(ln.get("line_id", "") or "").strip()]
            if not any(
                self._expense_line_missing_receipt_file(
                    ln, llm_matches=existing_m, approved_prev=approved_prev_pre
                )
                for ln in line_list_full_pre
            ):
                self.set_status("No lines without a receipt file — nothing to re-analyze.")
                return

        def worker() -> None:
            try:
                snap = save_analyses_snapshot(APP_DIR, list(analyses_src))
                self._schedule_log_event(
                    "step",
                    f"Receipt analyses snapshot: {snap} ({len(analyses_src)} item(s)).",
                )

                line_list_full = [ln for ln in lines if str(ln.get("line_id", "") or "").strip()]
                existing_m = load_receipt_line_matches(APP_DIR)
                approved_prev = load_approved_matches(APP_DIR)
                if only_line_ids is not None:
                    want = {str(x).strip() for x in only_line_ids if str(x).strip()}
                    accumulated = dict(existing_m)
                    line_list = [
                        ln
                        for ln in line_list_full
                        if str(ln.get("line_id", "") or "").strip() in want
                    ]
                    if not line_list:

                        def _early_empty_sel() -> None:
                            self._match_receipts_worker_active = False
                            self.set_busy_status("Ready.")
                            self.set_status(
                                "Analyze selected blocked: selected lines are not in the scraped expense cache."
                            )

                        self.root.after(0, _early_empty_sel)
                        return
                elif only_without_receipt_file:
                    accumulated = dict(existing_m)
                    line_list = [
                        ln
                        for ln in line_list_full
                        if self._expense_line_missing_receipt_file(
                            ln, llm_matches=existing_m, approved_prev=approved_prev
                        )
                    ]
                    if not line_list:

                        def _early_empty() -> None:
                            self._match_receipts_worker_active = False
                            self.set_busy_status("Ready.")
                            self.set_status("No lines without a receipt file — nothing to re-analyze.")

                        self.root.after(0, _early_empty)
                        return
                else:
                    accumulated = {}
                    line_list = line_list_full

                n_total = len(line_list)
                out_path = receipt_line_match_path(APP_DIR)
                deterministic_rows = match_transactions_to_receipts(
                    line_list,
                    analyses_src,
                    amount_tolerance=0.5,
                    date_window_days=3,
                )
                deterministic_by_line = {
                    str(row.get("transaction_id", "") or "").strip(): row
                    for row in deterministic_rows
                    if isinstance(row, dict)
                }
                deterministic_auto_count = 0

                def _status(msg: str) -> None:
                    self._schedule_log_event("llm", msg)

                for idx, line in enumerate(line_list, start=1):
                    lid = str(line.get("line_id", "") or "").strip()

                    def schedule_busy(i=idx, n=n_total, id_=lid) -> None:
                        self.set_busy_status(f"Matching receipts: line {i}/{n} ({id_})…")

                    self.root.after(0, schedule_busy)

                    seed = deterministic_by_line.get(lid) or {}
                    seed_receipt = str(seed.get("receipt_id", "") or "").strip()
                    try:
                        seed_conf = float(seed.get("confidence") or 0.0)
                    except (TypeError, ValueError):
                        seed_conf = 0.0
                    if seed_receipt and seed_conf >= 0.90 and Path(seed_receipt).expanduser().is_file():
                        deterministic_auto_count += 1
                        result = {
                            "best_receipt": seed_receipt,
                            "confidence": seed_conf,
                            "reason": (
                                "Deterministic high-confidence match "
                                "(amount/date/merchant) accepted without LLM."
                            ),
                        }
                        self._schedule_log_event(
                            "cache",
                            f"Line {lid}: deterministic high-confidence match (no LLM call).",
                        )
                    else:
                        result = match_one_expense_line_to_receipts(
                            api_key=api_key,
                            model=self.settings.openai_model,
                            line=line,
                            analyses=analyses_src,
                            http_verify_preferred=self.settings.openai_http_verify,
                            on_status=_status,
                        )
                    accumulated[lid] = result
                    out_path = save_receipt_line_matches(APP_DIR, accumulated)

                    def schedule_invalidate() -> None:
                        self._invalidate_receipt_table_match_cache()

                    self.root.after(0, schedule_invalidate)

                    def schedule_refresh() -> None:
                        self.refresh_expense_report_tab()

                    self.root.after(0, schedule_refresh)

                n_matched = sum(
                    1 for v in accumulated.values() if str(v.get("best_receipt") or "").strip()
                )

                n_expense_new = 0
                if also_resolve_expense_types:
                    n_expense_new = self._expense_types_resolve_for_line_list_in_worker(
                        line_list, api_key
                    )

                def _done_ok() -> None:
                    self._match_receipts_worker_active = False
                    self._emit_automation_event(
                        kind="matching.complete",
                        message="Transaction-to-receipt matching completed.",
                        phase="Matching",
                        data={
                            "matched_with_receipt": n_matched,
                            "total_records": len(accumulated),
                            "deterministic_auto_count": deterministic_auto_count,
                        },
                    )
                    try:
                        persist_expense_line_derived_fields(
                            APP_DIR,
                            load_receipt_line_matches(APP_DIR),
                            self._load_vendor_expense_cache(),
                        )
                    except Exception:
                        pass
                    try:
                        self._persist_runtime_state()
                    except Exception:
                        pass
                    self.refresh_all_tabs()
                    self.set_busy_status("Ready.")
                    if only_line_ids is not None:
                        n_sel = sum(
                            1
                            for ln in line_list
                            if str(
                                (accumulated.get(str(ln.get("line_id", "") or "").strip()) or {}).get(
                                    "best_receipt"
                                )
                                or ""
                            ).strip()
                        )
                        base = (
                            f"Selected line(s) analyzed: {len(line_list)} line(s); "
                            f"{n_sel} got a suggested receipt → {out_path}."
                        )
                    elif only_without_receipt_file:
                        base = (
                            f"Unmatched re-analysis done: {len(line_list)} line(s) re-matched; "
                            f"{n_matched} of {len(accumulated)} in match file have a suggested receipt → {out_path}."
                        )
                    else:
                        base = (
                            f"Receipt matching done: {n_matched} line(s) with a file, {len(accumulated)} total → {out_path}."
                        )
                    if also_resolve_expense_types:
                        if n_expense_new:
                            base += (
                                f" Expense types: resolved {n_expense_new} new merchant(s); "
                                f"cache {VENDOR_EXPENSE_CACHE_FILE}."
                            )
                        else:
                            base += " Expense types: all merchants were already in the vendor cache."
                    if deterministic_auto_count:
                        base += f" Deterministic matcher auto-filled {deterministic_auto_count} line(s) without LLM."
                    base += " Expense report: review table, then Create report (VPN on)."
                    self.set_status(base)

                self.root.after(0, _done_ok)
            except Exception as exc:
                def _done_err() -> None:
                    self._match_receipts_worker_active = False
                    self.set_busy_status("Ready.")
                    self.set_status(f"Receipt matching failed: {exc}")
                    self._emit_automation_event(
                        kind="matching.failed",
                        message="Transaction-to-receipt matching failed.",
                        phase="Matching",
                        data={"error": str(exc)},
                    )

                self.root.after(0, _done_err)

        self._match_receipts_worker_active = True
        self._emit_automation_event(
            kind="matching.start",
            message="Transaction-to-receipt matching started.",
            phase="Matching",
            data={
                "only_without_receipt_file": bool(only_without_receipt_file),
                "only_line_ids": len(only_line_ids or []),
                "also_resolve_expense_types": bool(also_resolve_expense_types),
            },
        )
        if only_line_ids is not None:
            self.set_status(
                "Matching receipts for selected line(s) (one API call each; VPN should be off)…"
            )
        elif only_without_receipt_file:
            self.set_status(
                "Matching receipts for lines without a file on disk (one API call each; VPN should be off)…"
            )
        else:
            self.set_status(
                "Matching receipts to scraped lines (one API call per line; VPN should be off)…"
            )
        threading.Thread(target=worker, daemon=True).start()

    def on_expense_report_analyze_line_items(self) -> None:
        """
        Match receipt analyses to scraped lines, then fill Expense type column from vendor cache
        or LLM (new merchants are saved to the Expense types tab cache). VPN off for API access.
        """
        self.on_match_receipts_off_vpn(skip_vpn_prompt=True, also_resolve_expense_types=True)

    def on_expense_report_analyze_unmatched_line_items(self) -> None:
        """Re-run receipt matching only for lines with no receipt file on disk; then resolve expense types for those lines."""
        self.on_match_receipts_off_vpn(
            skip_vpn_prompt=True,
            also_resolve_expense_types=True,
            only_without_receipt_file=True,
        )

    def on_expense_report_match_selected_lines(self) -> None:
        """LLM match: which receipt best fits each selected scraped line (does not re-parse images)."""
        if not hasattr(self, "expense_report_tree"):
            return
        sel = list(self.expense_report_tree.selection())
        if not sel:
            self.set_status("Analyze selected blocked: select one or more expense lines first.")
            return
        ids = frozenset(str(x).strip() for x in sel if str(x).strip())
        self.on_match_receipts_off_vpn(
            skip_vpn_prompt=True,
            also_resolve_expense_types=True,
            only_line_ids=ids,
        )

    def open_match_review_dialog(self) -> None:
        """Backward-compatible entry point: opens the Expense report tab."""
        ok, err = validate_lines_cache_for_match(APP_DIR)
        if not ok:
            self.set_status(f"Match review blocked: {err}")
            return
        if not load_receipt_line_matches(APP_DIR):
            self.set_status(
                f"No LLM matches in {receipt_line_match_path(APP_DIR)} — run Match lines on the Activity tab first."
            )
        self.focus_expense_report_tab()

    def _prepare_complete_report_llm_mode(self) -> bool:
        """Configure vpn_replay vs standard Step 3 fill. Returns False if neither cache nor API key is usable."""
        api_key = self.get_openai_key().strip()
        path = llm_pending_file(APP_DIR)
        doc = load_document(path)
        ok_replay, replay_err = validate_replay_ready(doc)
        if ok_replay:
            self._llm_replay_document = doc
            self._step3_vpn_mode = "vpn_replay"
            return True
        if api_key:
            self._llm_replay_document = None
            self._step3_vpn_mode = "standard"
            return True
        self.set_status(
            f"Complete report blocked (expense types): {replay_err} "
            "— add OpenAI key in Settings or Resolve types (VPN off) for llm_query_pending.json."
        )
        return False

    def on_complete_report(self) -> None:
        """VPN on: Step 2 table → first page; Next to Step 3; fill types; fix line errors; Step 6 uploads."""
        if not self.browser_page:
            self.set_status("Complete report blocked: open Step 2 first (controlled browser required).")
            return
        if self._step3_automation_active:
            self.set_status("Stop the running automation before Complete report.")
            return
        if not self.receipt_paths and not load_analyses_snapshot(APP_DIR):
            self.set_status(
                "Complete report blocked: import receipts (VPN off) or run matching once (analyses snapshot)."
            )
            return
        ok_m, err_m = validate_approved_for_attach(APP_DIR)
        if not ok_m:
            self.set_status(f"Complete report blocked (approvals): {err_m}")
            return
        if not self._prepare_complete_report_llm_mode():
            return
        self._run_step6_file_attach = True
        self.set_status(
            "Complete report (VPN on): Step 2 → Step 3 categorization → currency fixes → Step 6 attachments…"
        )
        self._run_populate_expense_report_flow(start_from="complete_report_step2")

    def _get_openai_client(self, api_key: str):
        if self._openai_client is None:
            self._openai_client = build_openai_client(
                api_key, http_verify_preferred=self.settings.openai_http_verify
            )
        return self._openai_client

    def _extract_step3_rows_in_any_frame(self) -> tuple[Frame | None, list[dict]]:
        if not self.browser_page:
            return None, []

        for frame in self.browser_page.frames:
            try:
                rows = frame.evaluate(
                    """
() => {
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const normalize = (value) => clean(value).toLowerCase();
  const rowData = [];
  const tables = Array.from(document.querySelectorAll('table'));

  tables.forEach((table, tableIndex) => {
    const headerRow = table.querySelector('tr');
    if (!headerRow) return;
    const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
    const headerTexts = headerCells.map((cell) => normalize(cell.textContent || ''));
    const merchantIdx = headerTexts.findIndex((txt) => txt.includes('merchant'));
    const expenseIdx = headerTexts.findIndex((txt) => txt.includes('expense type'));
    const justificationIdx = headerTexts.findIndex((txt) => txt.includes('justification'));
    if (merchantIdx < 0 || expenseIdx < 0 || justificationIdx < 0) return;

    const dateIdx = headerTexts.findIndex((txt) => txt.includes('date') && !txt.includes('update'));
    let receiptAmtIdx = headerTexts.findIndex(
      (txt) => txt.includes('receipt amount') || txt.includes('receipt amt')
    );
    if (receiptAmtIdx < 0) {
      receiptAmtIdx = headerTexts.findIndex((txt) => txt.includes('receipt') && txt.includes('amount'));
    }
    const receiptCurIdx = headerTexts.findIndex((txt) => txt.includes('receipt currency'));
    const detailsIdx = headerTexts.findIndex(
      (txt) => txt === 'details' || (txt.includes('detail') && !txt.includes('expense'))
    );

    let lineIdx = headerTexts.findIndex((txt) => {
      if (txt === 'line' || txt === 'line #' || txt === 'ln') return true;
      if (txt.includes('airline') || txt.includes('deadline')) return false;
      return /^line\\b/.test(txt);
    });

      const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
    if (lineIdx < 0 && bodyRows.length) {
      const cells0 = Array.from(bodyRows[0].querySelectorAll('td'));
      for (let j = 0; j < Math.min(cells0.length, 3); j++) {
        const raw = (cells0[j].innerText || cells0[j].textContent || '').replace(/\\s+/g, ' ').trim();
        if (/^\\d{1,4}$/.test(raw)) {
          lineIdx = j;
          break;
        }
      }
    }

    bodyRows.forEach((tr, rowIndex) => {
      try {
        tr.scrollIntoView({ block: 'center', inline: 'nearest' });
      } catch (e) {}
      const cells = Array.from(tr.querySelectorAll('td'));
      if (!cells.length) return;

      const expenseCell = cells[expenseIdx];
      const merchantCell = cells[merchantIdx];
      const justificationCell = cells[justificationIdx];
      if (!expenseCell || !merchantCell || !justificationCell) return;

      const select = expenseCell.querySelector('select');
      const justInput = justificationCell.querySelector('input, textarea');
      if (!select || !justInput) return;

      const merchant = clean(merchantCell.innerText || merchantCell.textContent || '');
      const options = Array.from(select.options)
        .map((opt) => clean(opt.textContent || ''))
        .filter((txt) => txt && !/^select/i.test(txt));
      if (!merchant || !options.length) return;

      let transaction_date = '';
      if (dateIdx >= 0 && cells[dateIdx]) {
        const dc = cells[dateIdx];
        const dinp = dc.querySelector('input:not([type="hidden"])');
        transaction_date = clean(
          dinp && dinp.value ? dinp.value : dc.innerText || dc.textContent || ''
        );
      }
      let receipt_amount = '';
      if (receiptAmtIdx >= 0 && cells[receiptAmtIdx]) {
        receipt_amount = clean(cells[receiptAmtIdx].innerText || cells[receiptAmtIdx].textContent || '');
      }
      let receipt_currency = '';
      if (receiptCurIdx >= 0 && cells[receiptCurIdx]) {
        receipt_currency = clean(cells[receiptCurIdx].innerText || cells[receiptCurIdx].textContent || '');
      }

      let line_no = null;
      if (lineIdx >= 0 && cells[lineIdx]) {
        const lt = clean(cells[lineIdx].innerText || cells[lineIdx].textContent || '');
        const lm = lt.match(/(\\d+)/);
        if (lm) line_no = parseInt(lm[1], 10);
      }

      rowData.push({
        row_key: `${tableIndex}:${rowIndex}`,
        merchant_name: merchant,
        options,
        transaction_date,
        receipt_amount,
        receipt_currency,
        line_no,
        has_details_column: detailsIdx >= 0,
      });
    });
  });

  return rowData;
}
                    """
                )
                if rows:
                    return frame, rows
            except Exception:
                continue
        return None, []

    def _step3_nudge_step3_table_for_lazy_dropdowns(self, frame: Frame) -> None:
        """Scroll each Step 3 grid row into view so Oracle/ADF can attach <select> options."""
        try:
            frame.evaluate(
                """
() => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const tables = Array.from(document.querySelectorAll('table'));
  for (const table of tables) {
    const headerRow = table.querySelector('tr');
    if (!headerRow) continue;
    const headerTexts = Array.from(headerRow.querySelectorAll('th, td')).map((cell) =>
      normalize(cell.textContent || '')
    );
    if (!headerTexts.some((t) => t.includes('expense type'))) continue;
    const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
    for (const tr of bodyRows) {
      try {
        tr.scrollIntoView({ block: 'center', inline: 'nearest' });
      } catch (e) {}
    }
  }
  return true;
}
"""
            )
        except Exception:
            pass

    def _step3_enrich_row_for_cache_match(self, row: dict) -> None:
        """Fill currency / normalized amount from receipt amount cell when needed."""
        amt_raw = str(row.get("receipt_amount") or "").strip()
        if not str(row.get("receipt_currency") or "").strip() and amt_raw:
            amt_p, cur_p = _parse_amount_currency_token(amt_raw)
            if cur_p:
                row["receipt_currency"] = cur_p
            if amt_p:
                row["receipt_amount"] = amt_p

    def _step3_row_cache_signature(self, row: dict) -> tuple[str, str, str]:
        self._step3_enrich_row_for_cache_match(row)
        cur = normalize_currency_code(row.get("receipt_currency"))
        if not cur:
            cur = str(row.get("receipt_currency") or "").strip()
        return signature_from_cached_line(
            {
                "merchant_name": row.get("merchant_name", ""),
                "transaction_date": row.get("transaction_date", ""),
                "amount": str(row.get("receipt_amount", "") or ""),
                "currency": cur,
            }
        )

    def _step3_row_has_attachable_receipt(self, row: dict) -> bool | None:
        """True if a scraped cache line matches and has an on-disk best_receipt; False if matched but none; None if uncertain."""
        if not str(row.get("merchant_name") or "").strip():
            return None
        if not str(row.get("transaction_date") or "").strip() and not str(row.get("receipt_amount") or "").strip():
            return None
        sig = self._step3_row_cache_signature(row)
        if not sig[0]:
            return None
        lines, _ = load_expense_lines_cache(APP_DIR)
        matches = load_receipt_line_matches(APP_DIR)
        hits = [L for L in lines if signature_from_cached_line(L) == sig]
        if not hits:
            return None
        for L in hits:
            lid = str(L.get("line_id", "") or "").strip()
            br = str((matches.get(lid) or {}).get("best_receipt") or "").strip()
            if br and Path(br).expanduser().is_file():
                return True
        return False

    def _apply_step3_single_row_assignment(self, frame: Frame, row_key: str, expense_type: str) -> bool:
        if not row_key or not expense_type:
            return False
        try:
            ok = frame.evaluate(
                """
([rowKey, selectedLabel]) => {
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const normalize = (value) => clean(value).toLowerCase();
  const pickOption = (select, label) => {
    const want = normalize(label);
    const opts = Array.from(select.options);
    let opt = opts.find((o) => normalize(o.textContent || '') === want);
    if (opt) return opt;
    opt = opts.find((o) => {
      const ot = normalize(o.textContent || '');
      return ot && !/^select/i.test(ot) && (ot.includes(want) || want.includes(ot));
    });
    return opt || null;
  };
  const tables = Array.from(document.querySelectorAll('table'));

  for (let tableIndex = 0; tableIndex < tables.length; tableIndex++) {
    const table = tables[tableIndex];
    const headerRow = table.querySelector('tr');
    if (!headerRow) continue;
    const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
    const headerTexts = headerCells.map((cell) => normalize(cell.textContent || ''));
    const expenseIdx = headerTexts.findIndex((txt) => txt.includes('expense type'));
    const justificationIdx = headerTexts.findIndex((txt) => txt.includes('justification'));
    if (expenseIdx < 0 || justificationIdx < 0) continue;

    const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
    for (let rowIndex = 0; rowIndex < bodyRows.length; rowIndex++) {
      if (`${tableIndex}:${rowIndex}` !== String(rowKey)) continue;
      const tr = bodyRows[rowIndex];
      try {
        tr.scrollIntoView({ block: 'center', inline: 'nearest' });
      } catch (e) {}
      const cells = Array.from(tr.querySelectorAll('td'));
      const expenseCell = cells[expenseIdx];
      const justificationCell = cells[justificationIdx];
      if (!expenseCell || !justificationCell) return false;
      const select = expenseCell.querySelector('select');
      const justInput = justificationCell.querySelector('input, textarea');
      if (!select || !justInput) return false;
      const option = pickOption(select, selectedLabel);
      if (!option) return false;
      const appliedLabel = clean(option.textContent || '') || selectedLabel;
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      select.dispatchEvent(new Event('input', { bubbles: true }));
      select.dispatchEvent(new Event('blur', { bubbles: true }));
      justInput.focus();
      justInput.value = appliedLabel;
      justInput.dispatchEvent(new Event('input', { bubbles: true }));
      justInput.dispatchEvent(new Event('change', { bubbles: true }));
      justInput.dispatchEvent(new Event('blur', { bubbles: true }));
      return true;
    }
  }
  return false;
}
                """,
                [row_key, expense_type],
            )
            if ok:
                self._step3_after_playwright_mutation()
            return bool(ok)
        except Exception:
            return False

    def _step3_row_key_for_line_number(self, frame: Frame, line_no: int) -> str | None:
        """Return ``tableIndex:rowIndex`` for the row whose Line column matches ``line_no``."""
        if line_no < 1:
            return None
        try:
            key = frame.evaluate(
                """
(lineNo) => {
  const want = Number(lineNo);
  if (!want || want < 1) return null;
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();

  function lineColumnIndex(headerTexts) {
    let idx = headerTexts.findIndex((txt) => {
      if (txt === 'line' || txt === 'line #' || txt === 'ln') return true;
      if (txt.includes('airline') || txt.includes('deadline')) return false;
      return /^line\\b/.test(txt);
    });
    return idx;
  }

  const tables = Array.from(document.querySelectorAll('table'));
  for (let tableIndex = 0; tableIndex < tables.length; tableIndex++) {
    const table = tables[tableIndex];
    const headerRow = table.querySelector('tr');
    if (!headerRow) continue;
    const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
    const headerTexts = headerCells.map((cell) => normalize(cell.textContent || ''));
    const merchantIdx = headerTexts.findIndex((txt) => txt.includes('merchant'));
    const expenseIdx = headerTexts.findIndex((txt) => txt.includes('expense type'));
    if (merchantIdx < 0 || expenseIdx < 0) continue;

    let lineIdx = lineColumnIndex(headerTexts);
    const rows = Array.from(table.querySelectorAll('tr')).slice(1);
    if (lineIdx < 0 && rows.length) {
      const tr0 = rows[0];
      const cells0 = Array.from(tr0.querySelectorAll('td'));
      for (let j = 0; j < Math.min(cells0.length, 3); j++) {
        const raw = (cells0[j].innerText || cells0[j].textContent || '').replace(/\\s+/g, ' ').trim();
        if (/^\\d{1,4}$/.test(raw)) {
          lineIdx = j;
          break;
        }
      }
    }
    if (lineIdx < 0) continue;

    for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
      const tr = rows[rowIndex];
      const cells = Array.from(tr.querySelectorAll('td'));
      if (cells.length <= lineIdx) continue;
      const lineText = (cells[lineIdx].innerText || cells[lineIdx].textContent || '').replace(/\\s+/g, ' ').trim();
      const lm = lineText.match(/(\\d+)/);
      if (!lm) continue;
      if (parseInt(lm[1], 10) === want) {
        return `${tableIndex}:${rowIndex}`;
      }
    }
  }
  return null;
}
                """,
                line_no,
            )
            return str(key).strip() if key else None
        except Exception:
            return None

    def _step3_wait_for_line_detail_ready(self, timeout_ms: int = 16000) -> bool:
        """True when a line Details subpage is open (Return control visible)."""
        if not self.browser_page:
            return False
        ret = re.compile(r"^\s*Return\s*$", re.IGNORECASE)
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            for frame in self.browser_page.frames:
                try:
                    for role in ("button", "link"):
                        loc = frame.get_by_role(role, name=ret)
                        for i in range(loc.count()):
                            c = loc.nth(i)
                            try:
                                if c.is_visible() and c.is_enabled():
                                    return True
                            except Exception:
                                continue
                except Exception:
                    continue
            time.sleep(0.12)
        return False

    def _step3_shift_oracle_date_in_any_frame(self) -> bool:
        """On the open Details form, decrement the first visible Oracle-style date field by one day."""
        if not self.browser_page:
            return False
        js = """
() => {
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const monthMap = {
    jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
  };
  const monthNames = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ];

  function shiftOracleDateString(s) {
    const t = (s || '').trim();
    const m = t.match(/^(\\d{1,2})-([A-Za-z]{3})-(\\d{4})$/);
    if (!m) return null;
    const mon = monthMap[m[2].toLowerCase()];
    if (mon === undefined) return null;
    const d = new Date(parseInt(m[3], 10), mon, parseInt(m[1], 10));
    if (isNaN(d.getTime())) return null;
    d.setDate(d.getDate() - 1);
    const day = String(d.getDate()).padStart(2, '0');
    return `${day}-${monthNames[d.getMonth()]}-${d.getFullYear()}`;
  }

  function isVisible(el) {
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 2 && r.height > 2;
  }

  const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"])'));
  for (const inp of inputs) {
    if (!isVisible(inp) || inp.disabled) continue;
    const cur = (inp.value || '').trim();
    if (!/^\\d{1,2}-[A-Za-z]{3}-\\d{4}$/.test(cur)) continue;
    const nextVal = shiftOracleDateString(cur);
    if (!nextVal || nextVal === cur) continue;
    inp.removeAttribute('readonly');
    inp.focus();
    inp.value = nextVal;
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    inp.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
  }
  return false;
}
"""
        for frame in self.browser_page.frames:
            try:
                if frame.evaluate(js):
                    return True
            except Exception:
                continue
        return False

    def _step3_fix_currency_line_date_via_details(self, frame: Frame, line_no: int) -> bool:
        """Open Details for ``line_no``, shift date −1 day on the detail form, Return to the table."""
        if not self.browser_page:
            return False
        row_key = self._step3_row_key_for_line_number(frame, line_no)
        if not row_key:
            return False
        if not self._step3_click_details_for_row(frame, row_key):
            self.log_event("warn", f"Step 3: Details not clicked for line {line_no} (row {row_key}).")
            return False
        self.browser_page.wait_for_timeout(650 + _step3_details_flow_throttle_ms())
        if not self._step3_wait_for_line_detail_ready(18000):
            self.log_event("warn", f"Step 3: line {line_no} details view did not load (no Return).")
            self._step3_try_click_return_any_frame(5000)
            return False
        if not self._step3_shift_oracle_date_in_any_frame():
            self.log_event(
                "warn",
                f"Step 3: no Oracle date field updated on details for line {line_no} — try manual fix.",
            )
        self.browser_page.wait_for_timeout(350)
        if not self._step3_try_click_return_any_frame(14000):
            self.log_event("warn", f"Step 3: Return failed after date change for line {line_no}.")
            return False
        self.browser_page.wait_for_timeout(900)
        self._step3_after_playwright_mutation()
        return True

    def _step3_wait_for_detail_line_subpage(self, timeout_ms: int = 15000) -> bool:
        if not self.browser_page:
            return False
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            for frame in self.browser_page.frames:
                try:
                    blob = (
                        frame.evaluate("() => (document.body && document.body.innerText) || ''") or ""
                    ).lower()
                except Exception:
                    continue
                if "original receipt missing" in blob:
                    return True
            time.sleep(0.15)
        return False

    def _step3_check_original_receipt_missing_any_frame(self) -> bool:
        if not self.browser_page:
            return False
        js = """
() => {
  const labels = Array.from(document.querySelectorAll('label, span, td, div, p, li'));
  for (const el of labels) {
    const t = (el.innerText || el.textContent || '').toLowerCase();
    if (!t.includes('original receipt missing')) continue;
    const lid = el.getAttribute('for');
    if (lid) {
      const cb = document.getElementById(lid);
      if (cb && cb.type === 'checkbox' && !cb.checked) {
        cb.click();
        return true;
      }
    }
  }
  const inputs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
  for (const n of inputs) {
    let p = n;
    for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
      const t = (p.innerText || p.textContent || '').toLowerCase();
      if (t.includes('original receipt missing')) {
        if (!n.checked) n.click();
        return true;
      }
    }
  }
  return false;
}
"""
        for frame in self.browser_page.frames:
            try:
                if frame.evaluate(js):
                    return True
            except Exception:
                continue
        return False

    _STEP3_MODAL_YES_OK_JS = """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return (
      st.visibility !== 'hidden' &&
      st.display !== 'none' &&
      r.width > 2 &&
      r.height > 2
    );
  };
  const blob = norm((document.body && document.body.innerText) || '');
  const hasConfirmText =
    blob.includes('you have entered invalid data') ||
    blob.includes('have not completed all required fields') ||
    blob.includes('do you want to continue');
  const roots = Array.from(
    document.querySelectorAll('[role="dialog"], .modal, [class*="Dialog"]')
  );
  const searchRoots = roots.length ? roots : (hasConfirmText ? [document] : []);
  for (const root of searchRoots) {
    const buttons = root.querySelectorAll(
      "button, a[href], input[type='button'], input[type='submit'], span[role='button']"
    );
    let okCandidate = null;
    for (const el of buttons) {
      const raw = norm(el.textContent || el.value || el.getAttribute('aria-label') || '');
      if (!raw) continue;
      if (raw === 'yes' && visible(el)) {
        el.click();
        return true;
      }
      if (raw === 'ok' && visible(el)) okCandidate = el;
    }
    // Prefer "Yes" when present; fall back to "OK" only if needed.
    if (okCandidate) {
      okCandidate.click();
      return true;
    }
  }
  return false;
}
"""

    def _step3_try_click_yes_ok_playwright(self, frame: Frame) -> bool:
        """Fallback when JS click misses Oracle controls: click visible Yes first, then OK."""
        yes_re = re.compile(r"^\s*yes\s*$", re.IGNORECASE)
        ok_re = re.compile(r"^\s*ok\s*$", re.IGNORECASE)
        # Role path first for semantic controls.
        for role in ("button", "link"):
            try:
                loc = frame.get_by_role(role, name=yes_re)
                for i in range(min(loc.count(), 6)):
                    c = loc.nth(i)
                    if c.is_visible() and c.is_enabled():
                        c.click(timeout=800)
                        return True
            except Exception:
                continue
        # Oracle often renders as input buttons where value carries label text.
        ok_candidate = None
        try:
            inputs = frame.locator("input[type='button'], input[type='submit']")
            for i in range(min(inputs.count(), 16)):
                c = inputs.nth(i)
                if not (c.is_visible() and c.is_enabled()):
                    continue
                raw = (c.get_attribute("value") or "").strip()
                if yes_re.match(raw):
                    c.click(timeout=800)
                    return True
                if ok_re.match(raw):
                    ok_candidate = c
        except Exception:
            pass
        if ok_candidate is not None:
            try:
                ok_candidate.click(timeout=800)
                return True
            except Exception:
                pass
        return False

    def _step3_dismiss_in_page_yes_ok_any_frame(self, timeout_ms: int = 12000) -> bool:
        """Oracle line Details often opens an in-page confirm after Return — click Yes (or OK fallback)."""
        if not self.browser_page:
            return False
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            for frame in self.browser_page.frames:
                try:
                    if frame.evaluate(self._STEP3_MODAL_YES_OK_JS):
                        self.browser_page.wait_for_timeout(350)
                        return True
                except Exception:
                    pass
                try:
                    if self._step3_try_click_yes_ok_playwright(frame):
                        self.browser_page.wait_for_timeout(350)
                        return True
                except Exception:
                    continue
            time.sleep(0.12)
        return False

    def _step3_try_click_return_any_frame(self, timeout_ms: int = 12000) -> bool:
        if not self.browser_page:
            return False
        page = self.browser_page

        def on_dialog(dialog) -> None:
            try:
                msg = (dialog.message or "").strip()
                short = msg[:160] + ("…" if len(msg) > 160 else "")
                self.log_event(
                    "browser",
                    f"Step 3: accepting native browser dialog ({dialog.type}): {short}",
                )
            except Exception:
                pass
            try:
                dialog.accept()
            except Exception:
                pass

        page.on("dialog", on_dialog)
        try:
            post_return_delay = _step3_post_return_modal_delay_ms()
            ret = re.compile(r"^\s*Return\s*$", re.IGNORECASE)
            deadline = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < deadline:
                self._pump_ui_and_check_cancel()
                for frame in page.frames:
                    try:
                        for role in ("button", "link"):
                            loc = frame.get_by_role(role, name=ret)
                            for i in range(loc.count()):
                                c = loc.nth(i)
                                try:
                                    if c.is_visible() and c.is_enabled():
                                        c.click(timeout=5000)
                                        page.wait_for_timeout(post_return_delay)
                                        self._step3_dismiss_in_page_yes_ok_any_frame(12000)
                                        return True
                                except Exception:
                                    continue
                    except Exception:
                        continue
                time.sleep(0.12)
            # Fallback: Oracle sometimes exposes Return in a way role=button misses; Enter activates default.
            if self._step3_wait_for_line_detail_ready(1500):
                try:
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(post_return_delay)
                    self._step3_dismiss_in_page_yes_ok_any_frame(12000)
                    return True
                except Exception:
                    pass
            return False
        finally:
            try:
                page.remove_listener("dialog", on_dialog)
            except Exception:
                pass

    def _step3_click_details_for_row(self, frame: Frame, row_key: str) -> bool:
        if not row_key:
            return False
        self._step3_pause_details_flow()
        try:
            clicked = bool(
                frame.evaluate(
                    """
(rowKey) => {
  const parts = String(rowKey).split(':');
  const tableIndex = parseInt(parts[0], 10);
  const rowIndex = parseInt(parts[1], 10);
  if (Number.isNaN(tableIndex) || Number.isNaN(rowIndex)) return false;
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const tables = Array.from(document.querySelectorAll('table'));
  const table = tables[tableIndex];
  if (!table) return false;
  const headerRow = table.querySelector('tr');
  if (!headerRow) return false;
  const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
  const headerTexts = headerCells.map((cell) => normalize(cell.textContent || ''));
  const detailsIdx = headerTexts.findIndex(
    (txt) => txt === 'details' || (txt.includes('detail') && !txt.includes('expense'))
  );
  if (detailsIdx < 0) return false;
  const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
  const tr = bodyRows[rowIndex];
  if (!tr) return false;
  const cells = Array.from(tr.querySelectorAll('td'));
  const det = cells[detailsIdx];
  if (!det) return false;
  const candidates = det.querySelectorAll('a, button, [role="button"], img, .x27, span');
  for (const el of candidates) {
    if (el.disabled) continue;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (st.visibility === 'hidden' || st.display === 'none' || r.width < 2 || r.height < 2) continue;
    el.click();
    return true;
  }
  det.click();
  return true;
}
                    """,
                    row_key,
                )
            )
        except Exception:
            return False
        if clicked:
            self._step3_pause_details_flow()
            self._chromium_interaction_gap()
        return clicked

    def _step3_open_details_mark_receipt_missing_and_return(self, frame: Frame, row_key: str) -> bool:
        """Details column → Original Receipt Missing → Return (stay on Step 3 table)."""
        if not self.browser_page:
            return False
        if not self._step3_click_details_for_row(frame, row_key):
            self.log_event("warn", f"Step 3: Details icon not clicked for row {row_key}.")
            return False
        self.browser_page.wait_for_timeout(950 + _step3_details_flow_throttle_ms())
        if not self._step3_wait_for_detail_line_subpage(16000):
            self.log_event(
                "warn",
                "Step 3: line details page did not load (expected Original Receipt Missing).",
            )
            self._step3_try_click_return_any_frame(4000)
            return False
        if not self._step3_check_original_receipt_missing_any_frame():
            self.log_event(
                "warn",
                "Step 3: could not check Original Receipt Missing (checkbox not found).",
            )
        self._step3_pause_details_flow()
        self.browser_page.wait_for_timeout(350 + _step3_details_flow_throttle_ms())
        if not self._step3_try_click_return_any_frame():
            self.log_event("warn", "Step 3: Return control not found from line details.")
            return False
        self._step3_pause_details_flow()
        self.browser_page.wait_for_timeout(800 + _step3_details_flow_throttle_ms())
        self._step3_after_playwright_mutation()
        return True

    def _fallback_expense_type(self, merchant_name: str, options: list[str]) -> str:
        merchant = merchant_name.lower()
        lookup = {opt.lower(): opt for opt in options}

        def contains_any(keywords: list[str]) -> str | None:
            for option in options:
                lowered = option.lower()
                for keyword in keywords:
                    if keyword in lowered:
                        return option
            return None

        if any(token in merchant for token in ["hotel", "inn", "resort", "marriott", "hilton"]):
            hit = contains_any(["hotel"])
            if hit:
                return hit
        if any(token in merchant for token in ["air", "airline", "airport", "delta", "united", "southwest"]):
            hit = contains_any(["airfare", "flight"])
            if hit:
                return hit
        if any(token in merchant for token in ["uber", "lyft", "taxi", "parking", "shell", "exxon", "chevron", "gas"]):
            hit = contains_any(["transportation", "car rental", "parking"])
            if hit:
                return hit
        if any(token in merchant for token in ["coffee", "cafe", "restaurant", "grill", "bistro", "summer moon"]):
            hit = contains_any(["meals", "meal"])
            if hit:
                return hit

        if "miscellaneous travel" in lookup:
            return lookup["miscellaneous travel"]
        if "miscellaneous personnel expense" in lookup:
            return lookup["miscellaneous personnel expense"]
        return options[0]

    def _choose_expense_type_with_llm(
        self,
        api_key: str,
        merchant_name: str,
        options: list[str],
        *,
        log_event_fn: Callable[[str, str], None] | None = None,
    ) -> str:
        log = log_event_fn or self.log_event
        log(
            "llm",
            f'Sending OpenAI request: model={self.settings.openai_model}, '
            f'vendor="{merchant_name}", {len(options)} dropdown option(s). '
            "Waiting for network response (can take 5-60s if the API is slow)…",
        )
        client = self._get_openai_client(api_key=api_key)
        prompt = expense_type_prompt(merchant_name, options)
        t0 = time.monotonic()
        try:
            response = client.responses.create(
                model=self.settings.openai_model,
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
            )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            tls_hint = openai_tls_troubleshooting_hint(exc)
            detail = f" {exc.__cause__!r}" if exc.__cause__ is not None else ""
            log(
                "err",
                f"OpenAI API failed after {elapsed:.1f}s ({type(exc).__name__}): {exc}.{detail}"
                f"{tls_hint} Also check internet/VPN, API key in Settings, and https://status.openai.com .",
            )
            raise
        elapsed = time.monotonic() - t0
        raw_text = (response.output_text or "").strip()
        log(
            "llm",
            f"OpenAI replied in {elapsed:.1f}s for \"{merchant_name}\" "
            f"({len(raw_text)} character(s) of output).",
        )
        cleaned = raw_text
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.replace("json\n", "", 1).strip()

        try:
            parsed = json.loads(cleaned)
            candidate = str(parsed.get("expense_type", "")).strip()
        except json.JSONDecodeError:
            candidate = raw_text.strip()

        exact = {opt.lower(): opt for opt in options}
        if candidate.lower() in exact:
            chosen = exact[candidate.lower()]
            log("llm", f'Parsed choice for "{merchant_name}" -> "{chosen}" (exact match).')
            return chosen
        for option in options:
            if option.lower() in candidate.lower() or candidate.lower() in option.lower():
                log(
                    "llm",
                    f'Parsed choice for "{merchant_name}" -> "{option}" (fuzzy match to model text).',
                )
                return option
        chosen = self._fallback_expense_type(merchant_name=merchant_name, options=options)
        log(
            "warn",
            f'LLM output did not match a dropdown option for "{merchant_name}"; using rules -> "{chosen}".',
        )
        return chosen

    def _resolve_expense_type_for_merchant(
        self,
        api_key: str,
        merchant_name: str,
        options: list[str],
        *,
        collect_doc: dict | None = None,
        replay_doc: dict | None = None,
    ) -> str:
        cache_key = _normalize_vendor_key(merchant_name)
        cached = self.vendor_expense_cache.get(cache_key, "").strip()
        if cached:
            matched = _match_label_to_options(cached, options)
            if matched:
                self.log_event(
                    "cache",
                    f'Hit: "{merchant_name}" -> "{matched}" (saved vendor cache; no OpenAI call).',
                )
                return matched
            if self._step3_vpn_mode != "vpn_collect":
                self.log_event(
                    "warn",
                    f'Stale cache: stored "{cached}" is not in this report\'s dropdown; calling LLM for "{merchant_name}".',
                )
        elif self._step3_vpn_mode != "vpn_collect":
            self.log_event("cache", f'No cache entry for vendor "{merchant_name}"; calling OpenAI.')

        if self._step3_vpn_mode == "vpn_collect":
            if collect_doc is None:
                collect_doc = new_empty_document()
            register_expense_type_query(collect_doc, merchant_name, options)
            qid = expense_type_query_id(merchant_name, options)
            self.log_event(
                "step",
                f'[VPN collect] Queued LLM prompt {qid} for "{merchant_name}"; '
                "leaving expense type and justification blank in the portal.",
            )
            return ""

        if self._step3_vpn_mode == "vpn_replay" and replay_doc is not None:
            qid = expense_type_query_id(merchant_name, options)
            from_file = response_expense_type(replay_doc, qid)
            if from_file:
                exact = {opt.lower(): opt for opt in options}
                if from_file.lower() in exact:
                    chosen = exact[from_file.lower()]
                else:
                    chosen = None
                    for option in options:
                        if (
                            option.lower() in from_file.lower()
                            or from_file.lower() in option.lower()
                        ):
                            chosen = option
                            break
                    if chosen is None:
                        chosen = _match_label_to_options(from_file, options)
                if chosen:
                    self.vendor_expense_cache[cache_key] = chosen
                    self._persist_vendor_expense_cache()
                    self.log_event(
                        "cache",
                        f'[VPN replay] "{merchant_name}" -> "{chosen}" (from LLM cache file).',
                    )
                    return chosen
                self.log_event(
                    "warn",
                    f'[VPN replay] Cached label "{from_file}" for "{merchant_name}" '
                    "did not match dropdown; using rules fallback.",
                )
            else:
                self.log_event(
                    "warn",
                    f'[VPN replay] Missing cache response for {qid} ("{merchant_name}"); using rules.',
                )
            return self._fallback_expense_type(merchant_name=merchant_name, options=options)

        chosen = self._choose_expense_type_with_llm(
            api_key=api_key,
            merchant_name=merchant_name,
            options=options,
        )
        self.vendor_expense_cache[cache_key] = chosen
        self._persist_vendor_expense_cache()
        self.log_event("cache", f'Saved to vendor cache: "{merchant_name}" -> "{chosen}"')
        return chosen

    def _apply_step3_assignments(self, frame: Frame, assignments: list[dict]) -> int:
        if not assignments:
            return 0
        applied = frame.evaluate(
            """
(payload) => {
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const normalize = (value) => clean(value).toLowerCase();
  const pickOption = (select, label) => {
    const want = normalize(label);
    const opts = Array.from(select.options);
    let opt = opts.find((o) => normalize(o.textContent || '') === want);
    if (opt) return opt;
    opt = opts.find((o) => {
      const ot = normalize(o.textContent || '');
      return ot && !/^select/i.test(ot) && (ot.includes(want) || want.includes(ot));
    });
    return opt || null;
  };
  const byKey = new Map(payload.map((item) => [item.row_key, item.expense_type]));
  let appliedCount = 0;
  const tables = Array.from(document.querySelectorAll('table'));

  tables.forEach((table, tableIndex) => {
    const headerRow = table.querySelector('tr');
    if (!headerRow) return;
    const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
    const headerTexts = headerCells.map((cell) => normalize(cell.textContent || ''));
    const expenseIdx = headerTexts.findIndex((txt) => txt.includes('expense type'));
    const justificationIdx = headerTexts.findIndex((txt) => txt.includes('justification'));
    if (expenseIdx < 0 || justificationIdx < 0) return;

    const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
    bodyRows.forEach((tr, rowIndex) => {
      const rowKey = `${tableIndex}:${rowIndex}`;
      const selectedLabel = byKey.get(rowKey);
      if (!selectedLabel) return;

      const cells = Array.from(tr.querySelectorAll('td'));
      const expenseCell = cells[expenseIdx];
      const justificationCell = cells[justificationIdx];
      if (!expenseCell || !justificationCell) return;

      const select = expenseCell.querySelector('select');
      const justInput = justificationCell.querySelector('input, textarea');
      if (!select || !justInput) return;

      const option = pickOption(select, selectedLabel);
      if (!option) return;

      const appliedLabel = clean(option.textContent || '') || selectedLabel;
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      select.dispatchEvent(new Event('input', { bubbles: true }));
      select.dispatchEvent(new Event('blur', { bubbles: true }));

      justInput.focus();
      justInput.value = appliedLabel;
      justInput.dispatchEvent(new Event('input', { bubbles: true }));
      justInput.dispatchEvent(new Event('change', { bubbles: true }));
      justInput.dispatchEvent(new Event('blur', { bubbles: true }));
      appliedCount += 1;
    });
  });

  return appliedCount;
}
            """,
            assignments,
        )
        return int(applied or 0)

    def auto_fill_step3_expense_types(self, api_key: str) -> tuple[list[dict], list[int]]:
        """Fill Expense Type + Justification for every row, following table pagination (Next N).

        Returns ``(assignments, receipt_missing_line_numbers)`` — line numbers where a receipt file is
        absent are deferred (mark Original Receipt Missing only after the first Save).
        """
        collect_doc: dict | None = (
            new_empty_document() if self._step3_vpn_mode == "vpn_collect" else None
        )
        replay_doc: dict | None = (
            self._llm_replay_document if self._step3_vpn_mode == "vpn_replay" else None
        )
        mode_note = {
            "standard": "cache + LLM",
            "vpn_collect": "VPN collect (queue prompts only; no form fill)",
            "vpn_replay": "VPN replay (LLM cache file + vendor cache)",
        }.get(self._step3_vpn_mode, "cache + LLM")

        all_assignments: list[dict] = []
        receipt_missing_lines: list[int] = []
        max_pages = 50
        for page_idx in range(max_pages):
            self.log_event(
                "browser",
                f"Scanning browser for Business Expenses table (wizard page {page_idx + 1})…",
            )
            frame, rows = self._extract_step3_rows_in_any_frame()
            if not frame or not rows:
                if page_idx == 0:
                    raise RuntimeError(
                        "Could not locate Step 3 business expenses table. "
                        "Keep Step 3 visible and try again."
                    )
                self.log_event("browser", "No more expense rows found in browser; finished table pages.")
                break

            self._step3_nudge_step3_table_for_lazy_dropdowns(frame)
            if self.browser_page:
                self.browser_page.wait_for_timeout(500)
            frame, rows = self._extract_step3_rows_in_any_frame()
            if not frame or not rows:
                self.log_event("warn", "Step 3: table disappeared after scroll-nudge — retrying once.")
                if self.browser_page:
                    self.browser_page.wait_for_timeout(400)
                frame, rows = self._extract_step3_rows_in_any_frame()
            if not frame or not rows:
                raise RuntimeError(
                    "Step 3: Business Expenses table not found after preparing rows. "
                    "Keep Step 3 visible and try again."
                )

            n_rows = len(rows)
            self.log_event(
                "step",
                f"Step 3: table page {page_idx + 1} — {n_rows} row(s) to categorize ({mode_note}).",
            )
            assignments: list[dict] = []
            applied_count = 0
            skipped_no_data = 0
            skipped_apply = 0
            for row_i, row in enumerate(rows, start=1):
                if row_i > 1 or page_idx > 0:
                    self._step3_pause_between_row_entries()
                self._pump_ui_and_check_cancel()
                merchant_name = str(row.get("merchant_name", "")).strip()
                options = [str(opt).strip() for opt in row.get("options", []) if str(opt).strip()]
                row_key = str(row.get("row_key", "")).strip()
                if not merchant_name or not options or not row_key:
                    skipped_no_data += 1
                    self.log_event(
                        "warn",
                        "Step 3: skipping a grid row (missing merchant, dropdown options, or row id). "
                        "Often the last row needs scrolling — try Save, refresh this step, or set the type manually.",
                    )
                    continue

                short_m = merchant_name if len(merchant_name) <= 64 else merchant_name[:61] + "..."
                self.log_event(
                    "step",
                    f"Categorizing row {row_i}/{n_rows} on this page: {short_m}",
                )
                selected = self._resolve_expense_type_for_merchant(
                    api_key=api_key,
                    merchant_name=merchant_name,
                    options=options,
                    collect_doc=collect_doc,
                    replay_doc=replay_doc,
                )
                if selected:
                    canon = _match_label_to_options(selected, options)
                    if canon:
                        selected = canon

                pending_assignment = {
                    "row_key": row_key,
                    "merchant_name": merchant_name,
                    "expense_type": selected,
                }

                if self._step3_vpn_mode == "vpn_collect":
                    assignments.append(pending_assignment)
                    continue

                applied_ok = self._apply_step3_single_row_assignment(frame, row_key, selected)
                if not applied_ok:
                    if self.browser_page:
                        self.browser_page.wait_for_timeout(450)
                    f_retry, _ = self._extract_step3_rows_in_any_frame()
                    if f_retry is not None:
                        frame = f_retry
                    self._step3_nudge_step3_table_for_lazy_dropdowns(frame)
                    if self.browser_page:
                        self.browser_page.wait_for_timeout(350)
                    applied_ok = self._apply_step3_single_row_assignment(frame, row_key, selected)
                if not applied_ok:
                    skipped_apply += 1
                    self.log_event(
                        "warn",
                        f"Step 3: could not apply expense type for row {row_key} ({short_m}) — skipping. "
                        "Fill this line manually or resume after fixing the grid.",
                    )
                    continue

                assignments.append(pending_assignment)
                applied_count += 1

                receipt_state = self._step3_row_has_attachable_receipt(row)
                if receipt_state is False and row.get("has_details_column"):
                    ln = row.get("line_no")
                    if isinstance(ln, int) and ln >= 1:
                        if ln not in receipt_missing_lines:
                            receipt_missing_lines.append(ln)
                        self.log_event(
                            "step",
                            f"Step 3: line {ln} has no receipt file — will mark Original Receipt Missing after Save.",
                        )
                    else:
                        self.log_event(
                            "warn",
                            f"Step 3: row {row_key} needs Original Receipt Missing but Line # was not read from "
                            "the grid; fill Details manually if required.",
                        )
                elif receipt_state is False and not row.get("has_details_column"):
                    self.log_event(
                        "warn",
                        f"Step 3: row {row_key} has no receipt file but no Details column was found.",
                    )

            if not assignments:
                detail = ""
                if skipped_no_data or skipped_apply:
                    detail = (
                        f" ({skipped_no_data} row(s) missing merchant/dropdown options; "
                        f"{skipped_apply} row(s) where apply failed.)"
                    )
                raise RuntimeError(
                    "Step 3: no expense types were recorded on this table page."
                    + detail
                    + " See Activity log; fill or skip lines manually if needed."
                )

            if self._step3_vpn_mode == "vpn_collect":
                self.log_event(
                    "browser",
                    f"VPN collect: skipping form fill for {len(assignments)} row(s) on this page "
                    "(expense type / justification stay blank).",
                )
            else:
                if applied_count == 0:
                    raise RuntimeError("No Step 3 rows were updated.")
                self.log_event(
                    "browser",
                    f"Browser form updated: {applied_count} row(s) on this page "
                    f"(expense type / justification; receipt-missing lines are handled after Save).",
                )
                if skipped_no_data or skipped_apply:
                    self.log_event(
                        "warn",
                        f"Step 3: this page skipped {skipped_no_data} row(s) with incomplete grid data and "
                        f"{skipped_apply} row(s) where the form rejected the update.",
                    )

            if (
                self._step3_vpn_mode != "vpn_collect"
                and applied_count > 0
                and self.browser_page
            ):
                pr_save = (
                    self.get_transactions_page_range_in_frame(frame, visible_row_count=n_rows)
                    or self.get_transactions_page_range_in_any_frame(visible_row_count=n_rows)
                )
                if pr_save and pr_save[1] < pr_save[2]:
                    self.set_status(
                        "Step 3: saving Business Expenses (keeps table edits when loading the next page)…"
                    )
                    self.log_event(
                        "browser",
                        "Step 3: Save after this table page so Oracle keeps expense types before pagination.",
                    )
                    if self.click_save_button_wizard_in_any_frame(wizard_step=3):
                        self.browser_page.wait_for_timeout(900)
                        self._chromium_interaction_gap()
                        self._step3_wait_main_ready_state_complete()
                    else:
                        self.log_event(
                            "warn",
                            "Step 3: Save before next table page failed — continuing; "
                            "some rows may need manual re-entry after pagination.",
                        )
                    f_after, _ = self._extract_step3_rows_in_any_frame()
                    if f_after is not None:
                        frame = f_after

            all_assignments.extend(assignments)

            page_range = (
                self.get_transactions_page_range_in_frame(frame, visible_row_count=n_rows)
                or self.get_transactions_page_range_in_any_frame(visible_row_count=n_rows)
            )
            if page_range:
                start, end, total = page_range
                self.set_status(
                    f"Step 3: auto-filled rows {start}-{end} of {total} "
                    f"({len(all_assignments)} line(s) total so far)."
                )
                if end >= total:
                    break
            elif len(rows) < 10:
                break

            self.log_event(
                "browser",
                "Clicking table pagination (e.g. Next 10) to load more expense lines if any…",
            )
            if not self.click_expense_table_pagination_next_in_any_frame(
                preferred_frame=frame,
            ):
                page_range = (
                    self.get_transactions_page_range_in_frame(frame, visible_row_count=n_rows)
                    or self.get_transactions_page_range_in_any_frame(visible_row_count=n_rows)
                )
                if page_range and page_range[1] >= page_range[2]:
                    break
                raise RuntimeError(
                    "Could not advance the expense table to the next page. "
                    "If the range already shows the last rows (e.g. 31-40 of 40), you are done with "
                    "this table — use the green wizard 'Next' next to 'Step 3 of 6'. "
                    "Otherwise fix the Chromium window and resume from this step."
                )
            if self.browser_page:
                self.browser_page.wait_for_timeout(900)
                self._chromium_interaction_gap()
                self._step3_wait_main_ready_state_complete()

        if not all_assignments:
            raise RuntimeError("No Step 3 rows were updated.")

        if self._step3_vpn_mode == "vpn_collect" and collect_doc is not None:
            pending_path = llm_pending_file(APP_DIR)
            prev = load_document(pending_path)
            prev_r = prev.get("responses", {})
            if isinstance(prev_r, dict):
                for qid in collect_doc.get("queries", {}):
                    old = prev_r.get(qid)
                    if isinstance(old, dict) and old.get("expense_type"):
                        collect_doc.setdefault("responses", {})[qid] = old
            save_document(pending_path, collect_doc)
            nq = len(collect_doc.get("queries", {}))
            self.log_event(
                "step",
                f"Saved {nq} unique LLM prompt(s) to {pending_path} (turn VPN off, then Activity → Resolve types).",
            )

        return all_assignments, receipt_missing_lines

    def _wizard_any_frame_contains(self, needle: str) -> bool:
        if not self.browser_page or not needle:
            return False
        for frame in self.browser_page.frames:
            try:
                blob = frame.evaluate(
                    "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                )
                if needle in blob:
                    return True
            except Exception:
                continue
        return False

    def _wizard_any_frame_on_step(self, step: int, total: int = 6) -> bool:
        if not self.browser_page or step < 1:
            return False
        for frame in self.browser_page.frames:
            try:
                blob = frame.evaluate(
                    "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                )
                if _blob_shows_wizard_step(blob or "", step, total):
                    return True
            except Exception:
                continue
        return False

    def _step3_full_body_text(self) -> str:
        if not self.browser_page:
            return ""
        parts: list[str] = []
        for frame in self.browser_page.frames:
            try:
                parts.append(
                    frame.evaluate("() => (document.body && document.body.innerText) || ''") or ""
                )
            except Exception:
                continue
        return "\n".join(parts)

    def _scrape_step3_banner_errors_ordered(self) -> list[tuple[int, str]]:
        """Parse ``Line N Error -`` segments in document order. Kind: ``currency`` | ``expense_justification`` | ``other``."""
        blob = self._step3_full_body_text()
        if not blob.strip():
            return []
        pattern = re.compile(r"Line\s+(\d+)\s+Error\s*-\s*", re.IGNORECASE)
        matches = list(pattern.finditer(blob))
        if not matches:
            return []
        out: list[tuple[int, str]] = []
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(blob)
            chunk = blob[start:end]
            try:
                line_no = int(m.group(1))
            except ValueError:
                continue
            cl = chunk.lower()
            if _step3_banner_chunk_is_currency_exchange_error(chunk):
                kind = "currency"
            elif "expense type" in cl or "justification" in cl:
                kind = "expense_justification"
            else:
                kind = "other"
            out.append((line_no, kind))
        return out

    def _step3_currency_error_banner_visible(self) -> bool:
        return any(self._wizard_any_frame_contains(m) for m in _STEP3_CURRENCY_ERROR_MARKERS)

    def _scrape_step3_currency_error_line_numbers(self) -> set[int]:
        """Parse Step 3 yellow banner for lines whose message is the receipt/reimbursement currency rule (not Expense Type)."""
        if not self.browser_page:
            return set()
        found: set[int] = set()
        for frame in self.browser_page.frames:
            try:
                blob = (
                    frame.evaluate("() => (document.body && document.body.innerText) || ''") or ""
                )
            except Exception:
                continue
            if not blob:
                continue
            blob_lower = blob.lower()
            if not any(m.lower() in blob_lower for m in _STEP3_CURRENCY_ERROR_MARKERS):
                continue
            for m in re.finditer(
                r"Line\s+(\d+)\s+Error\s*-\s*",
                blob,
                re.IGNORECASE,
            ):
                start = m.end()
                next_m = re.search(r"(?:^|\n)\s*Line\s+\d+\s+Error", blob[start:], re.IGNORECASE)
                end = start + next_m.start() if next_m else len(blob)
                chunk = blob[start:end]
                if not _step3_banner_chunk_is_currency_exchange_error(chunk):
                    continue
                try:
                    found.add(int(m.group(1)))
                except ValueError:
                    continue
        return found

    def _step3_navigate_to_line_number(self, line_no: int) -> tuple[Frame | None, str | None]:
        """Paginate the Step 3 Business Expenses table from the first page until ``line_no`` is visible."""
        if not self.browser_page or line_no < 1:
            return None, None
        try:
            self.expense_table_go_to_first_page_in_any_frame(credit_card_step2=False)
        except RuntimeError as exc:
            self.log_event("warn", f"Step 3: {exc}")
        max_pages = 60
        for _ in range(max_pages):
            self._pump_ui_and_check_cancel()
            frame, rows = self._extract_step3_rows_in_any_frame()
            if not frame:
                return None, None
            rk = self._step3_row_key_for_line_number(frame, line_no)
            if rk:
                self._chromium_interaction_gap()
                return frame, rk
            n_vis = len(rows) if rows else 0
            page_range = (
                self.get_transactions_page_range_in_frame(frame, visible_row_count=n_vis)
                or self.get_transactions_page_range_in_any_frame(visible_row_count=n_vis)
            )
            if page_range and page_range[1] >= page_range[2]:
                break
            if len(rows or []) < 10 and not page_range:
                break
            if not self.click_expense_table_pagination_next_in_any_frame(preferred_frame=frame):
                page_range = (
                    self.get_transactions_page_range_in_frame(frame, visible_row_count=n_vis)
                    or self.get_transactions_page_range_in_any_frame(visible_row_count=n_vis)
                )
                if page_range and page_range[1] >= page_range[2]:
                    break
                break
            self.browser_page.wait_for_timeout(700)
            self._chromium_interaction_gap()
        return None, None

    def step3_apply_receipt_missing_pass(self, line_numbers: list[int]) -> None:
        """Open Details for each line (in order) and mark Original Receipt Missing; assumes Step 3 table context."""
        if not self.browser_page or not line_numbers:
            return
        for line_no in line_numbers:
            self._pump_ui_and_check_cancel()
            frame, row_key = self._step3_navigate_to_line_number(line_no)
            if not frame or not row_key:
                self.log_event(
                    "warn",
                    f"Step 3: could not reach line {line_no} for Original Receipt Missing.",
                )
                continue
            self.log_event("step", f"Step 3: line {line_no} — marking Original Receipt Missing (Details)…")
            self._step3_open_details_mark_receipt_missing_and_return(frame, row_key)

    def step3_resolve_banner_errors_in_order(self, assignments: list[dict], api_key: str) -> None:
        """After Save, walk header ``Line N Error`` messages in order: fill expense/justification or currency date fix."""
        if not self.browser_page:
            return
        assign_by_key = {
            str(a.get("row_key", "")).strip(): a for a in assignments if str(a.get("row_key", "")).strip()
        }
        replay_doc = self._llm_replay_document if self._step3_vpn_mode == "vpn_replay" else None
        collect_doc: dict | None = None
        noop_nav = 0
        fix_fail_streak = 0
        max_rounds = 40
        for _ in range(max_rounds):
            self._pump_ui_and_check_cancel()
            self._chromium_interaction_gap()
            errs = self._scrape_step3_banner_errors_ordered()
            if not errs:
                return
            line_no, kind = errs[0]
            self.set_status(f"Step 3: fixing banner error on line {line_no} ({kind})…")
            frame, row_key = self._step3_navigate_to_line_number(line_no)
            if not frame or not row_key:
                self.log_event(
                    "warn",
                    f"Step 3: could not find row for line {line_no} (banner: {kind}).",
                )
                noop_nav += 1
                if noop_nav >= 4:
                    break
                if not self.click_save_button_wizard_in_any_frame(wizard_step=3):
                    break
                self.browser_page.wait_for_timeout(900)
                continue
            noop_nav = 0
            fixed = False
            if kind == "currency":
                fixed = self._step3_fix_currency_line_date_via_details(frame, line_no)
            else:
                _, rows = self._extract_step3_rows_in_any_frame()
                row = next(
                    (r for r in (rows or []) if str(r.get("row_key", "")).strip() == row_key),
                    None,
                )
                if not row:
                    self.log_event(
                        "warn",
                        f"Step 3: scraped rows missing line {line_no} ({row_key}) for banner fix.",
                    )
                else:
                    pa = assign_by_key.get(row_key)
                    exp = (pa or {}).get("expense_type") if pa else None
                    if not exp:
                        exp = self._resolve_expense_type_for_merchant(
                            api_key,
                            str(row.get("merchant_name", "")),
                            [str(x).strip() for x in (row.get("options") or []) if str(x).strip()],
                            collect_doc=collect_doc,
                            replay_doc=replay_doc,
                        )
                    if exp and self._apply_step3_single_row_assignment(frame, row_key, exp):
                        fixed = True
                    elif kind == "other":
                        self.log_event(
                            "warn",
                            f"Step 3: could not auto-fix line {line_no} — check the banner text manually.",
                        )
            if not fixed and kind == "currency":
                self.log_event("warn", f"Step 3: currency/date fix did not complete for line {line_no}.")
            if fixed:
                fix_fail_streak = 0
            else:
                fix_fail_streak += 1
                if fix_fail_streak >= 4:
                    self.log_event(
                        "warn",
                        "Step 3: stopping banner auto-fix after repeated unsuccessful attempts.",
                    )
                    break
            if not self.click_save_button_wizard_in_any_frame(wizard_step=3):
                self.log_event("warn", "Step 3: Save failed after banner fix.")
                break
            self.browser_page.wait_for_timeout(1100)

    def fix_step3_exchange_rate_errors_on_all_pages(self) -> int:
        """Walk every Business Expenses table page; fix receipt/reimbursement currency lines via Details → date −1 → Return.

        Only rows whose banner text is the currency/exchange-rate message are updated (not ``Expense Type`` errors).
        Oracle often ignores inline date edits on the grid; the detail view is authoritative.
        """
        if not self.browser_page:
            return 0

        banner_lines = self._scrape_step3_currency_error_line_numbers()
        if not banner_lines and self._step3_currency_error_banner_visible():
            self.log_event(
                "warn",
                "Step 3: currency banner visible but no line numbers parsed — check banner text format.",
            )

        if banner_lines:
            preview = ", ".join(str(x) for x in sorted(banner_lines)[:12])
            more = "…" if len(banner_lines) > 12 else ""
            self.set_status(
                f"Step 3: currency rule applies to line(s) {preview}{more} — Details → date −1 → Return on each…"
            )
            try:
                self.expense_table_go_to_first_page_in_any_frame(credit_card_step2=False)
            except RuntimeError as exc:
                self.log_event("warn", f"Step 3: {exc}; fixing from current table page.")

            remaining = set(banner_lines)
            total_fixed = 0
            max_pages = 60
            for _page_idx in range(max_pages):
                frame, rows = self._extract_step3_rows_in_any_frame()
                if not frame or not rows:
                    break
                for line_no in sorted(remaining):
                    if self._step3_fix_currency_line_date_via_details(frame, line_no):
                        remaining.discard(line_no)
                        total_fixed += 1
                        f2, _ = self._extract_step3_rows_in_any_frame()
                        if f2 is not None:
                            frame = f2
                if not remaining:
                    break
                n_rows = len(rows)
                page_range = (
                    self.get_transactions_page_range_in_frame(frame, visible_row_count=n_rows)
                    or self.get_transactions_page_range_in_any_frame(visible_row_count=n_rows)
                )
                if page_range:
                    _, end, tot = page_range
                    if end >= tot:
                        break
                elif len(rows) < 10:
                    break
                if not self.click_expense_table_pagination_next_in_any_frame(
                    preferred_frame=frame,
                ):
                    page_range = (
                        self.get_transactions_page_range_in_frame(frame, visible_row_count=n_rows)
                        or self.get_transactions_page_range_in_any_frame(visible_row_count=n_rows)
                    )
                    if page_range and page_range[1] >= page_range[2]:
                        break
                    break
                self.browser_page.wait_for_timeout(700)
                self._chromium_interaction_gap()
            if remaining:
                self.log_event(
                    "warn",
                    "Step 3: currency date fix could not find rows for line(s): "
                    + ", ".join(str(x) for x in sorted(remaining)[:20])
                    + ("…" if len(remaining) > 20 else ""),
                )
            return total_fixed

        return 0

    def advance_step3_wizard_past_exchange_rate_errors(self) -> None:
        """Leave Step 3: for receipt/reimbursement currency banner lines, open Details, date −1, Return; Save; Next.

        Only banner messages about exchange rate / matching receipt and reimbursement currency trigger fixes
        (not ``Expense Type`` errors). Grid date cells are skipped in favor of the line Details form.
        """
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")

        if not self._wizard_any_frame_on_step(3):
            raise RuntimeError("Expected Step 3 of 6 before advancing past Business Expenses.")

        noop_fix_streak = 0
        max_rounds = 15
        for _ in range(max_rounds):
            self._pump_ui_and_check_cancel()
            if not self._wizard_any_frame_on_step(3):
                return

            currency_lines = self._scrape_step3_currency_error_line_numbers()
            needs_fix = self._step3_currency_error_banner_visible() or bool(currency_lines)
            if needs_fix:
                self.set_status(
                    "Step 3: receipt/reimbursement currency line errors — opening Details, shifting date −1, Return…"
                )
                n = self.fix_step3_exchange_rate_errors_on_all_pages()
                if n == 0:
                    noop_fix_streak += 1
                else:
                    noop_fix_streak = 0
                self.set_status(
                    f"Step 3: updated {n} date(s) on this pass; saving expense report…"
                    if n
                    else "Step 3: saving expense report (no further date changes on this pass)…"
                )
                if not self.click_save_button_wizard_in_any_frame(wizard_step=3):
                    raise RuntimeError("Could not Save on Step 3 after correcting line dates.")
                self.browser_page.wait_for_timeout(1200)
                if noop_fix_streak >= 2:
                    noop_fix_streak = 0
                    if not self.wait_for_wizard_next_enabled_and_click(wizard_step=3):
                        raise RuntimeError("Could not click the wizard Next button on Step 3.")
                    self.browser_page.wait_for_timeout(900)
                continue

            noop_fix_streak = 0
            if not self.wait_for_wizard_next_enabled_and_click(wizard_step=3):
                raise RuntimeError("Could not click the wizard Next button on Step 3.")
            self.browser_page.wait_for_timeout(900)

            if not self._wizard_any_frame_on_step(3):
                return

            if not self._step3_currency_error_banner_visible() and not self._scrape_step3_currency_error_line_numbers():
                raise RuntimeError(
                    "Step 3: wizard Next did not advance and no receipt/reimbursement currency line errors "
                    "were detected (Expense Type-only errors need the grid/cache filled, not date fixes). "
                    "Fix the Chromium window manually or resume from this step."
                )

        raise RuntimeError("Step 3: gave up after too many validation retries.")

    def click_text_in_any_frame(self, text: str, timeout_ms: int = 12000) -> bool:
        if not self.browser_page:
            return False
        for frame in self.browser_page.frames:
            try:
                role_button = frame.get_by_role("button", name=re.compile(text, re.IGNORECASE))
                if role_button.count() > 0:
                    role_button.first.click(timeout=timeout_ms)
                    return True

                role_link = frame.get_by_role("link", name=re.compile(text, re.IGNORECASE))
                if role_link.count() > 0:
                    role_link.first.click(timeout=timeout_ms)
                    return True

                text_locator = frame.get_by_text(text, exact=False)
                if text_locator.count() > 0:
                    text_locator.first.click(timeout=timeout_ms)
                    return True
            except Exception:
                continue
        return False

    def _body_contains_text(self, needle: str) -> bool:
        if not self.browser_page or not needle:
            return False
        nl = needle.lower()
        for frame in self.browser_page.frames:
            try:
                blob = frame.evaluate("() => (document.body && document.body.innerText) || ''") or ""
                if nl in blob.lower():
                    return True
            except Exception:
                continue
        return False

    def _oracle_expand_navigator_row_for_label(self, label_substring: str) -> bool:
        """Click disclosure / first-cell control on Navigator row (Oracle OA Framework tree)."""
        if not self.browser_page:
            return False
        js = """
(needle) => {
  const nl = needle.toLowerCase();
  const rows = Array.from(document.querySelectorAll('tr'));
  for (const tr of rows) {
    const t = (tr.innerText || '').toLowerCase();
    if (!t.includes(nl)) continue;
    const tds = tr.querySelectorAll('td');
    if (!tds.length) continue;
    const first = tds[0];
    const imgs = first.querySelectorAll('img');
    for (const img of imgs) {
      const alt = ((img.getAttribute('alt') || '') + (img.getAttribute('title') || '')).toLowerCase();
      const src = (img.getAttribute('src') || '').toLowerCase();
      if (src.includes('disclose') || src.includes('expand') || alt.includes('expand') || alt.includes('disclose')) {
        img.click();
        return true;
      }
    }
    const a = first.querySelector('a');
    if (a) {
      a.click();
      return true;
    }
    for (const img of tr.querySelectorAll('img')) {
      const src = (img.getAttribute('src') || '').toLowerCase();
      if (src.includes('disclose') || src.includes('expand') || src.includes('folder') || src.includes('menu')) {
        img.click();
        return true;
      }
    }
  }
  return false;
}
"""
        for frame in self.browser_page.frames:
            try:
                if frame.evaluate(js, label_substring):
                    return True
            except Exception:
                continue
        return False

    def _oracle_expand_nic_iexpenses_menu(self) -> None:
        """Ensure the Navigator folder is expanded so children (e.g. Create Expense Report) appear."""
        if not self.browser_page:
            return
        nav_label = getattr(self.settings, "nav_menu_label", "") or "iExpenses"
        if self._body_contains_text("Create Expense Report"):
            return
        self._oracle_expand_navigator_row_for_label(nav_label)
        self.browser_page.wait_for_timeout(900)
        if self._body_contains_text("Create Expense Report"):
            return
        self.click_text_in_any_frame(nav_label)
        self.browser_page.wait_for_timeout(1000)
        if self._body_contains_text("Create Expense Report"):
            return
        self._oracle_expand_navigator_row_for_label(nav_label)
        self.browser_page.wait_for_timeout(700)

    def _frames_preferred_first(self, preferred: Frame | None) -> list[Frame]:
        """Iterate Playwright frames with ``preferred`` first (Step 2 credit table iframe)."""
        if not self.browser_page:
            return []
        frames = list(self.browser_page.frames)
        if preferred is None:
            return frames
        try:
            idx = frames.index(preferred)
            return [frames[idx]] + frames[:idx] + frames[idx + 1 :]
        except ValueError:
            return frames

    def _click_table_pagination_next_via_dom_in_frame(self, frame: Frame) -> bool:
        """Click visible 'Next N' / 'Next ten' control when get_by_role name matching fails."""
        js = r"""
() => {
  const words = 'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split(' ');
  const reNum = /^Next\s+(\d+)/i;
  const reWord = new RegExp('^Next\\s+(' + words.join('|') + ')\\b', 'i');
  const isVis = (el) => {
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
  };
  const nodes = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"]'));
  for (const el of nodes) {
    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!reNum.test(t) && !reWord.test(t)) continue;
    if (!isVis(el)) continue;
    if (el.disabled) continue;
    el.click();
    return true;
  }
  return false;
}
"""
        try:
            return bool(frame.evaluate(js))
        except Exception:
            return False

    def _frame_has_table_next_control_dom(self, frame: Frame) -> bool:
        """True if a visible Next N control exists (does not click)."""
        js = r"""
() => {
  const reNum = /^Next\s+(\d+)/i;
  const isVis = (el) => {
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
  };
  for (const el of document.querySelectorAll('a, button, [role="button"], [role="link"]')) {
    const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!reNum.test(t)) continue;
    if (!isVis(el) || el.disabled) continue;
    return true;
  }
  return false;
}
"""
        try:
            return bool(frame.evaluate(js))
        except Exception:
            return False

    def click_expense_table_pagination_next_in_any_frame(
        self,
        timeout_ms: int = 12000,
        *,
        retry_rounds: int = 12,
        pause_between_ms: int = 700,
        preferred_frame: Frame | None = None,
    ) -> bool:
        """Click the expense-table 'Next N' link/button (not the wizard's standalone 'Next').

        After Save or row updates, Oracle often needs a short delay before pagination is
        visible/enabled; we retry with pauses instead of failing immediately.
        """
        if not self.browser_page:
            return False
        for attempt in range(retry_rounds):
            for frame in self._frames_preferred_first(preferred_frame):
                try:
                    for role in ("link", "button"):
                        for name_pat in (
                            _EXPENSE_TABLE_NEXT_NAME,
                            _EXPENSE_TABLE_NEXT_NAME_LOOSE,
                        ):
                            loc = frame.get_by_role(role, name=name_pat)
                            count = loc.count()
                            for i in range(count):
                                candidate = loc.nth(i)
                                try:
                                    if candidate.is_visible() and candidate.is_enabled():
                                        candidate.click(timeout=timeout_ms)
                                        return True
                                except Exception:
                                    continue
                except Exception:
                    continue
            if self._click_plain_next_pagination_link(timeout_ms, preferred_frame=preferred_frame):
                return True
            for frame in self._frames_preferred_first(preferred_frame):
                try:
                    if self._click_table_pagination_next_via_dom_in_frame(frame):
                        return True
                except Exception:
                    continue
            if attempt < retry_rounds - 1:
                if attempt == 0:
                    self.set_status(
                        "Table pagination (e.g. Next 10) not ready yet — waiting and retrying…"
                    )
                self.browser_page.wait_for_timeout(pause_between_ms)
        return False

    def click_expense_table_pagination_previous_in_any_frame(
        self,
        timeout_ms: int = 12000,
        *,
        retry_rounds: int = 8,
        pause_between_ms: int = 700,
        preferred_frame: Frame | None = None,
    ) -> bool:
        """Click the expense-table 'Previous N' control (not the wizard)."""
        if not self.browser_page:
            return False
        for attempt in range(retry_rounds):
            for frame in self._frames_preferred_first(preferred_frame):
                try:
                    for role in ("link", "button"):
                        for name_pat in (
                            _EXPENSE_TABLE_PREV_NAME,
                            _EXPENSE_TABLE_PREV_NAME_LOOSE,
                        ):
                            loc = frame.get_by_role(role, name=name_pat)
                            for i in range(loc.count()):
                                candidate = loc.nth(i)
                                try:
                                    if candidate.is_visible() and candidate.is_enabled():
                                        candidate.click(timeout=timeout_ms)
                                        return True
                                except Exception:
                                    continue
                except Exception:
                    continue
            if self._click_plain_previous_pagination_link(timeout_ms, preferred_frame=preferred_frame):
                return True
            if attempt < retry_rounds - 1:
                self.browser_page.wait_for_timeout(pause_between_ms)
        return False

    def _click_plain_previous_pagination_link(
        self, timeout_ms: int, *, preferred_frame: Frame | None = None
    ) -> bool:
        if not self.browser_page:
            return False
        plain_prev = re.compile(r"^\s*Previous\s*$", re.IGNORECASE)
        for frame in self._frames_preferred_first(preferred_frame):
            if not self._frame_shows_transaction_page_range(frame):
                continue
            try:
                loc = frame.get_by_role("link", name=plain_prev)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    try:
                        if candidate.is_visible() and candidate.is_enabled():
                            candidate.click(timeout=timeout_ms)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def expense_table_go_to_first_page_in_any_frame(
        self, *, max_steps: int = 80, credit_card_step2: bool = False
    ) -> None:
        """Walk table pagination backward until range starts at 1 or controls are exhausted."""
        if not self.browser_page:
            return

        def _page_range() -> tuple[int, int, int] | None:
            if credit_card_step2:
                return self.get_step2_credit_table_page_range_in_any_frame()
            step3_frame, step3_rows = self._extract_step3_rows_in_any_frame()
            n_vis = len(step3_rows) if step3_rows else 0
            if step3_frame and n_vis > 0:
                pr = self.get_transactions_page_range_in_frame(
                    step3_frame, visible_row_count=n_vis
                )
                if pr:
                    return pr
                pr2 = self.get_transactions_page_range_in_any_frame(
                    visible_row_count=n_vis
                )
                if pr2:
                    return pr2
            return self.get_transactions_page_range_in_any_frame()

        if credit_card_step2:
            picked = self._step2_pick_best_credit_snapshot()
            if picked:
                self._step2_credit_card_frame = picked[0]

        for _ in range(max_steps):
            self._pump_ui_and_check_cancel()
            pr = _page_range()
            if pr is not None and pr[0] <= 1:
                self.browser_page.wait_for_timeout(350)
                return
            prev_pref = self._step2_credit_card_frame if credit_card_step2 else None
            if not self.click_expense_table_pagination_previous_in_any_frame(
                preferred_frame=prev_pref,
            ):
                break
            self.browser_page.wait_for_timeout(500)
        pr = _page_range()
        if pr is not None and pr[0] > 1:
            if credit_card_step2:
                self.log_event(
                    "warn",
                    f"Step 2: pagination still shows range starting at {pr[0]} after Previous; "
                    "continuing scrape from the current table page.",
                )
                return
            raise RuntimeError(
                "Could not reach the first page of the table (pagination Previous did not reach rows 1–N of total). "
                "Fix the Chromium window and try again."
            )

    def wait_for_step1_general_information_ready(self, timeout_ms: int = 120000) -> None:
        """Wait until the wizard shows Step 1 and the form content is present (Oracle iframes)."""
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate(
                        "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                    )
                    if not blob:
                        continue
                    if not _blob_shows_wizard_step(blob, 1):
                        continue
                    if "Purpose" not in blob and "purpose" not in blob.lower():
                        continue
                    self.browser_page.wait_for_load_state("domcontentloaded", timeout=5000)
                    self.browser_page.wait_for_timeout(400)
                    return
                except Exception:
                    continue
            self.browser_page.wait_for_timeout(250)
        raise RuntimeError(
            "Timeout waiting for Create Expense Report — General Information (Step 1) to finish loading."
        )

    def click_save_button_wizard_in_any_frame(
        self,
        timeout_ms: int = 20000,
        body_must_contain: str | None = "Step 1 of 6",
        *,
        wizard_step: int | None = None,
        wizard_total: int = 6,
    ) -> bool:
        """Click the primary Save control (exact name), not links or incidental 'Save' text."""
        if not self.browser_page:
            return False

        def frame_context_matches(blob: str) -> bool:
            if wizard_step is not None:
                return _blob_shows_wizard_step(blob, wizard_step, wizard_total)
            if body_must_contain:
                return body_must_contain in blob
            return True

        name_pat = re.compile(r"^\s*Save\s*$", re.IGNORECASE)
        for frame in self.browser_page.frames:
            try:
                blob = frame.evaluate(
                    "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                )
                if not frame_context_matches(blob or ""):
                    continue
                btn = frame.get_by_role("button", name=name_pat)
                if btn.count() > 0:
                    first = btn.first
                    if first.is_enabled():
                        first.click(timeout=timeout_ms)
                        return True
            except Exception:
                continue
        for frame in self.browser_page.frames:
            try:
                blob = frame.evaluate(
                    "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                )
                if not frame_context_matches(blob or ""):
                    continue
                clicked = frame.evaluate(
                    """
                    () => {
                      const inputs = Array.from(
                        document.querySelectorAll("input[type='submit'], input[type='button']")
                      );
                      for (const el of inputs) {
                        const v = (el.value || '').trim();
                        if (!/^save$/i.test(v)) continue;
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden') continue;
                        if (el.disabled) continue;
                        el.click();
                        return true;
                      }
                      return false;
                    }
                    """
                )
                if clicked:
                    return True
            except Exception:
                continue
        if body_must_contain or wizard_step is not None:
            return self.click_save_button_wizard_in_any_frame(
                timeout_ms=timeout_ms, body_must_contain=None, wizard_step=None
            )
        return False

    def click_cancel_button_wizard_in_any_frame(
        self,
        timeout_ms: int = 20000,
        *,
        wizard_step: int | None = None,
        wizard_total: int = 6,
    ) -> bool:
        """Click the wizard footer Cancel (exact label), scoped to a wizard step when given."""
        if not self.browser_page:
            return False

        def frame_context_matches(blob: str) -> bool:
            if wizard_step is not None:
                return _blob_shows_wizard_step(blob, wizard_step, wizard_total)
            return True

        name_pat = re.compile(r"^\s*Cancel\s*$", re.IGNORECASE)
        for frame in self.browser_page.frames:
            try:
                blob = frame.evaluate(
                    "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                )
                if not frame_context_matches(blob or ""):
                    continue
                btn = frame.get_by_role("button", name=name_pat)
                if btn.count() > 0:
                    first = btn.first
                    if first.is_enabled():
                        first.click(timeout=timeout_ms)
                        return True
                link = frame.get_by_role("link", name=name_pat)
                if link.count() > 0:
                    first = link.first
                    if first.is_enabled():
                        first.click(timeout=timeout_ms)
                        return True
            except Exception:
                continue
        for frame in self.browser_page.frames:
            try:
                blob = frame.evaluate(
                    "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                )
                if not frame_context_matches(blob or ""):
                    continue
                clicked = frame.evaluate(
                    """
                    () => {
                      const norm = (s) =>
                        (s || '')
                          .replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '')
                          .replace(/\\s+/g, ' ')
                          .trim()
                          .toLowerCase();
                      const visible = (el) => {
                        if (!el || el.disabled) return false;
                        const st = window.getComputedStyle(el);
                        const r = el.getBoundingClientRect();
                        return (
                          st.visibility !== 'hidden' &&
                          st.display !== 'none' &&
                          r.width > 2 &&
                          r.height > 2
                        );
                      };
                      const inputs = Array.from(
                        document.querySelectorAll("input[type='submit'], input[type='button'], button, a")
                      );
                      for (const el of inputs) {
                        const v = norm(el.value || el.textContent || '');
                        if (v !== 'cancel') continue;
                        if (!visible(el)) continue;
                        el.click();
                        return true;
                      }
                      return false;
                    }
                    """
                )
                if clicked:
                    return True
            except Exception:
                continue
        if wizard_step is not None:
            return self.click_cancel_button_wizard_in_any_frame(
                timeout_ms=timeout_ms, wizard_step=None, wizard_total=wizard_total
            )
        return False

    _UNSAVED_PROMPT_CLICK_OK_JS = """
() => {
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return (
      st.visibility !== 'hidden' &&
      st.display !== 'none' &&
      r.width > 2 &&
      r.height > 2
    );
  };
  const blob = ((document.body && document.body.innerText) || '').toLowerCase();
  if (
    !blob.includes('have not been saved') &&
    !blob.includes('changes will be discarded')
  ) {
    return false;
  }
  const candidates = Array.from(
    document.querySelectorAll(
      "button, a[href], input[type='button'], input[type='submit'], span[role='button']"
    )
  );
  for (const el of candidates) {
    const raw = (el.textContent || el.value || el.getAttribute('aria-label') || '')
      .replace(/\\s+/g, ' ')
      .trim();
    if (!raw) continue;
    if (!/^ok$/i.test(raw) && !/^yes$/i.test(raw)) continue;
    if (visible(el)) {
      el.click();
      return true;
    }
  }
  return false;
}
"""

    def _dismiss_unsaved_changes_prompt_in_any_frame(self, timeout_ms: int = 15000) -> bool:
        """Oracle or custom modal: message about unsaved changes — click OK. Returns True if a click ran."""
        if not self.browser_page:
            return False
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            for frame in self.browser_page.frames:
                try:
                    if frame.evaluate(self._UNSAVED_PROMPT_CLICK_OK_JS):
                        self.browser_page.wait_for_timeout(400)
                        return True
                except Exception:
                    continue
            self.browser_page.wait_for_timeout(200)
        return False

    def _vpn_collect_finish_step2_cancel_wizard(self) -> None:
        """After scrape: Cancel Step 2 wizard, accept discard prompt; leave Chromium open."""
        page = self.browser_page
        if not page:
            raise RuntimeError("Browser page not available.")

        def on_dialog(dialog) -> None:
            msg = (dialog.message or "").strip()
            short = msg[:120] + ("…" if len(msg) > 120 else "")
            self.log_event("browser", f"Step 2 Cancel: accepting browser dialog ({dialog.type}): {short}")
            dialog.accept()

        page.once("dialog", on_dialog)
        self.set_status("Step 2 scrape done — clicking Cancel to leave the expense report (unsaved portal state)…")
        self.log_event("browser", "VPN collect: clicking wizard Cancel on Credit Card Transactions (Step 2).")
        if not self.click_cancel_button_wizard_in_any_frame(wizard_step=2):
            raise RuntimeError(
                "Could not click Cancel on Credit Card Transactions (Step 2). "
                "Dismiss any overlay in Chromium and retry the scrape."
            )
        page.wait_for_timeout(600)
        if self._dismiss_unsaved_changes_prompt_in_any_frame():
            self.log_event("browser", "VPN collect: dismissed in-page unsaved-changes prompt (OK).")
        page.wait_for_timeout(400)

    def _try_click_wizard_next_in_frame_dom(self, frame: Frame) -> bool:
        """Oracle often uses submit inputs or links for the green Next; role=button may miss them."""
        try:
            return bool(
                frame.evaluate(
                    """
() => {
  const norm = (s) =>
    (s || '')
      .replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '')
      .replace(/\\s+/g, ' ')
      .trim()
      .toLowerCase();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return (
      st.visibility !== 'hidden' &&
      st.display !== 'none' &&
      r.width > 2 &&
      r.height > 2
    );
  };
  const label = (el) =>
    norm(
      el.textContent ||
        el.value ||
        el.getAttribute('aria-label') ||
        el.getAttribute('title') ||
        ''
    );
  const els = Array.from(
    document.querySelectorAll('button, input[type="submit"], input[type="button"], a')
  );
  for (const el of els) {
    const t = label(el);
    if (t !== 'next') continue;
    if (!visible(el)) continue;
    el.click();
    return true;
  }
  return false;
}
                    """
                )
            )
        except Exception:
            return False

    def wait_for_wizard_next_enabled_and_click(
        self,
        timeout_ms: int = 120000,
        body_must_contain: str | None = None,
        *,
        wizard_step: int | None = None,
        wizard_total: int = 6,
    ) -> bool:
        """Poll until the wizard Next control is enabled, then click (Playwright role + DOM fallback)."""
        if not self.browser_page:
            return False

        def frame_context_matches(blob: str) -> bool:
            if wizard_step is not None:
                return _blob_shows_wizard_step(blob, wizard_step, wizard_total)
            if body_must_contain:
                return body_must_contain in blob
            return True

        name_pat = re.compile(r"^\s*Next\s*$", re.IGNORECASE)
        deadline = time.monotonic() + timeout_ms / 1000.0
        last_wait_status = 0.0
        while time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate(
                        "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                    )
                    if not frame_context_matches(blob or ""):
                        continue
                    btn = frame.get_by_role("button", name=name_pat)
                    if btn.count() > 0:
                        first = btn.first
                        if first.is_enabled():
                            first.click(timeout=20000)
                            return True
                    if self._try_click_wizard_next_in_frame_dom(frame):
                        return True
                except Exception:
                    continue
            now = time.monotonic()
            if now - last_wait_status >= 10.0:
                last_wait_status = now
                ctx = (
                    f"step {wizard_step} of {wizard_total}"
                    if wizard_step is not None
                    else (body_must_contain or "wizard")
                )
                self.log_event(
                    "browser",
                    f"Still waiting for enabled green Next ({ctx}); if this repeats, the button may be disabled until you fix validation on the page.",
                )
            self.browser_page.wait_for_timeout(300)
        return False

    def wait_for_step2_credit_card_transactions(self, timeout_ms: int = 120000) -> None:
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate(
                        "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                    )
                    if not blob:
                        continue
                    if _blob_shows_wizard_step(blob, 2):
                        self.browser_page.wait_for_timeout(500)
                        return
                    if not _blob_shows_wizard_step(blob, 1) and "Credit Card Transactions" in blob:
                        self.browser_page.wait_for_timeout(500)
                        return
                except Exception:
                    continue
            self.browser_page.wait_for_timeout(300)
        raise RuntimeError("Timeout waiting for Credit Card Transactions (Step 2) to load.")

    def select_travel_template_in_any_frame(self) -> bool:
        if not self.browser_page:
            return False
        for frame in self.browser_page.frames:
            try:
                script_result = frame.evaluate(
                    """
() => {
  const selects = Array.from(document.querySelectorAll('select'));
  const score = (el) => {
    const txt = ((el.id || '') + ' ' + (el.name || '') + ' ' +
      (el.getAttribute('aria-label') || '') + ' ' +
      (el.closest('tr')?.innerText || '')).toLowerCase();
    return txt.includes('template') || txt.includes('expense type') || txt.includes('type');
  };
  const candidates = selects.filter(score);
  const targetSelect = candidates[0] || selects[0];
  if (!targetSelect) return false;

  const option = Array.from(targetSelect.options).find(o =>
    (o.textContent || '').toLowerCase().includes('travel')
  );
  if (!option) return false;
  targetSelect.value = option.value;
  targetSelect.dispatchEvent(new Event('change', { bubbles: true }));
  targetSelect.dispatchEvent(new Event('input', { bubbles: true }));
  return true;
}
                    """
                )
                if script_result:
                    return True
            except Exception:
                continue
        return False

    def fill_purpose_in_any_frame(self, value: str) -> bool:
        if not self.browser_page:
            return False
        wanted = re.sub(r"\s+", " ", str(value or "").strip()).lower()
        for frame in self.browser_page.frames:
            try:
                for locator in [
                    frame.get_by_label(re.compile("purpose", re.IGNORECASE)),
                    frame.locator("input[name*='purpose' i], input[id*='purpose' i], textarea[name*='purpose' i], textarea[id*='purpose' i]"),
                ]:
                    if locator.count() > 0:
                        target = locator.first
                        target.click(timeout=5000)
                        current = ""
                        try:
                            current = target.input_value(timeout=1200) or ""
                        except Exception:
                            current = target.text_content(timeout=1200) or ""
                        have = re.sub(r"\s+", " ", str(current).strip()).lower()
                        if wanted and have == wanted:
                            return True
                        target.fill(value, timeout=5000)
                        return True
            except Exception:
                continue
        return False

    _APPROVER_DOM_MARK = "data-rpa-approver-target"

    def _mark_approver_field_via_dom(self, frame: Frame) -> bool:
        """Tag the Approver value control with a temporary attribute (Oracle / LOV UIs often lack labels/roles)."""
        try:
            return bool(
                frame.evaluate(
                    f"""
() => {{
  const MARK = "{self._APPROVER_DOM_MARK}";
  const doc = document;
  const allRoots = [];
  function walk(root) {{
    allRoots.push(root);
    const tree = root.querySelectorAll("*");
    for (let i = 0; i < tree.length; i++) {{
      const el = tree[i];
      if (el.shadowRoot) walk(el.shadowRoot);
    }}
  }}
  walk(doc);

  for (let r = 0; r < allRoots.length; r++) {{
    allRoots[r].querySelectorAll("[" + MARK + "]").forEach((e) => e.removeAttribute(MARK));
  }}

  const visible = (el) => {{
    const s = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (s.visibility === "hidden" || s.display === "none") return false;
    if (rect.width < 2 || rect.height < 2) return false;
    return true;
  }};
  const isOkInput = (inp) => {{
    if (inp.getAttribute && inp.getAttribute("contenteditable") === "true") return true;
    const tag = inp.tagName;
    if (tag === "TEXTAREA") return true;
    if (tag !== "INPUT") return false;
    const t = (inp.type || "text").toLowerCase();
    return !["hidden", "submit", "button", "checkbox", "radio", "file", "image"].includes(t);
  }};

  function findInRoot(root) {{
    const titled = root.querySelectorAll("input[title], textarea[title]");
    for (let q = 0; q < titled.length; q++) {{
      const inp = titled[q];
      const tit = (inp.getAttribute("title") || "").toLowerCase();
      if (tit.includes("approver") && isOkInput(inp) && visible(inp)) return inp;
    }}
    const cells = "td, th, span, label, div, li";
    const nodes = root.querySelectorAll(cells);
    for (let i = 0; i < nodes.length; i++) {{
      const el = nodes[i];
      if (!visible(el)) continue;
      const t = (el.textContent || "").trim().replace(/\\s+/g, " ");
      if (t.length > 72) continue;
      if (!/^Approver\\b/i.test(t)) continue;

      const row = el.closest("tr");
      if (row && visible(row)) {{
        const tds = Array.from(row.querySelectorAll("td, th"));
        const idx = tds.findIndex((c) => c.contains(el));
        if (idx >= 0) {{
          for (let j = idx + 1; j < tds.length; j++) {{
            const inps = tds[j].querySelectorAll("input, textarea");
            for (let k = 0; k < inps.length; k++) {{
              const inp = inps[k];
              if (isOkInput(inp) && visible(inp)) return inp;
            }}
          }}
        }}
        const inRow = Array.from(row.querySelectorAll("input, textarea")).filter(
          (inp) => isOkInput(inp) && visible(inp)
        );
        if (inRow.length === 1) return inRow[0];
      }}

      let sib = el.nextElementSibling;
      for (let d = 0; d < 6 && sib; d++, sib = sib.nextElementSibling) {{
        if (sib.tagName === "INPUT" || sib.tagName === "TEXTAREA") {{
          if (isOkInput(sib) && visible(sib)) return sib;
        }}
        const inner = sib.querySelector && sib.querySelector("input, textarea");
        if (inner && isOkInput(inner) && visible(inner)) return inner;
      }}
    }}
    return null;
  }}

  for (let i = 0; i < allRoots.length; i++) {{
    const found = findInRoot(allRoots[i]);
    if (found) {{
      found.setAttribute(MARK, "1");
      return true;
    }}
  }}
  return false;
}}
"""
                )
            )
        except Exception:
            return False

    def fill_approver_in_any_frame(self, value: str) -> bool:
        if not self.browser_page:
            return False
        first_token = (value.split(",")[0].strip() or value.strip() or value)
        approver_query = first_token[:10] if len(first_token) > 10 else first_token
        if not approver_query:
            approver_query = value
        approver_name = re.compile(r"approver", re.IGNORECASE)
        mark_sel = f'[{self._APPROVER_DOM_MARK}="1"]'
        name_pat = re.compile(re.escape(value), re.IGNORECASE)

        def run_approver_interaction(fr: Frame, target) -> bool:
            try:
                current = ""
                try:
                    current = target.input_value(timeout=1200) or ""
                except Exception:
                    current = target.text_content(timeout=1200) or ""
                if current and value.lower() in current.lower():
                    return True
                target.click(timeout=4000)
                try:
                    target.fill("", timeout=3500)
                except Exception:
                    target.press("ControlOrMeta+a")
                    target.press("Backspace")
                # fill() is much faster than type(delay=…); LOV usually listens to input events from fill.
                try:
                    target.fill(approver_query, timeout=3500)
                except Exception:
                    target.press_sequentially(approver_query, delay=12, timeout=8000)
                self.browser_page.wait_for_timeout(280)

                selected_from_dropdown = False
                popup_roots = fr.locator(
                    '[role="listbox"], [role="tree"], [role="grid"], '
                    '[class*="popup" i], [class*="Popup" i], [class*="lov" i]'
                )
                try:
                    nroots = popup_roots.count()
                    for ri in range(min(nroots, 6)):
                        root = popup_roots.nth(ri)
                        if not root.is_visible(timeout=400):
                            continue
                        for opt in (
                            root.get_by_role("option", name=name_pat),
                            root.get_by_role("row", name=name_pat),
                        ):
                            try:
                                if opt.count() > 0:
                                    opt.first.click(timeout=3500)
                                    selected_from_dropdown = True
                                    break
                            except Exception:
                                continue
                        if selected_from_dropdown:
                            break
                except Exception:
                    pass

                if not selected_from_dropdown:
                    for suggestion in (
                        fr.get_by_role("option", name=name_pat),
                        fr.get_by_role("row", name=name_pat),
                    ):
                        try:
                            if suggestion.count() > 0 and suggestion.first.is_visible(timeout=800):
                                suggestion.first.click(timeout=3500)
                                selected_from_dropdown = True
                                break
                        except Exception:
                            continue

                if not selected_from_dropdown:
                    try:
                        narrow = fr.locator("div").filter(has_text=name_pat).first
                        if narrow.is_visible(timeout=500):
                            narrow.click(timeout=3000)
                            selected_from_dropdown = True
                    except Exception:
                        pass

                if not selected_from_dropdown:
                    target.press("ArrowDown")
                    self.browser_page.wait_for_timeout(120)
                    target.press("Enter")
                return True
            except Exception:
                return False

        def approver_locator_candidates(fr: Frame):
            yield fr.get_by_label(approver_name)
            yield fr.get_by_role("combobox", name=approver_name)
            yield fr.get_by_role("searchbox", name=approver_name)
            yield fr.get_by_role("textbox", name=approver_name)
            yield fr.locator(
                "input[aria-label*='approver' i], input[placeholder*='approver' i], "
                "[role='combobox'][aria-label*='approver' i]"
            )
            yield fr.locator(
                "input[name*='approver' i], input[id*='approver' i], "
                "input[name*='manager' i], input[id*='manager' i]"
            )
            yield fr.locator("tr").filter(
                has_text=re.compile(r"\bApprover\b", re.IGNORECASE)
            ).locator("input, [role='textbox'], [role='combobox'], [contenteditable='true']")

        def clear_marks_in_frame(fr: Frame) -> None:
            try:
                fr.evaluate(
                    f"""
() => {{
  const MARK = "{self._APPROVER_DOM_MARK}";
  function clearUnder(root) {{
    root.querySelectorAll("[" + MARK + "]").forEach((e) => e.removeAttribute(MARK));
    root.querySelectorAll("*").forEach((el) => {{
      if (el.shadowRoot) clearUnder(el.shadowRoot);
    }});
  }}
  clearUnder(document);
}}
"""
                )
            except Exception:
                pass

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate(
                        "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                    )
                except Exception:
                    continue
                # Skip frames that are clearly past Step 1 (wrong wizard or chrome).
                if any(_blob_shows_wizard_step(blob, s) for s in range(2, 7)):
                    continue

                if _frame_inner_text_has_approver_label(blob):
                    for locator in approver_locator_candidates(frame):
                        try:
                            if locator.count() == 0:
                                continue
                            target = locator.first
                            if not target.is_visible(timeout=500):
                                continue
                        except Exception:
                            continue
                        if run_approver_interaction(frame, target):
                            clear_marks_in_frame(frame)
                            return True

                # LOV fields often expose "Approver" only on the input title, not in body innerText.
                clear_marks_in_frame(frame)
                if self._mark_approver_field_via_dom(frame):
                    try:
                        dom_loc = frame.locator(mark_sel)
                        if dom_loc.count() > 0 and dom_loc.first.is_visible(timeout=1200):
                            if run_approver_interaction(frame, dom_loc.first):
                                clear_marks_in_frame(frame)
                                return True
                    finally:
                        clear_marks_in_frame(frame)

            self.browser_page.wait_for_timeout(220)

        return False

    def get_transactions_page_range_in_frame(
        self, frame: Frame, *, visible_row_count: int | None = None
    ) -> tuple[int, int, int] | None:
        """Parse 'X - Y of Z' from a single frame.

        Oracle pages often contain several ranges (hidden chrome, other widgets). Without a row
        count, we take all matches, prefer the largest Z (total), then the largest Y — but a bogus
        full-range string (e.g. '1 - 41 of 41') can steal that pick and skip table pagination.

        When ``visible_row_count`` is set (number of data rows on the current table page), we only
        keep triples where ``Y - X + 1`` equals that count, then apply the same max-Z / max-Y
        tie-break among those. That matches the real footer (e.g. '1 - 10 of 41' for 10 rows).
        """
        try:
            text_match = frame.evaluate(
                """
(expectedVisible) => {
  const txt = document.body?.innerText || '';
  const re = /(\\d+)\\s*-\\s*(\\d+)\\s+of\\s+(\\d+)/gi;
  const triples = [];
  let m;
  while ((m = re.exec(txt)) !== null) {
    triples.push([Number(m[1]), Number(m[2]), Number(m[3])]);
  }
  if (!triples.length) return null;

  const pickFrom = (cands) => {
    if (!cands.length) return null;
    const maxTotal = Math.max(...cands.map((t) => t[2]));
    const same = cands.filter((t) => t[2] === maxTotal);
    return same.reduce((a, b) => (b[1] > a[1] ? b : a));
  };

  if (
    expectedVisible != null &&
    Number.isFinite(expectedVisible) &&
    expectedVisible > 0
  ) {
    const ev = Math.floor(Number(expectedVisible));
    const filtered = triples.filter((t) => t[1] - t[0] + 1 === ev);
    return pickFrom(filtered);
  }
  return pickFrom(triples);
}
                """,
                visible_row_count,
            )
            if text_match and len(text_match) == 3:
                return int(text_match[0]), int(text_match[1]), int(text_match[2])
        except Exception:
            pass
        return None

    def get_transactions_page_range_in_any_frame(
        self, *, visible_row_count: int | None = None
    ) -> tuple[int, int, int] | None:
        """Best page-range across iframes.

        With ``visible_row_count``, each frame is parsed with that filter so we do not merge a
        spurious full-range from another iframe with the real Step 3 footer.

        Step 2 credit card lines should use ``get_step2_credit_table_page_range_in_any_frame`` instead,
        so unrelated iframe text does not masquerade as the transaction table footer.
        """
        if not self.browser_page:
            return None
        triples: list[tuple[int, int, int]] = []
        for frame in self.browser_page.frames:
            try:
                parsed = self.get_transactions_page_range_in_frame(
                    frame, visible_row_count=visible_row_count
                )
                if parsed:
                    triples.append(parsed)
            except Exception:
                continue
        if not triples:
            return None
        max_total = max(t[2] for t in triples)
        best = [t for t in triples if t[2] == max_total]
        return max(best, key=lambda t: t[1])

    def _frame_shows_transaction_page_range(self, frame: Frame) -> bool:
        try:
            blob = frame.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
            )
            return bool(
                blob and re.search(r"\d+\s*-\s*\d+\s+of\s+\d+", blob, re.IGNORECASE)
            )
        except Exception:
            return False

    def _credit_card_table_pagination_can_advance(
        self, *, preferred_frame: Frame | None = None
    ) -> bool:
        """True if an enabled table pager exists (Next N or plain Next link near row counts)."""
        if not self.browser_page:
            return False
        for frame in self._frames_preferred_first(preferred_frame):
            try:
                for role in ("link", "button"):
                    for name_pat in (
                        _EXPENSE_TABLE_NEXT_NAME,
                        _EXPENSE_TABLE_NEXT_NAME_LOOSE,
                    ):
                        loc = frame.get_by_role(role, name=name_pat)
                        for i in range(loc.count()):
                            candidate = loc.nth(i)
                            try:
                                if candidate.is_visible() and candidate.is_enabled():
                                    return True
                            except Exception:
                                continue
            except Exception:
                continue
        plain_next = re.compile(r"^\s*Next\s*$", re.IGNORECASE)
        for frame in self._frames_preferred_first(preferred_frame):
            if not self._frame_shows_transaction_page_range(frame):
                continue
            try:
                loc = frame.get_by_role("link", name=plain_next)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    try:
                        if candidate.is_visible() and candidate.is_enabled():
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        for frame in self._frames_preferred_first(preferred_frame):
            try:
                if self._frame_has_table_next_control_dom(frame):
                    return True
            except Exception:
                continue
        return False

    def _click_plain_next_pagination_link(
        self, timeout_ms: int, *, preferred_frame: Frame | None = None
    ) -> bool:
        """Oracle sometimes uses a plain 'Next' link (not 'Next 10') for the transaction table."""
        if not self.browser_page:
            return False
        plain_next = re.compile(r"^\s*Next\s*$", re.IGNORECASE)
        for frame in self._frames_preferred_first(preferred_frame):
            if not self._frame_shows_transaction_page_range(frame):
                continue
            try:
                loc = frame.get_by_role("link", name=plain_next)
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    try:
                        if candidate.is_visible() and candidate.is_enabled():
                            candidate.click(timeout=timeout_ms)
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def select_transactions_header_checkbox_in_any_frame(
        self, preferred_frame: Frame | None = None
    ) -> bool:
        if not self.browser_page:
            return False
        for frame in self._frames_preferred_first(preferred_frame):
            try:
                selected = frame.evaluate(self._STEP2_SELECT_ALL_TBODY_CHECKBOXES_JS)
                if selected:
                    return True
                fallback = frame.evaluate(
                    """
() => {
  const isVisible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const table = document.querySelector('table');
  if (!table) return false;

  // Never toggle the header checkbox here. We only enforce row-level checkboxes.
  const rowBoxes = Array.from(table.querySelectorAll('tbody input[type="checkbox"], tr td input[type="checkbox"]'))
    .filter((cb) => isVisible(cb));
  if (!rowBoxes.length) return false;

  let touched = false;
  for (const cb of rowBoxes) {
    if (!cb.checked) {
      cb.click();
      touched = true;
    }
  }
  return touched || rowBoxes.some((cb) => cb.checked);
}
                    """
                )
                if fallback:
                    return True
            except Exception:
                continue
        return False

    def _step2_target_signature_counter_for_selected_rows(self) -> Counter[tuple[str, str, str]]:
        """
        Build a multiset of (merchant, date, amount|currency) signatures for rows that should be
        added in Step 4.2. Preference order:
        1) UI Include column (current table state)
        2) Saved approved matches (fallback, e.g. resume/restart flows)
        """
        include_ids: set[str] = set()
        if hasattr(self, "expense_report_tree"):
            for lid in self.expense_report_tree.get_children():
                if self._assign_row_include.get(lid, False):
                    include_ids.add(str(lid).strip())
        if not include_ids:
            approved = load_approved_matches(APP_DIR)
            for lid, block in approved.items():
                src = str((block or {}).get("source_file") or "").strip()
                if src:
                    include_ids.add(str(lid).strip())
        if not include_ids:
            return Counter()

        lines, _ = load_expense_lines_cache(APP_DIR)
        line_by_id = {
            str(line.get("line_id", "") or "").strip(): line
            for line in lines
            if str(line.get("line_id", "") or "").strip()
        }
        sigs: Counter[tuple[str, str, str]] = Counter()
        for lid in include_ids:
            line = line_by_id.get(lid)
            if not line:
                continue
            sig = signature_from_cached_line(line)
            if any(sig):
                sigs[sig] += 1
        return sigs

    _STEP2_SET_VISIBLE_ROW_CHECKBOXES_BY_INDEX_JS = """
([rowIndexes]) => {
  const norm = (v) => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
  };
  const wanted = new Set((Array.isArray(rowIndexes) ? rowIndexes : []).map((n) => Number(n)));
  let best = null;
  let score = -1;
  for (const table of document.querySelectorAll('table')) {
    const hr = table.querySelector('thead tr') || table.querySelector('tr');
    if (!hr) continue;
    const ht = Array.from(hr.querySelectorAll('th, td')).map((c) => norm(c.textContent || ''));
    const hasM = ht.some((t) => t.includes('merchant') || t.includes('vendor'));
    let s = hasM ? 4 : 0;
    if (table.querySelector('input[type="checkbox"]')) s += 1;
    if (s > score) {
      score = s;
      best = table;
    }
  }
  if (!best || score < 4) return { ok: false, total: 0, selected: 0, wanted: wanted.size };
  let rows = [];
  if (best.tBodies && best.tBodies.length > 0) {
    rows = Array.from(best.tBodies[0].querySelectorAll('tr'));
  } else {
    rows = Array.from(best.querySelectorAll('tr')).filter((tr) => tr.querySelector('td'));
  }
  let total = 0;
  let selected = 0;
  let toggled = 0;
  rows.forEach((tr, idx) => {
    const cb = tr.querySelector('input[type="checkbox"]');
    if (!cb || !isVisible(cb)) return;
    total += 1;
    const shouldCheck = wanted.has(idx);
    if (cb.checked !== shouldCheck) {
      cb.click();
      toggled += 1;
    }
    if (cb.checked === shouldCheck && shouldCheck) selected += 1;
  });
  return { ok: true, total, selected, wanted: wanted.size, toggled };
}
"""

    def _step2_apply_row_selection_for_current_page(
        self,
        frame: Frame,
        *,
        selected_row_indexes: set[int],
    ) -> bool:
        if self._step3_vpn_mode == "vpn_collect":
            return self.select_transactions_header_checkbox_in_any_frame(frame)
        payload = sorted(int(i) for i in selected_row_indexes if int(i) >= 0)
        try:
            result = frame.evaluate(self._STEP2_SET_VISIBLE_ROW_CHECKBOXES_BY_INDEX_JS, payload)
        except Exception:
            return False
        if isinstance(result, dict):
            return bool(result.get("ok"))
        return False

    def _ingest_scraped_credit_row(self, raw: dict, page_idx: int) -> None:
        ri = int(raw.get("row_index", 0))
        merchant = str(raw.get("merchant_name", "")).strip()
        if not merchant:
            return
        date_s = str(raw.get("transaction_date", "") or "").strip()
        cur = normalize_currency_code(raw.get("currency"))
        amt = str(raw.get("amount", "") or "").strip()
        fp = hashlib.sha256(f"{merchant}|{date_s}|{cur}|{amt}".encode("utf-8")).hexdigest()[:20]
        line_id = f"p{page_idx}:r{ri}"
        self._scraped_expense_lines.append(
            {
                "line_id": line_id,
                "merchant_name": merchant,
                "transaction_date": date_s,
                "currency": cur,
                "amount": amt,
                "fingerprint": fp,
                "page_index": page_idx,
                "row_index": ri,
            }
        )

    def _persist_scraped_lines_after_step2(self) -> None:
        path = save_expense_lines_cache(APP_DIR, self._scraped_expense_lines, source="step2_credit_card")
        for p in prune_receipt_sidecars_after_step2_scrape(APP_DIR, self._scraped_expense_lines):
            self.log_event(
                "cache",
                f"Step 2 updated expense lines — pruned stale line_id entries from {p.name}",
            )
        if self._scraped_expense_lines:
            self.log_event(
                "step",
                f"Scraped {len(self._scraped_expense_lines)} expense line(s) -> {path}",
            )
        else:
            self.log_event(
                "warn",
                f"No rows scraped from Step 2 tables (check merchant/date/amount headers). "
                f"Wrote empty cache: {path}",
            )
        self._session_progress_scrape_done = True
        try:
            self.root.after(0, self.refresh_all_tabs)
        except tk.TclError:
            pass

    _STEP2_SELECT_ALL_TBODY_CHECKBOXES_JS = """
() => {
  const norm = (v) => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
  };
  let best = null;
  let score = -1;
  for (const table of document.querySelectorAll('table')) {
    const hr = table.querySelector('thead tr') || table.querySelector('tr');
    if (!hr) continue;
    const ht = Array.from(hr.querySelectorAll('th, td')).map((c) => norm(c.textContent || ''));
    const hasM = ht.some((t) => t.includes('merchant') || t.includes('vendor'));
    let s = hasM ? 4 : 0;
    if (table.querySelector('input[type="checkbox"]')) s += 1;
    if (s > score) {
      score = s;
      best = table;
    }
  }
  if (!best || score < 4) return false;
  const boxes = best.querySelectorAll('tbody input[type="checkbox"]');
  let n = 0;
  boxes.forEach((cb) => {
    if (!isVisible(cb)) return;
    if (!cb.checked) {
      cb.click();
      n++;
    } else {
      n++;
    }
  });
  return n > 0;
}
"""

    _STEP2_CREDIT_TABLE_SNAPSHOT_JS = """
() => {
  const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();
  const isVisible = (el) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none'
      && rect.width > 2 && rect.height > 2;
  };

  let best = null;
  let bestScore = -1;
  const tables = Array.from(document.querySelectorAll('table'));
  for (const table of tables) {
    const headerRow = table.querySelector('thead tr') || table.querySelector('tr');
    if (!headerRow) continue;
    const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
    const ht = headerCells.map((c) => norm(c.textContent || ''));
    const mi = ht.findIndex((t) =>
      t.includes('merchant') || t.includes('vendor') || t.includes('payee') || t.includes('supplier'));
    const di = ht.findIndex((t) =>
      /\\bdate\\b/.test(t) || t.includes('trans date') || t.includes('transaction date')
      || t.includes('posting date') || t.includes('post date'));
    const ci = ht.findIndex((t) =>
      t.includes('currency') || /^curr/.test(t) || /\\bccy\\b/.test(t));
    const ai = ht.findIndex((t) =>
      /\\bamount\\b/.test(t) || t.includes('trans amt') || t.includes('txn amt')
      || t.includes('transaction amt') || /\\bamt\\b/.test(t));
    let score = 0;
    if (mi >= 0) score += 4;
    if (di >= 0) score += 2;
    if (ci >= 0) score += 1;
    if (ai >= 0) score += 3;
    if (table.querySelector('input[type="checkbox"]')) score += 1;
    if (score > bestScore) {
      bestScore = score;
      best = { table, mi, di, ci, ai };
    }
  }
  if (!best || best.mi < 0) return { ok: false };

  const { table, mi, di, ci, ai } = best;
  const rect = table.getBoundingClientRect();
  const visibleArea = isVisible(table) ? Math.round(rect.width * rect.height) : 0;

  let pageRange = null;
  let el = table;
  for (let depth = 0; depth < 12 && el; depth++) {
    const txt = el.innerText || '';
    const triples = [];
    const r = new RegExp('(\\d+)\\s*[-\\u2013\\u2014]\\s*(\\d+)\\s+of\\s+(\\d+)', 'gi');
    let m;
    while ((m = r.exec(txt)) !== null) {
      triples.push([Number(m[1]), Number(m[2]), Number(m[3])]);
    }
    if (triples.length) {
      const maxTotal = Math.max(...triples.map((t) => t[2]));
      const same = triples.filter((t) => t[2] === maxTotal);
      pageRange = same.reduce((a, b) => (b[1] > a[1] ? b : a));
      break;
    }
    el = el.parentElement;
  }

  let bodyRows = [];
  if (table.tBodies && table.tBodies.length > 0) {
    bodyRows = Array.from(table.tBodies[0].querySelectorAll('tr'));
  } else {
    const withTd = Array.from(table.querySelectorAll('tr')).filter((tr) => tr.querySelector('td'));
    bodyRows = withTd.length ? withTd : Array.from(table.querySelectorAll('tr')).slice(1);
  }
  const rows = [];
  bodyRows.forEach((tr, rowIndex) => {
    const cells = Array.from(tr.querySelectorAll('td'));
    if (!cells.length) return;
    const merchant = mi < cells.length ? clean(cells[mi].innerText || cells[mi].textContent) : '';
    if (!merchant) return;
    const dateStr = di >= 0 && di < cells.length ? clean(cells[di].innerText || cells[di].textContent) : '';
    const cur = ci >= 0 && ci < cells.length ? clean(cells[ci].innerText || cells[ci].textContent) : '';
    const amt = ai >= 0 && ai < cells.length ? clean(cells[ai].innerText || cells[ai].textContent) : '';
    rows.push({
      row_index: rowIndex,
      merchant_name: merchant,
      transaction_date: dateStr,
      currency: cur,
      amount: amt,
    });
  });

  return { ok: true, visibleArea, pageRange, rows };
}
"""

    def _step2_pick_best_credit_snapshot(self) -> tuple[Frame, dict] | None:
        """Choose the credit-card grid iframe whose table has the largest on-screen area.

        Oracle embeds multiple iframes with similar tables; the first match is often hidden
        or another program tab, producing rows that do not match the visible page.
        """
        if not self.browser_page:
            return None
        ranked: list[tuple[Frame, dict, int, int]] = []
        for frame in self.browser_page.frames:
            try:
                data = frame.evaluate(self._STEP2_CREDIT_TABLE_SNAPSHOT_JS)
                if not data or not isinstance(data, dict) or not data.get("ok"):
                    continue
                rows = data.get("rows") or []
                pr = data.get("pageRange")
                if not rows and not pr:
                    continue
                va = int(data.get("visibleArea") or 0)
                ranked.append((frame, data, va, len(rows)))
            except Exception:
                continue
        if not ranked:
            return None
        ranked.sort(key=lambda x: (x[2], x[3]), reverse=True)
        fr, data, _, _ = ranked[0]
        return (fr, data)

    def get_step2_credit_table_page_range_in_any_frame(self) -> tuple[int, int, int] | None:
        """Pagination 'X - Y of Z' for the visible Step 2 credit card grid only."""
        picked = self._step2_pick_best_credit_snapshot()
        if not picked:
            return self.get_transactions_page_range_in_any_frame()
        _, data = picked
        pr = data.get("pageRange")
        if pr and isinstance(pr, (list, tuple)) and len(pr) == 3:
            return (int(pr[0]), int(pr[1]), int(pr[2]))
        return self.get_transactions_page_range_in_any_frame()

    def _step2_paging_fully_done(
        self, page_range: tuple[int, int, int] | None, lines_scraped_total: int
    ) -> bool:
        """Oracle sometimes reports end>=total after Save while fewer rows were collected (log: 10 vs 21)."""
        if not page_range or page_range[2] <= 0:
            return False
        _start, end, total = page_range
        return end >= total and lines_scraped_total >= total

    def complete_credit_card_transactions_step(self) -> None:
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        self._emit_automation_event(
            kind="scrape.start",
            message="Scraping transactions started.",
            phase="OracleScraping",
        )

        if self._step3_vpn_mode == "vpn_collect" and not self._wizard_any_frame_on_step(2):
            raise RuntimeError(
                "VPN scrape expects Credit Card Transactions (Step 2 of 6). "
                "Navigate to Credit Card Transactions (Step 2 of 6) in the wizard, then try again."
            )

        self._scraped_expense_lines = []
        self._step2_credit_card_frame = None
        remaining_targets = Counter()
        if self._step3_vpn_mode != "vpn_collect":
            remaining_targets = self._step2_target_signature_counter_for_selected_rows()
            if not remaining_targets:
                raise RuntimeError(
                    "No checked Include rows are available for Step 4.2 selection. "
                    "Check Include boxes in the Expense Report tab, then retry Create report."
                )
        max_pages = 150
        self.set_status("Step 2: moving to first page of credit card transactions…")
        self.expense_table_go_to_first_page_in_any_frame(credit_card_step2=True)
        if self.browser_page:
            self.browser_page.wait_for_timeout(500)
        for page_idx in range(max_pages):
            self._emit_automation_event(
                kind="scrape.page.start",
                message=f"Scraping transactions page {page_idx + 1}.",
                phase="OracleScraping",
                data={"page_index": page_idx + 1},
            )
            locate = self._step2_pick_best_credit_snapshot()
            if not locate:
                raise RuntimeError(
                    "Could not find the credit card transactions table. "
                    "Stay on Step 2 (Credit Card Transactions) with the grid visible in Chromium."
                )
            self._step2_credit_card_frame = locate[0]
            credit_frame, snap = locate
            self._step2_credit_card_frame = credit_frame

            page_range: tuple[int, int, int] | None = None
            pr = snap.get("pageRange")
            if pr and isinstance(pr, (list, tuple)) and len(pr) == 3:
                page_range = (int(pr[0]), int(pr[1]), int(pr[2]))
            scraped = list(snap.get("rows") or [])
            if page_range:
                start, end, total = page_range
                self.set_status(
                    f"Selecting credit card transactions {start}-{end} of {total}..."
                )
            else:
                self.set_status("Selecting all transactions on current page...")

            selected_indexes: set[int] = set()
            if self._step3_vpn_mode == "vpn_collect":
                selected_indexes = {
                    int(raw.get("row_index", -1))
                    for raw in scraped
                    if int(raw.get("row_index", -1)) >= 0
                }
            else:
                for raw in scraped:
                    sig = signature_from_cached_line(
                        {
                            "merchant_name": str(raw.get("merchant_name", "") or ""),
                            "transaction_date": str(raw.get("transaction_date", "") or ""),
                            "amount": str(raw.get("amount", "") or ""),
                            "currency": normalize_currency_code(raw.get("currency")),
                        }
                    )
                    if remaining_targets.get(sig, 0) <= 0:
                        continue
                    idx = int(raw.get("row_index", -1))
                    if idx < 0:
                        continue
                    selected_indexes.add(idx)
                    remaining_targets[sig] -= 1
                    if remaining_targets[sig] <= 0:
                        del remaining_targets[sig]

            if not self._step2_apply_row_selection_for_current_page(
                self._step2_credit_card_frame,
                selected_row_indexes=selected_indexes,
            ):
                raise RuntimeError("Could not apply transaction row selection on this page.")

            self.browser_page.wait_for_timeout(400)
            picked = self._step2_pick_best_credit_snapshot()
            if not picked:
                raise RuntimeError(
                    "Could not read the credit card transactions table after selecting rows."
                )
            credit_frame, snap = picked
            self._step2_credit_card_frame = credit_frame

            self.log_event(
                "browser",
                f"Step 2 scrape page {page_idx + 1}: {len(scraped)} row(s) from visible credit table.",
            )
            for raw in scraped:
                self._ingest_scraped_credit_row(raw, page_idx)

            if self._step3_vpn_mode == "vpn_collect" and self._scraped_expense_lines:
                self.refresh_expense_report_tab(
                    progress_lines=list(self._scraped_expense_lines),
                )

            self.set_status("Saving selected transactions for current page...")
            if not self.click_text_in_any_frame("Save"):
                raise RuntimeError("Could not click Save while processing transactions.")
            self.browser_page.wait_for_timeout(900)

            page_range = self.get_step2_credit_table_page_range_in_any_frame()
            have = len(self._scraped_expense_lines)
            fully_done = self._step2_paging_fully_done(page_range, have)
            if fully_done:
                break

            self.set_status("Moving to next page of transactions (table pagination)...")
            clicked_next = self.click_expense_table_pagination_next_in_any_frame(
                preferred_frame=self._step2_credit_card_frame,
            )
            if clicked_next:
                self.browser_page.wait_for_timeout(900)
                continue
            self._emit_automation_event(
                kind="scrape.page.retry",
                message=f"Retrying page {page_idx + 1}: table pagination did not advance.",
                phase="OracleScraping",
                data={"page_index": page_idx + 1},
            )
            self.set_status(f"Retrying page {page_idx + 1} (table pagination)…")
            clicked_next = self.click_expense_table_pagination_next_in_any_frame(
                preferred_frame=self._step2_credit_card_frame,
            )
            if clicked_next:
                self.browser_page.wait_for_timeout(900)
                continue

            page_range = self.get_step2_credit_table_page_range_in_any_frame()
            have2 = len(self._scraped_expense_lines)
            if self._step2_paging_fully_done(page_range, have2):
                break
            # Last page: row count shows end == total but a stale range elsewhere confused us,
            # or the small table Next is disabled — proceed to wizard Next, do not error.
            can_adv = self._credit_card_table_pagination_can_advance(
                preferred_frame=self._step2_credit_card_frame,
            )
            if not can_adv:
                self.set_status(
                    "On last transaction page (no enabled table pagination) — continuing to wizard Next…"
                )
                break
            if page_range and page_range[1] < page_range[2]:
                raise RuntimeError(
                    "Could not click table pagination (e.g. 'Next 10', 'Next 3') but more "
                    "transactions remain on later pages. Fix the Chromium window and resume."
                )
            break

        if self._step3_vpn_mode != "vpn_collect" and remaining_targets:
            unmatched = sum(int(v) for v in remaining_targets.values())
            raise RuntimeError(
                "Step 4.2 could not find all checked Include rows in Oracle transactions table. "
                f"Missing {unmatched} row(s); refresh/scrape and verify the report tab selections."
            )

        self._persist_scraped_lines_after_step2()
        self._emit_automation_event(
            kind="scrape.complete",
            message=f"Scraping complete: {len(self._scraped_expense_lines)} transaction rows captured.",
            phase="OracleScraping",
            data={"row_count": len(self._scraped_expense_lines)},
        )

        if self._step3_vpn_mode == "vpn_collect":
            self._vpn_collect_finish_step2_cancel_wizard()
            self.set_status(
                "VPN collect: scrape saved to disk — left Chromium open after Cancel (discard portal edits). "
                "Match lines (VPN off) when ready."
            )
            return

        self.set_status("Final save on Credit Card Transactions step...")
        if not self.click_text_in_any_frame("Save"):
            raise RuntimeError("Could not click final Save on transactions step.")
        self.browser_page.wait_for_timeout(900)

        self.set_status("Advancing from Step 2 to Step 3...")
        if not self.wait_for_wizard_next_enabled_and_click(wizard_step=2):
            raise RuntimeError("Could not click Next on transactions step.")

    def _wait_until_wizard_step_visible(self, step: int, timeout_ms: int = 120000) -> None:
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate(
                        "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                    )
                    if blob and _blob_shows_wizard_step(blob, step):
                        self.browser_page.wait_for_timeout(400)
                        return
                except Exception:
                    continue
            self.browser_page.wait_for_timeout(300)
        raise RuntimeError(f"Timeout waiting for wizard Step {step} to appear.")

    def _extract_step6_expense_lines_rows(self) -> list[dict]:
        """Parse Step 6 review table: Date, Receipt Amount, Merchant Name (Oracle Expense Lines)."""
        if not self.browser_page:
            return []
        js = """
() => {
  const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();
  const tables = Array.from(document.querySelectorAll('table'));
  let best = null;
  let bestScore = -1;
  tables.forEach((table, tableIndex) => {
    const rows = Array.from(table.rows || []);
    const headerRow = rows.length ? rows[0] : null;
    if (!headerRow) return;
    const hc = Array.from(headerRow.cells || []).map(c => norm(c.textContent || ''));
    if (!hc.length) return;
    const di = hc.findIndex(t => t === 'date' || (t.startsWith('date') && !t.includes('expense')));
    let rai = hc.findIndex(t =>
      (t.includes('receipt') && (t.includes('amount') || t.includes('amt'))) || t === 'receipt amount'
    );
    if (rai < 0) {
      rai = hc.findIndex(t =>
        t.includes('amount') && !t.includes('reimbursable') && !t.includes('reimb')
      );
    }
    const mi = hc.findIndex(t => t.includes('merchant'));
    const ai = hc.findIndex(t => t.includes('attachment'));
    const score = (di >= 0 ? 2 : 0) + (rai >= 0 ? 5 : 0) + (mi >= 0 ? 4 : 0);
    if (score > bestScore) {
      bestScore = score;
      best = { table, tableIndex, di, rai, mi, ai };
    }
  });
  if (!best || best.mi < 0 || best.di < 0) return [];
  const { table, tableIndex, di, rai, mi, ai } = best;
  const bodyRows = Array.from(table.rows || []).slice(1);
  const out = [];
  bodyRows.forEach((tr, bodyRowIndex) => {
    const cells = Array.from(tr.cells || []);
    if (cells.length <= mi) return;
    const date = di < cells.length ? clean(cells[di].innerText || cells[di].textContent) : '';
    const receiptAmt = (rai >= 0 && rai < cells.length)
      ? clean(cells[rai].innerText || cells[rai].textContent) : '';
    const merchant = clean(cells[mi].innerText || cells[mi].textContent);
    let hasExistingAttachment = false;
    if (ai >= 0 && ai < cells.length) {
      const ac = cells[ai];
      const markers = Array.from(ac.querySelectorAll('img, a, span, div, input[type="image"]'));
      for (const el of markers) {
        const blob = norm([
          el.textContent || '',
          el.getAttribute('title') || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('alt') || '',
          el.getAttribute('class') || '',
          el.getAttribute('src') || ''
        ].join(' '));
        if (blob.includes('paperclip') || blob.includes('attached') || blob.includes('attachment') || blob.includes('clip')) {
          hasExistingAttachment = true;
          break;
        }
      }
    }
    if (norm(merchant).includes('merchant name')) return;
    if (norm(date).includes('date receipt amount')) return;
    if (!merchant) return;
    out.push({ tableIndex, bodyRowIndex, date, receiptAmount: receiptAmt, merchant, hasExistingAttachment });
  });
  return out;
}
"""
        for frame_idx, frame in enumerate(self.browser_page.frames):
            try:
                rows = frame.evaluate(js)
                if rows and isinstance(rows, list) and len(rows) > 0:
                    for row in rows:
                        if isinstance(row, dict):
                            row["frameIndex"] = frame_idx
                    return rows
            except Exception:
                continue
        return []

    def _step6_click_attach_plus(
        self,
        table_index: int,
        body_row_index: int,
        *,
        frame_index: int | None = None,
    ) -> bool:
        if not self.browser_page:
            return False
        js = """
([tableIndex, bodyRowIndex]) => {
  const norm = (v) => String(v || '').trim().toLowerCase();
  const clean = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
  const scoreCandidate = (el) => {
    const parts = [
      el.textContent || '',
      el.getAttribute('title') || '',
      el.getAttribute('aria-label') || '',
      el.getAttribute('alt') || '',
      el.getAttribute('name') || '',
      el.getAttribute('id') || '',
      el.getAttribute('class') || '',
      el.getAttribute('src') || '',
      el.getAttribute('href') || ''
    ];
    const blob = norm(parts.join(' '));
    let score = 0;
    if (blob.includes('attach')) score += 12;
    if (blob.includes('receipt')) score += 9;
    if (blob.includes('add')) score += 8;
    if (blob.includes('plus')) score += 7;
    if (blob.includes('create')) score += 3;
    if (blob.includes('icon')) score += 1;
    if ((el.tagName || '').toLowerCase() === 'img') score += 2;
    if ((el.tagName || '').toLowerCase() === 'input' && norm(el.getAttribute('type')) === 'image') score += 3;
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : {width: 0, height: 0};
    if (r.width > 0 && r.height > 0) score += 2;
    return score;
  };
  const safeClick = (el) => {
    try {
      if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({block:'center', inline:'center'});
    } catch (_) {}
    try { if (el && typeof el.click === 'function') { el.click(); return true; } } catch (_) {}
    try {
      const ev = new MouseEvent('click', {bubbles: true, cancelable: true, view: window});
      return !!(el && el.dispatchEvent && el.dispatchEvent(ev));
    } catch (_) { return false; }
  };
  const tables = document.querySelectorAll('table');
  const table = tables[tableIndex];
  if (!table) return false;
  const headerRow = (table.rows && table.rows.length) ? table.rows[0] : null;
  const headerCells = Array.from((headerRow && headerRow.cells) ? headerRow.cells : []);
  let attachIdx = -1;
  for (let i = 0; i < headerCells.length; i++) {
    const ht = norm(clean(headerCells[i].innerText || headerCells[i].textContent));
    if (ht === 'attachments' || ht.includes('attachment')) {
      attachIdx = i;
      break;
    }
  }
  const bodyRows = Array.from(table.rows || []).slice(1);
  const tr = bodyRows[bodyRowIndex];
  if (!tr) return false;
  if (attachIdx >= 0) {
    const tds = Array.from(tr.cells || []);
    if (attachIdx < tds.length) {
      const attachCell = tds[attachIdx];
      const addish = Array.from(
        attachCell.querySelectorAll('a, button, img, input[type="image"], span, div')
      ).filter((el) => {
        const blob = norm([
          el.textContent || '',
          el.getAttribute('title') || '',
          el.getAttribute('aria-label') || '',
          el.getAttribute('alt') || '',
          el.getAttribute('name') || '',
          el.getAttribute('id') || '',
          el.getAttribute('class') || '',
          el.getAttribute('src') || ''
        ].join(' '));
        return blob.includes('add') || blob.includes('attach') || blob.includes('plus') || /\\+/.test(el.textContent || '');
      });
      if (addish.length > 0) {
        let best = null;
        let bestScore = -1;
        for (const c of addish) {
          const s = scoreCandidate(c);
          if (s > bestScore) {
            bestScore = s;
            best = c;
          }
        }
        if (best && safeClick(best)) return true;
        for (const c of addish) {
          if (safeClick(c)) return true;
        }
      }
      if (safeClick(attachCell)) return true;
    }
  }
  const candidates = Array.from(
    tr.querySelectorAll('a, button, img, input[type="image"], span[onclick], div[onclick]')
  );
  if (candidates.length > 0) {
    let best = null;
    let bestScore = -1;
    for (const c of candidates) {
      const s = scoreCandidate(c);
      if (s > bestScore) {
        bestScore = s;
        best = c;
      }
    }
    if (best && safeClick(best)) return true;
    for (const c of candidates) {
      if (safeClick(c)) return true;
    }
  }
  const tds = Array.from(tr.querySelectorAll('td'));
  if (!tds.length) return false;
  const rightToLeft = [...tds].reverse();
  for (const td of rightToLeft) {
    const tdCandidates = Array.from(
      td.querySelectorAll('a, button, img, input[type="image"], span[onclick], div[onclick]')
    );
    for (const c of tdCandidates) {
      if (safeClick(c)) return true;
    }
    if (safeClick(td)) return true;
  }
  return false;
}
"""
        frames = list(self.browser_page.frames)
        targets: list[Frame] = []
        if frame_index is not None and 0 <= frame_index < len(frames):
            targets.append(frames[frame_index])
        targets.extend([f for i, f in enumerate(frames) if i != frame_index])
        for frame in targets:
            try:
                if frame.evaluate(js, [table_index, body_row_index]):
                    return True
            except Exception:
                continue
        return False

    def _step6_wait_for_add_attachment_modal(self, timeout_s: float = 8.0) -> bool:
        if not self.browser_page:
            return False
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate("() => document.body?.innerText || ''") or ""
                except Exception:
                    continue
                if "Add Attachment" in blob or "Choose File" in blob:
                    return True
            self.browser_page.wait_for_timeout(180)
        return False

    def _step6_complete_add_attachment_modal(self, file_path: Path, timeout_s: float = 50.0) -> bool:
        if not self.browser_page or not file_path.is_file():
            return False
        deadline = time.monotonic() + timeout_s
        file_set = False
        apply_wait_logged = False
        while time.monotonic() < deadline:
            self._pump_ui_and_check_cancel()
            page = self.browser_page
            for frame in page.frames:
                try:
                    blob = frame.evaluate("() => document.body?.innerText || ''") or ""
                except Exception:
                    continue
                if "Add Attachment" not in blob and "Choose File" not in blob:
                    continue
                try:
                    frame.evaluate(
                        """
() => {
  const sels = Array.from(document.querySelectorAll('select'));
  for (const s of sels) {
    const opts = Array.from(s.options);
    for (let i = 0; i < opts.length; i++) {
      const t = (opts[i].textContent || '').trim();
      if (/^receipt$/i.test(t) || /^receipt\b/i.test(t)) {
        s.selectedIndex = i;
        s.dispatchEvent(new Event('change', { bubbles: true }));
        s.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      }
    }
  }
  return false;
}
"""
                    )
                except Exception:
                    pass
                if not file_set:
                    try:
                        fi = frame.locator("input[type=file]")
                        if fi.count() > 0:
                            fi.first.set_input_files(str(file_path))
                            file_set = True
                    except Exception:
                        pass
                if not file_set:
                    continue
                apply_state: dict | None = None
                try:
                    apply_state = frame.evaluate(
                        """
() => {
  const els = Array.from(document.querySelectorAll('input[type="button"], button, a, span[onclick], div[onclick]'));
  for (const el of els) {
    const txt = String(el.textContent || el.getAttribute('value') || '').trim().toLowerCase();
    if (txt !== 'apply') continue;
    const disabledAttr = el.getAttribute('disabled');
    const ariaDisabled = (el.getAttribute('aria-disabled') || '').toLowerCase();
    const cls = (el.getAttribute('class') || '').toLowerCase();
    const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
    const disabled = disabledAttr !== null || ariaDisabled === 'true' || cls.includes('disabled');
    return {found: true, visible, disabled};
  }
  return {found: false, visible: false, disabled: true};
}
"""
                    )
                except Exception:
                    apply_state = None
                if isinstance(apply_state, dict):
                    if apply_state.get("found") and (not apply_state.get("disabled")) and apply_state.get("visible"):
                        try:
                            loc = frame.get_by_role("button", name=re.compile(r"^\s*Apply\s*$", re.I))
                            if loc.count() > 0 and loc.first.is_enabled(timeout=1500):
                                loc.first.click(timeout=15000)
                            else:
                                raise RuntimeError("Apply not enabled via role locator")
                        except Exception:
                            try:
                                frame.evaluate(
                                    """
() => {
  const els = Array.from(document.querySelectorAll('input[type="button"], button, a, span[onclick], div[onclick]'));
  for (const el of els) {
    const txt = String(el.textContent || el.getAttribute('value') || '').trim().toLowerCase();
    if (txt !== 'apply') continue;
    el.click();
    return true;
  }
  return false;
}
"""
                                )
                            except Exception:
                                pass
                        page.wait_for_timeout(500)
                        still_modal = self._step6_wait_for_add_attachment_modal(timeout_s=2.0)
                        if not still_modal:
                            return True
                    elif apply_state.get("found") and not apply_wait_logged:
                        apply_wait_logged = True
                try:
                    clicked = frame.evaluate(
                        """
() => {
  const els = Array.from(document.querySelectorAll('input[type="button"], button, a, span[onclick], div[onclick]'));
  for (const el of els) {
    const txt = String(el.textContent || el.getAttribute('value') || '').trim().toLowerCase();
    if (txt === 'apply') {
      try { el.click(); return true; } catch (_) {}
      try {
        const ev = new MouseEvent('click', {bubbles: true, cancelable: true, view: window});
        return !!el.dispatchEvent(ev);
      } catch (_) { return false; }
    }
  }
  return false;
}
"""
                    )
                    if clicked:
                        page.wait_for_timeout(500)
                        still_modal = self._step6_wait_for_add_attachment_modal(timeout_s=2.0)
                        if not still_modal:
                            return True
                except Exception:
                    continue
            page.wait_for_timeout(200)
        return False

    def _step6_resolve_attachment_path(
        self,
        *,
        line_id: str,
        raw_source_file: str,
        llm_matches: dict[str, dict],
        line_by_id: dict[str, dict],
        existing_by_basename: dict[str, list[Path]],
    ) -> Path | None:
        """Resolve one approved attachment path to an existing local file."""
        raw = str(raw_source_file or "").strip()
        if not raw:
            return None
        primary = Path(raw).expanduser()
        if primary.is_file():
            return primary

        candidates: list[tuple[int, Path, str]] = []
        seen: set[str] = set()

        def add_candidate(path_str: str, score: int, reason: str) -> None:
            p = Path(str(path_str or "").strip()).expanduser()
            key = str(p)
            if not key or key in seen or not p.is_file():
                return
            seen.add(key)
            candidates.append((score, p, reason))

        # Highest confidence: the line's latest LLM-selected best_receipt.
        block = llm_matches.get(line_id) or {}
        add_candidate(str(block.get("best_receipt") or ""), 500, "line best_receipt")

        # Next-best fallback: cached best receipt embedded on the line cache.
        line = line_by_id.get(line_id) or {}
        add_candidate(str(line.get("cached_best_receipt") or ""), 480, "cached_best_receipt")

        # If the original source still names a unique filename in known files, accept it.
        base_key = primary.name.lower()
        base_hits = existing_by_basename.get(base_key, [])
        if len(base_hits) == 1:
            add_candidate(str(base_hits[0]), 240, "unique basename")

        # Last resort: allow basename fallback only if unambiguous.
        if not candidates and len(base_hits) == 1:
            return base_hits[0]

        if not candidates:
            # region agent log
            self._debug_log(
                hypothesis_id="H4",
                location="receipt_automation_ui.py:_step6_resolve_attachment_path",
                message="No candidate files found for approved source",
                data={
                    "line_id": line_id,
                    "source_name": primary.name if primary.name else "",
                    "source_basename_hits": len(basename_hits),
                },
            )
            # endregion
            return None
        candidates.sort(key=lambda x: (-x[0], str(x[1]).lower()))
        top_score = candidates[0][0]
        top = [c for c in candidates if c[0] == top_score]
        if len(top) > 1:
            return None
        chosen = candidates[0][1]
        chosen_reason = candidates[0][2]
        self.log_event(
            "browser",
            f"Step 6: resolved missing path for {line_id} via {chosen_reason}: {chosen}",
        )
        return chosen

    def _step6_stage_attachment_file(self, line_id: str, source_path: Path) -> Path | None:
        """
        Copy attachment to a stable app-owned path before upload.
        This avoids transient-temp file disappearance during long Step 6 runs.
        """
        if not source_path.is_file():
            return None
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source_path.name).strip("._")
        if not safe_name:
            safe_name = "receipt.bin"
        stage_dir = APP_DIR / "step6-upload-cache"
        stage_dir.mkdir(parents=True, exist_ok=True)
        staged = stage_dir / f"{line_id}-{int(time.time() * 1000)}-{safe_name}"
        try:
            shutil.copy2(source_path, staged)
        except OSError as exc:
            self.log_event(
                "warn",
                f"Step 6: could not stage {source_path} for {line_id} ({exc}); using source path directly.",
            )
            return source_path if source_path.is_file() else None
        return staged if staged.is_file() else None

    def _attach_matched_receipts_step6(self) -> None:
        lines, _ = load_expense_lines_cache(APP_DIR)
        approved = load_approved_matches(APP_DIR)
        llm_matches = load_receipt_line_matches(APP_DIR)
        analyses_snapshot = load_analyses_snapshot(APP_DIR)
        line_by_id = {str(L.get("line_id", "")): L for L in lines if str(L.get("line_id", "")).strip()}
        sig_by_line: dict[str, tuple[str, str, str]] = {}
        for lid, line in line_by_id.items():
            sig_by_line[lid] = signature_from_cached_line(line)

        existing_by_basename: dict[str, list[Path]] = {}

        def index_existing(path_str: str) -> None:
            p = Path(str(path_str or "").strip()).expanduser()
            if not p.is_file():
                return
            k = p.name.lower()
            arr = existing_by_basename.setdefault(k, [])
            ps = str(p)
            if all(str(x) != ps for x in arr):
                arr.append(p)

        for p in self.receipt_paths:
            index_existing(p)
        for item in self.analyses:
            index_existing(str((item or {}).get("source_file") or ""))
        for item in analyses_snapshot:
            index_existing(str((item or {}).get("source_file") or ""))
        for block in llm_matches.values():
            index_existing(str((block or {}).get("best_receipt") or ""))

        work: list[tuple[str, Path, tuple[str, str, str], str]] = []
        approved_updated = False
        missing_path_count = 0
        staged_fail_count = 0
        screenshot_candidates = 0
        photos_candidates = 0
        missing_logged = 0
        stage_fail_logged = 0
        for lid, block in sorted(approved.items(), key=lambda x: x[0]):
            raw_source = str(block.get("source_file", "") or "").strip()
            raw_name = Path(raw_source).name if raw_source else ""
            raw_low = raw_name.lower()
            if raw_low.startswith("screenshot_"):
                screenshot_candidates += 1
            if raw_low.startswith("img_"):
                photos_candidates += 1
            p = self._step6_resolve_attachment_path(
                line_id=lid,
                raw_source_file=raw_source,
                llm_matches=llm_matches,
                line_by_id=line_by_id,
                existing_by_basename=existing_by_basename,
            )
            if not p:
                missing_path_count += 1
                if missing_logged < 8:
                    # region agent log
                    self._debug_log(
                        hypothesis_id="H1",
                        location="receipt_automation_ui.py:_attach_matched_receipts_step6",
                        message="Attachment source path unresolved",
                        data={
                            "line_id": lid,
                            "source_name": raw_name,
                            "source_kind": (
                                "screenshot"
                                if raw_low.startswith("screenshot_")
                                else ("photos_img" if raw_low.startswith("img_") else "other")
                            ),
                        },
                    )
                    # endregion
                    missing_logged += 1
                self.log_event("warn", f"Step 6: skip {lid} — missing or ambiguous path: {raw_source}")
                continue
            if lid not in sig_by_line:
                self.log_event("warn", f"Step 6: skip {lid} — not in scrape cache.")
                continue
            staged = self._step6_stage_attachment_file(lid, p)
            if not staged:
                staged_fail_count += 1
                if stage_fail_logged < 4:
                    # region agent log
                    self._debug_log(
                        hypothesis_id="H2",
                        location="receipt_automation_ui.py:_attach_matched_receipts_step6",
                        message="Attachment staging copy failed",
                        data={"line_id": lid, "resolved_name": p.name},
                    )
                    # endregion
                    stage_fail_logged += 1
                self.log_event("warn", f"Step 6: skip {lid} — could not stage upload file: {p}")
                continue
            work.append((lid, staged, sig_by_line[lid], str(p)))
            if str(p) != raw_source:
                block["source_file"] = str(p)
                approved_updated = True

        if approved_updated:
            save_approved_matches(APP_DIR, approved)
            self._invalidate_receipt_table_match_cache()

        # region agent log
        self._debug_log(
            hypothesis_id="H3",
            location="receipt_automation_ui.py:_attach_matched_receipts_step6",
            message="Step6 path prep summary",
            data={
                "approved_count": len(approved),
                "work_count": len(work),
                "missing_path_count": missing_path_count,
                "staged_fail_count": staged_fail_count,
                "screenshot_candidates": screenshot_candidates,
                "photos_candidates": photos_candidates,
            },
        )
        # endregion

        used_pairs: set[tuple[int, int]] = set()
        attempted = 0
        attached_ok = 0
        for lid, path, want_sig, original_source in work:
            self._pump_ui_and_check_cancel()
            rows = self._extract_step6_expense_lines_rows()
            if not rows:
                self.log_event("err", f"Step 6: could not read expense lines table ({lid}).")
                continue
            # region agent log
            self._debug_log(
                hypothesis_id="H8",
                location="receipt_automation_ui.py:_attach_matched_receipts_step6",
                message="Step6 row attachment-icon snapshot",
                data={
                    "line_id": lid,
                    "rows_count": len(rows),
                    "rows_with_existing_attachment": sum(
                        1 for r in rows if bool((r or {}).get("hasExistingAttachment"))
                    ),
                },
            )
            # endregion
            hit: dict | None = None
            for r in rows:
                ti = int(r.get("tableIndex", 0))
                bri = int(r.get("bodyRowIndex", 0))
                if (ti, bri) in used_pairs:
                    continue
                got = signature_from_step6_row(
                    str(r.get("date", "") or ""),
                    str(r.get("receiptAmount", "") or ""),
                    str(r.get("merchant", "") or ""),
                )
                if got == want_sig:
                    # region agent log
                    self._debug_log(
                        hypothesis_id="H9",
                        location="receipt_automation_ui.py:_attach_matched_receipts_step6",
                        message="Matched Step6 row candidate",
                        data={
                            "line_id": lid,
                            "table_index": ti,
                            "body_row_index": bri,
                            "has_existing_attachment": bool((r or {}).get("hasExistingAttachment")),
                        },
                    )
                    # endregion
                    hit = r
                    break
            if not hit:
                self.log_event(
                    "warn",
                    f"Step 6: no exact row for {lid} (signature {want_sig}). "
                    "Compare scrape cache to Step 6 Date / Receipt Amount / Merchant.",
                )
                continue
            ti = int(hit["tableIndex"])
            bri = int(hit["bodyRowIndex"])
            fidx = int(hit.get("frameIndex", -1))
            self.log_event(
                "browser",
                f"Step 6: {lid} → row bri={bri} “{str(hit.get('merchant', ''))[:48]}…”",
            )
            attempted += 1
            clicked = self._step6_click_attach_plus(ti, bri, frame_index=fidx if fidx >= 0 else None)
            if not clicked:
                self.log_event("warn", f"Step 6: could not click attach control for {lid}.")
                continue
            if self.browser_page:
                self.browser_page.wait_for_timeout(800)
            if not self._step6_wait_for_add_attachment_modal(timeout_s=8.0):
                # One retry on stale/collapsed rows before giving up this line.
                clicked_retry = self._step6_click_attach_plus(
                    ti, bri, frame_index=fidx if fidx >= 0 else None
                )
                if clicked_retry and self.browser_page:
                    self.browser_page.wait_for_timeout(800)
                if not clicked_retry or not self._step6_wait_for_add_attachment_modal(timeout_s=8.0):
                    self.log_event(
                        "warn",
                        f"Step 6: attach dialog did not open for {lid} after clicking +.",
                    )
                    continue
            if not self._step6_complete_add_attachment_modal(path):
                self.log_event("warn", f"Step 6: Add Attachment flow failed for {lid}.")
                continue
            used_pairs.add((ti, bri))
            attached_ok += 1
            record_submitted_receipt(APP_DIR, source_file=original_source, line_id=lid)
            self.log_event("browser", f"Step 6: applied {path.name} for {lid}")
            if self.browser_page:
                self.browser_page.wait_for_timeout(1200)
        self.log_event(
            "browser",
            f"Step 6 summary: attached {attached_ok}/{len(work)} prepared file(s), "
            f"attempted {attempted} row(s).",
        )
        if work and attached_ok == 0:
            raise RuntimeError(
                "Step 6 reached Attachments, but no files were uploaded. "
                "Automation paused so you can review the Attachments + / Add Attachment dialog state."
            )

    def _run_complete_report_step2_preamble(self) -> None:
        """Step 2: rewind card table, Save, Next. Step 3: rewind Business Expenses table to row 1."""
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        on2 = self._wizard_any_frame_on_step(2)
        on3 = self._wizard_any_frame_on_step(3)
        if on2:
            self.set_status("Complete report: first page of Credit Card Transactions (Step 2 of 6)…")
            self.log_event("browser", "Complete report: paginating to first page of Step 2 table.")
            self.expense_table_go_to_first_page_in_any_frame(credit_card_step2=True)
            self.set_status("Complete report: Save, then Next to Business Expenses (Step 3 of 6)…")
            if not self.click_save_button_wizard_in_any_frame(wizard_step=2):
                if not self.click_text_in_any_frame("Save"):
                    raise RuntimeError("Could not Save on Credit Card Transactions before Step 3.")
            self.browser_page.wait_for_timeout(700)
            if not self.wait_for_wizard_next_enabled_and_click(wizard_step=2):
                raise RuntimeError(
                    "Could not click wizard Next from Credit Card Transactions (Step 2 of 6)."
                )
            self.browser_page.wait_for_timeout(800)
        elif not on3:
            raise RuntimeError(
                "Complete report needs the wizard on Step 2 (Credit Card Transactions) "
                "or Step 3 (Business Expenses). Open the expense report there and try again."
            )
        else:
            self.log_event(
                "browser",
                "Complete report: wizard already on Step 3 — skipping Step 2 rewind and Next.",
            )
        self._wait_until_wizard_step_visible(3)
        self.set_status("Complete report: first page of Business Expenses (Step 3 of 6)…")
        self.log_event(
            "browser",
            "Complete report: paginating to first page before expense type / justification fill.",
        )
        self.expense_table_go_to_first_page_in_any_frame()
        self.browser_page.wait_for_timeout(500)

    def _populate_at_or_after(self, start_from: str, phase: str) -> bool:
        try:
            start_i = POPULATE_RESUME_KEYS.index(start_from)
        except ValueError:
            start_i = 0
        try:
            phase_i = POPULATE_RESUME_KEYS.index(phase)
        except ValueError:
            return True
        return phase_i >= start_i

    def _populate_stopping_before(self, phase: str, stop_before_phase: str | None) -> bool:
        """True if catch-up should return before executing this populate phase."""
        return bool(stop_before_phase and phase == stop_before_phase)

    def _resume_dialog_default_key(self, default_step: str) -> str:
        if default_step in RESUME_DIALOG_KEYS:
            return default_step
        try:
            wanted_i = POPULATE_RESUME_KEYS.index(default_step)
        except ValueError:
            return RESUME_DIALOG_KEYS[0]
        fallback = RESUME_DIALOG_KEYS[0]
        for key in RESUME_DIALOG_KEYS:
            try:
                idx = POPULATE_RESUME_KEYS.index(key)
            except ValueError:
                continue
            if idx <= wanted_i:
                fallback = key
        return fallback

    def _ask_resume_populate_step(
        self,
        default_step: str,
        *,
        title: str,
        intro: str,
        show_relaunch_browser: bool = False,
        relaunch_button_text: str = "Relaunch browser",
        relaunch_button_action: Callable[[], None] | None = None,
        show_resume_after_crash: bool = False,
    ) -> str | None:
        auto_detect_display = "0. Auto detect from browser (recommended)"
        step_values = [f"{i + 1}. {label}" for i, (_, label) in enumerate(RESUME_DIALOG_STEPS)]
        display_values = [auto_detect_display, *step_values]
        key_by_display = {auto_detect_display: AUTO_DETECT_RESUME_CHOICE}
        key_by_display.update({step_values[i]: RESUME_DIALOG_KEYS[i] for i in range(len(step_values))})
        default_key = self._resume_dialog_default_key(default_step)
        try:
            default_i = RESUME_DIALOG_KEYS.index(default_key) + 1
        except ValueError:
            default_i = 1
        default_display = display_values[default_i]

        result: dict[str, str | None] = {"key": None}
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=intro, wraplength=520, justify=tk.LEFT).pack(anchor=tk.W, padx=14, pady=(12, 8))
        var = tk.StringVar(value=default_display)
        combo = ttk.Combobox(
            win,
            textvariable=var,
            values=display_values,
            state="readonly",
            width=58,
        )
        combo.pack(fill=tk.X, padx=14, pady=(0, 12))

        def on_ok() -> None:
            disp = var.get().strip()
            selected = key_by_display.get(disp)
            if selected == AUTO_DETECT_RESUME_CHOICE:
                detected, reason = self._auto_detect_resume_anchor_from_browser()
                if not detected:
                    self.set_status(f"Resume auto detect failed: {reason}")
                    messagebox.showwarning("Auto detect", reason, parent=win)
                    return
                selected = detected
                self.set_status(f"{reason} Resuming from {selected}.")
            result["key"] = selected
            win.destroy()

        def on_cancel() -> None:
            result["key"] = None
            win.destroy()

        def on_relaunch() -> None:
            action = relaunch_button_action or self.relaunch_controlled_browser_for_resume
            action()

        def on_resume_after_crash() -> None:
            result["key"] = CRASH_RESUME_DIALOG_CHOICE
            win.destroy()

        btn_row = ttk.Frame(win)
        btn_row.pack(fill=tk.X, padx=14, pady=(0, 12))
        if show_relaunch_browser:
            ttk.Button(btn_row, text=relaunch_button_text, command=on_relaunch).pack(
                side=tk.LEFT, padx=(0, 6)
            )
        if show_resume_after_crash:
            ttk.Button(
                btn_row,
                text="Resume after crash",
                command=on_resume_after_crash,
            ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Cancel", command=on_cancel).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text="Continue automation", command=on_ok).pack(side=tk.RIGHT)
        win.protocol("WM_DELETE_WINDOW", on_cancel)
        self.root.wait_window(win)
        return result["key"]

    def _resume_anchor_page_matches(self, key: str) -> tuple[bool, str]:
        if key in ("wait_step1",):
            if not self._wizard_any_frame_on_step(1):
                return (
                    False,
                    "Resume check failed: this anchor expects General Information (Oracle Step 1 of 6).",
                )
        elif key in ("wait_step2", "credit_card_transactions"):
            if not self._wizard_any_frame_on_step(2):
                return (
                    False,
                    "Resume check failed: this anchor expects Credit Card Transactions (Oracle Step 2 of 6).",
                )
        elif key in ("complete_report_step2", "step3_autofill"):
            if not (self._wizard_any_frame_on_step(2) or self._wizard_any_frame_on_step(3)):
                return (
                    False,
                    "Resume check failed: this anchor expects Step 2 or Step 3 of the Oracle wizard.",
                )
        elif key == "step4_no_action_next":
            if not (
                self._wizard_any_frame_on_step(4)
                or self._wizard_any_frame_on_step(5)
                or self._wizard_any_frame_on_step(6)
            ):
                return (
                    False,
                    "Resume check failed: this anchor expects Oracle Step 4, 5, or 6.",
                )
        elif key == "step5_no_action_next":
            if not (self._wizard_any_frame_on_step(5) or self._wizard_any_frame_on_step(6)):
                return (
                    False,
                    "Resume check failed: this anchor expects Oracle Step 5 or 6.",
                )
        elif key == "step6_attach_files":
            if not self._wizard_any_frame_on_step(6):
                return (
                    False,
                    "Resume check failed: this anchor expects Attachments (Oracle Step 6 of 6).",
                )
        return True, ""

    def _auto_detect_resume_anchor_from_browser(self) -> tuple[str | None, str]:
        """Infer best resume anchor from Oracle UI state (prefers visible 'Step X of 6')."""
        if not self._controlled_browser_usable() or not self.browser_page:
            return None, "Auto detect needs a connected Chromium session."

        if self._body_contains_text("Update Expense Report: Review"):
            return "step6_attach_files", "Auto detect: Review page heading detected (Oracle Step 6 of 6)."
        if self._body_contains_text("Step 6 of 6"):
            return "step6_attach_files", "Auto detect: explicit 'Step 6 of 6' text detected."

        scanned_steps: set[int] = set()
        for frame in self.browser_page.frames:
            try:
                blob = frame.evaluate(
                    "() => ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ')"
                )
            except Exception:
                continue
            if not blob:
                continue
            for m in re.finditer(r"step\s*([1-6])\s*of\s*[56]", blob, re.IGNORECASE):
                try:
                    scanned_steps.add(int(m.group(1)))
                except ValueError:
                    continue
        if scanned_steps:
            best = max(scanned_steps)
            step_to_anchor_scan = {
                1: "wait_step1",
                2: "credit_card_transactions",
                3: "step3_autofill",
                4: "step4_no_action_next",
                5: "step5_no_action_next",
                6: "step6_attach_files",
            }
            return step_to_anchor_scan[best], f"Auto detect: scanned wizard text suggests Step {best} of 6."

        step_to_anchor = {
            1: "wait_step1",
            2: "credit_card_transactions",
            3: "step3_autofill",
            4: "step4_no_action_next",
            5: "step5_no_action_next",
            6: "step6_attach_files",
        }
        for step_n in (6, 5, 4, 3, 2, 1):
            if self._wizard_any_frame_on_step(step_n):
                key = step_to_anchor[step_n]
                return key, f"Auto detect: Oracle shows Step {step_n} of 6."

        if self._body_contains_text("Update Expense Reports") or self._body_contains_text("Expenses Home"):
            return "nic_iexpenses", "Auto detect: on Expenses Home / Update Expense Reports."
        if self._body_contains_text("Create Expense Report"):
            return "create_report", "Auto detect: Create Expense Report entry point detected."
        if self._body_contains_text("General Information"):
            return "wait_step1", "Auto detect: General Information detected."
        if self._body_contains_text("Credit Card Transactions"):
            return "credit_card_transactions", "Auto detect: Credit Card Transactions detected."
        if self._body_contains_text("Business Expenses"):
            return "step3_autofill", "Auto detect: Business Expenses detected."
        if self._body_contains_text("Attachments"):
            return "step6_attach_files", "Auto detect: Attachments detected."
        return None, (
            "Could not auto detect Oracle step. Navigate until 'Step X of 6' is visible, "
            "or choose a specific resume step."
        )

    def _resume_key_needs_step6_attach(self, key: str) -> bool:
        return key in {
            "step3_autofill",
            "step4_no_action_next",
            "step5_no_action_next",
            "step6_attach_files",
            "complete_report_step2",
        }

    def _prepare_resume_step6_attach_if_needed(
        self, key: str, *, status_prefix: str, require_complete_mode_for_step2: bool
    ) -> bool:
        if not self._resume_key_needs_step6_attach(key):
            return True
        ok_m, err_m = validate_approved_for_attach(APP_DIR)
        if not ok_m:
            self.set_status(f"{status_prefix}: {err_m}")
            return False
        if key == "complete_report_step2" and require_complete_mode_for_step2:
            if not self._prepare_complete_report_llm_mode():
                return False
        self._run_step6_file_attach = True
        return True

    def _prompt_manual_resume(self, failure_detail: str, default_step: str) -> str | None:
        self.set_status(
            f"Automation paused — fix Chromium if needed, then pick resume step. ({failure_detail})"
        )
        return self._ask_resume_populate_step(
            default_step,
            title="Resume expense automation",
            intro=(
                "Pick the phase to run from. The wizard should match that point before you continue. "
                "If Chromium died or you need to reopen the saved in-progress report, use “Resume after crash” "
                "(relaunch, sign in, Update Expense Reports → pencil, then continue from the last step)."
            ),
            show_relaunch_browser=True,
            show_resume_after_crash=True,
        )

    def _execute_populate_from(
        self,
        start_from: str,
        *,
        stop_before_phase: str | None = None,
    ) -> None:
        """Run Step 3 automation from a named resume anchor; raises on failure.

        If ``stop_before_phase`` is set, return immediately **before** running that phase
        (crash resume: replay earlier steps, then call again starting at the saved anchor).
        """
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        self._pump_ui_and_check_cancel()
        self._last_populate_step = start_from
        self._populate_ui_current = start_from
        self._refresh_activity_panel()

        if self._populate_at_or_after(start_from, "nic_iexpenses"):
            if self._populate_stopping_before("nic_iexpenses", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "nic_iexpenses"
            self._last_populate_step = "nic_iexpenses"
            self._refresh_activity_panel()
            self.set_status("Expanding iExpenses in Navigator…")
            self._oracle_expand_nic_iexpenses_menu()
            self.browser_page.wait_for_timeout(400)

        if self._populate_at_or_after(start_from, "create_report"):
            if self._populate_stopping_before("create_report", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "create_report"
            self._last_populate_step = "create_report"
            self._refresh_activity_panel()
            self.set_status("Opening Create Expense Report…")
            if not self._body_contains_text("Create Expense Report"):
                self._oracle_expand_nic_iexpenses_menu()
                self.browser_page.wait_for_timeout(600)
            if not self.click_text_in_any_frame("Create Expense Report"):
                raise RuntimeError(
                    "Could not click 'Create Expense Report'. "
                    "Expand the iExpenses folder in the left Navigator (folder/disclosure) until the link appears, "
                    "then use Resume from this step."
                )

        if self._populate_at_or_after(start_from, "wait_step1"):
            if self._populate_stopping_before("wait_step1", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "wait_step1"
            self._last_populate_step = "wait_step1"
            self._refresh_activity_panel()
            self.set_status("Step 4.1 (Oracle 1 of 6): waiting for General Information to load…")
            self.wait_for_step1_general_information_ready()

        if self._populate_at_or_after(start_from, "select_template"):
            if self._populate_stopping_before("select_template", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "select_template"
            self._last_populate_step = "select_template"
            self._refresh_activity_panel()
            self.set_status("Step 4.1: verifying report template (Travel)…")
            if not self.select_travel_template_in_any_frame():
                raise RuntimeError("Could not find template dropdown or Travel option.")
            self.browser_page.wait_for_timeout(350)

        if self._populate_at_or_after(start_from, "fill_purpose"):
            if self._populate_stopping_before("fill_purpose", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "fill_purpose"
            self._last_populate_step = "fill_purpose"
            self._refresh_activity_panel()
            purpose = getattr(self, "_submit_report_name", None) or "Expense Report"
            self.set_status(f"Step 4.1: setting purpose to '{purpose}'…")
            if not self.fill_purpose_in_any_frame(purpose):
                raise RuntimeError("Could not locate Purpose field.")

        if self._populate_at_or_after(start_from, "fill_approver"):
            if self._populate_stopping_before("fill_approver", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "fill_approver"
            self._last_populate_step = "fill_approver"
            self._refresh_activity_panel()
            self.set_status("Step 4.1: verifying approver (set if needed)…")
            approver = (self.settings.approver or "").strip()
            if not approver:
                raise RuntimeError(
                    "Approver not configured — set the approver display name in Settings "
                    '(e.g. "Smith, John").'
                )
            if not self.fill_approver_in_any_frame(approver):
                raise RuntimeError("Could not locate Approver field.")

        if self._populate_at_or_after(start_from, "save_step1"):
            if self._populate_stopping_before("save_step1", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "save_step1"
            self._last_populate_step = "save_step1"
            self._refresh_activity_panel()
            self.browser_page.wait_for_timeout(300)
            self.set_status("Step 4.1: saving General Information (enables Next)…")
            if not self.click_save_button_wizard_in_any_frame():
                raise RuntimeError("Could not click Save on General Information.")

        if self._populate_at_or_after(start_from, "next_from_step1"):
            if self._populate_stopping_before("next_from_step1", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "next_from_step1"
            self._last_populate_step = "next_from_step1"
            self._refresh_activity_panel()
            self.set_status("Step 4.1: clicking Next to continue to Oracle Step 2…")
            if not self.wait_for_wizard_next_enabled_and_click(wizard_step=1):
                raise RuntimeError("Next did not become enabled after Save (waited up to 2 minutes).")

        if self._populate_at_or_after(start_from, "wait_step2"):
            if self._populate_stopping_before("wait_step2", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "wait_step2"
            self._last_populate_step = "wait_step2"
            self._refresh_activity_panel()
            self.set_status("Step 4.2 (Oracle 2 of 6): waiting for Credit Card Transactions…")
            self.wait_for_step2_credit_card_transactions()

        if self._populate_at_or_after(start_from, "credit_card_transactions"):
            if self._populate_stopping_before("credit_card_transactions", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "credit_card_transactions"
            self._last_populate_step = "credit_card_transactions"
            self._refresh_activity_panel()
            self.complete_credit_card_transactions_step()
            self.browser_page.wait_for_timeout(1000)

        if self._step3_vpn_mode == "vpn_collect":
            self._pump_ui_and_check_cancel()
            self._finish_vpn_collect_after_step2(len(self._scraped_expense_lines))
            return

        if start_from == "complete_report_step2":
            if self._populate_stopping_before("complete_report_step2", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "complete_report_step2"
            self._last_populate_step = "complete_report_step2"
            self._refresh_activity_panel()
            self._run_complete_report_step2_preamble()
            if self.browser_page:
                self.browser_page.wait_for_timeout(600)

        if self._populate_at_or_after(start_from, "step3_autofill"):
            if self._populate_stopping_before("step3_autofill", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "step3_autofill"
            self._last_populate_step = "step3_autofill"
            self._refresh_activity_panel()
            auto_step3_note = "Step 3 auto-categorization skipped (set OpenAI key in Settings)."
            api_key = self.get_openai_key().strip()
            run_step3_fill = False
            if self._step3_vpn_mode == "vpn_replay":
                run_step3_fill = True
                self.log_event(
                    "step",
                    "Step 3 VPN replay: applying answers from llm_query_pending.json (+ vendor cache).",
                )
                self.set_status("Step 4.3.1: filling expense type/justification from replay cache…")
            elif api_key:
                run_step3_fill = True
                self.log_event(
                    "step",
                    "Step 3 auto-fill started: each line logs as [CACHE] or [LLM]; "
                    "OpenAI waits show as [LLM] until a reply time appears.",
                )
                self.set_status("Step 4.3.1: setting expense type + justification on each row…")

            if run_step3_fill:
                assignments, receipt_missing_lines = self.auto_fill_step3_expense_types(api_key=api_key)
                if self._step3_vpn_mode == "vpn_replay":
                    auto_step3_note = (
                        f"Step 3 VPN replay: {len(assignments)} line(s) from cached LLM + vendor cache."
                    )
                else:
                    auto_step3_note = (
                        f"Step 3 auto-categorized {len(assignments)} line(s): "
                        "Expense Type + matching Justification."
                    )
                self.set_status("Step 4.3.1: saving Business Expenses after updates…")
                if not self.click_save_button_wizard_in_any_frame(wizard_step=3):
                    raise RuntimeError("Could not Save on Step 3 after expense type updates.")
                self.browser_page.wait_for_timeout(900)

                if self._step3_vpn_mode != "vpn_collect" and receipt_missing_lines:
                    self.set_status("Step 4.3.2: marking Original Receipt Missing where documents are absent…")
                    self.step3_apply_receipt_missing_pass(receipt_missing_lines)
                    if not self.click_save_button_wizard_in_any_frame(wizard_step=3):
                        raise RuntimeError("Could not Save on Step 3 after Original Receipt Missing.")
                    self.browser_page.wait_for_timeout(900)

                if self._step3_vpn_mode != "vpn_collect":
                    self.set_status("Step 4.3.3: resolving Oracle line errors in banner order…")
                    self.step3_resolve_banner_errors_in_order(assignments, api_key=api_key)

                self.set_status("Step 4.3: advancing from Business Expenses after validation fixes…")
                self.advance_step3_wizard_past_exchange_rate_errors()
                self.set_status("Step 4.4/4.5: no data entry needed; clicking Next through both steps…")
                for step_n in (4, 5):
                    self._pump_ui_and_check_cancel()
                    self._refresh_activity_panel()
                    if not self.wait_for_wizard_next_enabled_and_click(wizard_step=step_n):
                        raise RuntimeError(
                            f"Could not click the wizard Next control (expected step {step_n} of 6)."
                        )
                    self.browser_page.wait_for_timeout(700)
                if self._run_step6_file_attach:
                    auto_step3_note = f"{auto_step3_note} Advanced toward Step 6 (file attachments next)."
                else:
                    auto_step3_note = f"{auto_step3_note} Wizard advanced to Step 6."

            self.set_status(f"Step 4.3 complete: report lines updated and validated. {auto_step3_note}")

        if self._populate_at_or_after(start_from, "step4_no_action_next"):
            if self._populate_stopping_before("step4_no_action_next", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "step4_no_action_next"
            self._last_populate_step = "step4_no_action_next"
            self._refresh_activity_panel()
            if self._wizard_any_frame_on_step(4):
                self.set_status("Step 4.4 (Oracle 4 of 6): no action required, clicking Next…")
                if not self.wait_for_wizard_next_enabled_and_click(wizard_step=4):
                    raise RuntimeError("Could not click Next on Oracle Step 4.")
                self.browser_page.wait_for_timeout(700)

        if self._populate_at_or_after(start_from, "step5_no_action_next"):
            if self._populate_stopping_before("step5_no_action_next", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "step5_no_action_next"
            self._last_populate_step = "step5_no_action_next"
            self._refresh_activity_panel()
            if self._wizard_any_frame_on_step(5):
                self.set_status("Step 4.5 (Oracle 5 of 6): no action required, clicking Next…")
                if not self.wait_for_wizard_next_enabled_and_click(wizard_step=5):
                    raise RuntimeError("Could not click Next on Oracle Step 5.")
                self.browser_page.wait_for_timeout(700)

        if self._run_step6_file_attach and self._populate_at_or_after(start_from, "step6_attach_files"):
            if self._populate_stopping_before("step6_attach_files", stop_before_phase):
                return
            self._pump_ui_and_check_cancel()
            self._populate_ui_current = "step6_attach_files"
            self._last_populate_step = "step6_attach_files"
            self._refresh_activity_panel()
            self.set_status("Step 4.6 (Oracle 6 of 6): waiting for Attachments page…")
            self._wait_until_wizard_step_visible(6)
            self._attach_matched_receipts_step6()
            self.set_status(
                "Step 4.6: attachment pass finished — review browser/log for rows skipped (for example missing documents)."
            )

    def _run_populate_expense_report_flow(
        self,
        start_from: str = "nic_iexpenses",
        *,
        crash_resume_continue: str | None = None,
    ) -> None:
        if self._step3_automation_active:
            self.set_status("Step 3 automation is already running.")
            return

        self._automation_cancel.clear()
        self._activity_stopped_at_key = None
        self._populate_flow_completed = False
        self._step3_automation_active = True
        self._disable_crash_resume_button()
        self._refresh_activity_panel()

        current = start_from
        crash_first = crash_resume_continue is not None
        restart_crash_resume = False
        try:
            while True:
                try:
                    if crash_first and crash_resume_continue is not None:
                        self._execute_resume_in_progress_and_continue(crash_resume_continue)
                        crash_first = False
                        current = crash_resume_continue
                    else:
                        self._execute_populate_from(current)
                    self._populate_flow_completed = True
                    self._populate_ui_current = None
                    self._disable_crash_resume_button()
                    return
                except AutomationCancelled:
                    self._activity_stopped_at_key = (
                        self._populate_ui_current or self._last_populate_step
                    )
                    self.set_status("Step 3 automation stopped (between phases).")
                    return
                except PlaywrightTimeoutError as exc:
                    self._crash_resume_anchor = self._populate_ui_current or self._last_populate_step
                    self._enable_crash_resume_button()
                    self.set_status(f"Paused (timeout): {exc}")
                    choice = self._prompt_manual_resume(str(exc), self._last_populate_step)
                    self._refresh_activity_panel()
                    if not choice:
                        self.set_status("Step 3 cancelled after timeout.")
                        return
                    if choice == CRASH_RESUME_DIALOG_CHOICE:
                        self._crash_resume_anchor = self._populate_ui_current or self._last_populate_step
                        restart_crash_resume = True
                        break
                    self._disable_crash_resume_button()
                    crash_first = False
                    current = choice
                except RuntimeError as exc:
                    self._crash_resume_anchor = self._populate_ui_current or self._last_populate_step
                    self._enable_crash_resume_button()
                    self.set_status(f"Paused: {exc}")
                    choice = self._prompt_manual_resume(str(exc), self._last_populate_step)
                    self._refresh_activity_panel()
                    if not choice:
                        self.set_status("Step 3 automation cancelled.")
                        return
                    if choice == CRASH_RESUME_DIALOG_CHOICE:
                        self._crash_resume_anchor = self._populate_ui_current or self._last_populate_step
                        restart_crash_resume = True
                        break
                    self._disable_crash_resume_button()
                    crash_first = False
                    current = choice
                except Exception as exc:
                    self._crash_resume_anchor = self._populate_ui_current or self._last_populate_step
                    self._enable_crash_resume_button()
                    self.set_status(f"Paused (unexpected error): {exc}")
                    self.log_event("err", f"Step 3 unexpected failure: {exc}")
                    self._emit_automation_event(
                        kind="submission.recovery_needed",
                        message="Unexpected automation error; manual resume required.",
                        phase="Submission",
                        data={"error": str(exc), "anchor": self._crash_resume_anchor or ""},
                    )
                    choice = self._prompt_manual_resume(str(exc), self._last_populate_step)
                    self._refresh_activity_panel()
                    if not choice:
                        self.set_status("Step 3 automation cancelled after unexpected error.")
                        return
                    if choice == CRASH_RESUME_DIALOG_CHOICE:
                        self._crash_resume_anchor = self._populate_ui_current or self._last_populate_step
                        restart_crash_resume = True
                        break
                    self._disable_crash_resume_button()
                    crash_first = False
                    current = choice
        finally:
            self._populate_ui_current = None
            self._step3_automation_active = False
            self._automation_cancel.clear()
            self._step3_vpn_mode = "standard"
            self._llm_replay_document = None
            self._run_step6_file_attach = False
            if self._pending_release_browser:
                self._pending_release_browser = False
                if self._controlled_browser_usable():
                    self._disconnect_playwright_keep_chrome()
                    self.set_status(
                        "Sequence stopped — automation disconnected. Chromium stays open; "
                        "use “Open Oracle” to reconnect automation when ready."
                    )
                else:
                    self.set_status(
                        "Sequence stopped — browser was already closed or unavailable; "
                        "use Step 2 to open Chromium again."
                    )
            self._refresh_activity_panel()
        if restart_crash_resume:
            self.on_resume_automation_after_crash()

    def on_resume_expense_report_from_step(self) -> None:
        if self._step3_automation_active:
            self.set_status("Stop the running Step 3 automation before using Resume from step…")
            return
        key = self._ask_resume_populate_step(
            self._last_populate_step,
            title="Resume expense report",
            intro=(
                "Use this if you moved the Oracle wizard manually. "
                "Choose the step that matches the page currently shown in Chromium. "
                "If Chromium crashed, relaunch, sign in, navigate to the correct step, then continue."
            ),
            show_relaunch_browser=True,
        )
        if not key:
            self.set_status("Resume cancelled.")
            return
        if not self._controlled_browser_usable():
            self.set_status(
                "No live browser connection — use Relaunch browser (in this dialog next time) or Step 2 (Open Oracle), "
                "then try Resume again."
            )
            return
        if not self.receipt_paths and key != "step6_attach_files":
            self.set_status("Resume blocked: no imported receipts yet. Run Step 1 first.")
            return
        if not self._prepare_resume_step6_attach_if_needed(
            key,
            status_prefix="Resume blocked",
            require_complete_mode_for_step2=True,
        ):
            return
        ok_page, msg = self._resume_anchor_page_matches(key)
        if not ok_page:
            self.set_status(msg)
            return
        self.set_status(f"Resuming expense report from: {key}…")
        self._run_populate_expense_report_flow(start_from=key)

    def _confirm_ready_to_resume_step(self, key: str) -> bool:
        label = RESUME_DIALOG_LABEL_BY_KEY.get(key, dict(POPULATE_RESUME_STEPS).get(key, key))
        return bool(
            messagebox.askokcancel(
                "Ready to resume?",
                "Chromium is open for manual prep.\n\n"
                "Before continuing:\n"
                "1) Finish sign-in / SSO / 2FA.\n"
                "2) Navigate Oracle to the matching screen.\n"
                f"3) Confirm this selected step is correct:\n   {label}\n\n"
                "Click OK to resume automation from that step.",
                parent=self.root,
            )
        )

    def _resume_in_progress_expense_report(self, *, use_crash_anchor: bool) -> None:
        """Relaunch Chromium, wait for login, open in-progress report (pencil), replay steps to saved anchor, then continue."""
        if self._step3_automation_active:
            self.set_status("Automation is already running.")
            return
        if use_crash_anchor:
            anchor = (self._crash_resume_anchor or self._last_populate_step or "wait_step2").strip()
            no_receipts = "Resume after crash blocked: no imported receipts. Run Step 1 first."
            relaunching = (
                "Resume after crash: relaunching browser — then wait for login before opening your report…"
            )
            browser_bad = "Browser not available after relaunch — use Open Oracle, then try Resume after crash again."
            continuing = (
                "Continuing: will wait for the expense home page, then open the in-progress report…"
            )
        else:
            anchor = (self._last_populate_step or "wait_step2").strip()
            no_receipts = "Resume previous report blocked: no imported receipts. Run Step 1 first."
            relaunching = (
                "Resume previous report: relaunching browser — then wait for login before opening your in-progress report…"
            )
            browser_bad = "Browser not available after relaunch — use Activity → Open Oracle, then try again."
            continuing = "Continuing: waiting for expense home, then opening your in-progress report…"
        if anchor not in POPULATE_RESUME_KEYS:
            anchor = "wait_step2"
        if anchor in ("nic_iexpenses", "create_report"):
            anchor = "wait_step1"
        if not self.receipt_paths and anchor != "step6_attach_files":
            self.set_status(no_receipts)
            return
        if anchor == "step6_attach_files":
            ok_m, err_m = validate_approved_for_attach(APP_DIR)
            if not ok_m:
                self.set_status(f"Resume Step 6 blocked: {err_m}")
                return
            self._run_step6_file_attach = True
        if anchor == "complete_report_step2":
            ok_m, err_m = validate_approved_for_attach(APP_DIR)
            if not ok_m:
                self.set_status(f"Resume blocked: {err_m}")
                return
            if not self._prepare_complete_report_llm_mode():
                return
            self._run_step6_file_attach = True
        self._step3_vpn_mode = "standard"
        self.set_status(relaunching)
        self.relaunch_controlled_browser_for_resume()
        if not self._controlled_browser_usable():
            self.set_status(browser_bad)
            return
        self.set_status(continuing)
        self._run_populate_expense_report_flow(crash_resume_continue=anchor)

    def on_resume_automation_after_crash(self) -> None:
        """Activity tab: relaunch, sign in, open in-progress report, continue from last crash anchor (or last step)."""
        self._resume_in_progress_expense_report(use_crash_anchor=True)

    def on_resume_previous_expense_report(self) -> None:
        """Expense report tab: choose resume step, prepare browser/login manually, then continue on confirmation."""
        if self._step3_automation_active:
            self.set_status("Stop the running Step 3 automation before resuming from a step.")
            return
        key = self._ask_resume_populate_step(
            self._last_populate_step,
            title="Resume previous report",
            intro=(
                "Choose the Oracle-aligned step to resume. Use “Launch browser & login” to open/reuse Chromium, "
                "finish login/2FA, and manually navigate to the matching Oracle page. "
                "The app verifies the selected step context before resuming."
            ),
            show_relaunch_browser=True,
            relaunch_button_text="Launch browser & login",
            relaunch_button_action=self.on_step_login,
        )
        if not key:
            self.set_status("Resume previous report cancelled.")
            return
        if not self.receipt_paths and key != "step6_attach_files":
            self.set_status("Resume previous report blocked: no imported receipts. Run Step 1 first.")
            return
        if not self._prepare_resume_step6_attach_if_needed(
            key,
            status_prefix="Resume blocked",
            require_complete_mode_for_step2=True,
        ):
            return
        if not self._controlled_browser_usable():
            self.set_status("Resume previous report: opening Chromium for login before resume…")
            self.on_step_login()
        else:
            self.set_status(
                "Resume previous report: Chromium is ready — finish login/2FA and navigate to the right state."
            )
        if not self._controlled_browser_usable():
            self.set_status("Resume previous report blocked: Chromium is not connected.")
            return
        if not self._confirm_ready_to_resume_step(key):
            self.set_status("Resume previous report paused: browser left open for manual prep.")
            return
        ok_page, msg = self._resume_anchor_page_matches(key)
        if not ok_page:
            self.set_status(msg)
            return
        self.set_status(f"Resuming expense report from: {key}…")
        self._run_populate_expense_report_flow(start_from=key)

    def _show_table_context_menu(self, event: tk.Event) -> None:
        row_id = self.table.identify_row(event.y)
        self._table_context_receipt_path = row_id if row_id else None
        if row_id:
            current_selection = self.table.selection()
            if row_id not in current_selection:
                self.table.selection_set(row_id)
                self.table.focus(row_id)
        try:
            self.table_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.table_menu.grab_release()

    def _remove_receipts(self, paths_to_remove: list[str]) -> int:
        if not paths_to_remove:
            return 0
        removed_set = set(paths_to_remove)
        original_count = len(self.receipt_paths)
        self.receipt_paths = [path for path in self.receipt_paths if path not in removed_set]
        self.analyses = [
            analysis
            for analysis in self.analyses
            if str(analysis.get("source_file", "")) not in removed_set
        ]
        for path in removed_set:
            self.assignment_map.pop(path, None)
        self.refresh_receipt_table()
        self._persist_runtime_state()
        return original_count - len(self.receipt_paths)

    def delete_selected_receipts(self) -> None:
        selected_paths = list(self.table.selection())
        if not selected_paths:
            self.set_status("Delete blocked: select one or more rows first.")
            return
        removed = self._remove_receipts(selected_paths)
        self.set_status(f"Removed {removed} selected image(s) from the table.")

    def delete_all_receipts(self) -> None:
        if not self.receipt_paths:
            self.set_status("Delete blocked: no images in the table.")
            return
        removed = self._remove_receipts(list(self.receipt_paths))
        self.set_status(f"Removed all images ({removed}) from the table.")

    def _upsert_analysis(self, analysis: dict) -> None:
        source_path = str(analysis.get("source_file", "")).strip()
        if not source_path:
            return
        merged = {item.get("source_file", ""): item for item in self.analyses}
        merged[source_path] = analysis
        self.analyses = list(merged.values())

    def _apply_single_analysis_result(self, analysis: dict) -> None:
        self._upsert_analysis(analysis)
        self.refresh_receipt_table()
        self._persist_runtime_state()

    def _rescan_receipts(self, target_paths: list[str], status_prefix: str) -> None:
        if not target_paths:
            self.set_status(f"{status_prefix} blocked: no images selected.")
            return
        api_key = self.get_openai_key().strip()
        if not api_key:
            self.set_status(f"{status_prefix} blocked: set OpenAI key in Settings first.")
            return

        if self._receipt_llm_worker_guard_or_notify():
            return

        self._receipt_llm_cancel.clear()
        self._begin_receipt_llm_worker_ui()

        def worker() -> None:
            t0 = time.monotonic()
            cancelled = False
            try:
                self.root.after(
                    0,
                    lambda: self.set_status(
                        f"{status_prefix}: analyzing {len(target_paths)} image(s) with LLM..."
                    ),
                )
                total = len(target_paths)
                for idx, source_path in enumerate(target_paths, start=1):
                    if self._receipt_llm_cancel.is_set():
                        cancelled = True
                        break
                    rescanned = analyze_receipts_with_llm(
                        receipt_paths=[source_path],
                        model=self.settings.openai_model,
                        api_key=api_key,
                        on_status=self.set_status,
                        http_verify_preferred=self.settings.openai_http_verify,
                    )
                    if rescanned:
                        analysis = rescanned[0]
                        self.root.after(0, lambda item=analysis: self._apply_single_analysis_result(item))
                    elapsed = int(time.monotonic() - t0)
                    self.root.after(
                        0,
                        lambda i=idx, t=total, e=elapsed, p=status_prefix: self.set_status(
                            f"{p}: analyzed {i}/{t} image(s) ({e}s elapsed)."
                        ),
                    )
                self.root.after(
                    0,
                    lambda: write_analysis_report(
                        self.analyses, Path(self.settings.photos_export_dir).expanduser()
                    ),
                )
                if cancelled:
                    self.root.after(
                        0,
                        lambda p=status_prefix: self.set_status(
                            f"{p}: stopped — partial re-scan kept; use Rescan on remaining files if needed."
                        ),
                    )
                else:
                    self.root.after(
                        0,
                        lambda: self.set_status(
                            f"{status_prefix}: completed re-scan for {len(target_paths)} image(s)."
                        ),
                    )
                self.root.after(
                    0,
                    lambda: setattr(self, "_session_progress_parsed_done", bool(self.analyses)),
                )
            except Exception as exc:
                self.root.after(0, lambda e=exc: self.set_status(f"{status_prefix} failed: {e}"))
            finally:
                self.root.after(0, self._end_receipt_llm_worker_ui)

        threading.Thread(target=worker, daemon=True).start()

    def rescan_context_row_receipt(self) -> None:
        path = getattr(self, "_table_context_receipt_path", None)
        if not path or path not in self.receipt_paths:
            self.set_status("Rescan item blocked: right-click a receipt row first.")
            return
        self._rescan_receipts([path], status_prefix="Rescan item")

    def rescan_selected_receipts(self) -> None:
        selected_paths = list(self.table.selection())
        if not selected_paths:
            self.set_status("Rescan blocked: select one or more rows first.")
            return
        self._rescan_receipts(selected_paths, status_prefix="Rescan selected")

    def rescan_all_receipts(self) -> None:
        if not self.receipt_paths:
            self.set_status("Rescan blocked: no images in the table.")
            return
        self._rescan_receipts(list(self.receipt_paths), status_prefix="Rescan all")

    def analyze_new_receipt_files_vpn_off(self) -> None:
        """Run LLM parse only on receipts not yet present in self.analyses (Documents tab; VPN off for OpenAI)."""
        if not self.receipt_paths:
            self.set_status("Analyze new files blocked: no images in the table.")
            return
        existing_analyses = {item.get("source_file", ""): item for item in self.analyses}
        new_paths = [p for p in self.receipt_paths if p not in existing_analyses]
        if not new_paths:
            self.set_status("Analyze new files: nothing to do — every file already has LLM parse data.")
            return
        self._rescan_receipts(new_paths, status_prefix="Analyze new files (VPN Off)")

    def export_and_optimize_all_receipts(self) -> None:
        """Documents tab: export/down-convert all receipt images and relink all caches to new paths."""
        if not self.receipt_paths:
            self.set_status("Export/down-convert blocked: no files in Documents.")
            return
        if self._receipt_llm_worker_active:
            self.set_status("Export/down-convert blocked: wait for receipt parsing to finish first.")
            return

        original_paths = list(self.receipt_paths)
        prepared_paths, copied_count, optimized_count, old_to_new = self._prepare_receipt_files_for_import(
            original_paths
        )
        if not prepared_paths:
            self.set_status("Export/down-convert blocked: no readable files were found.")
            return

        remap = {
            old: new
            for old, new in old_to_new.items()
            if str(old).strip() and str(new).strip() and old != new
        }
        # region agent log
        self._debug_log(
            hypothesis_id="HX2",
            location="receipt_automation_ui.py:export_and_optimize_all_receipts",
            message="Export/down-convert remap summary",
            data={
                "original_count": len(original_paths),
                "prepared_count": len(prepared_paths),
                "remap_count": len(remap),
                "optimized_count": optimized_count,
                "copied_count": copied_count,
            },
            run_id="export_downconvert_probe",
        )
        # endregion
        if not remap:
            self.set_status("Export/down-convert: no changes needed (files already optimized/stable).")
            return

        self.receipt_paths = list(dict.fromkeys(prepared_paths))

        remapped_analyses = 0
        merged_analyses: dict[str, dict] = {}
        for row in self.analyses:
            if not isinstance(row, dict):
                continue
            src = str(row.get("source_file", "") or "").strip()
            new_src = remap.get(src, src)
            if new_src != src:
                remapped_analyses += 1
            copy_row = dict(row)
            copy_row["source_file"] = new_src
            merged_analyses[new_src] = copy_row
        self.analyses = list(merged_analyses.values())

        remapped_assignments = 0
        new_assign: dict[str, str] = {}
        for k, v in self.assignment_map.items():
            kk = remap.get(str(k), str(k))
            if kk != str(k):
                remapped_assignments += 1
            new_assign[kk] = str(v)
        self.assignment_map = new_assign

        match_changed = 0
        matches = load_receipt_line_matches(APP_DIR)
        for block in matches.values():
            if not isinstance(block, dict):
                continue
            br = str(block.get("best_receipt", "") or "").strip()
            if br and br in remap:
                block["best_receipt"] = remap[br]
                match_changed += 1
        if match_changed:
            save_receipt_line_matches(APP_DIR, matches)

        approved_changed = 0
        approved = load_approved_matches(APP_DIR)
        for block in approved.values():
            if not isinstance(block, dict):
                continue
            sf = str(block.get("source_file", "") or "").strip()
            if sf and sf in remap:
                block["source_file"] = remap[sf]
                approved_changed += 1
        if approved_changed:
            save_approved_matches(APP_DIR, approved)

        lines_changed = 0
        lines, meta = load_expense_lines_cache(APP_DIR)
        if lines:
            for row in lines:
                if not isinstance(row, dict):
                    continue
                cbr = str(row.get("cached_best_receipt", "") or "").strip()
                if cbr and cbr in remap:
                    row["cached_best_receipt"] = remap[cbr]
                    lines_changed += 1
            if lines_changed:
                source = str(meta.get("source", "") or "step2_credit_card")
                save_expense_lines_cache(APP_DIR, lines, source=source)

        if match_changed:
            persist_expense_line_derived_fields(APP_DIR, matches=matches)

        self._invalidate_receipt_table_match_cache()
        self.refresh_all_tabs()
        self._persist_runtime_state()

        self.set_status(
            "Export/down-convert complete: "
            f"{optimized_count} optimized, {copied_count} copied, {len(remap)} relinked file path(s), "
            f"{match_changed} match link(s), {approved_changed} approved link(s), "
            f"{lines_changed} cached line link(s), {remapped_analyses} analyses."
        )

    def refresh_receipt_table(self) -> None:
        prev_sel = [x for x in self.table.selection() if x in self.receipt_paths]
        for item in self.table.get_children():
            self.table.delete(item)

        analysis_by_source = {entry.get("source_file", ""): entry for entry in self.analyses}
        if self._receipt_table_ma_cache is None:
            matches = load_receipt_line_matches(APP_DIR)
            approved = load_approved_matches(APP_DIR)
            approved_paths = {
                str((b or {}).get("source_file") or "").strip()
                for b in approved.values()
                if str((b or {}).get("source_file") or "").strip()
            }
            self._receipt_table_ma_cache = (matches, approved_paths)
        matches, approved_paths = self._receipt_table_ma_cache
        receipt_to_lines: dict[str, list[str]] = {}
        for lid, block in matches.items():
            br = str((block or {}).get("best_receipt") or "").strip()
            if br:
                receipt_to_lines.setdefault(br, []).append(str(lid))
        approved_by_line = load_approved_matches(APP_DIR)
        drift_rows: list[dict[str, str]] = []
        for lid, block in matches.items():
            best = str((block or {}).get("best_receipt") or "").strip()
            approved_src = str((approved_by_line.get(str(lid)) or {}).get("source_file") or "").strip()
            if best and approved_src and Path(best).expanduser() != Path(approved_src).expanduser():
                drift_rows.append(
                    {
                        "line_id": str(lid),
                        "llm_best_receipt": best,
                        "approved_source_file": approved_src,
                    }
                )
        if drift_rows:
            # region agent log
            self._debug_log(
                hypothesis_id="H2",
                location="receipt_automation_ui.py:refresh_receipt_table",
                message="Documents line-hint differs from approved source_file for one or more lines",
                data={
                    "drift_count": len(drift_rows),
                    "sample": drift_rows[:5],
                },
                run_id="pairing_drift_probe",
            )
            # endregion

        report_filter = self._get_selected_report_line_ids()
        if report_filter is not None:
            allowed_paths: set[str] = set()
            for lid in report_filter:
                br = str((matches.get(lid) or {}).get("best_receipt") or "").strip()
                if br:
                    allowed_paths.add(br)
                asrc = str((approved_by_line.get(lid) or {}).get("source_file") or "").strip()
                if asrc:
                    allowed_paths.add(asrc)
            visible_paths = [p for p in self.receipt_paths if p in allowed_paths]
        else:
            visible_paths = list(self.receipt_paths)

        for path_str in visible_paths:
            analysis = analysis_by_source.get(path_str, {})
            assignment_value = self.assignment_map.get(path_str, "")
            is_assigned = bool(assignment_value.strip())
            file_label = f"{Path(path_str).name} (assigned)" if is_assigned else Path(path_str).name
            assign_label = f"(assigned) {assignment_value}" if is_assigned else "(available)"
            parsed = "Yes" if path_str in analysis_by_source else "No"
            lids = receipt_to_lines.get(path_str) or []
            line_id = ", ".join(lids) if lids else "—"
            appr = "Yes" if path_str in approved_paths else "No"
            date_val = analysis.get("receipt_date") or analysis.get("transaction_date") or ""
            row = (
                file_label,
                parsed,
                str(analysis.get("vendor", "") or "")[:40],
                format_date_for_ui(str(date_val).strip())[:32],
                receipt_local_amount_display(analysis)[:160],
                receipt_usd_amount_display(analysis)[:64],
                analysis.get("confidence", ""),
                line_id,
                appr,
                assign_label,
            )
            tags = ("assigned",) if is_assigned else ()
            self.table.insert("", tk.END, iid=path_str, values=row, tags=tags)
        # region agent log
        self._debug_log(
            hypothesis_id="H3",
            location="receipt_automation_ui.py:refresh_receipt_table",
            message="Documents table rendered from match cache",
            data={
                "receipt_rows": len(self.receipt_paths),
                "match_entries": len(matches),
                "receipt_to_lines_keys": len(receipt_to_lines),
                "approved_receipt_paths": len(approved_paths),
            },
            run_id="pairing_drift_probe",
        )
        # endregion

        if prev_sel:
            ok = [p for p in prev_sel if self.table.exists(p)]
            if ok:
                self.table.selection_set(ok[0])
                for p in ok[1:]:
                    self.table.selection_add(p)
                self.table.focus(ok[0])
        elif self.receipt_paths:
            first = self.receipt_paths[0]
            self.table.selection_set(first)
            self.table.focus(first)

        self._documents_update_preview_from_selection()
        self._refresh_activity_panel()
        self._refresh_workflow_checklist()
        self._update_activity_recommendation_hint()


def main() -> None:
    root = tk.Tk()
    app = ReceiptAutomationUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
