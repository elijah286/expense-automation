"""
Headless Oracle transaction scraper.

Launches Chromium via Playwright CDP, navigates Oracle iExpenses,
creates a temporary expense report, scrapes all credit-card rows from
Step 2, cancels the wizard, and persists to the shared expense-lines cache.

Designed to be called from the web server without any Tk dependency.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from browser.reliability import RetryPolicy, execute_with_retry
from browser_automation import normalize_currency_code
from expense_lines_cache import (
    prune_receipt_sidecars_after_step2_scrape,
    save_expense_lines_cache,
)

APP_DIR = Path.home() / ".expense-automator"
CHROMIUM_USER_DATA = APP_DIR / "chromium-profile"

_EXPENSE_TABLE_NEXT_NAME = re.compile(
    r"^\s*Next\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s*$",
    re.IGNORECASE,
)
_EXPENSE_TABLE_NEXT_NAME_LOOSE = re.compile(
    r"Next\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
    re.IGNORECASE,
)
_EXPENSE_TABLE_PREV_NAME = re.compile(
    r"^\s*Previous\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\s*$",
    re.IGNORECASE,
)
_EXPENSE_TABLE_PREV_NAME_LOOSE = re.compile(
    r"Previous\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b",
    re.IGNORECASE,
)


def _blob_shows_wizard_step(blob: str, step: int, total: int = 6) -> bool:
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
    return bool(blob and re.search(r"\bapprovers?\b", blob, re.IGNORECASE))


StatusCallback = Callable[[str], None]

_ORACLE_LOGGED_IN_MARKERS = (
    "Update Expense Reports",
    "NIC iExpenses",
    "Create Expense Report",
    "Expenses Home",
    "Track Submitted",
    "Logged In As",
)


class TransactionScraper:
    """Headless Oracle iExpenses transaction scraper driven by Playwright."""

    def __init__(self, *, on_status: StatusCallback | None = None) -> None:
        self._on_status = on_status or (lambda _msg: None)
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.browser_context: BrowserContext | None = None
        self.browser_page: Page | None = None
        self._chromium_proc: subprocess.Popen[bytes] | None = None
        self._cdp_http_url: str | None = None
        self._scraped_expense_lines: list[dict[str, Any]] = []
        self._step2_credit_card_frame: Frame | None = None

    def set_status(self, msg: str) -> None:
        self._on_status(msg)

    # ------------------------------------------------------------------
    # Page-readiness helpers (Oracle EBS is slow / iframe-heavy)
    # ------------------------------------------------------------------

    _ORACLE_DOM_STABLE_JS = """
