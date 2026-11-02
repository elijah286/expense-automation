"""Browser smoke test with isolated storage; never connects to Oracle or a keychain."""

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
import web.env_paths
web.env_paths.user_data_dir = lambda: Path(sys.argv[1])
import portal_expense_types
portal_expense_types.get_expense_type_options = lambda: ["Parking", "Meals"]
import web.app as module
from web.service import ExpenseService, ReportReadiness
from web.manual_expenses import ManualExpenseStore
module.svc = ExpenseService(Path(sys.argv[1]))
module.manual_expenses = ManualExpenseStore(Path(sys.argv[1]))
module.svc.create_expense_report_group("Travel")
module.svc.credentials_ready = lambda: True
module.svc.get_report_readiness = lambda rid: ReportReadiness(
    ready=True, submittable=True, total_lines=1, submittable_lines=1,
    matched=1, approved=1, classified=1, with_receipt=1)
module.svc.prepare_report_for_submission = lambda rid: {"approved": 1, "total": 1}
module._schedule_keychain_consent_if_needed = lambda: None
module._launch_splash = lambda: None
module._open_oracle_manual_login_dialog = lambda callback: module.ui.notify("Credit-card path reached")
module.ui.run(host="127.0.0.1", port=int(sys.argv[2]), show=False, reload=False)
"""


class ManualExpenseBrowserTests(unittest.TestCase):
    def test_manual_ui_and_credit_card_guard(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp, socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            with open(Path(temp) / "server.log", "w+") as log:
                server = subprocess.Popen(
                    [sys.executable, "-c", _SERVER, temp, str(port)], cwd=root,
                    env={**os.environ, "PYTHONPATH": str(root)}, stdout=log, stderr=log,
                )
                try:
                    base = f"http://127.0.0.1:{port}"
                    for _ in range(100):
                        if server.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        try:
                            with urllib.request.urlopen(base + "/cash", timeout=1) as response:
                                self.assertEqual(response.status, 200)
                            break
                        except (urllib.error.URLError, TimeoutError):
                            time.sleep(0.1)
                    else:
                        log.flush()
                        log.seek(0)
                        self.fail(f"Isolated UI server did not become ready:\n{log.read()}")
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        page = browser.new_page(viewport={"width": 1280, "height": 900})
                        # The UI's existing external font resources aren't needed for this test.
                        page.route("**/*", lambda route: route.continue_()
                                   if route.request.url.startswith(base) else route.abort())
                        errors = []
                        page.on("pageerror", lambda exc: errors.append(str(exc)))
                        page.goto(base + "/submit")
                        page.get_by_role("button", name="Submit Report", exact=True).click()
                        expect(page.get_by_text("Credit-card path reached", exact=True)).to_be_visible()
                        expect(page.get_by_text("Manual drafts will not be submitted", exact=True)).to_have_count(0)

                        page.goto(base + "/cash")
                        expect(page.locator('a[href="/mileage"]')).to_be_visible()
                        page.get_by_role("button", name="Add cash transaction").click()
                        dialog = page.get_by_role("dialog")
                        dialog.get_by_role("button", name="Save draft").click()
                        expect(page.get_by_text("Enter a description / business purpose.", exact=True)).to_be_visible()
                        dialog.get_by_label("Description / business purpose").fill("Client parking")
                        dialog.get_by_label("Merchant / payee").fill("City parking")
                        dialog.get_by_label("Amount", exact=True).fill("12.50")
                        dialog.get_by_label("Expense type").click()
                        page.get_by_role("option", name="Parking", exact=True).click()
                        dialog.get_by_label("Expense report").click()
                        page.get_by_role("option", name="Travel", exact=True).click()
                        dialog.get_by_role("button", name="Save draft").click()
                        expect(dialog).not_to_be_visible()
                        expect(page.get_by_text("Client parking", exact=True)).to_be_visible()
                        page.reload()
                        expect(page.get_by_text("Client parking", exact=True)).to_be_visible()
                        page.get_by_role("button", name="Edit", exact=True).click()
                        dialog.get_by_label("Amount", exact=True).fill("15")
                        dialog.get_by_role("button", name="Save draft").click()
                        expect(page.get_by_text("USD 15.00", exact=False)).to_be_visible()

                        page.locator('a[href="/mileage"]').click()
                        page.get_by_role("button", name="Add mileage transaction").click()
                        dialog.get_by_label("Date", exact=True).fill("2026-06-30")
                        dialog.get_by_label("Miles", exact=True).fill("10")
                        dialog.get_by_label("Description / business purpose").fill("Client visit")
                        expect(dialog.get_by_text("USD 0.725 / mile x 10 miles = USD 7.25", exact=True)).to_be_visible()
                        dialog.get_by_label("Date", exact=True).fill("2026-07-01")
                        expect(dialog.get_by_text("USD 0.76 / mile x 10 miles = USD 7.60", exact=True)).to_be_visible()
                        dialog.get_by_role("button", name="Save draft").click()
                        expect(page.get_by_text("10 miles x USD 0.76 / mile (IRS estimate)", exact=True)).to_be_visible()

                        page.goto(base + "/submit")
                        expect(page.get_by_text("1 manual draft(s) - NOT included in Oracle submission", exact=True)).to_be_visible()
                        page.get_by_role("button", name="Submit Report", exact=True).click()
                        expect(dialog.get_by_text("Manual drafts will not be submitted", exact=True)).to_be_visible()
                        dialog.get_by_role("button", name="Cancel", exact=True).click()
                        page.get_by_role("button", name="Submit Report", exact=True).click()
                        dialog.get_by_role("button", name="Submit credit-card items only", exact=True).click()
                        expect(page.get_by_text("Credit-card path reached", exact=True)).to_be_visible()

                        page.goto(base + "/mileage")
                        page.get_by_role("button", name="Delete", exact=True).click()
                        dialog.get_by_role("button", name="Cancel", exact=True).click()
                        expect(dialog).not_to_be_visible()
                        expect(page.get_by_text("Client visit", exact=True)).to_be_visible()
                        page.get_by_role("button", name="Delete", exact=True).click()
                        dialog.get_by_role("button", name="Delete", exact=True).click()
                        expect(page.get_by_text("No mileage transactions yet. This step is optional.", exact=True)).to_be_visible()
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.get_by_role("button", name="Add mileage transaction").click()
                        expect(dialog.get_by_label("Miles", exact=True)).to_be_visible()
                        self.assertLessEqual(dialog.bounding_box()["width"], 390)
                        self.assertEqual(errors, [])
                        browser.close()
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
