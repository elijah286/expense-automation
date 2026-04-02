"""
Normalize scraped portal data and Step 6 review table text for exact matching (no LLM).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any


def normalize_merchant(name: str) -> str:
    s = re.sub(r"\s+", " ", (name or "").strip()).upper()
    return s


_NUM_SEP_DATE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$")


def parse_to_iso_date(s: str) -> str:
    """Parse common receipt/portal date strings to ``YYYY-MM-DD``. Returns '' if unknown."""
    t = (s or "").strip()
    if not t:
        return ""
    t_compact = re.sub(r"\s+", " ", t)
    for fmt in (
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d.%m.%y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d-%b-%y",
        "%d-%B-%y",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%d/%m/%Y",
        "%d/%m/%y",
    ):
        try:
            return datetime.strptime(t_compact, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = _NUM_SEP_DATE.match(t_compact)
    if m:
        a, b, ys = int(m.group(1)), int(m.group(2)), m.group(3)
        if len(ys) == 4:
            y = int(ys)
        else:
            y = 2000 + int(ys)
        if a > 12:
            day, month = a, b
        elif b > 12:
            month, day = a, b
        else:
            # Both ≤ 12: treat as U.S. month/day (matches Oracle / scraped card lines).
            month, day = a, b
        try:
            return datetime(y, month, day).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def format_date_for_ui(s: str) -> str:
    """User-visible date: ISO ``YYYY-MM-DD`` when parseable, otherwise the stripped original."""
    t = (s or "").strip()
    if not t:
        return ""
    iso = parse_to_iso_date(t)
    return iso if iso else t


def normalize_date_for_match(s: str) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    iso = parse_to_iso_date(t)
    if iso:
        return iso
    return re.sub(r"\s+", " ", t).upper()


def _parse_amount_currency_token(s: str) -> tuple[str, str]:
    """Split '17.47 USD' or '39,98 EUR' into normalized amount + currency code."""
    raw = re.sub(r"\s+", " ", (s or "").strip())
    if not raw:
        return "", ""
    parts = raw.rsplit(None, 1)
    if len(parts) == 2 and re.match(r"^[A-Z]{3}$", parts[1].upper()):
        num, cur = parts[0], parts[1].upper()
    else:
        m = re.search(r"([\-+]?[\d,]+\.?\d*)\s*([A-Za-z]{3})\s*$", raw)
        if m:
            num, cur = m.group(1), m.group(2).upper()
        else:
            num, cur = raw, ""
    num_clean = num.replace(",", "").replace(" ", "")
    try:
        amt = f"{abs(float(num_clean)):.2f}"
    except ValueError:
        amt = num_clean.upper()
    return amt, cur


def amount_currency_key(amount_field: str, currency_field: str) -> str:
    """Build '12.34|USD' from separate cache fields or single combined cell."""
    a = (amount_field or "").strip()
    c = (currency_field or "").strip().upper()
    if c and a and not re.search(r"[A-Za-z]{3}", a):
        return f"{_parse_amount_currency_token(a + ' ' + c)[0]}|{c or _parse_amount_currency_token(a + ' ' + c)[1]}"
    amt, cur = _parse_amount_currency_token(a)
    if cur:
        return f"{amt}|{cur}"
    if c:
        return f"{amt}|{c}"
    return f"{amt}|"


def signature_from_cached_line(line: dict[str, Any]) -> tuple[str, str, str]:
    """(merchant_norm, date_norm, amount_currency_key) for exact matching."""
    m = normalize_merchant(str(line.get("merchant_name", "") or ""))
    d = normalize_date_for_match(str(line.get("transaction_date", "") or ""))
    ak = amount_currency_key(
        str(line.get("amount", "") or ""),
        str(line.get("currency", "") or ""),
    )
    return (m, d, ak)


def signature_from_step6_row(date_cell: str, receipt_amount_cell: str, merchant_cell: str) -> tuple[str, str, str]:
    m = normalize_merchant(merchant_cell)
    d = normalize_date_for_match(date_cell)
    amt, cur = _parse_amount_currency_token(receipt_amount_cell)
    ak = f"{amt}|{cur}" if amt or cur else ""
    return (m, d, ak)