() => {
  const sig = () => {
    let n = 0;
    const walk = (root) => {
      n += root.querySelectorAll('*').length;
      for (const f of root.querySelectorAll('iframe')) {
        try { walk(f.contentDocument); } catch(e) {}
      }
    };
    try { walk(document); } catch(e) {}
    return n;
  };
  return sig();
}
"""

    def _wait_for_oracle_page_stable(
        self,
        *,
        timeout_s: float = 20.0,
        settle_ms: int = 800,
        poll_ms: int = 300,
    ) -> None:
        """Wait until the Oracle page DOM stops changing and the network is
        quiet.

        Oracle EBS renders via server round-trips and nested iframes.  Fixed
        delays are unreliable because page weight varies.  This method polls
        the total element count across all frames and waits until it stays
        constant for *settle_ms* milliseconds, or *timeout_s* elapses.

        It also attempts a Playwright ``networkidle`` wait (best-effort) so
        that pending XHR / resource loads finish before we proceed.
        """
        if not self.browser_page:
            return

        try:
            self.browser_page.wait_for_load_state(
                "domcontentloaded", timeout=min(timeout_s * 1000, 10_000)
            )
        except Exception:
            pass

        try:
            self.browser_page.wait_for_load_state(
                "networkidle", timeout=min(timeout_s * 1000, 10_000)
            )
        except Exception:
            pass

        deadline = time.monotonic() + timeout_s
        prev_count: int | None = None
        stable_since: float | None = None

        while time.monotonic() < deadline:
            try:
                count = self.browser_page.evaluate(self._ORACLE_DOM_STABLE_JS)
            except Exception:
                count = -1

            if count == prev_count and count > 0:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif (time.monotonic() - stable_since) * 1000 >= settle_ms:
                    return
            else:
                stable_since = None
                prev_count = count

            self.browser_page.wait_for_timeout(poll_ms)

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    @staticmethod
    def _wait_cdp_http_ready(http_base: str, timeout: float = 45.0) -> None:
        version_url = http_base.rstrip("/") + "/json/version"
        deadline = time.monotonic() + timeout
        last_exc: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(version_url, timeout=2) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
            time.sleep(0.2)
        raise RuntimeError(f"Chromium CDP did not become ready at {http_base} ({last_exc})")

    def _spawn_chromium(self) -> str:
        CHROMIUM_USER_DATA.mkdir(parents=True, exist_ok=True)
        port = self._find_free_port()
        http_base = f"http://127.0.0.1:{port}"
        if not self.playwright:
            self.playwright = sync_playwright().start()
        exe = self.playwright.chromium.executable_path
        args = [
            exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={CHROMIUM_USER_DATA}",
            "--no-first-run",
            "--no-default-browser-check",
            "--use-mock-keychain",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--disable-extensions",
            *(
                ()
                if os.environ.get("AUTOMATED_EXPENSES_CHROMIUM_ENABLE_GPU", "").strip() in ("1", "true", "yes")
                else ("--disable-gpu",)
            ),
            "about:blank",
        ]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._chromium_proc = proc
        self._wait_cdp_http_ready(http_base)
        self._cdp_http_url = http_base
        return http_base

    def _attach_to_cdp(self, http_base: str, target_url: str) -> None:
        if not self.playwright:
            self.playwright = sync_playwright().start()

        def _connect() -> Browser:
            assert self.playwright is not None
            return self.playwright.chromium.connect_over_cdp(http_base, slow_mo=0, is_local=True)

        self.browser = execute_with_retry(_connect, policy=RetryPolicy(max_attempts=5, initial_backoff_s=0.6))
        ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
        self.browser_context = ctx
        pages = ctx.pages
        page = pages[0] if pages else ctx.new_page()
        self.browser_page = page
        for p in list(ctx.pages):
            if p != page:
                try:
                    if not p.is_closed():
                        p.close()
                except Exception:
                    pass
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

    def open_browser(self, url: str) -> None:
        http_base = self._spawn_chromium()
        self._attach_to_cdp(http_base, url)

    def _reconnect_to_cdp(self, http_base: str) -> bool:
        """Reconnect Playwright to an existing Chromium CDP endpoint.

        Unlike ``_attach_to_cdp`` this does **not** navigate to a new URL,
        preserving whatever page state the browser is currently showing.
        Returns True when a usable page was found.
        """
        try:
            if not self.playwright:
                self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.connect_over_cdp(
                http_base, slow_mo=0, is_local=True
            )
            ctx = self.browser.contexts[0] if self.browser.contexts else None
            if not ctx or not ctx.pages:
                self._detach_playwright()
                return False
            self.browser_context = ctx
            self.browser_page = ctx.pages[0]
            self._cdp_http_url = http_base
            return True
        except Exception:
            self._detach_playwright()
            return False

    def _detach_playwright(self) -> None:
        """Disconnect Playwright from Chromium without killing the browser process."""
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
            self.browser_context = None
            self.browser_page = None
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        self._chromium_proc = None

    def close_browser(self) -> None:
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
            self.browser_context = None
            self.browser_page = None
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        proc = self._chromium_proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._chromium_proc = None

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def _oracle_shell_ready(self) -> bool:
        """True when the iExpenses shell (or wizard) is visible — past the login screen."""
        if not self.browser_page:
            return False
        for m in _ORACLE_LOGGED_IN_MARKERS:
            if self._body_contains_text(m):
                return True
        if self._wizard_any_frame_on_step(1) or self._wizard_any_frame_on_step(2):
            return True
        return False

    def wait_for_manual_oracle_login(self, *, timeout_s: float | None = None) -> None:
        """Block until the user signs in manually in Chromium (detects post-login Oracle UI)."""
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        raw = (os.environ.get("AUTOMATED_EXPENSES_MANUAL_LOGIN_TIMEOUT_S") or "").strip()
        if timeout_s is None:
            try:
                timeout_s = float(raw) if raw else 1200.0
            except ValueError:
                timeout_s = 1200.0
        timeout_s = max(60.0, float(timeout_s))

        self.set_status(
            "Waiting for you to sign in to Oracle in the browser (including 2FA if prompted)…"
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._oracle_shell_ready():
                self._wait_for_oracle_page_stable(settle_ms=500)
                self.set_status("Oracle session detected — continuing automation…")
                return
            self.browser_page.wait_for_timeout(400)

        raise RuntimeError(
            "Timed out waiting for Oracle login. Sign in in the Chromium window, then try again."
        )

    def try_auto_login(self, username: str, password: str) -> bool:
        if not self.browser_page:
            return False
        try:
            self.browser_page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        self.browser_page.wait_for_timeout(250)
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                if self._fill_and_submit_login(frame, username, password):
                    return True
            self.browser_page.wait_for_timeout(200)
        return False

    @staticmethod
    def _fill_and_submit_login(frame: Frame, username: str, password: str) -> bool:
        try:
            pass_loc = frame.locator(
                'input[name="passwordField"], input[name="password"][type="password"], '
                'input[type="password"]'
            )
            n_pw = pass_loc.count()
            if n_pw == 0:
                return False
            p = None
            for i in range(min(n_pw, 24)):
                cand = pass_loc.nth(i)
                try:
                    cand.wait_for(state="visible", timeout=150)
                    p = cand
                    break
                except Exception:
                    continue
            if p is None:
                return False

            user_loc = frame.locator(
                'input[name="usernameField"], input[name="userid"], '
                'input#usernameField, input[id*="Username" i][type="text"]'
            )
            for j in range(min(user_loc.count(), 12)):
                u = user_loc.nth(j)
                try:
                    u.wait_for(state="visible", timeout=150)
                    u.click(timeout=4000)
                    u.fill(username, timeout=4000)
                    break
                except Exception:
                    continue

            p.click(timeout=4000)
            p.fill(password, timeout=4000)

            for btn in [
                frame.locator("input[type='submit'][value*='Log' i]"),
                frame.locator("input[type='submit'][value*='Sign' i]"),
                frame.get_by_role("button", name=re.compile(r"log\s*in|sign\s*on|submit", re.I)),
            ]:
                try:
                    if btn.count() == 0:
                        continue
                    first = btn.first
                    first.wait_for(state="visible", timeout=400)
                    first.click(timeout=12000)
                    return True
                except Exception:
                    continue

            p.press("Enter")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Generic Oracle frame helpers
    # ------------------------------------------------------------------

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

    def _frames_preferred_first(self, preferred: Frame | None) -> list[Frame]:
        if not self.browser_page:
            return []
        frames = list(self.browser_page.frames)
        if preferred is None:
            return frames
        try:
            idx = frames.index(preferred)
            return [frames[idx]] + frames[:idx] + frames[idx + 1:]
        except ValueError:
            return frames

    # ------------------------------------------------------------------
    # Oracle navigation helpers
    # ------------------------------------------------------------------

    def _oracle_expand_navigator_row_for_label(self, label_substring: str) -> bool:
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
    if (a) { a.click(); return true; }
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
        if not self.browser_page:
            return
        if self._body_contains_text("Create Expense Report"):
            return
        self._oracle_expand_navigator_row_for_label("NIC iExpenses")
        self.browser_page.wait_for_timeout(900)
        if self._body_contains_text("Create Expense Report"):
            return
        self.click_text_in_any_frame("NIC iExpenses")
        self.browser_page.wait_for_timeout(1000)
        if self._body_contains_text("Create Expense Report"):
            return
        self._oracle_expand_navigator_row_for_label("NIC iExpenses")
        self.browser_page.wait_for_timeout(700)

    # ------------------------------------------------------------------
    # Step 1 — General Information
    # ------------------------------------------------------------------

    def wait_for_step1_general_information_ready(self, timeout_ms: int = 120000) -> None:
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
        raise RuntimeError("Timeout waiting for General Information (Step 1) to load.")

    def select_travel_template_in_any_frame(self) -> bool:
        if not self.browser_page:
            return False
        js = """
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
        for frame in self.browser_page.frames:
            try:
                if frame.evaluate(js):
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
                    frame.locator(
                        "input[name*='purpose' i], input[id*='purpose' i], "
                        "textarea[name*='purpose' i], textarea[id*='purpose' i]"
                    ),
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
                        target.dispatch_event("input")
                        target.dispatch_event("change")
                        target.press("Tab")
                        self.browser_page.wait_for_timeout(600)

                        # Verify value committed; Oracle ADF PPR can clear it
                        try:
                            verify = target.input_value(timeout=1200) or ""
                        except Exception:
                            verify = ""
                        if not verify.strip():
                            target.click(timeout=5000)
                            target.press_sequentially(
                                value, delay=30, timeout=10000
                            )
                            target.press("Tab")
                            self.browser_page.wait_for_timeout(600)
                        return True
            except Exception:
                continue
        return False

    _APPROVER_DOM_MARK = "data-rpa-approver-target"

    def _mark_approver_field_via_dom(self, frame: Frame) -> bool:
        try:
            return bool(
                frame.evaluate(
                    f"""
() => {{
  const MARK = "{self._APPROVER_DOM_MARK}";
  const allRoots = [];
  function walk(root) {{
    allRoots.push(root);
    const tree = root.querySelectorAll("*");
    for (let i = 0; i < tree.length; i++) {{
      const el = tree[i];
      if (el.shadowRoot) walk(el.shadowRoot);
    }}
  }}
  walk(document);
  for (let r = 0; r < allRoots.length; r++) {{
    allRoots[r].querySelectorAll("[" + MARK + "]").forEach((e) => e.removeAttribute(MARK));
  }}
  const visible = (el) => {{
    const s = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return s.visibility !== "hidden" && s.display !== "none" && rect.width >= 2 && rect.height >= 2;
  }};
  const isOkInput = (inp) => {{
    if (inp.getAttribute && inp.getAttribute("contenteditable") === "true") return true;
    const tag = inp.tagName;
    if (tag === "TEXTAREA") return true;
    if (tag !== "INPUT") return false;
    const t = (inp.type || "text").toLowerCase();
    return !["hidden","submit","button","checkbox","radio","file","image"].includes(t);
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
        first_token = value.split(",")[0].strip() or value.strip() or value
        approver_query = first_token[:10] if len(first_token) > 10 else first_token
        if not approver_query:
            approver_query = value
        approver_name = re.compile(r"approver", re.IGNORECASE)
        mark_sel = f'[{self._APPROVER_DOM_MARK}="1"]'
        name_pat = re.compile(re.escape(value), re.IGNORECASE)

        def run_approver_interaction(fr: Frame, target: Any) -> bool:
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
                try:
                    target.fill(approver_query, timeout=3500)
                except Exception:
                    target.press_sequentially(approver_query, delay=12, timeout=8000)
                assert self.browser_page is not None
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
                    assert self.browser_page is not None
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

        assert self.browser_page is not None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate(
                        "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                    )
                except Exception:
                    continue
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

    # ------------------------------------------------------------------
    # Wizard controls (Save / Next / Cancel)
    # ------------------------------------------------------------------

    def click_save_button_wizard_in_any_frame(
        self,
        timeout_ms: int = 20000,
        body_must_contain: str | None = "Step 1 of 6",
        *,
        wizard_step: int | None = None,
        wizard_total: int = 6,
    ) -> bool:
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
                clicked = frame.evaluate("""
() => {
  const inputs = Array.from(document.querySelectorAll("input[type='submit'], input[type='button']"));
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
""")
                if clicked:
                    return True
            except Exception:
                continue
        if body_must_contain or wizard_step is not None:
            return self.click_save_button_wizard_in_any_frame(
                timeout_ms=timeout_ms, body_must_contain=None, wizard_step=None
            )
        return False

    def _try_click_wizard_next_in_frame_dom(self, frame: Frame) -> bool:
        try:
            return bool(
                frame.evaluate("""
() => {
  const norm = (s) => (s || '').replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 2 && r.height > 2;
  };
  const label = (el) => norm(el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
  const els = Array.from(document.querySelectorAll('button, input[type="submit"], input[type="button"], a'));
  for (const el of els) { if (label(el) === 'next' && visible(el)) { el.click(); return true; } }
  return false;
}
""")
            )
        except Exception:
            return False

    def wait_for_wizard_next_enabled_and_click(
        self,
        timeout_ms: int = 120000,
        *,
        wizard_step: int | None = None,
        wizard_total: int = 6,
    ) -> bool:
        if not self.browser_page:
            return False

        def frame_context_matches(blob: str) -> bool:
            if wizard_step is not None:
                return _blob_shows_wizard_step(blob, wizard_step, wizard_total)
            return True

        name_pat = re.compile(r"^\s*Next\s*$", re.IGNORECASE)
        deadline = time.monotonic() + timeout_ms / 1000.0
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
            self.browser_page.wait_for_timeout(300)
        return False

    def click_cancel_button_wizard_in_any_frame(
        self, timeout_ms: int = 20000, *, wizard_step: int | None = None, wizard_total: int = 6
    ) -> bool:
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
                if btn.count() > 0 and btn.first.is_enabled():
                    btn.first.click(timeout=timeout_ms)
                    return True
                link = frame.get_by_role("link", name=name_pat)
                if link.count() > 0 and link.first.is_enabled():
                    link.first.click(timeout=timeout_ms)
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
                clicked = frame.evaluate("""
() => {
  const norm = (s) => (s || '').replace(/[\\u200b\\u200c\\u200d\\ufeff]/g, '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 2 && r.height > 2;
  };
  const inputs = Array.from(document.querySelectorAll("input[type='submit'], input[type='button'], button, a"));
  for (const el of inputs) {
    const v = norm(el.value || el.textContent || '');
    if (v !== 'cancel') continue;
    if (!visible(el)) continue;
    el.click();
    return true;
  }
  return false;
}
""")
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
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 2 && r.height > 2;
  };
  const blob = ((document.body && document.body.innerText) || '').toLowerCase();
  if (!blob.includes('have not been saved') && !blob.includes('changes will be discarded')) return false;
  const candidates = Array.from(
    document.querySelectorAll("button, a[href], input[type='button'], input[type='submit'], span[role='button']")
  );
  for (const el of candidates) {
    const raw = (el.textContent || el.value || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
    if (!raw) continue;
    if (!/^ok$/i.test(raw) && !/^yes$/i.test(raw)) continue;
    if (visible(el)) { el.click(); return true; }
  }
  return false;
}
"""

    def _dismiss_unsaved_changes_prompt_in_any_frame(self, timeout_ms: int = 15000) -> bool:
        if not self.browser_page:
            return False
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                try:
                    if frame.evaluate(self._UNSAVED_PROMPT_CLICK_OK_JS):
                        self.browser_page.wait_for_timeout(400)
                        return True
                except Exception:
                    continue
            self.browser_page.wait_for_timeout(200)
        return False

    # ------------------------------------------------------------------
    # Step 2 — Credit Card Transactions (scraping)
    # ------------------------------------------------------------------

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
    if (s > score) { score = s; best = table; }
  }
  if (!best || score < 4) return false;
  const boxes = best.querySelectorAll('tbody input[type="checkbox"]');
  let n = 0;
  boxes.forEach((cb) => {
    if (!isVisible(cb)) return;
    if (!cb.checked) { cb.click(); n++; } else { n++; }
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
    if (score > bestScore) { bestScore = score; best = { table, mi, di, ci, ai }; }
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
    const r = new RegExp('(\\\\d+)\\\\s*[-\\\\u2013\\\\u2014]\\\\s*(\\\\d+)\\\\s+of\\\\s+(\\\\d+)', 'gi');
    let m;
    while ((m = r.exec(txt)) !== null) { triples.push([Number(m[1]), Number(m[2]), Number(m[3])]); }
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
    rows.push({ row_index: rowIndex, merchant_name: merchant, transaction_date: dateStr, currency: cur, amount: amt });
  });
  return { ok: true, visibleArea, pageRange, rows };
}
"""

    def _step2_pick_best_credit_snapshot(self) -> tuple[Frame, dict] | None:
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
        return (ranked[0][0], ranked[0][1])

    def select_transactions_header_checkbox_in_any_frame(
        self, preferred_frame: Frame | None = None
    ) -> bool:
        if not self.browser_page:
            return False
        for frame in self._frames_preferred_first(preferred_frame):
            try:
                if frame.evaluate(self._STEP2_SELECT_ALL_TBODY_CHECKBOXES_JS):
                    return True
            except Exception:
                continue
        return False

    def get_step2_credit_table_page_range_in_any_frame(self) -> tuple[int, int, int] | None:
        picked = self._step2_pick_best_credit_snapshot()
        if not picked:
            return self._get_transactions_page_range_in_any_frame()
        _, data = picked
        pr = data.get("pageRange")
        if pr and isinstance(pr, (list, tuple)) and len(pr) == 3:
            return (int(pr[0]), int(pr[1]), int(pr[2]))
        return self._get_transactions_page_range_in_any_frame()

    def _get_transactions_page_range_in_any_frame(self) -> tuple[int, int, int] | None:
        if not self.browser_page:
            return None
        triples: list[tuple[int, int, int]] = []
        for frame in self.browser_page.frames:
            try:
                text_match = frame.evaluate("""
(expectedVisible) => {
  const txt = document.body?.innerText || '';
  const re = /(\\d+)\\s*-\\s*(\\d+)\\s+of\\s+(\\d+)/gi;
  const triples = [];
  let m;
  while ((m = re.exec(txt)) !== null) { triples.push([Number(m[1]), Number(m[2]), Number(m[3])]); }
  if (!triples.length) return null;
  const pickFrom = (cands) => {
    if (!cands.length) return null;
    const maxTotal = Math.max(...cands.map((t) => t[2]));
    const same = cands.filter((t) => t[2] === maxTotal);
    return same.reduce((a, b) => (b[1] > a[1] ? b : a));
  };
  return pickFrom(triples);
}
""", None)
                if text_match and len(text_match) == 3:
                    triples.append((int(text_match[0]), int(text_match[1]), int(text_match[2])))
            except Exception:
                continue
        if not triples:
            return None
        max_total = max(t[2] for t in triples)
        best = [t for t in triples if t[2] == max_total]
        return max(best, key=lambda t: t[1])

    @staticmethod
    def _step2_paging_fully_done(
        page_range: tuple[int, int, int] | None, lines_scraped_total: int
    ) -> bool:
        if not page_range or page_range[2] <= 0:
            return False
        _start, end, total = page_range
        return end >= total and lines_scraped_total >= total

    # ------------------------------------------------------------------
    # Table pagination
    # ------------------------------------------------------------------

    def _click_table_pagination_next_via_dom_in_frame(self, frame: Frame) -> bool:
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
    const t = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
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

    def _frame_shows_transaction_page_range(self, frame: Frame) -> bool:
        try:
            blob = frame.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
            )
            return bool(blob and re.search(r"\d+\s*-\s*\d+\s+of\s+\d+", blob, re.IGNORECASE))
        except Exception:
            return False

    def _click_plain_next_pagination_link(
        self, timeout_ms: int, *, preferred_frame: Frame | None = None
    ) -> bool:
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

    def click_expense_table_pagination_next_in_any_frame(
        self,
        timeout_ms: int = 12000,
        *,
        retry_rounds: int = 12,
        pause_between_ms: int = 700,
        preferred_frame: Frame | None = None,
    ) -> bool:
        if not self.browser_page:
            return False
        for attempt in range(retry_rounds):
            for frame in self._frames_preferred_first(preferred_frame):
                try:
                    for role in ("link", "button"):
                        for name_pat in (_EXPENSE_TABLE_NEXT_NAME, _EXPENSE_TABLE_NEXT_NAME_LOOSE):
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

    def click_expense_table_pagination_previous_in_any_frame(
        self,
        timeout_ms: int = 12000,
        *,
        retry_rounds: int = 8,
        pause_between_ms: int = 700,
        preferred_frame: Frame | None = None,
    ) -> bool:
        if not self.browser_page:
            return False
        for attempt in range(retry_rounds):
            for frame in self._frames_preferred_first(preferred_frame):
                try:
                    for role in ("link", "button"):
                        for name_pat in (_EXPENSE_TABLE_PREV_NAME, _EXPENSE_TABLE_PREV_NAME_LOOSE):
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

    def expense_table_go_to_first_page_in_any_frame(self, *, max_steps: int = 80) -> None:
        if not self.browser_page:
            return
        picked = self._step2_pick_best_credit_snapshot()
        if picked:
            self._step2_credit_card_frame = picked[0]
        for _ in range(max_steps):
            pr = self.get_step2_credit_table_page_range_in_any_frame()
            if pr is not None and pr[0] <= 1:
                self.browser_page.wait_for_timeout(350)
                return
            if not self.click_expense_table_pagination_previous_in_any_frame(
                preferred_frame=self._step2_credit_card_frame,
            ):
                break
            self.browser_page.wait_for_timeout(500)

    def _credit_card_table_pagination_can_advance(
        self, *, preferred_frame: Frame | None = None
    ) -> bool:
        if not self.browser_page:
            return False
        for frame in self._frames_preferred_first(preferred_frame):
            try:
                for role in ("link", "button"):
                    for name_pat in (_EXPENSE_TABLE_NEXT_NAME, _EXPENSE_TABLE_NEXT_NAME_LOOSE):
                        loc = frame.get_by_role(role, name=name_pat)
                        for i in range(loc.count()):
                            cand = loc.nth(i)
                            try:
                                if cand.is_visible() and cand.is_enabled():
                                    return True
                            except Exception:
                                continue
            except Exception:
                continue
        for frame in self._frames_preferred_first(preferred_frame):
            if not self._frame_shows_transaction_page_range(frame):
                continue
            plain_next = re.compile(r"^\s*Next\s*$", re.IGNORECASE)
            try:
                loc = frame.get_by_role("link", name=plain_next)
                for i in range(loc.count()):
                    cand = loc.nth(i)
                    try:
                        if cand.is_visible() and cand.is_enabled():
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    # ------------------------------------------------------------------
    # Row ingestion
    # ------------------------------------------------------------------

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

    def _persist_scraped_lines(self) -> Path:
        path = save_expense_lines_cache(APP_DIR, self._scraped_expense_lines, source="step2_credit_card")
        prune_receipt_sidecars_after_step2_scrape(APP_DIR, self._scraped_expense_lines)
        return path

    # ------------------------------------------------------------------
    # Complete Step 2 scrape (paginated)
    # ------------------------------------------------------------------

    def _scrape_credit_card_transactions(self) -> None:
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")

        if not self._wizard_any_frame_on_step(2):
            raise RuntimeError(
                "Scrape expects Credit Card Transactions (Step 2 of 6). "
                "Navigate to Step 2 in the wizard, then try again."
            )

        self._scraped_expense_lines = []
        self._step2_credit_card_frame = None
        max_pages = 150
        self.set_status("Step 2: moving to first page of credit card transactions…")
        self.expense_table_go_to_first_page_in_any_frame()
        if self.browser_page:
            self.browser_page.wait_for_timeout(500)

        for page_idx in range(max_pages):
            locate = self._step2_pick_best_credit_snapshot()
            if not locate:
                raise RuntimeError("Could not find the credit card transactions table.")
            credit_frame, snap = locate
            self._step2_credit_card_frame = credit_frame

            page_range: tuple[int, int, int] | None = None
            pr = snap.get("pageRange")
            if pr and isinstance(pr, (list, tuple)) and len(pr) == 3:
                page_range = (int(pr[0]), int(pr[1]), int(pr[2]))
            scraped = list(snap.get("rows") or [])
            if page_range:
                start, end, total = page_range
                self.set_status(f"Selecting credit card transactions {start}-{end} of {total}...")
            else:
                self.set_status("Selecting all transactions on current page...")

            if not self.select_transactions_header_checkbox_in_any_frame(credit_frame):
                raise RuntimeError("Could not apply transaction row selection on this page.")
            self.browser_page.wait_for_timeout(400)

            picked = self._step2_pick_best_credit_snapshot()
            if not picked:
                raise RuntimeError("Could not read the credit card transactions table after selecting rows.")
            credit_frame, snap = picked
            self._step2_credit_card_frame = credit_frame

            self.set_status(
                f"Step 2 scrape page {page_idx + 1}: {len(scraped)} row(s) from visible credit table."
            )
            for raw in scraped:
                self._ingest_scraped_credit_row(raw, page_idx)

            self.set_status("Saving selected transactions for current page...")
            if not self.click_text_in_any_frame("Save"):
                raise RuntimeError("Could not click Save while processing transactions.")
            self.browser_page.wait_for_timeout(900)

            page_range = self.get_step2_credit_table_page_range_in_any_frame()
            have = len(self._scraped_expense_lines)
            if self._step2_paging_fully_done(page_range, have):
                break

            self.set_status("Moving to next page of transactions (table pagination)...")
            clicked_next = self.click_expense_table_pagination_next_in_any_frame(
                preferred_frame=self._step2_credit_card_frame,
            )
            if clicked_next:
                self.browser_page.wait_for_timeout(900)
                continue

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
            can_adv = self._credit_card_table_pagination_can_advance(
                preferred_frame=self._step2_credit_card_frame,
            )
            if not can_adv:
                self.set_status("On last transaction page — continuing…")
                break
            if page_range and page_range[1] < page_range[2]:
                raise RuntimeError(
                    "Could not click table pagination but more transactions remain. "
                    "Fix the browser and retry."
                )
            break

        path = self._persist_scraped_lines()
        self.set_status(f"Scraped {len(self._scraped_expense_lines)} transaction(s) -> {path}")

    def _cancel_wizard_step2(self) -> None:
        page = self.browser_page
        if not page:
            return

        def on_dialog(dialog: Any) -> None:
            dialog.accept()

        page.once("dialog", on_dialog)
        self.set_status("Cancelling wizard (discarding portal edits)…")
        if not self.click_cancel_button_wizard_in_any_frame(wizard_step=2):
            self.set_status("Warning: could not click Cancel on Step 2.")
            return
        page.wait_for_timeout(600)
        self._dismiss_unsaved_changes_prompt_in_any_frame()
        page.wait_for_timeout(400)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def scrape(
        self,
        portal_url: str,
        approver: str,
        *,
        keep_browser_on_error: bool = True,
    ) -> list[dict[str, Any]]:
        """Full scraping flow: launch browser, wait for manual login, navigate, scrape, cancel, close.

        Returns the list of scraped expense line dicts.
        When *keep_browser_on_error* is True (default), Chromium stays open on
        failure so the user can inspect the Oracle page state.
        """
        succeeded = False
        approver = re.sub(r"\s+", " ", str(approver or "").strip())
        if not approver:
            raise RuntimeError("Approver display name is required (set it in Settings).")

        try:
            self.set_status("Launching Chromium…")
            self.open_browser(portal_url)

            self.wait_for_manual_oracle_login()
            assert self.browser_page is not None
            self.browser_page.wait_for_timeout(500)

            self.set_status("Expanding NIC iExpenses in Navigator…")
            self._oracle_expand_nic_iexpenses_menu()
            self.browser_page.wait_for_timeout(400)

            self.set_status("Opening Create Expense Report…")
            if not self._body_contains_text("Create Expense Report"):
                self._oracle_expand_nic_iexpenses_menu()
                self.browser_page.wait_for_timeout(600)
            if not self.click_text_in_any_frame("Create Expense Report"):
                raise RuntimeError(
                    "Could not click 'Create Expense Report'. "
                    "Ensure the Oracle portal is accessible."
                )

            self.set_status("Waiting for General Information (Step 1)…")
            self.wait_for_step1_general_information_ready()

            self.set_status("Selecting Travel template…")
            if not self.select_travel_template_in_any_frame():
                raise RuntimeError("Could not find template dropdown or Travel option.")
            self.browser_page.wait_for_timeout(350)

            self.set_status("Setting purpose…")
            if not self.fill_purpose_in_any_frame("travel"):
                raise RuntimeError("Could not locate Purpose field.")
            self.browser_page.wait_for_timeout(300)

            self.set_status(f"Setting approver: {approver}…")
            if not self.fill_approver_in_any_frame(approver):
                raise RuntimeError("Could not locate Approver field.")
            # Oracle LOV dropdown interaction can trigger background re-renders;
            # wait for the page to settle before clicking Save.
            self.browser_page.wait_for_timeout(1200)

            self.set_status("Saving General Information…")
            if not self.click_save_button_wizard_in_any_frame():
                # Retry once: the page may still be processing the approver LOV.
                self.browser_page.wait_for_timeout(2000)
                if not self.click_save_button_wizard_in_any_frame():
                    raise RuntimeError("Could not click Save on General Information.")
            self.browser_page.wait_for_timeout(600)

            self.set_status("Clicking Next to Step 2…")
            if not self.wait_for_wizard_next_enabled_and_click(wizard_step=1):
                raise RuntimeError("Next did not become enabled after Save.")

            self.set_status("Waiting for Credit Card Transactions (Step 2)…")
            self.wait_for_step2_credit_card_transactions()

            self._scrape_credit_card_transactions()

            self._cancel_wizard_step2()

            succeeded = True
            return list(self._scraped_expense_lines)
        finally:
            if succeeded or not keep_browser_on_error:
                self.close_browser()
            else:
                # Detach Playwright but leave the Chromium process running so
                # the user can inspect the page that caused the failure.
                self.set_status(
                    "Scrape failed — Chromium left open for inspection. "
                    "Close it manually when done."
                )
                self._detach_playwright()
