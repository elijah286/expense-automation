import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from expense_lines_cache import (
    save_approved_matches, save_expense_lines_cache, save_receipt_line_matches,
)
from web.manual_expenses import ManualExpenseStore, mileage_estimate
from web.service import ExpenseService


class MileageRateTests(unittest.TestCase):
    def test_date_specific_rates_and_rounding(self):
        for day, rate, total in (
            ("2024-01-01", "0.67", "6.70"),
            ("2024-12-31", "0.67", "6.70"),
            ("2025-01-01", "0.70", "7.00"),
            ("2025-12-31", "0.70", "7.00"),
            ("2026-01-01", "0.725", "7.25"),
            ("2026-06-30", "0.725", "7.25"),
            ("2026-07-01", "0.76", "7.60"),
            ("2026-12-31", "0.76", "7.60"),
        ):
            with self.subTest(day=day):
                self.assertEqual(mileage_estimate(day, "10"), (rate, total))
        self.assertEqual(mileage_estimate("2026-01-01", "1"), ("0.725", "0.73"))
        self.assertEqual(mileage_estimate("2025-02-01", "12.345"), ("0.70", "8.64"))

    def test_unsupported_invalid_dates_and_distances(self):
        for day in ("2023-12-31", "2027-01-01", "2025-02-29", "", "2026-1-01"):
            with self.subTest(day=day), self.assertRaises(ValueError):
                mileage_estimate(day, "1")
        for miles in ("", "NaN", "Infinity", "-1", "0", "1.2345", "1e99", "abc", "0.001"):
            with self.subTest(miles=miles), self.assertRaises(ValueError):
                mileage_estimate("2025-01-01", miles)


class ManualExpenseStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ManualExpenseStore(self.root)

    def cash(self, **overrides):
        values = dict(
            kind="cash", transaction_date="2026-07-12", description="Client parking",
            merchant="City parking", expense_type="Parking", amount="12.50",
            currency="USD", report_id="report-a",
        )
        values.update(overrides)
        return self.store.save(**values)

    def test_optional_empty_store_does_not_create_files(self):
        self.assertEqual(self.store.list(), [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cash_crud_and_reload(self):
        entry = self.cash()
        self.assertEqual(ManualExpenseStore(self.root).list(), [entry])
        edited = self.cash(entry_id=entry.id, amount="15", report_id="report-b")
        self.assertEqual(self.store.list(), [edited])
        self.assertEqual(edited.amount, "15.00")
        self.assertEqual(edited.report_id, "report-b")
        self.store.delete(entry.id)
        self.assertEqual(self.store.list(), [])

    def test_mileage_recomputes_instead_of_trusting_amount(self):
        entry = self.store.save(
            kind="mileage", transaction_date="2026-06-30", description="Client visit",
            miles="10", amount="999", currency="EUR",
        )
        self.assertEqual((entry.amount, entry.rate, entry.currency), ("7.25", "0.725", "USD"))
        self.assertTrue(entry.rate_source.startswith("https://www.irs.gov/"))
        edited = self.store.save(
            kind="mileage", transaction_date="2026-07-01", description="Client visit",
            miles="10", entry_id=entry.id,
        )
        self.assertEqual((edited.amount, edited.rate), ("7.60", "0.76"))

    def test_validation_does_not_write(self):
        for override in (
            {"description": " "}, {"amount": "-1"}, {"amount": "NaN"},
            {"amount": "0"}, {"amount": "1.001"}, {"amount": "1e99"},
            {"amount": "Infinity"}, {"amount": "abc"},
            {"merchant": ""}, {"expense_type": ""}, {"currency": "US"},
            {"transaction_date": "2026-02-30"}, {"kind": "other"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.cash(**override)
        self.assertFalse(self.store.path.exists())

    def test_stale_edits_and_deletes_fail(self):
        with self.assertRaises(ValueError):
            self.cash(entry_id="missing")
        with self.assertRaises(ValueError):
            self.store.delete("missing")
        entry = self.cash()
        with self.assertRaises(ValueError):
            self.store.save(
                kind="mileage", transaction_date="2026-01-01", description="Trip",
                miles="1", entry_id=entry.id,
            )
        self.assertEqual(self.store.list(), [entry])

    def test_corrupt_store_is_never_overwritten(self):
        for content in ("not json", "{}", '[{"unexpected": "field"}]'):
            self.store.path.write_text(content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.cash()
            self.assertEqual(self.store.path.read_text(), content)

    def test_concurrent_saves_preserve_entries(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda n: self.cash(description=f"Expense {n}"), range(12)))
        self.assertEqual(len(self.store.list()), 12)
        self.assertEqual(len({entry.id for entry in self.store.list()}), 12)

    def test_credit_card_caches_and_readiness_are_unchanged(self):
        service = ExpenseService(self.root)
        group = service.create_expense_report_group("Travel")
        before = service.get_report_readiness(group.id)
        existing = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.cash(report_id=group.id)
        self.store.save(
            kind="mileage", transaction_date="2026-01-01", description="Trip",
            miles="10", report_id=group.id,
        )
        self.assertEqual(service.get_report_readiness(group.id), before)
        self.assertEqual(service.get_transactions(), [])
        self.assertEqual(service.prepare_report_for_submission(group.id), {"error": "Report has no transactions"})
        for name, contents in existing.items():
            self.assertEqual((self.root / name).read_bytes(), contents)
        self.assertEqual(
            set(json.loads(self.store.path.read_text())[0]),
            {"id", "kind", "date", "description", "amount", "currency", "merchant",
             "expense_type", "report_id", "miles", "rate", "rate_source", "exchange_rate"},
        )
        service.delete_expense_report_group(group.id)
        self.assertEqual(len(self.store.list()), 2)

    def test_foreign_cash_requires_and_persists_actual_exchange_rate(self):
        with self.assertRaisesRegex(ValueError, "Enter an exchange rate"):
            self.cash(currency="EUR")
        for rate in ("0", "-1", "NaN", "1.12345678901"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.cash(currency="EUR", exchange_rate=rate)
        entry = self.cash(currency="EUR", exchange_rate="1.25")
        self.assertEqual(entry.exchange_rate, "1.25")
        self.assertEqual(ManualExpenseStore(self.root).list(), [entry])
        with self.assertRaisesRegex(ValueError, "USD cash expenses"):
            self.cash(exchange_rate="2")

    def test_old_drafts_without_exchange_rate_still_load(self):
        entry = self.cash()
        content = json.loads(self.store.path.read_text())
        content[0].pop("exchange_rate")
        self.store.path.write_text(json.dumps(content))
        self.assertEqual(self.store.list(), [entry])

    def test_ready_credit_card_report_stays_submittable(self):
        service = ExpenseService(self.root)
        group = service.create_expense_report_group("Travel")
        receipt = self.root / "receipt.jpg"
        receipt.write_bytes(b"test receipt")
        save_expense_lines_cache(self.root, [{
            "line_id": "card-1", "merchant_name": "Parking", "amount": "25.00",
            "transaction_date": "2026-07-12", "currency": "USD",
        }])
        service.assign_transactions_to_report(["card-1"], group.id)
        service.set_vendor_classification("parking", "Transportation (Gas, Parking, Cabs & Other)")
        save_receipt_line_matches(self.root, {
            "card-1": {"best_receipt": str(receipt), "confidence": 0.99},
        })
        save_approved_matches(self.root, {
            "card-1": {"source_file": str(receipt), "approved": True},
        })
        original_rows = service.get_transactions()
        original_readiness = service.get_report_readiness(group.id)
        original_preparation = service.prepare_report_for_submission(group.id)
        self.assertTrue(original_readiness.submittable)
        self.assertTrue(original_readiness.ready)
        self.assertEqual(original_preparation["ready_total"], 1)
        snapshot = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.cash(report_id=group.id)
        self.store.save(
            kind="mileage", transaction_date="2026-07-12", description="Client visit",
            miles="10", report_id=group.id,
        )
        self.assertEqual(service.get_transactions(), original_rows)
        self.assertEqual(service.get_report_readiness(group.id), original_readiness)
        for name, contents in snapshot.items():
            self.assertEqual((self.root / name).read_bytes(), contents)
        self.assertEqual(service.prepare_report_for_submission(group.id), original_preparation)
        for entry in self.store.list():
            self.store.delete(entry.id)
        self.assertEqual(service.get_transactions(), original_rows)
        self.assertEqual(service.get_report_readiness(group.id), original_readiness)


class ReceiptMissingReadinessTests(unittest.TestCase):
    def test_receipt_missing_counts_as_resolved_and_approved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = ExpenseService(root)
            group = service.create_expense_report_group("Travel")
            save_expense_lines_cache(root, [{
                "line_id": "card-1", "merchant_name": "Parking", "amount": "25.00",
                "transaction_date": "2026-07-12", "currency": "USD",
            }])
            service.assign_transactions_to_report(["card-1"], group.id)
            service.set_vendor_classification("parking", "Transportation (Gas, Parking, Cabs & Other)")
            service.mark_receipt_missing("card-1")

            readiness = service.get_report_readiness(group.id)
            self.assertTrue(readiness.ready)
            self.assertTrue(readiness.submittable)
            self.assertEqual(readiness.receipt_missing_marked, 1)
            self.assertEqual(readiness.matched, 1)
            self.assertEqual(readiness.approved, 1)

            service.unmark_receipt_missing("card-1")
            readiness = service.get_report_readiness(group.id)
            self.assertFalse(readiness.ready)
            self.assertEqual(readiness.matched, 0)
            self.assertEqual(readiness.approved, 0)


if __name__ == "__main__":
    unittest.main()
