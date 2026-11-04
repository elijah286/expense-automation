"""Optional, isolated entry of Step 4 cash and mileage lines."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Callable, Literal

from playwright.sync_api import Dialog, Frame, Locator, Page


@dataclass(frozen=True)
class ManualSubmissionLine:
    id: str
    kind: Literal["cash", "mileage"]
    date: str
    description: str
    amount: str = ""
    currency: str = "USD"
    merchant: str = ""
    expense_type: str = ""
    miles: str = ""
    exchange_rate: str = ""

    @property
    def marker(self) -> str:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", self.id):
            raise ValueError("Invalid manual expense identifier.")
        return f"[EA:{self.id}]"

    @property
    def justification(self) -> str:
        purpose = f"{self.merchant}: {self.description}" if self.kind == "cash" else self.description
        return f"{purpose} {self.marker}"

    def validate(self) -> None:
        if self.kind not in ("cash", "mileage"):
            raise ValueError("Unknown manual expense kind.")
        date.fromisoformat(self.date)
        if not self.description.strip():
            raise ValueError("Manual expense business purpose is required.")
        self.marker
        if self.kind == "cash":
            if not self.merchant.strip() or not self.expense_type.strip():
                raise ValueError("Cash merchant and expense type are required.")
            if not re.fullmatch(r"[A-Z]{3}", self.currency):
                raise ValueError("Invalid cash currency.")
            if self.currency != "USD":
                if not self.exchange_rate.strip():
                    raise ValueError("Non-USD cash requires an exchange rate in USD per 1 receipt currency.")
                _positive(self.exchange_rate)
        _positive(self.amount if self.kind == "cash" else self.miles)


def _positive(value: str) -> Decimal:
    try:
        number = Decimal(value.replace(",", "").strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid Oracle numeric value: {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"Expected a positive value, got {value!r}.")
    return number


def _option_label_key(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip().casefold()
    return re.sub(r"\s*([,&():-])\s*", r"\1", value)


class ManualCleanupError(RuntimeError):
    """Continuing could leave a partial or invalid Oracle line behind."""


class OracleManualEntry:
    def __init__(
        self, page: Page, *, settle: Callable[[], None],
        save: Callable[[], bool], status: Callable[[str], None],
    ):
        self.page = page
        self.settle = settle
        self.save = save
        self.status = status
        self.created: list[ManualSubmissionLine] = []

    # Discover columns from the rendered table, not Oracle's session-specific IDs.
    _SCAN = r"""
(kind) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const heading = c => norm(c.getAttribute('colheadertxt') || c.innerText);
  const names = {
    'date':'date', 'start date':'date', 'receipt amount':'amount',
    'expense type':'expense_type', 'justification':'description',
    'unit of measure':'unit', 'trip distance':'miles',
    'receipt currency':'currency', 'currency':'currency',
    'exchange rate':'exchange_rate',
    'distance rate':'rate', 'reimbursable amount (usd)':'reimbursement'
  };
  for (const table of document.querySelectorAll('table')) {
    if (!visible(table)) continue;
    const trs = Array.from(table.rows).filter(tr => tr.closest('table') === table);
    const header = trs.find(tr => {
      const labels = Array.from(tr.cells).map(heading);
      return labels.includes('justification') &&
        labels.includes(kind === 'cash' ? 'receipt amount' : 'trip distance');
    });
    if (!header) continue;
    const columns = Array.from(header.cells).map(c => names[heading(c)] || '');
    table.setAttribute('data-ea-manual-table', kind);
    const rows = [];
    for (const tr of trs) {
      if (tr === header || !visible(tr)) continue;
      const cells = Array.from(tr.cells);
      if (!cells.some(c => c.querySelector('input:not([type=hidden]):not([type=checkbox])'))) continue;
      const key = `${kind}-${rows.length}`;
      tr.setAttribute('data-ea-manual-row', key);
      const fields = {};
      columns.forEach((name, i) => {
        if (!name || !cells[i]) return;
        const control = Array.from(cells[i].querySelectorAll('input:not([type=hidden]),select,textarea'))
          .find(e => visible(e) && e.type !== 'checkbox' && e.type !== 'image');
        if (control) {
          control.setAttribute('data-ea-manual-field', name);
          fields[name] = {value:control.value, editable:!control.disabled && !control.readOnly,
            max:control.maxLength || -1,
            options:control.options ? Array.from(control.options).map(o => ({label:o.text, value:o.value})) : null};
        } else fields[name] = {value:cells[i].innerText.trim(), editable:false};
      });
      rows.push({key, fields});
    }
    return rows;
  }
  return null;
}
"""

    _ACTION = r"""
