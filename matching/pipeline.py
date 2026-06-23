from __future__ import annotations

from datetime import date, datetime
import re
from typing import Any


def _to_date(value: object) -> date | None:
    s = str(value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _to_amount(value: object) -> float | None:
    s = str(value or "").strip().replace(",", "")
    if not s:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _norm_tokens(value: object) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}", str(value or "").lower()))


def _merchant_similarity(a: object, b: object) -> float:
    ta = _norm_tokens(a)
    tb = _norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _correct_ddmmyy_misparse(receipt_d: date, txn_d: date) -> date | None:
    """Detect when an LLM returned DD.MM.YY as YYYY-MM-DD and correct it."""
    if abs((txn_d - receipt_d).days) <= 180:
        return None
    candidate_day = receipt_d.year % 100
    candidate_year = 2000 + receipt_d.day
    try:
        corrected = date(candidate_year, receipt_d.month, candidate_day)
    except ValueError:
        return None
    if abs((txn_d - corrected).days) <= 7:
        return corrected
    return None


def _confidence_level(c: float) -> str:
    if c >= 0.85:
        return "high"
    if c >= 0.60:
        return "medium"
    return "low"


def match_transactions_to_receipts(
    transactions: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    *,
    amount_tolerance: float = 0.5,
    date_window_days: int = 3,
) -> list[dict[str, Any]]:
    """
    Deterministic first-pass matcher:
    - Amount first
    - Date within +/- N days
    - Merchant similarity tie-break

    Uses greedy assignment: each receipt is assigned to at most one
    transaction (the one with the highest score).
    """
    # Phase 1: build scored candidates for every (txn, receipt) pair.
    # Each entry: (score, txn_index, receipt_key, receipt_obj, date_overridden)
    candidates: list[tuple[float, int, str, dict[str, Any], bool]] = []
    txn_ids: list[str] = []

    for ti, txn in enumerate(transactions):
        txn_id = str(txn.get("line_id") or txn.get("transaction_id") or "").strip()
        txn_ids.append(txn_id)
        t_amt = _to_amount(txn.get("amount"))
        t_date = _to_date(txn.get("transaction_date") or txn.get("date"))
        t_merchant = str(txn.get("merchant_name") or txn.get("merchant") or "").strip()
        for receipt in receipts:
            r_amt = _to_amount(receipt.get("matched_amount") or receipt.get("total_amount"))
            if t_amt is None or r_amt is None:
                continue
            delta = abs(t_amt - r_amt)
            if delta > amount_tolerance:
                cc_amt = _to_amount(receipt.get("card_charged_amount"))
                cc_cur = str(receipt.get("card_charged_currency") or "").strip().upper()
                t_cur = str(txn.get("currency") or "").strip().upper()
                amt_field = str(txn.get("amount") or "")
                if not t_cur:
                    cur_match = re.search(r"[A-Z]{3}", amt_field)
                    t_cur = cur_match.group(0) if cur_match else "USD"
                if cc_amt is not None and cc_cur and (cc_cur == t_cur or not t_cur):
                    delta = abs(t_amt - cc_amt)
                    if delta > amount_tolerance:
                        continue
                elif cc_amt is not None and abs(t_amt - cc_amt) <= amount_tolerance:
                    delta = abs(t_amt - cc_amt)
                else:
                    continue
            r_date = _to_date(receipt.get("receipt_date") or receipt.get("transaction_date"))
            if t_date and r_date:
                corrected = _correct_ddmmyy_misparse(r_date, t_date)
                if corrected is not None:
                    r_date = corrected
            date_out_of_window = bool(
                t_date and r_date and abs((t_date - r_date).days) > date_window_days
            )
            # An exact amount match (near-zero difference) is almost never a
            # coincidence, so a wrong receipt date — typically an OCR/parse error
            # such as the wrong year — should not discard the candidate. Keep it as
            # a review-band match instead of dropping it.
            amount_is_exact = delta <= 0.05
            if date_out_of_window and not amount_is_exact:
                continue
            merchant_score = _merchant_similarity(t_merchant, receipt.get("vendor"))
            if date_out_of_window:
                # Review band [0.60, 0.69): persisted, but flagged for review.
                score = min(0.69, 0.60 + 0.09 * merchant_score)
            else:
                amount_score = max(0.0, 1.0 - (delta / max(amount_tolerance, 0.01)))
                date_score = 1.0
                if t_date and r_date:
                    date_score = max(0.0, 1.0 - (abs((t_date - r_date).days) / max(1.0, float(date_window_days))))
                score = 0.55 * amount_score + 0.30 * date_score + 0.15 * merchant_score
            r_key = str(receipt.get("source_file") or receipt.get("receipt_id") or "").strip()
            if r_key:
                candidates.append((score, ti, r_key, receipt, date_out_of_window))

    # Phase 2: greedy assignment — highest score first, each receipt used once.
    candidates.sort(key=lambda c: c[0], reverse=True)
    assigned_txns: set[int] = set()
    assigned_receipts: set[str] = set()
    txn_result: dict[int, tuple[float, str, dict[str, Any], bool]] = {}

    for score, ti, r_key, receipt, date_overridden in candidates:
        if ti in assigned_txns or r_key in assigned_receipts:
            continue
        txn_result[ti] = (score, r_key, receipt, date_overridden)
        assigned_txns.add(ti)
        assigned_receipts.add(r_key)

    # Phase 3: build output for every transaction.
    output: list[dict[str, Any]] = []
    for ti in range(len(transactions)):
        txn_id = txn_ids[ti]
        if ti not in txn_result:
            output.append(
                {
                    "transaction_id": txn_id,
                    "receipt_id": None,
                    "confidence": 0.15,
                    "confidence_level": "low",
                    "reasoning": "No receipt candidate passed amount/date filters.",
                }
            )
            continue

        conf_raw, r_key, receipt, date_overridden = txn_result[ti]
        conf = round(float(conf_raw), 4)
        if date_overridden:
            reasoning = (
                "Exact amount match; receipt date is outside the expected window "
                "(likely OCR/parse error) — flagged for review."
            )
        else:
            reasoning = "Matched by amount tolerance first, date proximity second, merchant similarity third."
        output.append(
            {
                "transaction_id": txn_id,
                "receipt_id": r_key or None,
                "confidence": conf,
                "confidence_level": _confidence_level(conf),
                "reasoning": reasoning,
            }
        )
    return output
