"""Exercise submission marking, history search and archived receipts in Chromium."""

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import fitz
from playwright.sync_api import expect, sync_playwright


_SERVER = """
import sys
from pathlib import Path
from PIL import Image
import fitz
import web.env_paths
root = Path(sys.argv[1])
web.env_paths.user_data_dir = lambda: root
import web.app as module
from web.service import ExpenseService
from expense_lines_cache import (
    save_expense_lines_cache, save_receipt_line_matches,
    save_approved_matches, save_analyses_snapshot,
)
from persistence.atomic_json import atomic_write_json
module.svc = ExpenseService(root)
module.svc.credentials_ready = lambda: True
module.svc._auto_classify_unclassified = lambda lines: False
module._schedule_keychain_consent_if_needed = lambda: None
module._launch_splash = lambda: None
module._ensure_update_check = lambda: None
image = root / 'hotel.png'
Image.new('RGB', (100, 160), 'white').save(image)
pdf = root / 'meal.pdf'
with fitz.open() as document:
    for text in ('Receipt page one', 'Receipt page two'):
        document.new_page().insert_text((72, 72), text)
    document.save(pdf)
report = module.svc.create_expense_report_group('Conference')
save_expense_lines_cache(root, [
    {'line_id': 'hotel', 'merchant_name': 'Hotel One', 'transaction_date': '2026-10-12',
     'amount': '89.40', 'currency': 'USD', 'justification': 'Customer meeting'},
    {'line_id': 'meal', 'merchant_name': 'Cafe Two', 'transaction_date': '2026-10-13',
     'amount': '12.50', 'currency': 'USD'},
])
module.svc.assign_transactions_to_report(['hotel', 'meal'], report.id)
save_receipt_line_matches(root, {
    'hotel': {'best_receipt': str(image), 'confidence': .99},
    'meal': {'best_receipt': str(pdf), 'confidence': .99},
})
save_approved_matches(root, {
    'hotel': {'source_file': str(image)}, 'meal': {'source_file': str(pdf)},
})
save_analyses_snapshot(root, [
    {'source_file': str(image), 'vendor': 'Hotel One', 'notes': 'Breakfast included',
     'receipt_date': '2026-10-12', 'total_amount': '89.40', 'currency': 'USD'},
    {'source_file': str(pdf), 'vendor': 'Cafe Two', 'notes': 'Client lunch',
     'receipt_date': '2026-10-13', 'total_amount': '12.50', 'currency': 'USD'},
])
atomic_write_json(root / 'vendor_expense_types.json', {'hotel one': 'Hotel', 'cafe two': 'Meals'})
module.ui.run(host='127.0.0.1', port=int(sys.argv[2]), show=False, reload=False)
"""


class HistoryBrowserTests(unittest.TestCase):
    def test_mark_search_open_receipts_and_reload_without_originals(self):
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
                            with urllib.request.urlopen(base + "/history", timeout=1) as response:
                                self.assertEqual(response.status, 200)
                            break
                        except (urllib.error.URLError, TimeoutError):
                            time.sleep(0.1)
                    else:
                        log.seek(0)
                        self.fail(f"History server failed to start:\n{log.read()}")
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch(headless=True)
                        page = browser.new_page(viewport={"width": 1440, "height": 1000})
                        page.route("**/*", lambda route: route.continue_()
                                   if route.request.url.startswith(base) else route.abort())
                        errors = []
                        page.on("pageerror", lambda exc: errors.append(str(exc)))
                        page.goto(base + "/history")
                        expect(page.get_by_text(
                            "No submissions yet. Mark a report as submitted to save it here.", exact=True,
                        )).to_be_visible()
                        page.goto(base + "/submit")
                        page.get_by_role("button", name="Mark Report as Submitted", exact=True).click()
                        dialog = page.get_by_role("dialog")
                        expect(dialog.get_by_text(
                            "A permanent read-only copy", exact=False,
                        )).to_be_visible()
                        dialog.get_by_role("button", name="Mark as Submitted", exact=True).click()
                        expect(dialog).not_to_be_visible()
                        page.locator('a[href="/history"]').click()
                        expect(page.get_by_role("button", name="Open report", exact=True)).to_be_visible()
                        page.get_by_label("Search submissions", exact=True).fill("not present")
                        expect(page.get_by_role("button", name="Open report", exact=True)).to_have_count(0)
                        page.get_by_label("Search submissions", exact=True).fill("Breakfast")
                        page.get_by_role("button", name="Open report", exact=True).click()
                        expect(page.get_by_text("Conference", exact=True)).to_be_visible()
                        page.get_by_label("Search expenses", exact=True).fill("Meals")
                        expect(page.get_by_role("button", name="View expense", exact=True)).to_have_count(1)
                        expect(page.get_by_role("cell", name="Cafe Two", exact=True).first).to_be_visible()
                        page.get_by_label("Search expenses", exact=True).fill("Customer meeting")
                        page.get_by_role("button", name="View expense", exact=True).click()
                        dialog = page.get_by_role("dialog")
                        expect(dialog.get_by_text("Customer meeting", exact=True)).to_be_visible()
                        expect(dialog.get_by_role("button", name="Edit", exact=True)).to_have_count(0)
                        dialog.get_by_role("button", name="Open receipt: hotel.png", exact=True).click()
                        image = page.locator('img[src*="/api/image"]').last
                        expect(image).to_be_visible()
                        image_url = image.get_attribute("src")
                        self.assertEqual(page.request.get(base + image_url).status, 200)
                        page.keyboard.press("Escape")
                        expect(image).not_to_be_visible()
                        page.get_by_role("dialog").get_by_role("button", name="Close", exact=True).click()
                        expect(page.get_by_role("dialog")).not_to_be_visible()
                        page.get_by_label("Search expenses", exact=True).fill("")
                        page.get_by_label("Search receipts", exact=True).fill("Client lunch")
                        expect(page.get_by_role("button", name="Open receipt", exact=True)).to_have_count(1)
                        page.get_by_role("button", name="Open receipt", exact=True).click()
                        iframe = page.locator('iframe[title="Archived receipt PDF"]')
                        expect(iframe).to_be_visible()
                        pdf_url = iframe.get_attribute("src")
                        response = page.request.get(base + pdf_url)
                        self.assertEqual(response.status, 200)
                        with fitz.open(stream=response.body(), filetype="pdf") as document:
                            self.assertEqual(document.page_count, 2)
                        page.get_by_role("button", name="Close", exact=True).click()
                        (Path(temp) / "hotel.png").unlink()
                        (Path(temp) / "meal.pdf").unlink()
                        page.reload()
                        expect(page.get_by_text("Conference", exact=True)).to_be_visible()
                        self.assertEqual(page.request.get(base + image_url).status, 200)
                        self.assertEqual(page.request.get(base + pdf_url).status, 200)
                        page.get_by_label("Search receipts", exact=True).fill("not present")
                        expect(page.get_by_role("button", name="Open receipt", exact=True)).to_have_count(0)
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
