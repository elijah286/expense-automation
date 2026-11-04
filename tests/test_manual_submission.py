"""Step 4 integration tests against an isolated, dynamic Oracle-shaped DOM."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from browser.manual_entry import ManualCleanupError, ManualSubmissionLine, OracleManualEntry
from browser.submitter import ReportSubmitter, SubmissionPayload
from expense_lines_cache import save_expense_lines_cache
from web.manual_expenses import ManualExpenseStore
from web.service import ExpenseService


FIXTURE = r"""
<body>
<div>Step 4 of 6</div>
<a href="#" onclick="switchTab('cash');return false">Receipt-Based Expenses [0.00]</a>
<a href="#" onclick="switchTab('mileage');return false">Mileage Expenses [0.00]</a>
<button onclick="save()">Save</button>
<div id="region"></div>
<script>
let kind = 'cash', currencyVisible = false, failSave = false, failRemove = false, confirmRemove = false;
let saves = 0, adds = 0, calculations = 0, detailIndex = 0;
let failCalculation = false, dropMerchantOnSave = false;
const blank = () => ({date:'', description:'', amount:'', miles:'', expense_type:'', currency:'USD', unit:'MI', reimbursement:'', rate:'', merchant:'', exchange_rate:''});
const data = {cash:[blank(), blank()], mileage:[blank()]};
let saved = {cash:[], mileage:[]};
function capture() {
  for (const tr of document.querySelectorAll('#region tr[data-index]')) {
    const row = data[kind][Number(tr.dataset.index)];
    for (const el of tr.querySelectorAll('[data-field]')) row[el.dataset.field] = el.value;
  }
}
function switchTab(next) { capture(); kind=next; render(); }
function field(name, value) { return `<input data-field="${name}" value="${value}" maxlength="${name==='description'?240:80}">`; }
function select(name, value, options) {
  return `<select data-field="${name}">${options.map(([v,t]) => {
    const encoded = v ? v + '-session' : '';
    return `<option value="${encoded}" ${value===v || value===encoded?'selected':''}>${t}</option>`;
  }).join('')}</select>`;
}
function render() {
  const cash = kind==='cash';
  document.querySelector('#region').innerHTML = `
    <button onclick="capture();currencyVisible=true;render()">Show Receipt Currency</button>
    <button onclick="removeRows()">Remove</button>
    <a href="#" title="Add More Lines" onclick="capture();data[kind].push(blank());adds++;render();return false"><img alt="Add More Lines"></a>
    <table><tr><th><input type="checkbox" aria-label="Select all"></th><th>Line</th>
      <th>${cash?'Date':'Start Date'}</th>
      ${cash?'<th>Receipt Amount</th>':''}
      <th>Expense Type</th><th colheadertxt="Justification">Justification<span hidden>Sort internals</span></th>
      ${cash?(currencyVisible?'<th>Receipt Currency</th><th>Exchange Rate</th>':''):'<th>Unit of Measure</th><th>Trip Distance</th><th>Distance Rate</th>'}
      <th>Reimbursable Amount (USD)</th><th>Details</th></tr>
      ${data[kind].map((r,i) => `<tr data-index="${i}"><td><input type="checkbox"></td><td>${i+1}</td>
        <td>${field('date',r.date)}</td>
        ${cash?`<td>${field('amount',r.amount)}</td>`:''}
        <td>${cash?select('expense_type',r.expense_type,[['',''],['PARK','Parking'],['MEAL','Meals'],['TRANS','Transportation (Gas,Parking,Cabs& Other)']]):'Mileage - Business'}</td>
        <td>${field('description',r.description)}</td>
        ${cash?(currencyVisible?`<td>${select('currency',r.currency,[['USD','US Dollar (USD)'],['EUR','Euro (EUR)']])}</td><td>${field('exchange_rate',r.exchange_rate)}</td>`:''):
          `<td>${select('unit',r.unit,[['MI','Miles'],['KM','Kilometers']])}</td><td>${field('miles',r.miles)}</td><td>${r.rate}</td>`}
        <td>${r.reimbursement}</td><td><a href="#" onclick="openDetails(${i});return false"><img alt="Go to Enter Details for Expense page"></a></td></tr>`).join('')}
    </table>`;
}
function openDetails(index) { capture(); detailIndex=index; renderDetails(); }
function renderDetails() {
  const row = data[kind][detailIndex];
  document.querySelector('#region').innerHTML = `
    <textarea id="${kind==='cash'?'Justification':'DetailJust1'}">${row.description}</textarea>
    ${kind==='cash'?`<input title="Merchant Name" maxlength="80" value="${row.merchant}">`:
      `<button onclick="calculate()">Calculate Amount</button><span id="DistanceRate">${row.rate}</span>`}
    <button onclick="returnDetails()">Return</button>`;
}
function calculate() {
  calculations++;
  if (!failCalculation) {
    const row = data[kind][detailIndex];
    row.rate = '0.50';
    row.reimbursement = (Number(row.miles)*0.50).toFixed(2);
  }
  renderDetails();
}
function returnDetails() {
  if (kind==='cash') data[kind][detailIndex].merchant = document.querySelector('input[title="Merchant Name"]').value;
  render();
}
function removeRows() {
  if (failRemove) return;
  if (confirmRemove && !confirm('Remove selected expense lines?')) return;
  capture();
  const selected = Array.from(document.querySelectorAll('tr[data-index]'))
    .filter(tr => tr.querySelector('input[type=checkbox]').checked).map(tr => Number(tr.dataset.index));
  data[kind] = data[kind].filter((r,i) => !selected.includes(i));
  if (!data[kind].length) data[kind].push(blank());
  render();
}
function save() {
  capture(); saves++;
  if (failSave) { render(); return; }
  for (const r of data[kind]) {
    if (kind==='cash') r.reimbursement = r.date && r.description
      ? (Number(r.amount)*(r.currency.startsWith('USD')?1:Number(r.exchange_rate))).toFixed(2) : '';
    if (dropMerchantOnSave) r.merchant = '';
  }
  saved = JSON.parse(JSON.stringify(data));
  render();
}
render();
</script>
"""


CASH = ManualSubmissionLine(
    id="cash-one", kind="cash", date="2026-07-12", description="Client parking",
    amount="12.50", currency="USD", merchant="City parking", expense_type="Parking",
)
MILEAGE = ManualSubmissionLine(
    id="mileage-one", kind="mileage", date="2026-07-13", description="Client visit", miles="10",
)


class ManualEntryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.page.set_content(FIXTURE)
        self.status = []
        self.engine = OracleManualEntry(
            self.page, settle=lambda: None,
            save=lambda: (self.page.get_by_role("button", name="Save", exact=True).click() or True),
            status=self.status.append,
        )

    def test_cash_mileage_saved_exactly_and_oracle_rate_is_authoritative(self):
        result = self.engine.enter([CASH, MILEAGE])
        self.assertEqual(result["entered"], [CASH.id, MILEAGE.id])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["oracle_amounts"], {CASH.id: "12.50", MILEAGE.id: "5.00"})
        saved = self.page.evaluate("saved")
        self.assertEqual(saved["cash"][0]["date"], "12-JUL-2026")
        self.assertEqual(saved["cash"][0]["expense_type"], "PARK-session")
        self.assertEqual(saved["cash"][0]["description"], CASH.justification)
        self.assertEqual(saved["cash"][0]["currency"], "USD-session")
        self.assertEqual(saved["cash"][0]["merchant"], "City parking")
        self.assertEqual(saved["mileage"][0]["miles"], "10")
        self.assertEqual(saved["mileage"][0]["unit"], "MI-session")
        self.assertEqual(self.page.evaluate("calculations"), 1)

    def test_retries_do_not_duplicate_or_overwrite_existing_entries(self):
        self.engine.enter([CASH, MILEAGE])
        self.page.evaluate("currencyVisible = false; render()")
        result = self.engine.enter([CASH, MILEAGE])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len([r for r in self.page.evaluate("saved.cash") if r["date"]]), 1)
        result = self.engine.enter([replace(CASH, amount="99.00")])
        self.assertEqual(result["entered"], [])
        self.assertIn("did not retain amount", result["warnings"][0])
        self.assertEqual(self.page.evaluate("saved.cash[0].amount"), "12.50")

    def test_missing_type_rolls_back_only_new_row_and_continues_mileage(self):
        self.page.evaluate("""() => {
          data.cash[0] = {...blank(), date:'01-JUL-2026', amount:'20',
            description:'User-owned row', expense_type:'PARK', reimbursement:'20.00'};
          render();
          document.querySelector('tr[data-index="0"] input[type=checkbox]').checked = true;
        }""")
        result = self.engine.enter([replace(CASH, expense_type="Unmapped type"), MILEAGE])
        self.assertEqual(result["entered"], [MILEAGE.id])
        self.assertEqual(len(result["warnings"]), 1)
        cash = self.page.evaluate("data.cash")
        self.assertEqual(len(cash), 1)
        self.assertEqual(cash[0]["description"], "User-owned row")
        self.assertEqual(cash[0]["amount"], "20")

    def test_dynamic_add_more_than_initial_rows(self):
        lines = [replace(CASH, id=f"cash-{n}", amount=str(n + 1)) for n in range(13)]
        result = self.engine.enter(lines)
        self.assertEqual(len(result["entered"]), 13)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.page.evaluate("adds"), 11)
        self.assertEqual(len([r for r in self.page.evaluate("saved.cash") if r["date"]]), 13)

    def test_non_usd_currency_is_selected_not_assumed(self):
        result = self.engine.enter([replace(CASH, currency="EUR", exchange_rate="1.25")])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.page.evaluate("saved.cash[0].currency"), "EUR-session")
        self.assertEqual(self.page.evaluate("saved.cash[0].exchange_rate"), "1.25")
        self.assertEqual(result["oracle_amounts"][CASH.id], "15.63")
        result = self.engine.enter([replace(CASH, id="cash-gbp", currency="GBP", exchange_rate="1.25")])
        self.assertEqual(result["entered"], [])
        self.assertIn("missing or ambiguous", result["warnings"][0])
        self.assertEqual(len([r for r in self.page.evaluate("data.cash") if r["date"]]), 1)

    def test_missing_foreign_exchange_rate_does_not_create_partial_line(self):
        result = self.engine.enter([replace(CASH, currency="EUR"), MILEAGE])
        self.assertEqual(result["entered"], [MILEAGE.id])
        self.assertIn("requires an exchange rate", result["warnings"][0])
        self.assertFalse(any(row["date"] for row in self.page.evaluate("data.cash")))

    def test_incorrect_oracle_reimbursement_is_removed(self):
        self.page.evaluate("""() => {
          const original = save;
          save = () => { original(); data.cash[0].reimbursement = '99.00'; render(); };
        }""")
        result = self.engine.enter([CASH])
        self.assertEqual(result["entered"], [])
        self.assertIn("does not match expected USD", result["warnings"][0])

    def test_review_errors_are_reported_but_saved_information_is_not_an_error(self):
        self.page.set_content(
            '<table><tr><td><h1>Information</h1><div class="x47">Report saved</div></td></tr>'
            '<tr><td><table><tr><td><h1>Error</h1></td></tr></table>'
            '<div class="x47">Employer policy requires attention.</div></td></tr></table>'
        )
        submitter = ReportSubmitter.__new__(ReportSubmitter)
        submitter.browser_page = self.page
        self.assertEqual(submitter._oracle_validation_errors(), ["Employer policy requires attention."])

    def test_portal_punctuation_spacing_matches_without_guessing_another_category(self):
        result = self.engine.enter([
            replace(CASH, expense_type="Transportation (Gas, Parking, Cabs & Other)"),
        ])
        self.assertEqual(result["entered"], [CASH.id])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.page.evaluate("saved.cash[0].expense_type"), "TRANS-session")

    def test_failed_save_is_not_reported_as_success(self):
        self.page.evaluate("failSave = true")
        result = self.engine.enter([CASH])
        self.assertEqual(result["entered"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(len([r for r in self.page.evaluate("data.cash") if r["date"]]), 0)

    def test_cleanup_failure_stops_instead_of_leaving_invalid_data(self):
        self.page.evaluate("failRemove = true")
        with self.assertRaises(ManualCleanupError):
            self.engine.enter([replace(CASH, expense_type="Unknown")])

    def test_scoped_removal_confirmation_is_handled(self):
        self.page.evaluate("confirmRemove = true")
        result = self.engine.enter([replace(CASH, expense_type="Unknown"), MILEAGE])
        self.assertEqual(result["entered"], [MILEAGE.id])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertFalse(any(row["date"] for row in self.page.evaluate("data.cash")))

    def test_failed_mileage_calculation_is_removed_and_cash_continues(self):
        self.page.evaluate("failCalculation = true")
        result = self.engine.enter([MILEAGE, CASH])
        self.assertEqual(result["entered"], [CASH.id])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertFalse(any(row["date"] for row in self.page.evaluate("data.mileage")))

    def test_merchant_must_survive_save_and_details_readback(self):
        self.page.evaluate("dropMerchantOnSave = true")
        result = self.engine.enter([CASH, MILEAGE])
        self.assertEqual(result["entered"], [MILEAGE.id])
        self.assertIn("merchant name", result["warnings"][0])
        self.assertFalse(any(row["date"] for row in self.page.evaluate("data.cash")))

    def test_overlong_merchant_is_removed_without_leaving_details_open(self):
        result = self.engine.enter([replace(CASH, merchant="M" * 81), MILEAGE])
        self.assertEqual(result["entered"], [MILEAGE.id])
        self.assertIn("Merchant Name allows only 80", result["warnings"][0])
        self.assertFalse(any(row["date"] for row in self.page.evaluate("data.cash")))

    def test_unknown_mileage_classification_is_removed(self):
        self.page.goto("about:blank")
        self.page.set_content(FIXTURE.replace("'Mileage - Business'", "'Mileage - Personal'"))
        result = self.engine.enter([MILEAGE])
        self.assertEqual(result["entered"], [])
        self.assertIn("Mileage - Business", result["warnings"][0])
        self.assertFalse(any(row["date"] for row in self.page.evaluate("data.mileage")))

    def test_long_purpose_and_invalid_data_do_not_touch_rows(self):
        result = self.engine.enter([replace(CASH, description="x" * 250), replace(MILEAGE, miles="-1")])
        self.assertEqual(result["entered"], [])
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(self.page.evaluate("data.cash[0].description"), "")
        self.assertEqual(self.page.evaluate("saves"), 0)

    def test_tables_in_frames_are_supported(self):
        self.page.set_content("<iframe></iframe>")
        self.page.frames[1].set_content(FIXTURE)
        # Save must also run in the table's frame, like the submitter's shared helper.
        self.engine.save = lambda: (self.page.frames[1].get_by_role("button", name="Save", exact=True).click() or True)
        result = self.engine.enter([CASH, MILEAGE])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(result["entered"]), 2)


class SubmissionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = ExpenseService(self.root)
        self.group = self.service.create_expense_report_group("Travel")
        self.service._settings_path().write_text(json.dumps({
            "legacy_url": "https://oracle.example.test", "approver": "Manager",
        }))
        save_expense_lines_cache(self.root, [{
            "line_id": "card-one", "merchant_name": "Hotel", "amount": "25.00",
            "transaction_date": "2026-07-12", "currency": "USD",
        }])
        self.service.assign_transactions_to_report(["card-one"], self.group.id)
        self.service.mark_receipt_missing("card-one")
        self.store = ManualExpenseStore(self.root)
        self.cash = self.store.save(
            kind="cash", transaction_date=CASH.date, description=CASH.description,
            merchant=CASH.merchant, expense_type=CASH.expense_type, amount=CASH.amount,
            report_id=self.group.id,
        )
        self.mileage = self.store.save(
            kind="mileage", transaction_date=MILEAGE.date, description=MILEAGE.description,
            miles=MILEAGE.miles, report_id=self.group.id,
        )
        self.store.save(
            kind="cash", transaction_date=CASH.date, description="Other report",
            merchant="Other", expense_type="Parking", amount="1", report_id="other",
        )
        self.submitter = Mock()
        self.submitter._cdp_http_url = "http://127.0.0.1:12345"
        self.submitter.scraped_expense_type_options = []
        self.submitter.submit.side_effect = lambda *a, **k: {"submitted": False}
        self.factory = patch("browser.submitter.ReportSubmitter", return_value=self.submitter)
        self.factory.start()
        self.addCleanup(self.factory.stop)
        self.classification = patch.object(self.service, "_auto_classify_unclassified")
        self.classification.start()
        self.addCleanup(self.classification.stop)

    def test_default_does_not_read_drafts_and_card_payload_is_unchanged(self):
        with patch.object(ManualExpenseStore, "list", side_effect=AssertionError("Must not read drafts")):
            self.service.run_submission(self.group.id)
        payload = self.submitter.submit.call_args.args[1]
        self.assertEqual(payload.manual_lines, [])
        self.assertEqual([r.line_id for r in payload.lines], ["card-one"])

    def test_opt_in_is_independent_and_report_scoped(self):
        for cash, mileage, expected in (
            (True, False, [self.cash.id]), (False, True, [self.mileage.id]),
            (True, True, [self.cash.id, self.mileage.id]),
        ):
            with self.subTest(cash=cash, mileage=mileage):
                self.service.run_submission(self.group.id, include_cash=cash, include_mileage=mileage)
                payload = self.submitter.submit.call_args.args[1]
                self.assertEqual([r.id for r in payload.manual_lines], expected)
                self.assertEqual([r.line_id for r in payload.lines], ["card-one"])
        self.assertEqual(len(self.store.list()), 3)
        state = self.service._load_submission_state()
        self.assertTrue(state["completed"])
        self.assertIn("manual_expenses", state["result"])

    def test_foreign_exchange_rate_is_carried_to_oracle_payload(self):
        foreign = self.store.save(
            kind="cash", transaction_date=CASH.date, description="Foreign parking",
            merchant=CASH.merchant, expense_type=CASH.expense_type, amount="10",
            currency="EUR", exchange_rate="1.25", report_id=self.group.id,
        )
        self.service.run_submission(self.group.id, include_cash=True)
        payload = self.submitter.submit.call_args.args[1]
        self.assertEqual(next(row for row in payload.manual_lines if row.id == foreign.id).exchange_rate, "1.25")

    def test_corrupt_optional_store_warns_but_card_flow_runs(self):
        self.store.path.write_text("invalid json")
        messages = []
        result = self.service.run_submission(self.group.id, include_cash=True, on_status=messages.append)
        self.assertEqual(len(result["manual_expenses"]["warnings"]), 1)
        self.assertIn("omitted", messages[0])
        self.assertEqual(self.submitter.submit.call_args.args[1].manual_lines, [])


class WizardIsolationTests(unittest.TestCase):
    def submitter(self):
        submitter = ReportSubmitter.__new__(ReportSubmitter)
        submitter.browser_page = Mock()
        submitter.set_status = Mock()
        submitter._wait_for_oracle_page_stable = Mock()
        submitter.wait_for_wizard_next_enabled_and_click = Mock(return_value=True)
        submitter._detect_wizard_step = Mock(return_value=4)
        submitter._wait_for_step6_attachments = Mock()
        submitter._attach_receipts_step6 = Mock(return_value=1)
        submitter._oracle_validation_errors = Mock(return_value=[])
        return submitter

    def test_no_optional_lines_bypasses_entry_entirely(self):
        submitter = self.submitter()
        with patch("browser.submitter.OracleManualEntry") as entry:
            submitter._execute_wizard_steps(4, "Travel", SubmissionPayload("Travel", "Manager"), {})
        entry.assert_not_called()
        self.assertEqual(submitter.wait_for_wizard_next_enabled_and_click.call_count, 2)
        submitter._attach_receipts_step6.assert_called_once_with([])

    def test_validation_failure_removes_new_optional_line_and_continues_attachments(self):
        submitter = self.submitter()
        submitter.wait_for_wizard_next_enabled_and_click.side_effect = [False, True, True]
        submitter._detect_wizard_step.side_effect = [4, 4, 5]
        entry = Mock()
        entry.created = [CASH]
        entry.enter.return_value = {"entered": [CASH.id], "warnings": [], "oracle_amounts": {CASH.id: "12.50"}}
        summary = {}
        with patch("browser.submitter.OracleManualEntry", return_value=entry):
            submitter._execute_wizard_steps(
                4, "Travel", SubmissionPayload("Travel", "Manager", manual_lines=[CASH]), summary,
            )
        entry.remove.assert_called_once_with(CASH)
        self.assertEqual(summary["manual_expenses"]["entered"], [])
        self.assertEqual(len(summary["manual_expenses"]["warnings"]), 1)
        submitter._attach_receipts_step6.assert_called_once()

    def test_clicking_next_without_advancing_triggers_optional_cleanup(self):
        submitter = self.submitter()
        submitter._detect_wizard_step.side_effect = [4, 4, 5]
        submitter._oracle_validation_errors.return_value = ["Please enter an Exchange Rate."]
        entry = Mock()
        entry.created = [CASH]
        entry.enter.return_value = {"entered": [CASH.id], "warnings": [], "oracle_amounts": {CASH.id: "12.50"}}
        summary = {}
        with patch("browser.submitter.OracleManualEntry", return_value=entry):
            submitter._execute_wizard_steps(
                4, "Travel", SubmissionPayload("Travel", "Manager", manual_lines=[CASH]), summary,
            )
        entry.remove.assert_called_once_with(CASH)
        self.assertEqual(summary["manual_expenses"]["entered"], [])
        self.assertIn("Please enter an Exchange Rate", summary["manual_expenses"]["warnings"][0])
        submitter._attach_receipts_step6.assert_called_once()

    def test_accepted_step4_keeps_optional_lines(self):
        submitter = self.submitter()
        submitter._detect_wizard_step.return_value = 5
        entry = Mock()
        entry.created = [CASH]
        entry.enter.return_value = {"entered": [CASH.id], "warnings": [], "oracle_amounts": {CASH.id: "12.50"}}
        summary = {}
        with patch("browser.submitter.OracleManualEntry", return_value=entry):
            submitter._execute_wizard_steps(
                4, "Travel", SubmissionPayload("Travel", "Manager", manual_lines=[CASH]), summary,
            )
        entry.remove.assert_not_called()
        self.assertEqual(summary["manual_expenses"]["entered"], [CASH.id])
        submitter._attach_receipts_step6.assert_called_once()

    def test_rejected_step4_after_cleanup_does_not_claim_success(self):
        submitter = self.submitter()
        submitter._detect_wizard_step.return_value = 4
        entry = Mock()
        entry.created = [CASH]
        entry.enter.return_value = {"entered": [CASH.id], "warnings": [], "oracle_amounts": {CASH.id: "12.50"}}
        with patch("browser.submitter.OracleManualEntry", return_value=entry):
            with self.assertRaisesRegex(RuntimeError, "still rejected Step 4"):
                submitter._execute_wizard_steps(
                    4, "Travel", SubmissionPayload("Travel", "Manager", manual_lines=[CASH]), {},
                )
        submitter._attach_receipts_step6.assert_not_called()

    def test_oracle_can_skip_empty_allocations_without_duplicate_navigation(self):
        submitter = self.submitter()
        submitter._detect_wizard_step.return_value = 6
        entry = Mock()
        entry.created = [MILEAGE]
        entry.enter.return_value = {"entered": [MILEAGE.id], "warnings": [], "oracle_amounts": {MILEAGE.id: "7.60"}}
        with patch("browser.submitter.OracleManualEntry", return_value=entry):
            submitter._execute_wizard_steps(
                4, "Travel", SubmissionPayload("Travel", "Manager", manual_lines=[MILEAGE]), {},
            )
        self.assertEqual(submitter.wait_for_wizard_next_enabled_and_click.call_count, 1)
        submitter._attach_receipts_step6.assert_called_once()

    def test_review_policy_errors_are_surfaced_in_result(self):
        submitter = self.submitter()
        submitter._oracle_validation_errors.return_value = ["Employer policy rejected this report."]
        summary = {}
        submitter._execute_wizard_steps(
            6, "Travel", SubmissionPayload("Travel", "Manager", manual_lines=[CASH]), summary,
        )
        self.assertTrue(any("Employer policy" in w for w in summary["manual_expenses"]["warnings"]))

    def test_resume_past_step4_warns_without_adding_duplicates(self):
        submitter = self.submitter()
        summary = {}
        with patch("browser.submitter.OracleManualEntry") as entry:
            submitter._execute_wizard_steps(
                5, "Travel", SubmissionPayload("Travel", "Manager", manual_lines=[MILEAGE]), summary,
            )
        entry.assert_not_called()
        self.assertIn("not verified", summary["manual_expenses"]["warnings"][0])

    def test_uncertain_cleanup_is_not_automatically_retried(self):
        submitter = self.submitter()
        submitter._reconnect_to_cdp = Mock(return_value=True)
        submitter._detach_playwright = Mock()
        submitter._execute_wizard_steps = Mock(side_effect=ManualCleanupError("Review partial row"))
        with self.assertRaises(ManualCleanupError):
            submitter.submit(
                "https://oracle.example.test", SubmissionPayload("Travel", "Manager"),
                resume_cdp_url="http://127.0.0.1:12345",
            )
        submitter._execute_wizard_steps.assert_called_once()
        submitter._detach_playwright.assert_called_once()


if __name__ == "__main__":
    unittest.main()
