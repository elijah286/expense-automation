from __future__ import annotations

from datetime import date, datetime
from itertools import combinations
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


def _find_line_item_subset(
    t_amt: float,
    line_items: list[dict[str, Any]],
    tolerance: float,
) -> tuple[tuple[int, ...], float] | None:
    """
    Return (indices, delta) for the subset of line_items whose amounts sum
    closest to t_amt (within tolerance).  Among equally-close subsets, the
    smallest (fewest items) is preferred to avoid spurious over-matching.
    Returns None if no subset qualifies.
    """
    n = len(line_items)
    if n == 0 or n > 14:  # safety cap on 2^n enumeration
        return None
    li_amounts = [_to_amount(item.get("amount")) for item in line_items]
    best: tuple[tuple[int, ...], float] | None = None
    for size in range(1, n + 1):
        for indices in combinations(range(n), size):
            if any(li_amounts[i] is None for i in indices):
                continue
            s = sum(li_amounts[i] for i in indices)  # type: ignore[operator]
            delta = abs(t_amt - s)
            if delta <= tolerance:
                # Prefer smaller delta; break ties by smaller subset size
                if best is None or delta < best[1] or (delta == best[1] and len(indices) < len(best[0])):
                    best = (indices, delta)
    return best


def match_transactions_to_receipts(
    transactions: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    *,
    amount_tolerance: float = 0.5,
    date_window_days: int = 3,
) -> list[dict[str, Any]]:
    """
    Deterministic first-pass matcher:
    - Amount first (total, card_charged_amount, or line_item subset sum)
    - Date within +/- N days
    - Merchant similarity tie-break

    Split-payment support: one receipt can match multiple transactions when
    each transaction amount corresponds to a non-overlapping subset of the
    receipt's line_items (e.g. an Uber receipt charged as two separate card
    transactions — fare+fees and tip+wait-time).

    Greedy assignment: total-matched receipts are blocked after one use;
    line-item-matched receipts may be reused as long as the required
    line_item indices haven't already been consumed by another transaction.
    """
    # Each candidate: (score, txn_index, receipt_key, receipt_obj, matched_line_indices)
    # matched_line_indices = () means matched against the receipt total (blocks whole receipt)
    # matched_line_indices = (i, j, ...) means matched via subset sum (allows reuse)
    candidates: list[tuple[float, int, str, dict[str, Any], tuple[int, ...]]] = []
    txn_ids: list[str] = []

    for ti, txn in enumerate(transactions):
        txn_id = str(txn.get("line_id") or txn.get("transaction_id") or "").strip()
        txn_ids.append(txn_id)
        t_amt = _to_amount(txn.get("amount"))
        t_date = _to_date(txn.get("transaction_date") or txn.get("date"))
        t_merchant = str(txn.get("merchant_name") or txn.get("merchant") or "").strip()
        t_cur = str(txn.get("currency") or "").strip().upper()
        if not t_cur:
            cur_m = re.search(r"[A-Z]{3}", str(txn.get("amount") or ""))
            t_cur = cur_m.group(0) if cur_m else "USD"

        for receipt in receipts:
            r_key = str(receipt.get("source_file") or receipt.get("receipt_id") or "").strip()
            if not r_key:
                continue
            r_amt = _to_amount(receipt.get("matched_amount") or receipt.get("total_amount"))
            if t_amt is None or r_amt is None:
                continue

            delta: float | None = abs(t_amt - r_amt)
            matched_indices: tuple[int, ...] = ()  # () = total match

            if delta > amount_tolerance:
                delta = None  # tentatively no match; try fallbacks below

                # Fallback 1: card_charged_amount (DCC / foreign currency charge)
                cc_amt = _to_amount(receipt.get("card_charged_amount"))
                cc_cur = str(receipt.get("card_charged_currency") or "").strip().upper()
                if cc_amt is not None:
                    if cc_cur and (cc_cur == t_cur or not t_cur):
                        d2 = abs(t_amt - cc_amt)
                        if d2 <= amount_tolerance:
                            delta = d2
                    elif abs(t_amt - cc_amt) <= amount_tolerance:
                        delta = abs(t_amt - cc_amt)

                # Fallback 2: line_item subset sum (split-payment receipts)
                if delta is None:
                    line_items = receipt.get("line_items") or []
                    result = _find_line_item_subset(t_amt, line_items, amount_tolerance)
                    if result is not None:
                        matched_indices, delta = result

                if delta is None:
                    continue  # no amount match found

            # Date check
            r_date = _to_date(receipt.get("receipt_date") or receipt.get("transaction_date"))
            if t_date and r_date:
                corrected = _correct_ddmmyy_misparse(r_date, t_date)
                if corrected is not None:
                    r_date = corrected
            if t_date and r_date and abs((t_date - r_date).days) > date_window_days:
                continue

            amount_score = max(0.0, 1.0 - (delta / max(amount_tolerance, 0.01)))
            date_score = 1.0
            if t_date and r_date:
                date_score = max(0.0, 1.0 - (abs((t_date - r_date).days) / max(1.0, float(date_window_days))))
            merchant_score = _merchant_similarity(t_merchant, receipt.get("vendor"))
            score = 0.55 * amount_score + 0.30 * date_score + 0.15 * merchant_score
            candidates.append((score, ti, r_key, receipt, matched_indices))

    # Phase 2: greedy assignment — highest score first.
    # Receipts matched by total are blocked entirely after one use.
    # Receipts matched via line_items may be reused for non-overlapping subsets.
    candidates.sort(key=lambda c: c[0], reverse=True)
    assigned_txns: set[int] = set()
    whole_receipt_used: set[str] = set()          # receipts consumed by a total match
    used_line_indices: dict[str, set[int]] = {}   # receipt_key → used line_item indices
    txn_result: dict[int, tuple[float, str, dict[str, Any], tuple[int, ...]]] = {}

    for score, ti, r_key, receipt, matched_indices in candidates:
        if ti in assigned_txns:
            continue
        if r_key in whole_receipt_used:
            continue

        if matched_indices:
            # Line-item subset match: only block the specific indices used
            already_used = used_line_indices.get(r_key, set())
            if already_used & set(matched_indices):
                continue  # overlap — these line_items already claimed
            txn_result[ti] = (score, r_key, receipt, matched_indices)
            assigned_txns.add(ti)
            used_line_indices.setdefault(r_key, set()).update(matched_indices)
        else:
            # Total match: block the whole receipt
            txn_result[ti] = (score, r_key, receipt, matched_indices)
            assigned_txns.add(ti)
            whole_receipt_used.add(r_key)

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

        conf_raw, r_key, receipt, matched_indices = txn_result[ti]
        conf = round(float(conf_raw), 4)
        if matched_indices:
            line_items = receipt.get("line_items") or []
            descs = [str(line_items[i].get("description", f"item {i}")) for i in matched_indices]
            reasoning = (
                f"Matched via split-payment line_items ({', '.join(descs)}); "
                "amount, date, and merchant scored."
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
