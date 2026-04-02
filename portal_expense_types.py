"""Canonical expense type labels from the NI expense portal dropdown (source of truth for LLM + UI)."""

from __future__ import annotations

# Exact strings as shown in the portal; keep order stable for reproducible prompts.
PORTAL_EXPENSE_TYPE_OPTIONS: list[str] = [
    "Airfare",
    "Airfare:Non-NI(Cust/Supp/Partner)",
    "Awards,prizes,gifts:Non-NI Recipients",
    "Bank charges & Cash Advance Fees",
    "Car Rental",
    "Entertainment:Non-NI(Cust/Supp/Partner)",
    "Hotel",
    "Hotel:Non-NI(Cust/Supp/Partner)",
    "Meals",
    "Meals:Non-NI(Cust/Supp/Partner)",
    "Miscellaneous Personnel Expense",
    "Miscellaneous Supplies",
    "Miscellaneous Travel",
    "Office Supplies",
    "Project COGS - Services",
    "Telephone - Cellular",
    "Transportation (Gas,Parking,Cabs& Other)",
    "User Symposium",
]
