import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nicegui import Client, core, ui
from nicegui.events import GenericEventArguments
from PIL import Image
from starlette.requests import Request

from expense_lines_cache import (
    load_approved_matches,
    load_receipt_line_matches,
    save_expense_lines_cache,
    save_receipt_line_matches,
)
from web.service import ExpenseService, ReceiptDoc

with (
    patch("keychain_credentials.warm_up"),
    patch("web.env_paths.user_data_dir", return_value=Path("/nonexistent/matching-ui-test")),
):
    from web import app as matching


class MatchingReviewTests(unittest.IsolatedAsyncioTestCase):
    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name)
        self.receipt_path = self.data_dir / "receipt.png"
        Image.new("RGB", (20, 40), "white").save(self.receipt_path)
        self.service = ExpenseService(self.data_dir)
        save_expense_lines_cache(self.data_dir, [
            {"line_id": "one", "merchant_name": "First merchant", "amount": "18.47"},
            {"line_id": "two", "merchant_name": "Second merchant", "amount": "20.00"},
        ])
        save_receipt_line_matches(self.data_dir, {
            "one": {"best_receipt": str(self.receipt_path), "confidence": 0.81},
            "two": {"best_receipt": str(self.receipt_path), "confidence": 0.95},
        })
        self.receipt = ReceiptDoc(source_file=str(self.receipt_path), vendor="Test receipt")
        for patcher in (
            patch.object(matching, "svc", self.service),
            patch.object(matching, "page_frame"),
            patch.object(self.service, "_auto_classify_unclassified", return_value=False),
            patch.object(self.service, "get_receipts", return_value=[self.receipt]),
            patch.object(core, "loop", asyncio.get_running_loop()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.timers = self.start_patch(patch.object(ui, "timer"))
        self.navigate = self.start_patch(patch.object(ui.navigate, "to"))
        self.notify = self.start_patch(patch.object(ui, "notify"))
        self.build_page()
        await asyncio.sleep(0)

    def build_page(self, query=b"line=one&report=__uncategorized__&filter=review"):
        self.client = Client(ui.page("/matching-test"), request=None)
        self.addCleanup(self.client.delete)
        with self.client:
            matching.page_matching(Request({
                "type": "http",
                "query_string": query,
            }))

    def button(self, text):
        return next(e for e in self.client.elements.values()
                    if isinstance(e, ui.button) and e.text == text)

    def click(self, button):
        listener = next(v for v in button._event_listeners.values() if v.type == "click")
        with self.client:
            button._handle_event({"listener_id": listener.id, "args": []})

    def key(self, key):
        keyboard = next(e for e in self.client.elements.values() if isinstance(e, ui.keyboard))
        with self.client:
            keyboard._handle_key(GenericEventArguments(
                sender=keyboard,
                client=self.client,
                args={
                    "action": "keydown", "repeat": False, "key": key,
                    "code": f"Key{key.upper()}", "location": 0,
                    "altKey": False, "ctrlKey": False, "metaKey": False, "shiftKey": False,
                },
            ))

    def preview(self):
        return next(e for e in self.client.elements.values()
                    if isinstance(e, ui.html) and '<img class="pz-img"' in e.content)

    def poll(self):
        callback = next(call.args[1] for call in self.timers.call_args_list
                        if call.args[1].__name__ == "_live_match_update")
        with self.client:
            callback()

    async def test_accept_preserves_preview_selection_and_polling(self):
        preview = self.preview()
        parent = preview.parent_slot.parent
        accept = self.button("Accept")
        reject = self.button("Reject")
        self.assertTrue(accept._props["outline"])
        self.assertTrue(reject._props["outline"])
        self.assertEqual(accept._props["aria-pressed"], "false")

        self.click(accept)
        await asyncio.sleep(0)
        self.assertIs(self.button("Accepted"), accept)
        self.assertFalse(accept.enabled)
        self.assertNotIn("outline", accept._props)
        self.assertTrue(accept._props["unelevated"])
        self.assertEqual(accept._props["aria-pressed"], "true")
        self.assertTrue(load_approved_matches(self.data_dir)["one"]["approved"])
        self.assertEqual(self.button("Approved (1)").text, "Approved (1)")
        self.assertTrue(any(isinstance(e, ui.label) and e.text == "1/2 approved"
                            for e in self.client.elements.values()))
        self.assertIs(self.preview(), preview)
        self.assertIs(preview.parent_slot.parent, parent)
        self.poll()
        self.assertIs(self.preview(), preview)
        self.navigate.assert_not_called()

    async def test_saved_acceptance_is_shown_on_reopen(self):
        self.click(self.button("Accept"))
        self.build_page(b"line=one&report=__uncategorized__&filter=approved")
        await asyncio.sleep(0)
        accepted = self.button("Accepted")
        self.assertFalse(accepted.enabled)
        self.assertTrue(accepted._props["unelevated"])
        self.assertNotIn("outline", accepted._props)
        self.assertEqual(accepted._props["aria-pressed"], "true")
        self.assertEqual(self.button("Approved (1)").text, "Approved (1)")
        self.navigate.assert_not_called()

    async def test_unapprove_updates_same_button_without_rebuilding_preview(self):
        preview = self.preview()
        accept = self.button("Accept")
        self.click(accept)
        self.click(self.button("Unapprove"))
        await asyncio.sleep(0)
        self.assertIs(self.button("Accept"), accept)
        self.assertTrue(accept.enabled)
        self.assertTrue(accept._props["outline"])
        self.assertNotIn("unelevated", accept._props)
        self.assertEqual(load_approved_matches(self.data_dir), {})
        self.assertIs(self.preview(), preview)
        self.poll()
        self.assertIs(self.preview(), preview)
        self.navigate.assert_not_called()

    async def test_keyboard_accept_and_reject_keep_transaction_selected(self):
        preview = self.preview()
        self.key("a")
        await asyncio.sleep(0)
        self.assertIs(self.preview(), preview)
        self.assertFalse(self.button("Accepted").enabled)
        self.key("r")
        await asyncio.sleep(0)
        self.assertFalse(self.button("Rejected").enabled)
        self.assertEqual(load_approved_matches(self.data_dir), {})
        self.assertIsNone(load_receipt_line_matches(self.data_dir)["one"]["best_receipt"])
        drawers = [e for e in self.client.elements.values() if isinstance(e, ui.right_drawer)]
        self.assertTrue(drawers[0].value)
        self.assertTrue(any(isinstance(e, ui.label) and e.text == "First merchant"
                            and "text-lg" in e.classes for e in self.client.elements.values()))
        self.navigate.assert_not_called()

    async def test_bulk_approval_and_unapproval_preserve_preview(self):
        preview = self.preview()
        self.click(self.button("Approve"))
        await asyncio.sleep(0)
        self.assertFalse(self.button("Accepted").enabled)
        self.assertIs(self.preview(), preview)
        self.click(self.button("Unapprove"))
        await asyncio.sleep(0)
        self.assertTrue(self.button("Accept").enabled)
        self.assertIs(self.preview(), preview)
        self.navigate.assert_not_called()

    async def test_missing_match_does_not_show_success(self):
        save_receipt_line_matches(self.data_dir, {})
        self.click(self.button("Accept"))
        await asyncio.sleep(0)
        self.assertFalse(any(isinstance(e, ui.button) and e.text in ("Accept", "Accepted")
                             for e in self.client.elements.values()))
        self.assertEqual(load_approved_matches(self.data_dir), {})
        self.assertEqual(self.notify.call_args.kwargs["type"], "negative")

    async def test_accept_all_high_updates_counts_without_navigation(self):
        preview = self.preview()
        button = self.button("Accept All High (1)")
        self.click(button)
        await asyncio.sleep(0)
        self.assertFalse(button.visible)
        self.assertIn("two", load_approved_matches(self.data_dir))
        self.assertEqual(self.button("Approved (1)").text, "Approved (1)")
        self.assertIs(self.preview(), preview)
        self.navigate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
