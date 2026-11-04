import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from expense_lines_cache import (
    load_expense_lines_cache,
    load_pending_deletions,
    save_analyses_snapshot,
    save_approved_matches,
    save_expense_lines_cache,
    save_pending_deletions,
    save_receipt_line_matches,
    soft_delete_report,
)
from persistence.atomic_json import atomic_write_json
from persistence.report_history import ReportHistoryStore
from web.manual_expenses import ManualExpenseStore
from web.service import ExpenseService


class ReportHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.svc = ExpenseService(self.root)
        self.receipt = self.root / "receipt.png"
        self.receipt.write_bytes(b"original receipt bytes")
        self.pdf = self.root / "receipt.pdf"
        self.pdf.write_bytes(b"original PDF bytes")
        self.report = self.svc.create_expense_report_group("Conference")
        save_expense_lines_cache(self.root, [{
            "line_id": "one", "merchant_name": "Hotel One", "transaction_date": "2026-10-12",
            "amount": "89.40", "currency": "USD", "justification": "Customer meeting",
        }])
        self.svc.assign_transactions_to_report(["one"], self.report.id)
        save_receipt_line_matches(self.root, {
            "one": {"best_receipt": str(self.receipt), "confidence": 0.99, "reason": "exact"},
        })
        save_approved_matches(self.root, {"one": {"source_file": str(self.receipt)}})
        self.svc.assign_receipts_to_report([str(self.pdf)], self.report.id)
        save_analyses_snapshot(self.root, [{
            "source_file": str(self.receipt), "vendor": "Hotel One", "total_amount": "89.40",
            "receipt_date": "2026-10-12", "notes": "Breakfast included",
        }])
        atomic_write_json(self.root / "vendor_expense_types.json", {
            "hotel one": "Hotel",
        })

    def expire(self):
        pending = load_pending_deletions(self.root)
        for entry in pending.values():
            entry["delete_after"] = "2000-01-01T00:00:00+00:00"
        save_pending_deletions(self.root, pending)

    def test_snapshot_survives_original_edits_purge_and_restart(self):
        result = self.svc.mark_report_submitted(self.report.id)
        snapshot = self.svc.history.get(result["history_id"])
        self.assertEqual(snapshot["report_name"], "Conference")
        self.assertEqual(snapshot["expenses"][0]["description"], "Customer meeting")
        self.assertEqual(snapshot["expenses"][0]["expense_type"], "Hotel")
        self.assertEqual(len(snapshot["receipts"]), 2)
        self.assertEqual(load_expense_lines_cache(self.root)[0], [])
        self.assertEqual(self.svc.get_expense_report_groups(), [])
        self.receipt.write_bytes(b"replaced image")
        self.expire()
        self.assertEqual(len(self.svc.purge_expired()), 1)
        self.assertFalse(self.receipt.exists())
        self.assertTrue(self.pdf.exists())  # Direct assignments are not deleted by legacy cleanup.
        self.pdf.unlink()
        restarted = ExpenseService(self.root)
        self.assertEqual(restarted.history.list(), [snapshot])
        files = {
            r["filename"]: restarted.history.receipt_path(snapshot["id"], r["id"]).read_bytes()
            for r in snapshot["receipts"]
        }
        self.assertEqual(files["receipt.png"], b"original receipt bytes")
        self.assertEqual(files["receipt.pdf"], b"original PDF bytes")

    def test_restore_and_resubmit_preserve_separate_immutable_snapshots(self):
        first = self.svc.mark_report_submitted(self.report.id, keep_documents=True)["history_id"]
        before = self.svc.history.get(first)
        self.assertEqual(self.svc.restore_report(self.report.id)["restored_lines"], 1)
        self.svc.rename_expense_report_group(self.report.id, "Changed name")
        self.receipt.write_bytes(b"updated receipt")
        second = self.svc.mark_report_submitted(self.report.id, keep_documents=True)["history_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.svc.history.list()), 2)
        self.assertEqual(self.svc.history.get(first), before)
        self.assertEqual(self.svc.history.get(second)["report_name"], "Changed name")
        self.assertIn("error", self.svc.mark_report_submitted(self.report.id))
        self.assertEqual(len(self.svc.history.list()), 2)
        self.expire()
        self.svc.purge_expired()
        self.assertTrue(self.receipt.exists())

    def test_missing_receipt_aborts_before_hiding_report(self):
        self.receipt.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "Cannot preserve receipt"):
            self.svc.mark_report_submitted(self.report.id)
        self.assertEqual(len(self.svc.get_expense_report_groups()), 1)
        self.assertEqual(len(load_expense_lines_cache(self.root)[0]), 1)
        self.assertEqual(self.svc.history.list(), [])
        self.assertEqual(list(self.svc.history.root.iterdir()), [])

    def test_copy_failure_leaves_active_data_and_no_partial_history(self):
        with patch("persistence.report_history.shutil.copyfile", side_effect=OSError("Disk full")):
            with self.assertRaisesRegex(OSError, "Disk full"):
                self.svc.mark_report_submitted(self.report.id)
        self.assertEqual(self.svc.history.list(), [])
        self.assertEqual(len(self.svc.get_expense_report_groups()), 1)

    def test_legacy_pending_reports_recovered_before_expired_cleanup(self):
        soft_delete_report(self.root, self.report.id)
        self.expire()
        self.svc.purge_expired()
        snapshot = self.svc.history.list()[0]
        self.assertTrue(snapshot["recovered"])
        self.assertEqual(snapshot["expenses"][0]["amount"], "89.40")
        self.assertEqual(len(snapshot["receipts"]), 2)
        self.assertFalse(self.receipt.exists())
        self.svc.purge_expired()
        self.assertEqual(len(self.svc.history.list()), 1)

    def test_legacy_missing_receipt_is_flagged_and_migration_is_idempotent(self):
        soft_delete_report(self.root, self.report.id)
        self.receipt.unlink()
        self.svc.preserve_pending_history()
        self.svc.preserve_pending_history()
        reports = self.svc.history.list()
        self.assertEqual(len(reports), 1)
        receipt = next(r for r in reports[0]["receipts"] if r["filename"] == "receipt.png")
        self.assertTrue(receipt["missing"])
        with self.assertRaisesRegex(FileNotFoundError, "unavailable"):
            self.svc.history.receipt_path(reports[0]["id"], receipt["id"])

    def test_only_confirmed_manual_entries_and_oracle_amount_are_archived(self):
        store = ManualExpenseStore(self.root)
        entered = store.save(
            kind="mileage", transaction_date="2026-10-12", description="Client visit",
            miles="10", report_id=self.report.id,
        )
        store.save(
            kind="cash", transaction_date="2026-10-12", description="Not entered",
            amount="5", merchant="Parking", expense_type="Parking", report_id=self.report.id,
        )
        self.svc._save_submission_state({
            "report_id": self.report.id, "completed": True,
            "result": {"manual_expenses": {
                "entered": [entered.id], "oracle_amounts": {entered.id: "5.00"}, "warnings": [],
            }},
        })
        result = self.svc.mark_report_submitted(self.report.id)
        report = self.svc.history.get(result["history_id"])
        self.assertEqual(len(report["expenses"]), 2)
        mileage = report["expenses"][1]
        self.assertEqual(mileage["oracle_amount"], "5.00")
        self.assertEqual(mileage["miles"], "10")

    def test_invalid_ids_and_corrupt_history_fail_explicitly_without_overwrite(self):
        for invalid in ("../outside", "", "not-an-id"):
            with self.assertRaisesRegex(ValueError, "not found"):
                self.svc.history.get(invalid)
        result = self.svc.mark_report_submitted(self.report.id)
        path = self.svc.history.root / result["history_id"] / "report.json"
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            ReportHistoryStore(self.root).list()
        self.assertEqual(path.read_text(), "{bad")


if __name__ == "__main__":
    unittest.main()
