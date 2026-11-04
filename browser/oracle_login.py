"""Single-attempt saved sign-in, shared by web and desktop workflows."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError, Frame, Page

import keychain_credentials as credentials


REJECTED_MESSAGE = (
    "[CREDENTIALS_REJECTED] Oracle rejected your saved sign-in or your password has expired. "
    "Update your Oracle password in Settings. Automatic sign-in is paused to avoid account "
    "lockouts; you can sign in manually in the browser to continue this run (including MFA)."
)
_REJECTION = re.compile(
    r"invalid (?:user ?name|user id|login|credentials|password)"
    r"|(?:login|logon|authentication|sign.in) (?:failed|denied|was unsuccessful)"
    r"|(?:incorrect|wrong) (?:user ?name|password)"
    r"|password (?:has expired|is expired|expired|must be changed)"
    r"|(?:account|user) (?:is |has been )?locked"
    r"|(?:user ?name|password)[^\n.]{0,60}(?:incorrect|invalid)"
    r"|you must (?:change|reset) your password",
    re.I,
)


class SavedOracleLogin:
    def __init__(self, portal_url: str, on_status: Callable[[str], None]) -> None:
        self.on_status = on_status
        self.attempted = False
        self.finished = False
        self.submitted_at = 0.0
        self.wait_notice_sent = False
        try:
            self.saved = credentials.get_oracle_credentials()
            self.origin = credentials.oracle_origin(portal_url)
        except (RuntimeError, ValueError):
            self.saved = {}
            self.finished = True
            on_status("Saved Oracle sign-in unavailable. Check secure storage and the HTTPS portal in Settings; sign in manually.")
            return
        if self.saved.get("rejected") == "true":
            on_status(REJECTED_MESSAGE)
            self.finished = True
        elif not self.saved.get("password") or self.saved.get("origin") != self.origin:
            self.finished = True
            on_status("No saved sign-in for this portal. Sign in manually or save Oracle credentials in Settings.")

    def poll(self, page: Page) -> None:
        if self.finished:
            return
        for frame in page.frames:
            try:
                # Check both top-level and frame origins, including after redirects.
                if (credentials.oracle_origin(page.url) != self.origin
                        or credentials.oracle_origin(frame.url) != self.origin):
                    continue
                if self.attempted:
                    text = frame.locator("body").inner_text(timeout=500)
                    if _REJECTION.search(text):
                        self.finished = True
                        self.on_status(REJECTED_MESSAGE)
                        try:
                            error = credentials.mark_oracle_credentials_rejected(self.saved)
                        except RuntimeError:
                            error = "Keychain unavailable"
                        if error:
                            self.on_status("Could not persist the rejected-password flag. Unlock the keychain and update Settings before another run.")
                        return
                elif self._submit(frame):
                    self.on_status("Saved Oracle sign-in submitted once. Complete MFA in the browser if prompted.")
                    return
                if self.finished:
                    return
            except ValueError:
                continue  # about:blank / redirects are never eligible for autofill.
            except PlaywrightError:
                self.on_status("Oracle login page changed while signing in. Continue manually; saved credentials will not be retried.")
                self.finished = True
                return
        if self.attempted and not self.wait_notice_sent and time.monotonic() - self.submitted_at >= 30:
            self.wait_notice_sent = True
            self.on_status(
                "Saved sign-in has not reached Oracle yet. Complete MFA/SSO or sign in manually. "
                "If your password changed, update it in Settings. No automatic retry will be made."
            )

    def _submit(self, frame: Frame) -> bool:
        users = frame.locator(
            'input[name="usernameField"], input[name="userid"], input[name="username"], '
            'input#usernameField, input[id*="Username" i][type="text"], input[autocomplete="username"]'
        )
        passwords = frame.locator('input[type="password"]')
        user = next((u for u in users.all() if u.is_visible() and u.is_editable()), None)
        password = next((p for p in passwords.all() if p.is_visible() and p.is_editable()), None)
        if user is None or password is None:
            return False
        action = password.evaluate("(el) => el.form ? el.form.action : window.location.href")
        if credentials.oracle_origin(urljoin(frame.url, action)) != self.origin:
            self.finished = True
            self.on_status("Oracle sign-in redirects to a different origin. Sign in manually; saved credentials were not filled.")
            return False
        # Mark before interactions: an ambiguous navigation/click must never trigger a retry.
        self.attempted = True
        self.submitted_at = time.monotonic()
        user.fill(self.saved["username"], timeout=4000)
        password.fill(self.saved["password"], timeout=4000)
        buttons = frame.get_by_role("button", name=re.compile(r"^(?:log\s*in|sign\s*(?:in|on)|submit)$", re.I))
        button = next((b for b in buttons.all() if b.is_visible() and b.is_enabled()), None)
        if button is not None:
            button.click(timeout=12000)
        else:
            password.press("Enter", timeout=12000)
        return True
