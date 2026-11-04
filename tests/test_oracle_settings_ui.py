"""Real settings-page smoke test with a fake keychain and isolated app data."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from playwright.sync_api import expect, sync_playwright


_SERVER = """
import sys
from pathlib import Path
import keyring
vault = {}
keyring.get_password = lambda service, account: vault.get(account)
keyring.set_password = lambda service, account, value: vault.update({account: value})
keyring.delete_password = lambda service, account: vault.pop(account, None)
import web.env_paths
web.env_paths.user_data_dir = lambda: Path(sys.argv[1])
import web.app as module
import keychain_credentials as credentials
from web.service import ExpenseService
module.svc = ExpenseService(Path(sys.argv[1]))
module.svc.save_settings(oracle_url="https://oracle.example.test/login", approver="Manager")
module._schedule_keychain_consent_if_needed = lambda: None
module._launch_splash = lambda: None
@module.app.post("/test/reject")
def reject():
    credentials.mark_oracle_credentials_rejected(credentials.get_oracle_credentials())
    module.activity_log.emit("warning", "[CREDENTIALS_REJECTED] Synthetic rejected sign-in")
    return {"ok": True}
@module.app.get("/test/state")
def state():
    saved = credentials.get_oracle_credentials()
    return {"username": saved.get("username"), "rotated": saved.get("password") == "rotated-test",
            "saved": bool(saved.get("password")), "rejected": saved.get("rejected") == "true"}
module.ui.run(host="127.0.0.1", port=int(sys.argv[2]), show=False, reload=False)
"""


class OracleSettingsBrowserTests(unittest.TestCase):
    def test_secure_storage_save_replace_reject_recover_and_forget(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp, socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            with open(Path(temp) / "server.log", "w+") as log:
                server = subprocess.Popen(
                    [sys.executable, "-c", _SERVER, temp, str(port)], cwd=root,
                    env={**os.environ, "PYTHONPATH": str(root), "OPENAI_API_KEY": "synthetic-api-key"},
                    stdout=log, stderr=log,
                )
                try:
                    base = f"http://127.0.0.1:{port}"
                    for _ in range(100):
                        if server.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        try:
                            with urllib.request.urlopen(base + "/settings", timeout=1) as response:
                                self.assertEqual(response.status, 200)
                            break
                        except (urllib.error.URLError, TimeoutError):
                            time.sleep(0.1)
                    else:
                        log.seek(0)
                        self.fail(f"Isolated settings server did not become ready:\n{log.read()}")
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        page = browser.new_page(viewport={"width": 1280, "height": 1000})
                        page.route("**/*", lambda route: route.continue_()
                                   if route.request.url.startswith(base) else route.abort())
                        errors = []
                        page.on("pageerror", lambda exc: errors.append(str(exc)))
                        page.goto(base + "/settings")
                        page.get_by_role("button", name="Enable secure storage").click()
                        page.get_by_role("dialog").get_by_role("button", name="Continue", exact=True).click()
                        expect(page.get_by_role("button", name="Enable secure storage")).to_have_count(0)
                        page.get_by_label("Oracle username (optional)", exact=True).fill("employee")
                        password = page.get_by_label("Oracle password", exact=True)
                        self.assertEqual(password.get_attribute("type"), "password")
                        password.fill("first-test")
                        page.get_by_role("button", name="Save Settings", exact=True).click()
                        expect(page.get_by_text("Oracle sign-in saved in the system keychain.", exact=True)).to_be_visible()
                        replacement = page.get_by_label("Replace Oracle password", exact=True)
                        expect(replacement).to_have_value("")
                        expect(page.get_by_label("Oracle username (optional)", exact=True)).to_have_value("employee")
                        page.get_by_role("button", name="Save Settings", exact=True).click()
                        expect(replacement).to_have_value("")
                        page.wait_for_timeout(800)
                        page.request.post(base + "/test/reject")
                        expect(page.get_by_text(
                            "Oracle rejected your saved sign-in. Update your password in Settings, "
                            "or sign in manually in the browser to continue.", exact=True,
                        )).to_be_visible()
                        page.reload()
                        expect(page.get_by_text(
                            "Oracle rejected the saved sign-in. Enter your current password below "
                            "to re-enable automatic login.", exact=True,
                        )).to_be_visible()
                        page.get_by_role("button", name="Save Settings", exact=True).click()
                        expect(page.get_by_text(
                            "Oracle rejected the saved password. Enter your current password to re-enable sign-in.",
                            exact=True,
                        )).to_be_visible()
                        page.wait_for_timeout(800)
                        replacement.fill("rotated-test")
                        page.get_by_role("button", name="Save Settings", exact=True).click()
                        expect(page.get_by_text("Oracle sign-in saved in the system keychain.", exact=True)).to_be_visible()
                        state = page.request.get(base + "/test/state").json()
                        self.assertTrue(state["rotated"])
                        self.assertFalse(state["rejected"])
                        page.get_by_role("button", name="Forget saved sign-in", exact=True).click()
                        expect(page.get_by_label("Oracle password", exact=True)).to_have_value("")
                        expect(page.get_by_label("Oracle username (optional)", exact=True)).to_have_value("")
                        self.assertFalse(page.request.get(base + "/test/state").json()["saved"])
                        settings = (Path(temp) / "settings.json").read_text()
                        for secret in ("first-test", "rotated-test", "employee"):
                            self.assertNotIn(secret, settings)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
                log.seek(0)
                output = log.read()
                self.assertNotIn("Traceback", output, output)
                self.assertNotIn("rotated-test", output)


if __name__ == "__main__":
    unittest.main()
