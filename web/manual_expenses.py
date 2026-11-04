"""Local manual drafts, deliberately separate from Oracle credit-card caches."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Literal

from persistence.atomic_json import atomic_write_json

ExpenseKind = Literal["cash", "mileage"]
MILEAGE_RATE_SOURCE = "https://www.irs.gov/tax-professionals/standard-mileage-rates"
# USD per business mile; use bounded periods so unknown dates never reuse a stale rate.
_MILEAGE_RATES = (
    ("2024-01-01", "2024-12-31", "0.67"),
    ("2025-01-01", "2025-12-31", "0.70"),
    ("2026-01-01", "2026-06-30", "0.725"),
    ("2026-07-01", "2026-12-31", "0.76"),
)


def expense_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Enter a valid date in YYYY-MM-DD format.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Enter a valid transaction date.") from exc


def positive_decimal(value: str, label: str, places: int) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a positive number.") from exc
    if not number.is_finite() or number <= 0 or number > Decimal("999999999"):
        raise ValueError(f"{label} must be greater than zero and at most 999999999.")
    if number != number.quantize(Decimal(1).scaleb(-places)):
        raise ValueError(f"{label} supports at most {places} decimal places.")
    return number


def mileage_estimate(transaction_date: str, miles: str) -> tuple[str, str]:
    day = expense_date(transaction_date)
    distance = positive_decimal(miles, "Miles", 3)
    for start, end, rate in _MILEAGE_RATES:
        if start <= day <= end:
            amount = (distance * Decimal(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if amount <= 0:
                raise ValueError("Mileage reimbursement must round to at least USD 0.01.")
            return rate, str(amount)
    raise ValueError("No verified mileage rate for this date. Supported dates: 2024-2026.")


@dataclass(frozen=True)
class ManualExpense:
    id: str
    kind: ExpenseKind
    date: str
    description: str
    amount: str
    currency: str
    merchant: str = ""
    expense_type: str = ""
    report_id: str = ""
    miles: str = ""
    rate: str = ""
    rate_source: str = ""
    exchange_rate: str = ""


class ManualExpenseStore:
    def __init__(self, app_dir: Path):
        self.path = app_dir / "manual_expenses.json"
        self._lock = threading.Lock()

    def list(self) -> list[ManualExpense]:
        with self._lock:
            return self._read()

    def _read(self) -> list[ManualExpense]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
                raise ValueError("Expected a list of manual expenses.")
            return [ManualExpense(**row) for row in raw]
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "Could not read manual expenses. Restore manual_expenses.json from backup "
                "before editing; existing data has not been overwritten."
            ) from exc

    def save(
        self, *, kind: ExpenseKind, transaction_date: str, description: str,
        amount: str = "", currency: str = "USD", merchant: str = "",
        expense_type: str = "", report_id: str = "", miles: str = "",
        entry_id: str = "",
        exchange_rate: str = "",
    ) -> ManualExpense:
        if kind not in ("cash", "mileage"):
            raise ValueError("Choose cash or mileage.")
        day = expense_date(transaction_date)
        if not description.strip():
            raise ValueError("Enter a description / business purpose.")
        rate = ""
        if kind == "mileage":
            rate, amount = mileage_estimate(day, miles)
            miles = format(positive_decimal(miles, "Miles", 3), "f")
            currency = "USD"
        else:
            amount = str(positive_decimal(amount, "Amount", 2).quantize(Decimal("0.01")))
            miles = ""
            if not merchant.strip():
                raise ValueError("Enter the merchant / payee.")
            if not expense_type.strip():
                raise ValueError("Choose an expense type.")
        currency = currency.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Enter a three-letter currency code, for example USD.")
        if kind == "cash" and currency != "USD":
            if not exchange_rate.strip():
                raise ValueError("Enter an exchange rate in USD per 1 receipt currency.")
            exchange_rate = format(positive_decimal(exchange_rate, "Exchange rate", 10), "f")
        else:
            if kind == "cash" and exchange_rate.strip() and positive_decimal(
                exchange_rate, "Exchange rate", 10,
            ) != Decimal("1"):
                raise ValueError("USD cash expenses must use an exchange rate of 1 or leave it blank.")
            exchange_rate = ""
        entry = ManualExpense(
            id=entry_id or str(uuid.uuid4()), kind=kind, date=day,
            description=description.strip(), amount=amount, currency=currency,
            merchant=merchant.strip(), expense_type=expense_type.strip(),
            report_id=report_id, miles=miles, rate=rate,
            rate_source=MILEAGE_RATE_SOURCE if kind == "mileage" else "",
            exchange_rate=exchange_rate,
        )
        with self._lock:
            entries = self._read()
            old = next((row for row in entries if row.id == entry_id), None)
            if entry_id and old is None:
                raise ValueError("This entry no longer exists. Reload the page.")
            if old and old.kind != kind:
                raise ValueError("An entry's transaction type cannot be changed.")
            entries = [entry if row.id == entry.id else row for row in entries]
            if not entry_id:
                entries.append(entry)
            atomic_write_json(self.path, [asdict(row) for row in entries])
        return entry

    def delete(self, entry_id: str) -> None:
        with self._lock:
            entries = self._read()
            if not any(row.id == entry_id for row in entries):
                raise ValueError("This entry no longer exists. Reload the page.")
            atomic_write_json(self.path, [asdict(row) for row in entries if row.id != entry_id])