({kind, action}) => {
  const table = document.querySelector(`[data-ea-manual-table="${kind}"]`);
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  for (let parent = table && table.parentElement; parent; parent = parent.parentElement) {
    const controls = Array.from(parent.querySelectorAll('a,button,input[type=button],input[type=image],img'));
    const target = controls.find(e => {
      if (!e.getClientRects().length) return false;
      const labels = [e.textContent, e.getAttribute('alt'), e.title, e.getAttribute('value')].map(norm);
      return labels.some(t => action === 'add'
        ? /^(add more lines|add another row|add row|add expense line|add another expense|add|[+])$/.test(t)
        : t === action);
    });
    if (target) { target.click(); return true; }
    if (parent.tagName === 'FORM' || parent.tagName === 'BODY') break;
  }
  return false;
}
"""

    def _tab(self, kind: str) -> tuple[Frame, list[dict]]:
        label = "Receipt-Based Expenses" if kind == "cash" else "Mileage Expenses"
        for frame in self.page.frames:
            rows = frame.evaluate(self._SCAN, kind)
            if rows is not None:
                return frame, rows
        for frame in self.page.frames:
            tab = frame.get_by_role("link", name=re.compile(rf"^{label}\s*(?:\[.*\])?$"))
            if tab.count() and tab.first.is_visible():
                tab.first.click()
                self.settle()
                break
        for frame in self.page.frames:
            rows = frame.evaluate(self._SCAN, kind)
            if rows is not None:
                return frame, rows
        raise RuntimeError(f"Oracle {label} table was not found.")

    def _marked(self, line: ManualSubmissionLine) -> tuple[Frame, dict]:
        frame, rows = self._tab(line.kind)
        matches = [r for r in rows if line.marker in r["fields"].get("description", {}).get("value", "")]
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one Oracle line for {line.marker}; found {len(matches)}.")
        return frame, matches[0]

    @staticmethod
    def _control(frame: Frame, row: dict, field: str) -> Locator:
        if not row["fields"].get(field, {}).get("editable"):
            raise RuntimeError(f"Oracle {field} field is missing or not editable.")
        return frame.locator(
            f'[data-ea-manual-row="{row["key"]}"] [data-ea-manual-field="{field}"]'
        )

    def _write(self, line: ManualSubmissionLine, field: str, value: str, *, select: bool = False) -> None:
        frame, row = self._marked(line)
        control = self._control(frame, row, field)
        if select:
            options = row["fields"][field].get("options") or []
            matches = [
                o for o in options
                if _option_label_key(o["label"]) == _option_label_key(value)
                or o["value"].strip().casefold() == value.casefold()
                or (field == "currency" and re.search(rf"\b{re.escape(value)}\b", o["label"]))
            ]
            if len(matches) != 1:
                raise ValueError(f"Oracle {field} option {value!r} is missing or ambiguous.")
            control.select_option(value=matches[0]["value"])
        else:
            maximum = row["fields"][field].get("max", -1)
            if maximum > 0 and len(value) > maximum:
                raise ValueError(f"Oracle {field} allows only {maximum} characters.")
            control.fill(value)
            control.press("Tab")
        self.settle()

    def _save(self) -> None:
        if not self.save():
            raise RuntimeError("Could not save Oracle Cash and Other Expenses.")
        self.settle()

    def _details(self, line: ManualSubmissionLine, *, update: bool) -> None:
        frame, row = self._marked(line)
        details = frame.locator(
            f'[data-ea-manual-row="{row["key"]}"] '
            'a:has(img[alt="Go to Enter Details for Expense page"])'
        )
        if details.count() != 1:
            raise RuntimeError("Cannot identify the optional expense's Details link.")
        details.click()
        self.settle()
        try:
            purpose = frame.locator('textarea[id="Justification"], textarea[id="DetailJust1"]')
            if purpose.count() != 1 or purpose.input_value() != line.justification:
                raise RuntimeError("Oracle opened Details for a different expense.")
            if line.kind == "cash":
                merchant = frame.locator('input[title="Merchant Name"]')
                if merchant.count() != 1:
                    raise RuntimeError("Oracle Merchant Name field was not found.")
                if update:
                    maximum = merchant.evaluate("(e) => e.maxLength")
                    if maximum > 0 and len(line.merchant) > maximum:
                        raise ValueError(f"Oracle Merchant Name allows only {maximum} characters.")
                    merchant.fill(line.merchant)
                    merchant.press("Tab")
                if merchant.input_value() != line.merchant:
                    raise ValueError("Oracle did not retain the merchant name in Details.")
            elif update:
                calculate = frame.get_by_role("button", name="Calculate Amount", exact=True)
                if not calculate.count():
                    raise RuntimeError("Oracle Calculate Amount action was not found.")
                calculate.first.click()
                self.settle()
                _positive(frame.locator('[id="DistanceRate"]').inner_text())
        finally:
            back = frame.get_by_role("button", name="Return", exact=True)
            if not back.count():
                raise RuntimeError("Cannot return from optional expense Details.")
            back.first.click()
            self.settle()

    def _verify(self, line: ManualSubmissionLine, *, saved: bool) -> dict:
        _, row = self._marked(line)
        fields = row["fields"]
        actual_date = fields.get("date", {}).get("value", "").strip()
        expected = date.fromisoformat(line.date)
        if actual_date.upper() not in (expected.strftime("%d-%b-%Y").upper(), expected.isoformat()):
            raise ValueError("Oracle did not retain the expense date.")
        if fields.get("description", {}).get("value") != line.justification:
            raise ValueError("Oracle did not retain the complete business purpose.")
        numeric = "amount" if line.kind == "cash" else "miles"
        if _positive(fields.get(numeric, {}).get("value", "")) != _positive(
            line.amount if line.kind == "cash" else line.miles
        ):
            raise ValueError(f"Oracle did not retain {numeric}.")
        if line.kind == "cash":
            for name, wanted in (("expense_type", line.expense_type), ("currency", line.currency)):
                info = fields.get(name, {})
                selected = next((o for o in info.get("options", []) or [] if o["value"] == info.get("value")), None)
                labels = [info.get("value", ""), selected["label"] if selected else ""]
                if not any(
                    _option_label_key(v) == _option_label_key(wanted)
                    or (name == "currency" and re.search(rf"\b{re.escape(wanted)}\b", v))
                    for v in labels
                ):
                    raise ValueError(f"Oracle did not retain {name}.")
        else:
            info = fields.get("expense_type", {})
            selected = next((o for o in info.get("options", []) or [] if o["value"] == info.get("value")), {})
            expense_type = selected.get("label", info.get("value", ""))
            if re.sub(r"\s+", " ", expense_type).strip().casefold() != "mileage - business":
                raise ValueError("Oracle mileage expense type is not Mileage - Business.")
            if fields.get("unit", {}).get("value", "").casefold() != "miles":
                info = fields.get("unit", {})
                selected = next((o for o in info.get("options", []) or [] if o["value"] == info.get("value")), {})
                if selected.get("label", "").strip().casefold() != "miles":
                    raise ValueError("Oracle did not retain Miles as the unit.")
        if saved:
            reimbursement = _positive(fields.get("reimbursement", {}).get("value", ""))
            if line.kind == "cash":
                rate = Decimal("1")
                if line.currency != "USD":
                    rate = _positive(fields.get("exchange_rate", {}).get("value", ""))
                    if rate != _positive(line.exchange_rate):
                        raise ValueError("Oracle did not retain the exchange rate.")
                expected_amount = _positive(line.amount) * rate
            else:
                rate = _positive(fields.get("rate", {}).get("value", ""))
                expected_amount = _positive(line.miles) * rate
            expected_amount = expected_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if reimbursement != expected_amount:
                raise ValueError(
                    f"Oracle reimbursement {reimbursement} does not match expected USD {expected_amount}."
                )
        return fields

    def remove(self, line: ManualSubmissionLine) -> None:
        frame, row = self._marked(line)
        checkbox = frame.locator(f'[data-ea-manual-row="{row["key"]}"] input[type=checkbox]')
        table = frame.locator(f'[data-ea-manual-table="{line.kind}"]')
        # Never let Oracle's Remove action delete a user's other selected rows.
        for selected in table.locator('input[type=checkbox]:checked').all():
            selected.uncheck()
        if checkbox.count() != 1:
            raise ManualCleanupError("Cannot identify the optional Oracle line's removal checkbox.")
        checkbox.check()

        def on_dialog(dialog: Dialog) -> None:
            if dialog.type == "confirm" and re.search(r"remove|delete", dialog.message, re.IGNORECASE):
                dialog.accept()
            else:
                self.status(f"Unexpected Oracle removal dialog: {dialog.message}")
                dialog.dismiss()

        self.page.on("dialog", on_dialog)
        try:
            if not frame.evaluate(self._ACTION, {"kind": line.kind, "action": "remove"}):
                raise ManualCleanupError("Oracle Remove action was not found.")
        finally:
            self.page.remove_listener("dialog", on_dialog)
        self.settle()
        _, rows = self._tab(line.kind)
        if any(line.marker in r["fields"].get("description", {}).get("value", "") for r in rows):
            raise ManualCleanupError("Oracle did not remove the partial optional expense.")
        self._save()
        _, rows = self._tab(line.kind)
        if any(line.marker in r["fields"].get("description", {}).get("value", "") for r in rows):
            raise ManualCleanupError("The removed optional expense reappeared after Save.")
        if line in self.created:
            self.created.remove(line)

    def enter(self, lines: list[ManualSubmissionLine]) -> dict:
        result: dict = {"entered": [], "warnings": [], "oracle_amounts": {}}
        for line in lines:
            claimed = False
            try:
                line.validate()
                frame, rows = self._tab(line.kind)
                if line.kind == "cash" and (
                    not rows or not rows[0]["fields"].get("currency", {}).get("editable")
                ):
                    if not frame.evaluate(self._ACTION, {"kind": line.kind, "action": "show receipt currency"}):
                        raise RuntimeError("Oracle receipt currency cannot be verified.")
                    self.settle()
                    frame, rows = self._tab(line.kind)
                existing = [r for r in rows if line.marker in r["fields"].get("description", {}).get("value", "")]
                if not existing:
                    blank = next((r for r in rows if not any(
                        r["fields"].get(f, {}).get("value", "").strip()
                        for f in ("date", "description", "amount", "miles")
                    )), None)
                    if blank is None:
                        if not frame.evaluate(self._ACTION, {"kind": line.kind, "action": "add"}):
                            raise RuntimeError("No empty Oracle row and no Add Row action.")
                        self.settle()
                        frame, rows = self._tab(line.kind)
                        blank = next((r for r in rows if not any(
                            r["fields"].get(f, {}).get("value", "").strip()
                            for f in ("date", "description", "amount", "miles")
                        )), None)
                    if blank is None:
                        raise RuntimeError("Oracle did not create an empty expense row.")
                    description = self._control(frame, blank, "description")
                    maximum = blank["fields"]["description"].get("max", -1)
                    if maximum > 0 and len(line.justification) > maximum:
                        raise ValueError(f"Business purpose exceeds Oracle's {maximum}-character limit.")
                    # Claim the row before changing any other field, so cleanup/retries are scoped.
                    description.fill(line.justification)
                    claimed = True
                    self.created.append(line)
                    description.press("Tab")
                    self.settle()
                    self._write(line, "date", date.fromisoformat(line.date).strftime("%d-%b-%Y").upper())
                    if line.kind == "cash":
                        self._write(line, "expense_type", line.expense_type, select=True)
                        self._write(line, "currency", line.currency, select=True)
                        self._write(line, "amount", line.amount)
                        if line.currency != "USD":
                            self._write(line, "exchange_rate", line.exchange_rate)
                    else:
                        self._write(line, "unit", "Miles", select=True)
                        self._write(line, "miles", line.miles)
                    self._verify(line, saved=False)
                    self._details(line, update=True)
                    self._save()
                if line.kind == "cash":
                    self._details(line, update=False)
                fields = self._verify(line, saved=True)
                result["entered"].append(line.id)
                result["oracle_amounts"][line.id] = fields["reimbursement"]["value"]
                self.status(f"Step 4: verified {line.kind} expense {line.id} saved in Oracle.")
            except Exception as exc:
                if claimed:
                    try:
                        self.remove(line)
                    except Exception as cleanup:
                        raise ManualCleanupError(
                            f"Optional expense failed ({exc}); cleanup could not be verified ({cleanup}). "
                            "Review Step 4 in Oracle before continuing."
                        ) from cleanup
                warning = f"Optional {line.kind} expense {line.id} was not included: {exc}"
                result["warnings"].append(warning)
                self.status(warning)
        return result
