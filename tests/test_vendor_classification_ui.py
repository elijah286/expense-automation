"""Browser coverage for AI picks; no real API, Oracle, keychain, or user data."""

import json
import os
from pathlib import Path
import re
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
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import web.env_paths
root = Path(sys.argv[1])
web.env_paths.user_data_dir = lambda: root
import portal_expense_types
portal_expense_types.get_expense_type_options = lambda: ["Parking", "Meals", "Hotel"]
import web.app as module
import browser_automation
from web.service import ExpenseService
module.svc = ExpenseService(root)
module.svc.credentials_ready = lambda: True
module.svc._get_openai_key = lambda: "synthetic-key"
module.svc.set_vendor_classification("airport parking", "Meals")
module.svc.set_vendor_classification("broken vendor", "Meals")
module.svc.set_vendor_classification("cafe sample", "Hotel")
module.svc.set_vendor_classification("invalid vendor", "Meals")
module._schedule_keychain_consent_if_needed = lambda: None
module._launch_splash = lambda: None
module._ensure_update_check = lambda: None
def respond(**kwargs):
    time.sleep(0.7)
    prompt = kwargs["input"][0]["content"][0]["text"]
    recovered = (root / "ai_recovered").exists()
    if "Merchant: broken vendor" in prompt and not recovered:
        raise RuntimeError("Synthetic AI failure")
    choice = "Invented" if "Merchant: invalid vendor" in prompt and not recovered else "Parking"
    return SimpleNamespace(output_text=json.dumps({"expense_type": choice}))
client = MagicMock()
client.__enter__.return_value = client
client.responses.create.side_effect = respond
browser_automation.build_openai_client = lambda *args, **kwargs: client
module.ui.run(host="127.0.0.1", port=int(sys.argv[2]), show=False, reload=False)
"""


class VendorClassificationBrowserTests(unittest.TestCase):
    def test_pick_loading_persistence_manual_override_and_errors(self):
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
                            with urllib.request.urlopen(base + "/classification", timeout=1) as response:
                                self.assertEqual(response.status, 200)
                            break
                        except (urllib.error.URLError, TimeoutError):
                            time.sleep(0.1)
                    else:
                        log.seek(0)
                        self.fail(f"Isolated server did not start:\n{log.read()}")
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        page = browser.new_page(viewport={"width": 1280, "height": 900})
                        page.route("**/*", lambda route: route.continue_()
                                   if route.request.url.startswith(base) else route.abort())
                        errors = []
                        page.on("pageerror", lambda exc: errors.append(str(exc)))
                        page.goto(base + "/classification")
                        row = page.locator(".classify-row").filter(has_text="airport parking")
                        expect(row).to_contain_text("Meals")
                        row.get_by_role("button", name="AI pick", exact=True).click()
                        expect(row.get_by_role("button", name="AI pick", exact=True)).to_be_disabled()
                        expect(page.get_by_role("button", name="AI pick all", exact=True)).to_be_disabled()
                        expect(row.locator(".q-select")).to_have_class(re.compile(".*q-field--disabled.*"))
                        expect(row).to_contain_text("Parking")
                        expect(row.get_by_role("button", name="AI pick", exact=True)).to_be_enabled()
                        expect(page.get_by_text("AI picked Parking for airport parking", exact=True)).to_be_visible()
                        saved = json.loads((Path(temp) / "vendor_expense_types.json").read_text())
                        self.assertEqual(saved["vendors"]["airport parking"], "Parking")
                        self.assertEqual(saved["vendors"]["cafe sample"], "Hotel")
                        page.reload()
                        expect(row).to_contain_text("Parking")

                        row.locator(".q-select").click()
                        page.get_by_role("option", name="Hotel", exact=True).click()
                        expect(row).to_contain_text("Hotel")
                        page.reload()
                        expect(row).to_contain_text("Hotel")
                        search = page.get_by_placeholder("Search merchants or expense types...")
                        search.fill("broken vendor")
                        expect(page.locator(".classify-row")).to_have_count(1)
                        broken = page.locator(".classify-row").filter(has_text="broken vendor")
                        broken.get_by_role("button", name="AI pick", exact=True).click()
                        expect(broken.get_by_role("button", name="AI pick", exact=True)).to_be_disabled()
                        expect(page.locator(".q-notification__message").filter(
                            has_text="Synthetic AI failure",
                        )).to_be_visible()
                        expect(broken).to_contain_text("Meals")
                        expect(broken.get_by_role("button", name="AI pick", exact=True)).to_be_enabled()
                        search.fill("invalid vendor")
                        invalid = page.locator(".classify-row").filter(has_text="invalid vendor")
                        invalid.get_by_role("button", name="AI pick", exact=True).click()
                        expect(page.locator(".q-notification__message").filter(
                            has_text="AI did not return an available expense type.",
                        )).to_be_visible()
                        expect(invalid).to_contain_text("Meals")
                        expect(invalid.get_by_role("button", name="AI pick", exact=True)).to_be_enabled()
                        page.reload()
                        saved = json.loads((Path(temp) / "vendor_expense_types.json").read_text())
                        self.assertEqual(saved["vendors"]["airport parking"], "Hotel")
                        self.assertEqual(saved["vendors"]["broken vendor"], "Meals")
                        self.assertEqual(saved["vendors"]["invalid vendor"], "Meals")

                        search.fill("cafe sample")
                        expect(page.locator(".classify-row")).to_have_count(1)
                        cafe = page.locator(".classify-row").filter(has_text="cafe sample")
                        bulk = page.get_by_role("button", name="AI pick all", exact=True)
                        bulk.click()
                        expect(bulk).to_be_disabled()
                        expect(cafe.get_by_role("button", name="AI pick", exact=True)).to_be_disabled()
                        expect(cafe.locator(".q-select")).to_have_class(re.compile(".*q-field--disabled.*"))
                        expect(page.locator(".page-container").get_by_text(
                            "AI pick all: 4/4 processed; 2 updated, 2 failed.", exact=True,
                        )).to_be_visible(timeout=15000)
                        expect(bulk).to_be_enabled()
                        expect(cafe).to_contain_text("Parking")
                        expect(cafe.get_by_role("button", name="AI pick", exact=True)).to_be_enabled()
                        saved = json.loads((Path(temp) / "vendor_expense_types.json").read_text())
                        self.assertEqual(saved["vendors"], {
                            "airport parking": "Parking", "broken vendor": "Meals",
                            "cafe sample": "Parking", "invalid vendor": "Meals",
                        })
                        page.reload()
                        expect(row).to_contain_text("Parking")
                        expect(cafe).to_contain_text("Parking")
                        (Path(temp) / "ai_recovered").touch()
                        bulk.click()
                        expect(page.locator(".page-container").get_by_text(
                            "AI pick all: 4/4 processed; 4 updated, 0 failed.", exact=True,
                        )).to_be_visible(timeout=15000)
                        expect(bulk).to_be_enabled()
                        page.reload()
                        saved = json.loads((Path(temp) / "vendor_expense_types.json").read_text())
                        self.assertEqual(set(saved["vendors"].values()), {"Parking"})
                        self.assertFalse(errors)
                        browser.close()
                except Exception:
                    log.seek(0)
                    print(log.read()[-12000:])
                    raise
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
