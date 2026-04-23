"""Canonical expense type labels from the expense portal dropdown (source of truth for LLM + UI).

On first launch, these generic defaults are used. Users should configure the exact
expense-type labels from their own Oracle portal in Settings → Expense Types.
Custom values are persisted in ~/.expense-automator/settings.json under
``portal_expense_types`` and take precedence when present.
"""

from __future__ import annotations

import json
from pathlib import Path

_SETTINGS_FILE = Path.home() / ".expense-automator" / "settings.json"

# Generic defaults — suitable for most Oracle iExpenses deployments.
_DEFAULT_EXPENSE_TYPE_OPTIONS: list[str] = [
    "Airfare",
    "Awards, Prizes, Gifts",
    "Bank Charges & Cash Advance Fees",
    "Car Rental",
    "Entertainment",
    "Hotel",
    "Meals",
    "Miscellaneous Personnel Expense",
    "Miscellaneous Supplies",
    "Miscellaneous Travel",
    "Office Supplies",
    "Telephone - Cellular",
    "Transportation (Gas, Parking, Cabs & Other)",
]


def _load_custom_expense_types() -> list[str] | None:
    """Load user-configured expense types from settings.json, if present."""
    try:
        if _SETTINGS_FILE.exists():
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            custom = data.get("portal_expense_types")
            if isinstance(custom, list) and custom:
                return [str(t) for t in custom if t]
    except Exception:
        pass
    return None


def get_expense_type_options() -> list[str]:
    """Return the active expense-type options (custom if configured, else defaults)."""
    return _load_custom_expense_types() or list(_DEFAULT_EXPENSE_TYPE_OPTIONS)


# Module-level constant for backward compatibility — always reflects the active list.
PORTAL_EXPENSE_TYPE_OPTIONS: list[str] = get_expense_type_options()
