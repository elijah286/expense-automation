"""Isolated keychain and sign-in tests: no real credentials or Oracle connections."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

import keychain_credentials as credentials
from browser.oracle_login import REJECTED_MESSAGE, SavedOracleLogin
from browser.scraper import TransactionScraper
from browser.submitter import ReportSubmitter, SubmissionPayload
from web.service import ExpenseService


PORTAL = "https://oracle.example.test/login"
PASSWORD = " synthetic test password "


class FakeKeychainTests(unittest.TestCase):
    def setUp(self):
        self.vault = {}
        self.patches = [
            patch.object(credentials, "_oracle_blob", None),
            patch.object(credentials, "_blob", None),
            patch.object(credentials, "_loaded", False),
            patch.object(credentials, "_keychain_gated", False),
            patch("keyring.get_password", side_effect=lambda service, account: self.vault.get(account)),
            patch("keyring.set_password", side_effect=lambda service, account, value: self.vault.update({account: value})),
            patch("keyring.delete_password", side_effect=lambda service, account: self.vault.pop(account, None)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def save_login(self):
        self.assertIsNone(credentials.save_oracle_credentials(PORTAL, " employee ", PASSWORD))


class CredentialStorageTests(FakeKeychainTests):
    def test_roundtrip_and_restart_preserves_password_whitespace(self):
        self.save_login()
        credentials._oracle_blob = None
        saved = credentials.get_oracle_credentials()
        self.assertEqual(saved["username"], "employee")
        self.assertEqual(saved["password"], PASSWORD)
        self.assertEqual(saved["origin"], "https://oracle.example.test:443")
        saved["password"] = "not the stored password"
        self.assertEqual(credentials.get_oracle_credentials()["password"], PASSWORD)

    def test_openai_update_and_legacy_migration_do_not_remove_oracle(self):
        self.save_login()
        self.vault[credentials.KEYRING_CREDENTIALS_V1] = json.dumps({
            "openai_api_key": "old-api", "expense_portal_password": "legacy",
        })
        self.assertEqual(credentials.get_keychain_openai_key(), "old-api")
        self.assertIsNone(credentials.set_keychain_openai_key("new-api"))
        self.assertEqual(credentials.get_keychain_openai_key(), "new-api")
        self.assertEqual(credentials.get_oracle_credentials()["password"], PASSWORD)
        credentials._loaded = False
        self.assertEqual(credentials.get_keychain_openai_key(), "new-api")

    def test_keep_replace_forget_and_rejection(self):
        self.save_login()
        self.assertIsNone(credentials.save_oracle_credentials(PORTAL, "employee", None))
        old = credentials.get_oracle_credentials()
        self.assertIsNone(credentials.mark_oracle_credentials_rejected(old))
        credentials._oracle_blob = None
        self.assertEqual(credentials.get_oracle_credentials()["rejected"], "true")
        with self.assertRaisesRegex(ValueError, "current password"):
            credentials.save_oracle_credentials(PORTAL, "employee", None)
        self.assertIsNone(credentials.save_oracle_credentials(PORTAL, "employee", "rotated"))
        self.assertEqual(credentials.get_oracle_credentials()["rejected"], "")
        self.assertIsNone(credentials.delete_oracle_credentials())
        credentials._oracle_blob = None
        self.assertEqual(credentials.get_oracle_credentials(), {})

    def test_rejection_does_not_invalidate_concurrently_updated_password(self):
        self.save_login()
        old = credentials.get_oracle_credentials()
        credentials.save_oracle_credentials(PORTAL, "employee", "rotated")
        credentials.mark_oracle_credentials_rejected(old)
        self.assertEqual(credentials.get_oracle_credentials()["rejected"], "")

    def test_new_account_or_origin_requires_password(self):
        self.save_login()
        for url, username in ((PORTAL, "other"), ("https://other.example.test", "employee")):
            with self.subTest(url=url, username=username), self.assertRaises(ValueError):
                credentials.save_oracle_credentials(url, username)
        for url in ("http://oracle.example.test", "https://user:pass@oracle.example.test", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                credentials.save_oracle_credentials(url, "employee", PASSWORD)
        self.assertEqual(credentials.oracle_origin("https://[::1]/login"), "https://[::1]:443")

    def test_incomplete_or_corrupt_keychain_entry_is_reported(self):
        for raw in ('not json', '[]', '{"password": "synthetic"}', '{"username": 5}'):
            with self.subTest(raw=raw):
                credentials._oracle_blob = None
                self.vault[credentials.ORACLE_CREDENTIALS] = raw
                with self.assertRaisesRegex(RuntimeError, "Could not read"):
                    credentials.get_oracle_credentials()

    def test_gate_blocks_reads_writes_and_deletion(self):
        self.save_login()
        before = self.vault.copy()
        credentials.enable_keychain_access_gate()
        self.assertEqual(credentials.get_oracle_credentials(), {})
        self.assertIn("Secure storage", credentials.save_oracle_credentials(PORTAL, "employee", PASSWORD))
        self.assertIn("Secure storage", credentials.delete_oracle_credentials())
        self.assertEqual(self.vault, before)

    def test_keychain_failures_are_explicit_and_never_update_cache(self):
        self.save_login()
        with patch("keyring.set_password", side_effect=RuntimeError(PASSWORD)):
            error = credentials.save_oracle_credentials(PORTAL, "employee", "new")
            self.assertIn("not saved", error)
            self.assertNotIn(PASSWORD, error)
        self.assertEqual(credentials.get_oracle_credentials()["password"], PASSWORD)
        credentials._oracle_blob = None
        with patch("keyring.get_password", side_effect=RuntimeError(PASSWORD)):
            with self.assertRaisesRegex(RuntimeError, "Could not read") as caught:
                credentials.get_oracle_credentials()
            self.assertNotIn(PASSWORD, str(caught.exception))

    def test_failed_rejection_persistence_still_blocks_this_process(self):
        self.save_login()
        with patch("keyring.set_password", side_effect=RuntimeError("locked")):
            self.assertIsNotNone(credentials.mark_oracle_credentials_rejected(credentials.get_oracle_credentials()))
        self.assertEqual(credentials.get_oracle_credentials()["rejected"], "true")

    def test_service_settings_mask_password_and_do_not_write_login_to_disk(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ExpenseService(Path(temp))
            self.assertEqual(service.save_settings(
                oracle_url=PORTAL, approver="Manager", oracle_username="employee",
                oracle_password=PASSWORD,
            ), [])
            settings = service.get_settings()
            self.assertTrue(settings["oracle_login_saved"])
            self.assertEqual(settings["oracle_username"], "employee")
            self.assertNotIn(PASSWORD, json.dumps(settings))
            raw = service._settings_path().read_text()
            self.assertNotIn(PASSWORD, raw)
            self.assertNotIn("employee", raw)
            self.assertEqual(service.save_settings(oracle_url=PORTAL, oracle_username="employee"), [])
            self.assertEqual(credentials.get_oracle_credentials()["password"], PASSWORD)

    def test_service_reports_save_failure_without_plaintext_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            service = ExpenseService(Path(temp))
            with patch("keyring.set_password", side_effect=RuntimeError("locked")):
                warnings = service.save_settings(
                    oracle_url=PORTAL, oracle_username="employee", oracle_password=PASSWORD,
                )
            self.assertEqual(len(warnings), 1)
            self.assertIn("not saved", warnings[0])
            self.assertNotIn(PASSWORD, service._settings_path().read_text())

    def test_reset_removes_both_secrets(self):
        self.save_login()
        credentials.set_keychain_openai_key("test-api")
        with tempfile.TemporaryDirectory() as temp:
            ExpenseService(Path(temp)).reset_all_data()
        self.assertEqual(credentials.get_oracle_credentials(), {})
        self.assertEqual(credentials.get_keychain_openai_key(), "")


class SavedLoginBrowserTests(FakeKeychainTests):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        super().setUp()
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.statuses = []
        self.save_login()

    def login_page(self, result="Create Expense Report", url=PORTAL, action=""):
        html = """
        <form action="ACTION"><input name="usernameField">
        <input type="password" name="passwordField">
        <button type="submit">Sign in</button></form>
        <script>
        window.submissions = 0;
        document.querySelector('form').onsubmit = event => {
            event.preventDefault();
            window.submissions++;
            window.loginValues = Array.from(document.querySelectorAll('input')).map(el => el.value);
            document.body.innerHTML = '<p>RESULT</p>';
        };
        </script>
        """.replace("ACTION", action).replace("RESULT", result)
        self.page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
        self.page.goto(url)
        return SavedOracleLogin(PORTAL, self.statuses.append)

    def test_successful_autofill_submits_once_and_never_logs_password(self):
        login = self.login_page()
        login.poll(self.page)
        login.poll(self.page)
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.assertEqual(self.page.evaluate("window.loginValues"), ["employee", PASSWORD])
        self.assertNotIn(PASSWORD, " ".join(self.statuses))

    def test_rejected_expired_and_locked_login_disable_future_attempts(self):
        for message in (
            "Login failed. Please verify your login information.",
            "Invalid username or password.", "Your password has expired.",
            "Your account is locked.", "You must change your password.",
        ):
            with self.subTest(message=message):
                credentials.save_oracle_credentials(PORTAL, "employee", PASSWORD)
                login = self.login_page(message)
                login.poll(self.page)
                login.poll(self.page)
                self.assertEqual(credentials.get_oracle_credentials()["rejected"], "true")
                credentials._oracle_blob = None
                blocked = SavedOracleLogin(PORTAL, self.statuses.append)
                self.assertTrue(blocked.finished)
                self.assertIn(REJECTED_MESSAGE, self.statuses)
                self.assertEqual(self.page.evaluate("window.submissions"), 1)
                self.page.unroute("**/*")

    def test_mfa_is_manual_without_invalidating_credentials(self):
        login = self.login_page("Enter your verification code")
        login.poll(self.page)
        with patch("browser.oracle_login.time.monotonic", return_value=login.submitted_at + 31):
            login.poll(self.page)
            login.poll(self.page)
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.assertEqual(credentials.get_oracle_credentials()["rejected"], "")
        self.assertEqual(sum("has not reached" in status for status in self.statuses), 1)
        self.page.set_content("<p>Your password has expired.</p>")
        login.poll(self.page)
        self.assertEqual(credentials.get_oracle_credentials()["rejected"], "true")

    def test_redirect_http_or_cross_origin_action_never_receive_password(self):
        for url, action in (
            ("https://sso.example.test/login", ""),
            ("http://oracle.example.test/login", ""),
            (PORTAL, "https://sso.example.test/login"),
        ):
            with self.subTest(url=url, action=action):
                login = self.login_page(url=url, action=action)
                login.poll(self.page)
                self.assertEqual(self.page.locator('input[type="password"]').input_value(), "")
                self.assertEqual(self.page.evaluate("window.submissions"), 0)
                self.page.unroute("**/*")

    def test_missing_credentials_and_changed_portal_do_not_autofill(self):
        credentials.delete_oracle_credentials()
        login = self.login_page()
        login.poll(self.page)
        self.assertEqual(self.page.evaluate("window.submissions"), 0)
        self.save_login()
        other = SavedOracleLogin("https://other.example.test", self.statuses.append)
        self.assertTrue(other.finished)

    def test_manual_login_still_completes_scraper_wait(self):
        login = self.login_page()
        credentials.delete_oracle_credentials()
        self.page.evaluate("setTimeout(() => {document.body.innerHTML = 'Create Expense Report'}, 100)")
        scraper = TransactionScraper(on_status=self.statuses.append)
        scraper.browser_page = self.page
        with patch.object(scraper, "_wait_for_oracle_page_stable"):
            scraper.wait_for_oracle_login(PORTAL, timeout_s=60)
        self.assertTrue(any("session detected" in status for status in self.statuses))

    def test_scraper_wait_uses_saved_login_and_detects_success(self):
        self.login_page()
        scraper = TransactionScraper(on_status=self.statuses.append)
        scraper.browser_page = self.page
        with patch.object(scraper, "_wait_for_oracle_page_stable"):
            scraper.wait_for_oracle_login(PORTAL, timeout_s=60)
        self.assertEqual(self.page.evaluate("window.submissions"), 1)

    def test_submitter_signs_in_before_report_navigation(self):
        self.login_page()
        submitter = ReportSubmitter(on_status=self.statuses.append)
        submitter.browser_page = self.page
        with (
            patch.object(submitter, "open_browser"),
            patch.object(submitter, "_wait_for_oracle_page_stable"),
            patch.object(submitter, "_oracle_expand_nic_iexpenses_menu", side_effect=RuntimeError("stop after login")),
            patch.object(submitter, "_detach_playwright"),
            self.assertRaisesRegex(RuntimeError, "stop after login"),
        ):
            submitter.submit(PORTAL, SubmissionPayload(report_name="Travel", approver="Manager"))
        self.assertEqual(self.page.evaluate("window.submissions"), 1)
        self.assertTrue(submitter._oracle_shell_ready())

    def test_desktop_uses_the_same_saved_login_without_a_tk_window(self):
        from receipt_automation_ui import ReceiptAutomationUI

        self.login_page()
        callbacks = []
        root = Mock()
        root.after.side_effect = lambda delay, callback: callbacks.append(callback)
        desktop = SimpleNamespace(
            root=root, browser_page=self.page, set_status=self.statuses.append,
            _expense_portal_shell_visible=lambda: "Create Expense Report" in self.page.inner_text("body"),
        )
        ReceiptAutomationUI._start_saved_oracle_login(desktop, PORTAL)
        while callbacks:
            callbacks.pop(0)()
        self.assertEqual(self.page.evaluate("window.submissions"), 1)

    def test_desktop_already_authenticated_wizard_is_not_treated_as_login(self):
        from receipt_automation_ui import ReceiptAutomationUI

        desktop = SimpleNamespace(
            browser_page=self.page, settings=SimpleNamespace(nav_menu_label=""),
            _body_contains_text=lambda marker: False,
            _wizard_any_frame_on_step=lambda step: step == 3,
        )
        self.assertTrue(ReceiptAutomationUI._expense_portal_shell_visible(desktop))

    def test_only_same_origin_iframe_receives_credentials(self):
        for origin, expected_submissions in (
            ("https://oracle.example.test", 1), ("https://sso.example.test", 0),
        ):
            with self.subTest(origin=origin):
                login = self.login_page()
                self.page.set_content(f'<iframe src="{origin}/frame"></iframe>')
                self.page.frame_locator("iframe").locator('input[type="password"]').wait_for()
                login.poll(self.page)
                frame = self.page.frames[1]
                self.assertEqual(frame.evaluate("window.submissions"), expected_submissions)
                if expected_submissions == 0:
                    self.assertEqual(frame.locator('input[type="password"]').input_value(), "")
                self.page.unroute("**/*")


if __name__ == "__main__":
    unittest.main()
