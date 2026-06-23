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


_TIP_RE = re.compile(r"tip|gratuit", re.IGNORECASE)


def _is_tip_item(desc: object) -> bool:
    return bool(_TIP_RE.search(str(desc or "")))


def _txn_currency(txn: dict[str, Any]) -> str:
    t_cur = str(txn.get("currency") or "").strip().upper()
    if not t_cur:
        m = re.search(r"[A-Z]{3}", str(txn.get("amount") or ""))
        t_cur = m.group(0) if m else ""
    return t_cur


def _best_amount_delta(txn: dict[str, Any], receipt: dict[str, Any], t_amt: float | None) -> float | None:
    """Smallest |txn amount - receipt amount signal| across all plausible signals.

    Besides the receipt total / matched / card-charged amount, this also considers
    the printed subtotal, individual line items, and subset sums of line items
    (e.g. fare + booking fee excluding a separately-charged tip). Corporate portals
    frequently bill only the base charge while the tip posts as a separate
    transaction, so the expense line can equal the subtotal rather than the total.
    Returns None when no signal is available.
    """
    if t_amt is None:
        return None
    t_cur = _txn_currency(txn)
    deltas: list[float] = []

    # Native (document-currency) signals: only trust them when the txn currency
    # is unknown or matches the receipt's own currency.
    nat_cur = str(receipt.get("currency") or "").strip().upper()
    if not t_cur or not nat_cur or nat_cur == t_cur:
        for key in ("matched_amount", "total_amount", "subtotal"):
            a = _to_amount(receipt.get(key))
            if a is not None:
                deltas.append(abs(t_amt - a))
        items = receipt.get("line_items")
        if isinstance(items, list):
            all_amts: list[float] = []
            non_tip_amts: list[float] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                a = _to_amount(it.get("amount"))
                if a is None:
                    continue
                all_amts.append(a)
                deltas.append(abs(t_amt - a))
                if not _is_tip_item(it.get("description")):
                    non_tip_amts.append(a)
            if all_amts:
                deltas.append(abs(t_amt - sum(all_amts)))
            if non_tip_amts and len(non_tip_amts) != len(all_amts):
                deltas.append(abs(t_amt - sum(non_tip_amts)))

    # Card-charged signal carries its own currency.
    cc_amt = _to_amount(receipt.get("card_charged_amount"))
    cc_cur = str(receipt.get("card_charged_currency") or "").strip().upper()
    if cc_amt is not None and (not t_cur or not cc_cur or cc_cur == t_cur):
        deltas.append(abs(t_amt - cc_amt))

    return min(deltas) if deltas else None


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
            if t_amt is None:
                continue
            delta = _best_amount_delta(txn, receipt, t_amt)
            if delta is None or delta > amount_tolerance:
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
