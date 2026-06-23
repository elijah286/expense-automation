"""
Batch LLM: map expense portal lines to receipt files using cached vision analyses.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from browser_automation import (
    build_openai_client,
    native_receipt_total_numeric,
    normalize_currency_code,
    openai_tls_troubleshooting_hint,
)
from expense_match_normalize import parse_to_iso_date


def _normalize_json_text(raw: str) -> str:
    t = raw.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _coerce_llm_receipt_path(raw: str | None) -> str:
    """Strip whitespace and optional file:// prefix from model output."""
    if raw is None:
        return ""
    s = str(raw).strip().strip('"').strip("'")
    if not s:
        return ""
    if s.lower().startswith("file:"):
        parsed = urlparse(s)
        path = unquote(parsed.path or "")
        if os.name == "nt" and path.startswith("/") and len(path) >= 3 and path[2] == ":":
            path = path[1:]
        s = path.strip()
    return s.strip()


_PHOTOS_EXPORT_UUID_RE = re.compile(
    r"uuid=([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)


def _photos_export_uuid(path_str: str) -> str | None:
    """Apple Photos NSItemProvider temp paths embed ``uuid=XXXXXXXX-....`` before the filename."""
    m = _PHOTOS_EXPORT_UUID_RE.search(path_str or "")
    return m.group(1) if m else None


def normalize_best_receipt_path(br_raw: str | None, valid_files: set[str]) -> str:
    """
    Map LLM best_receipt to a canonical path in valid_files.

    The model must pick a receipt we already analyzed; paths may still differ by
    normalization, case (macOS), or by returning only a basename. When the model
    clearly names a file we have, resolve it instead of dropping the match.
    """
    if not valid_files:
        return ""
    br = _coerce_llm_receipt_path(br_raw)
    if not br:
        return ""
    if br in valid_files:
        return br

    exp_br = os.path.expanduser(br)
    n_br = os.path.normpath(exp_br)
    for vf in valid_files:
        if os.path.normpath(os.path.expanduser(vf)) == n_br:
            return vf

    key_br = os.path.normcase(n_br)
    for vf in valid_files:
        vf_n = os.path.normcase(os.path.normpath(os.path.expanduser(vf)))
        if vf_n == key_br:
            return vf

    try:
        br_p = Path(exp_br)
        if br_p.is_file():
            br_res = br_p.resolve()
            for vf in valid_files:
                vf_p = Path(os.path.expanduser(vf))
                try:
                    if vf_p.is_file() and vf_p.resolve() == br_res:
                        return vf
                except OSError:
                    continue
    except OSError:
        pass

    base = Path(br).name
    if not base:
        return ""
    hits = [vf for vf in valid_files if Path(vf).name == base]
    if not hits:
        base_lower = base.lower()
        hits = [vf for vf in valid_files if Path(vf).name.lower() == base_lower]
    if not hits:
        return ""
    u_br = _photos_export_uuid(exp_br)
    if u_br:
        uuid_hits = [vf for vf in hits if u_br in vf]
        if len(uuid_hits) == 1:
            return uuid_hits[0]
        if not uuid_hits:
            return ""
        hits = uuid_hits
    # If the model's path still exists on disk, only map to a hit with the same inode.
    # Otherwise basename-only resolution can pair the wrong receipt when duplicate filenames exist
    # (e.g. multiple Apple Photos export paths for the same basename).
    try:
        br_p = Path(exp_br)
        if br_p.is_file():
            br_res = br_p.resolve()
            for vf in hits:
                vf_p = Path(os.path.expanduser(vf))
                try:
                    if vf_p.is_file() and vf_p.resolve() == br_res:
                        return vf
                except OSError:
                    continue
            return ""
    except OSError:
        pass

    if len(hits) == 1:
        return hits[0]

    n_br_slash = exp_br.replace("\\", "/").lower().rstrip("/")
    if n_br_slash and "/" in n_br_slash:
        narrowed: list[str] = []
        for vf in hits:
            vf_n = os.path.expanduser(vf).replace("\\", "/").lower().rstrip("/")
            if vf_n == n_br_slash or vf_n.endswith(n_br_slash) or vf_n.endswith("/" + n_br_slash.lstrip("/")):
                narrowed.append(vf)
        if len(narrowed) == 1:
            return narrowed[0]

    return ""


def _candidate_paths_from_br_raw(br_raw: object, valid_files: set[str]) -> list[str]:
    br = _coerce_llm_receipt_path(br_raw)
    if not br:
        return []
    base = Path(br).name
    if not base:
        return []
    key = base.lower()
    return [vf for vf in valid_files if Path(vf).name.lower() == key]


def _paths_mentioned_in_reason(reason: str, valid_files: set[str]) -> list[str]:
    """Find receipt paths whose basename appears in the model's reason string (disambiguation)."""
    if not reason or not valid_files:
        return []
    rl = reason.lower()
    out: list[str] = []
    seen: set[str] = set()
    for vf in valid_files:
        bn = Path(vf).name
        if len(bn) < 5:
            continue
        if bn.lower() in rl:
            if vf not in seen:
                seen.add(vf)
                out.append(vf)
    return out


def _token_jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]{2,}", (a or "").lower()))
    tb = set(re.findall(r"[a-z0-9]{2,}", (b or "").lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


MIN_MATCH_CONFIDENCE = 0.40
REVIEW_CONFIDENCE_THRESHOLD = 0.70


def _confidence_float(value: object, default: float = 0.0) -> float:
    try:
        f = float(value) if value is not None else default
    except (TypeError, ValueError):
        f = default
    return max(0.0, min(1.0, f))


def _parse_iso_to_date(value: object) -> date | None:
    iso = parse_to_iso_date(str(value or "").strip())
    if not iso:
        return None
    try:
        return datetime.strptime(iso, "%Y-%m-%d").date()
    except ValueError:
        return None


def _correct_ddmmyy_misparse(receipt_d: date, txn_d: date) -> date | None:
    """Detect when an LLM returned DD.MM.YY as YYYY-MM-DD.

    European receipts commonly print dates as DD.MM.YY (e.g. "19.03.26" for
    19-Mar-2026). LLMs sometimes return this as "2019-03-26", treating the
    2-digit day as a year prefix and the 2-digit year as the day. When the
    resulting gap is implausibly large, swap day↔year and check if the
    corrected date is close to the transaction date.
    """
    gap = abs((txn_d - receipt_d).days)
    if gap <= 180:
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


def _date_gap_days(line_date: object, receipt_date: object) -> int | None:
    ld = _parse_iso_to_date(line_date)
    rd = _parse_iso_to_date(receipt_date)
    if ld is None or rd is None:
        return None
    if ld is not None and rd is not None:
        corrected = _correct_ddmmyy_misparse(rd, ld)
        if corrected is not None:
            rd = corrected
    return abs((ld - rd).days)


def _disambiguate_receipt_candidates(
    hits: list[str],
    line: dict[str, Any],
    analyses_by_path: dict[str, dict[str, Any]],
    reason: str,
) -> str:
    """Pick one path when basename or LLM output is ambiguous; prefer amount fit, then reason text, then vendor."""
    if not hits:
        return ""
    if len(hits) == 1:
        return hits[0]
    lm = str(line.get("merchant_name") or "")
    reason_l = (reason or "").lower()
    scored: list[tuple[float, str]] = []
    for h in hits:
        a = analyses_by_path.get(h) or {}
        ven = str(a.get("vendor") or "")
        score = 0.0
        if line_and_receipt_amounts_align(line, a):
            score += 2000.0
        bn = Path(h).name.lower()
        if bn and bn in reason_l:
            score += 800.0
        if h.lower() in reason_l:
            score += 400.0
        score += 150.0 * _token_jaccard(lm, ven)
        scored.append((score, h))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return scored[0][1]


def finalize_best_receipt_path(
    br_raw: object,
    reason: str,
    valid_files: set[str],
    line: dict[str, Any] | None,
    analyses: list[dict[str, Any]],
) -> str:
    """
    Map model output to a canonical source_file path. When the model gives an exact path, use it; when
    basename-only or ambiguous, disambiguate using the reason string, amount alignment, and merchant/vendor.
    """
    direct = normalize_best_receipt_path(br_raw, valid_files)
    if direct:
        return direct

    by_path = {
        str(a.get("source_file", "")).strip(): a
        for a in analyses
        if str(a.get("source_file", "")).strip()
    }

    cand: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        if p and p in valid_files and p not in seen:
            seen.add(p)
            cand.append(p)

    for h in _candidate_paths_from_br_raw(br_raw, valid_files):
        add(h)
    for h in _paths_mentioned_in_reason(reason, valid_files):
        add(h)

    # Prefer Photos export UUID when the model path includes one, but do **not** fail closed if no
    # analyzed path contains that UUID. The model often echoes a stale NSItemProvider temp path while
    # `source_file` in analyses is the stable re-import path (same basename, different uuid= segment).
    # In that case we fall through and disambiguate by amount / merchant like any other duplicate basename.
    br_s = _coerce_llm_receipt_path(str(br_raw) if br_raw is not None else "")
    u_llm = _photos_export_uuid(br_s)
    if u_llm:
        filt = [c for c in cand if u_llm in c]
        if len(filt) == 1:
            return filt[0]
        if filt:
            cand = filt
        # len(filt)==0: keep `cand` (basename / reason matches) and resolve below

    if not cand:
        return ""

    if len(cand) == 1:
        return cand[0]

    if line is None:
        return cand[0]

    aligned = [c for c in cand if line_and_receipt_amounts_align(line, by_path.get(c) or {})]
    if len(aligned) == 1:
        return aligned[0]
    if len(aligned) > 1:
        return _disambiguate_receipt_candidates(aligned, line, by_path, reason)

    return _disambiguate_receipt_candidates(cand, line, by_path, reason)


def _compact_receipt_for_prompt(a: dict[str, Any]) -> dict[str, Any]:
    root_cur = normalize_currency_code(a.get("currency")) or None
    li_compact: list[dict[str, Any]] = []
    raw_li = a.get("line_items")
    if isinstance(raw_li, list):
        for it in raw_li[:12]:
            if not isinstance(it, dict):
                continue
            ic = normalize_currency_code(it.get("currency")) or root_cur
            li_entry: dict[str, Any] = {
                "amount": it.get("amount"),
                "currency": ic,
                "description": (str(it.get("description", "") or ""))[:80],
            }
            eu_item = it.get("estimated_usd")
            try:
                eu_f = float(eu_item) if eu_item is not None else None
            except (TypeError, ValueError):
                eu_f = None
            if eu_f is not None:
                li_entry["estimated_usd"] = round(eu_f, 2)
            li_compact.append(li_entry)
    cc_amt = a.get("card_charged_amount")
    cc_cur = normalize_currency_code(a.get("card_charged_currency")) or None
    try:
        cc_out = float(cc_amt) if cc_amt is not None and cc_cur else None
    except (TypeError, ValueError):
        cc_out = None
    est_raw = a.get("estimated_usd_total")
    try:
        est_out = float(est_raw) if est_raw is not None and str(est_raw).strip() != "" else None
    except (TypeError, ValueError):
        est_out = None
    fx_note = str(a.get("estimated_usd_fx_note", "") or "").strip()[:200]

    out: dict[str, Any] = {
        "source_file": str(a.get("source_file", "") or ""),
        "vendor": a.get("vendor"),
        "receipt_date": a.get("receipt_date"),
        "currency": root_cur,
        "matched_amount": a.get("matched_amount"),
        "total_amount": a.get("total_amount"),
        "line_items": li_compact,
        "confidence": a.get("confidence"),
        "notes": (str(a.get("notes", "") or ""))[:400],
    }
    if est_out is not None:
        out["estimated_usd_total"] = round(est_out, 2)
    if fx_note:
        out["estimated_usd_fx_note"] = fx_note
    if cc_out is not None and cc_cur:
        out["card_charged_amount"] = cc_out
        out["card_charged_currency"] = cc_cur
    return out


def _float_or_none(val: object | None) -> float | None:
    try:
        if val is None:
            return None
        s = str(val).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


# Rough USD-per-unit (order-of-magnitude; wide bands for plausibility only).
_ROUGH_UNIT_TO_USD: dict[str, float] = {
    "CHF": 1.18,
    "GBP": 1.28,
    "EUR": 1.09,
    "JPY": 0.0068,
    "CAD": 0.72,
    "AUD": 0.64,
    "SEK": 0.096,
    "NOK": 0.092,
    "DKK": 0.146,
    "PLN": 0.25,
    "CNY": 0.14,
    "INR": 0.012,
    "MXN": 0.056,
    "BRL": 0.18,
    "NZD": 0.59,
    "SGD": 0.74,
    "HKD": 0.128,
    "AED": 0.27,
    "SAR": 0.27,
}


def _plausible_usd_band_for_foreign(native_amt: float, cur: str) -> tuple[float, float]:
    unit = _ROUGH_UNIT_TO_USD.get((cur or "").upper(), None)
    if unit is None:
        unit = 1.0
    mid = native_amt * unit
    lo, hi = mid * 0.42, mid * 2.75
    return lo, hi


def _folio_est_usd_plausible(*, native_amt: float, doc_cur: str, est_usd: float | None) -> bool:
    """True if estimated_usd_total is compatible with the printed folio total (catch bogus ~192 USD on a ~5 CHF slip)."""
    if est_usd is None or native_amt <= 0 or not doc_cur or doc_cur.upper() == "USD":
        return True
    lo, hi = _plausible_usd_band_for_foreign(native_amt, doc_cur)
    return lo <= est_usd <= hi


def _native_total_from_analysis(analysis: dict[str, Any]) -> tuple[float | None, str]:
    return native_receipt_total_numeric(analysis)


def _line_rough_usd_equiv(line: dict[str, Any]) -> float | None:
    """Posted amount as USD for validation (expense portal is usually USD)."""
    amt = _float_or_none(line.get("amount"))
    if amt is None:
        return None
    cur = normalize_currency_code(line.get("currency")) or "USD"
    if not cur or cur == "USD":
        return amt
    unit = _ROUGH_UNIT_TO_USD.get(cur, 1.0)
    return amt * unit


def _usd_amount_tolerance(line_usd: float) -> float:
    return max(1.05, 0.006 * max(abs(line_usd), 1.0))


def line_and_receipt_amounts_align(line: dict[str, Any], analysis: dict[str, Any]) -> bool:
    """
    Deterministic guard: reject LLM picks where no USD/card/native signal plausibly matches the expense line.
    Stops hallucinated estimated_usd_total on unrelated small-amount receipts.
    """
    line_usd = _line_rough_usd_equiv(line)
    if line_usd is None:
        return True

    nat, doc_cur = _native_total_from_analysis(analysis)
    cc_amt = _float_or_none(analysis.get("card_charged_amount"))
    cc_cur = normalize_currency_code(analysis.get("card_charged_currency"))
    est = _float_or_none(analysis.get("estimated_usd_total"))
    tol = _usd_amount_tolerance(line_usd)

    if cc_cur == "USD" and cc_amt is not None and abs(cc_amt - line_usd) <= tol:
        return True

    line_amt_raw = _float_or_none(line.get("amount"))
    if cc_amt is not None and line_amt_raw is not None and abs(cc_amt - line_amt_raw) <= tol:
        return True

    if doc_cur == "USD" and nat is not None and abs(nat - line_usd) <= tol:
        return True

    if nat is not None and doc_cur and doc_cur != "USD":
        lo, hi = _plausible_usd_band_for_foreign(nat, doc_cur)
        if lo <= line_usd <= hi:
            return True
        if est is not None and _folio_est_usd_plausible(native_amt=nat, doc_cur=doc_cur, est_usd=est):
            if abs(est - line_usd) <= tol:
                return True

    raw_li = analysis.get("line_items")
    if isinstance(raw_li, list) and est is not None:
        for it in raw_li[:24]:
            if not isinstance(it, dict):
                continue
            eu = _float_or_none(it.get("estimated_usd"))
            if eu is None:
                continue
            li_amt = _float_or_none(it.get("amount"))
            lic = normalize_currency_code(it.get("currency")) or doc_cur
            if li_amt is not None and lic and lic != "USD":
                if not _folio_est_usd_plausible(native_amt=li_amt, doc_cur=lic, est_usd=eu):
                    continue
            if abs(eu - line_usd) <= tol:
                return True

    if nat is None and cc_amt is None and est is None:
        return True

    return False


def _enforce_amount_alignment_on_match(
    line: dict[str, Any],
    analyses_by_path: dict[str, dict[str, Any]],
    result: dict[str, Any],
) -> dict[str, Any]:
    br = result.get("best_receipt")
    if not br:
        return result
    path = str(br).strip()
    picked = analyses_by_path.get(path)
    if picked is None:
        return {
            "best_receipt": None,
            "confidence": min(float(result.get("confidence") or 0.0), 0.2),
            "reason": "Rejected match: receipt path not found in analysis set.",
        }
    if line_and_receipt_amounts_align(line, picked):
        return result
    prev = str(result.get("reason", "") or "")[:220]
    return {
        "best_receipt": None,
        "confidence": min(float(result.get("confidence") or 0.0), 0.22),
        "reason": (
            "Rejected match: receipt amounts / USD estimates do not plausibly match this expense line "
            f"(model had: {prev})"
        ),
    }


def _line_amount_matches_card_charge(line: dict[str, Any], analysis: dict[str, Any], tolerance: float = 1.0) -> bool:
    """True when the raw line amount numerically matches the card charged amount (likely FX / mislabelled currency)."""
    line_amt = _float_or_none(line.get("amount"))
    cc_amt = _float_or_none(analysis.get("card_charged_amount"))
    if line_amt is None or cc_amt is None:
        return False
    return abs(line_amt - cc_amt) <= tolerance


def _amount_match_strength(line: dict[str, Any], analysis: dict[str, Any]) -> str:
    """
    Classify how strongly the receipt amounts corroborate this expense line.

    Returns one of:
      "exact"    - an explicit same-currency / card-charged equality (high confidence).
                   A wrong receipt DATE on an exact-amount match is almost always an
                   OCR/parse error rather than a different transaction, so the date
                   penalty is softened to a review (not a rejection) for this tier.
      "estimate" - only a USD-estimate / foreign-currency-band signal aligns (medium).
                   A coincidental same amount is more plausible here, so keep strict
                   date gating.
      "none"     - no positive amount signal aligns.

    Mirrors the branches of line_and_receipt_amounts_align().
    """
    line_usd = _line_rough_usd_equiv(line)
    if line_usd is None:
        return "none"

    nat, doc_cur = _native_total_from_analysis(analysis)
    cc_amt = _float_or_none(analysis.get("card_charged_amount"))
    cc_cur = normalize_currency_code(analysis.get("card_charged_currency"))
    est = _float_or_none(analysis.get("estimated_usd_total"))
    tol = _usd_amount_tolerance(line_usd)

    # --- Explicit equality (exact) ---
    if cc_cur == "USD" and cc_amt is not None and abs(cc_amt - line_usd) <= tol:
        return "exact"
    line_amt_raw = _float_or_none(line.get("amount"))
    if cc_amt is not None and line_amt_raw is not None and abs(cc_amt - line_amt_raw) <= tol:
        return "exact"
    if doc_cur == "USD" and nat is not None and abs(nat - line_usd) <= tol:
        return "exact"

    # --- Estimate / foreign-currency band (medium) ---
    if nat is not None and doc_cur and doc_cur != "USD":
        lo, hi = _plausible_usd_band_for_foreign(nat, doc_cur)
        if lo <= line_usd <= hi:
            return "estimate"
        if est is not None and _folio_est_usd_plausible(native_amt=nat, doc_cur=doc_cur, est_usd=est):
            if abs(est - line_usd) <= tol:
                return "estimate"

    raw_li = analysis.get("line_items")
    if isinstance(raw_li, list) and est is not None:
        for it in raw_li[:24]:
            if not isinstance(it, dict):
                continue
            eu = _float_or_none(it.get("estimated_usd"))
            if eu is None:
                continue
            li_amt = _float_or_none(it.get("amount"))
            lic = normalize_currency_code(it.get("currency")) or doc_cur
            if li_amt is not None and lic and lic != "USD":
                if not _folio_est_usd_plausible(native_amt=li_amt, doc_cur=lic, est_usd=eu):
                    continue
            if abs(eu - line_usd) <= tol:
                return "estimate"

    return "none"


def _date_confidence_from_gap(gap: int | None) -> float:
    """Per-field date confidence in [0,1] from the absolute day gap."""
    if gap is None:
        return 0.5
    if gap <= 2:
        return 1.0
    if gap <= 4:
        return 0.85
    if gap <= 14:
        return 0.6
    if gap <= 30:
        return 0.45
    return 0.15


def _amount_confidence_from_strength(strength: str) -> float:
    """Per-field amount confidence in [0,1] from the match strength tier."""
    return {"exact": 0.95, "estimate": 0.7}.get(strength, 0.1)


def _apply_match_quality_policy(
    line: dict[str, Any],
    analyses_by_path: dict[str, dict[str, Any]],
    result: dict[str, Any],
) -> dict[str, Any]:
    """
    Post-LLM quality policy:
    - Amount alignment is mandatory.
    - Date (within ~1-2 days) adjusts confidence.
    - <0.70 is never matched; 0.70-0.89 is a review candidate.
    """
    gated = _enforce_amount_alignment_on_match(line, analyses_by_path, result)
    path = str(gated.get("best_receipt") or "").strip()
    conf = _confidence_float(gated.get("confidence"), default=0.0)
    reason = str(gated.get("reason") or "").strip()
    if not path:
        return {
            "best_receipt": None,
            "confidence": min(conf, MIN_MATCH_CONFIDENCE - 0.01),
            "reason": reason[:500],
        }
    analysis = analyses_by_path.get(path) or {}
    if not analysis:
        return {
            "best_receipt": None,
            "confidence": min(conf, MIN_MATCH_CONFIDENCE - 0.01),
            "reason": "Rejected match: selected receipt is not in the analysis snapshot.",
        }

    cap = 0.99
    floor = 0.0
    policy_notes: list[str] = []

    strength = _amount_match_strength(line, analysis)
    amount_exact = strength == "exact"

    gap = _date_gap_days(line.get("transaction_date"), analysis.get("receipt_date"))
    if gap is None:
        if amount_exact:
            # Exact amount but no usable receipt date (OCR failed / missing).
            # Surface for review rather than penalising toward rejection.
            cap = min(cap, REVIEW_CONFIDENCE_THRESHOLD - 0.05)
            floor = max(floor, MIN_MATCH_CONFIDENCE + 0.10)
            policy_notes.append("date missing; amount exact (likely OCR error)")
        else:
            cap = min(cap, 0.88)
            policy_notes.append("date missing/unparseable")
    elif gap <= 2:
        pass
    elif gap <= 4:
        cap = min(cap, 0.85)
        policy_notes.append(f"date gap {gap}d")
    elif _line_amount_matches_card_charge(line, analysis):
        cap = min(cap, REVIEW_CONFIDENCE_THRESHOLD - 0.05)
        policy_notes.append(f"date gap {gap}d (card-charge amount match)")
    elif gap <= 14:
        cap = min(cap, 0.55)
        policy_notes.append(f"date gap {gap}d")
    elif gap <= 30:
        cap = min(cap, 0.45)
        policy_notes.append(f"date gap {gap}d")
    elif amount_exact:
        # Large date gap (e.g. wrong year from OCR) but the amount matches exactly.
        # Don't reject on date alone — keep it as a review candidate so it stays
        # attributable to the correct line.
        cap = min(cap, REVIEW_CONFIDENCE_THRESHOLD - 0.05)
        floor = max(floor, MIN_MATCH_CONFIDENCE + 0.10)
        policy_notes.append(f"date gap {gap}d but amount exact; receipt date likely OCR error")
    else:
        cap = min(cap, MIN_MATCH_CONFIDENCE - 0.01)
        policy_notes.append(f"date gap {gap}d too large")

    field_confidence = {
        "amount": round(_amount_confidence_from_strength(strength), 4),
        "date": round(_date_confidence_from_gap(gap), 4),
        "merchant": round(_token_jaccard(str(line.get("merchant_name") or ""), str(analysis.get("vendor") or "")), 4),
    }

    conf = min(conf, cap)
    # An exact amount match must not be rejected purely because the model assigned
    # a low confidence to a wrong/implausible receipt date. Floor it into the
    # review band (still below auto-approve) so it remains attributable.
    if floor:
        conf = max(conf, min(floor, cap))
    if conf < MIN_MATCH_CONFIDENCE:
        policy_text = ", ".join(policy_notes) if policy_notes else "weak date signals"
        base = f"Rejected match: confidence policy (<{MIN_MATCH_CONFIDENCE:.2f}) due to {policy_text}. "
        return {
            "best_receipt": None,
            "confidence": min(conf, MIN_MATCH_CONFIDENCE - 0.01),
            "reason": (base + reason)[:500],
            "field_confidence": field_confidence,
        }
    if conf < REVIEW_CONFIDENCE_THRESHOLD:
        policy_text = ", ".join(policy_notes) if policy_notes else "partial evidence"
        prefix = (
            f"Review recommended: confidence {conf:.2f} (<{REVIEW_CONFIDENCE_THRESHOLD:.2f}) "
            f"because {policy_text}. "
        )
        reason = (prefix + reason)[:500]

    return {
        "best_receipt": path,
        "confidence": conf,
        "reason": reason[:500],
        "field_confidence": field_confidence,
    }


def _compact_line_for_prompt(line: dict[str, Any]) -> dict[str, Any]:
    lc = normalize_currency_code(line.get("currency")) or None
    return {
        "line_id": str(line.get("line_id", "") or ""),
        "merchant_name": line.get("merchant_name"),
        "transaction_date": line.get("transaction_date"),
        "currency": lc,
        "amount": line.get("amount"),
    }


def build_receipt_match_prompt(lines: list[dict[str, Any]], analyses: list[dict[str, Any]]) -> str:
    line_objs = [_compact_line_for_prompt(L) for L in lines]
    rec_objs = [_compact_receipt_for_prompt(a) for a in analyses if str(a.get("source_file", "")).strip()]
    return (
        "You match corporate credit card expense lines to receipt image/PDF analyses.\n"
        "Return strict JSON only, one object. Keys are line_id strings from the expense lines.\n"
        "Each value must be an object with exactly these keys:\n"
        '  "best_receipt": string or null  — MUST be copied exactly from one receipt\'s "source_file" in the JSON (full path). '
        "Never invent a path. The reason must refer to the same receipt as best_receipt (vendor and/or filename).\n"
        '  "confidence": number from 0 to 1\n'
        '  "reason": short string\n'
        '  "translated_merchant_name": string or null — if the matched receipt vendor name is NOT in English, '
        "provide a concise English translation. If already English or no match, set null.\n"
        "Currency on expense_line.currency is the posted/card line currency; receipt.currency is the folio/document "
        "currency (line_items[].currency inherits receipt currency when null). Some receipts include "
        "card_charged_amount / card_charged_currency (DCC / amount charged to card). Receipts may also include "
        "estimated_usd_total: approximate USD equivalent of the foreign total using transaction-date FX (from analysis), "
        "plus optional estimated_usd_fx_note. line_items may include estimated_usd per row for split foreign charges.\n"
        "Rules (apply in this order; do not prefer a receipt that fails an earlier step over one that passes it):\n"
        "Confidence policy (strict): amount evidence carries the most weight, then date proximity.\n"
        "- Missing/unparseable dates lower confidence.\n"
        f"- If final confidence would be below {MIN_MATCH_CONFIDENCE:.2f}, set best_receipt=null (no match).\n"
        f"- {MIN_MATCH_CONFIDENCE:.2f} to <{REVIEW_CONFIDENCE_THRESHOLD:.2f} means weak/partial match and should read as review-needed in reason.\n"
        "1) Amount + currency — three tiers for each line. **Always prefer the smallest |line amount − receipt amount signal|** "
        "among receipts that pass Phase A or consistent Phase B (exact/near-exact dollar match wins).\n"
        "   Phase A (explicit): First, if receipt.card_charged_amount is set and |line.amount - card_charged_amount| <= ~1.0, "
        "treat as a match — this applies **regardless of whether card_charged_currency matches line.currency**, "
        "because expense portals often show the billed/card amount but label it with the receipt's native currency. "
        "Else, when line.currency and receipt.currency are equal or either is null: compare line.amount to "
        "line_items[].amount (same currency) if any, else matched_amount and total_amount. Allow only trivial rounding.\n"
        "   Phase B (USD estimate / close): Use only when Phase A gives no satisfactory receipt for this line. "
        "**estimated_usd_total must be plausible for the receipt's own printed total** (matched_amount/total_amount in receipt.currency): "
        "e.g. a ~5 CHF snack receipt cannot have estimated_usd_total ~190 — treat that as invalid and do not match. "
        "If receipt.estimated_usd_total is consistent with the folio total in document currency, and expense_line.currency is USD or null/empty, "
        "parse line.amount as USD and match if |diff| vs estimated_usd_total <= max(1.50, 0.012 * max(line_amount, estimated_usd_total)) "
        "plus merchant/date fit—this absorbs small bank/DCC vs mid-market spread. "
        "If line_items have estimated_usd entries, only use them when that line item's amount/currency makes the estimate plausible. "
        "State 'USD estimate' in reason. Confidence moderate (e.g. 0.62–0.84), lower than Phase A.\n"
        "   Phase C (FX fallback): Only if Phase A and B fail. If line.currency and receipt.currency differ and DCC/estimates "
        "do not help, you may match when merchant/date fit and line.amount is a plausible conversion of receipt totals at "
        "transaction date. Mention currencies and implied FX in reason; confidence lower (e.g. cap ~0.72).\n"
        "2) Merchant: use vendor/merchant text only as a soft tie-breaker when amount/date evidence is otherwise similar.\n"
        "3) Date: Receipt date vs transaction_date commonly differs by 1-2 days. Posting delays, batch processing, "
        "and travel charges (especially hotels) can cause gaps of weeks. Gaps up to ~7 days are normal; "
        "7-30 days should reduce confidence but NOT cause rejection when amount and merchant evidence is strong. "
        "Only reject on date alone if the gap exceeds ~30 days.\n"
        "   Exception: when the amount is an EXACT Phase A match (card-charged or same-currency total equal to line.amount), "
        "do NOT reject even on a large or implausible date gap — a wrong year/month is almost always a receipt OCR/parse error, "
        "not a different transaction. Still pick that receipt, state 'date likely OCR error' in reason, and set a reduced "
        "confidence (~0.5-0.65) so it surfaces for review.\n"
        "- One best_receipt per expense line. The same source_file may be best_receipt for multiple lines when one "
        "receipt covers several card charges (e.g. rideshare trip amount on one line and tip on another). "
        "In that case each line should match a distinct line_items amount when possible.\n"
        "- If nothing fits, set best_receipt to null and confidence low.\n\n"
        f"expense_lines: {json.dumps(line_objs, ensure_ascii=False)}\n\n"
        f"receipts: {json.dumps(rec_objs, ensure_ascii=False)}\n"
    )


def build_single_line_receipt_match_prompt(line: dict[str, Any], analyses: list[dict[str, Any]]) -> str:
    line_obj = _compact_line_for_prompt(line)
    rec_objs = [_compact_receipt_for_prompt(a) for a in analyses if str(a.get("source_file", "")).strip()]
    return (
        "You match one corporate credit card expense line to receipt image/PDF analyses.\n"
        "Return strict JSON only, one object with exactly these keys:\n"
        '  "best_receipt": string or null  — MUST be copied exactly from one receipt\'s "source_file" value in the JSON below '
        "(full path string). Never invent a path. The reason must describe the same document (merchant and/or filename) as best_receipt.\n"
        '  "confidence": number from 0 to 1\n'
        '  "reason": short string\n'
        '  "translated_merchant_name": string or null — if the matched receipt vendor name is NOT in English, '
        "provide a concise English translation. If already English or no match, set null.\n"
        "expense_line.currency is the posted/card currency; receipt.currency is the folio currency. Receipts may include "
        "card_charged_amount / card_charged_currency (DCC), and estimated_usd_total (model USD equivalent for foreign totals "
        "at receipt_date), plus line_items[].estimated_usd for split lines.\n"
        "Rules (apply in this order):\n"
        "Confidence policy (strict): amount evidence is primary, then date proximity.\n"
        f"- If confidence would be below {MIN_MATCH_CONFIDENCE:.2f}, return best_receipt as null.\n"
        f"- If confidence is {MIN_MATCH_CONFIDENCE:.2f} to <{REVIEW_CONFIDENCE_THRESHOLD:.2f}, reason should indicate review is needed.\n"
        "1) Phase A — Explicit: card_charged_amount vs line.amount when amounts are numerically close (within ~1.0), "
        "regardless of whether card_charged_currency matches line.currency — expense portals often show the billed/card amount "
        "but label it with the receipt's native currency. Also try same-currency match to "
        "line_items or matched_amount/total_amount. **Prefer receipt whose card amount (or native total) is closest to line.amount.**\n"
        "2) Phase B — USD close: only if Phase A failed. **Ignore estimated_usd_total if it is impossible given the receipt's "
        "printed total in document currency** (e.g. single-digit foreign currency vs ~190 USD estimate). "
        "When estimated_usd_total is plausible vs the folio, if line.currency is USD or empty, compare line.amount to "
        "estimated_usd_total; match if |diff| <= max(1.50, 0.012 * max(|amounts|)); try line_items[].estimated_usd only when plausible. "
        "Reason must cite USD estimate; confidence below Phase A.\n"
        "3) Phase C — General FX if A and B fail; lowest confidence.\n"
        "4) Merchant: optional soft tie-breaker only; do not penalize confidence for vendor text mismatch when amount/date fit.\n"
        "5) Date: Receipt date vs transaction_date commonly differs by 1-2 days, but posting delays, batch processing, "
        "and travel charges (especially hotels) can cause gaps of weeks. Gaps up to ~7 days are normal; "
        "7-30 days should reduce confidence but NOT cause rejection when amount and merchant evidence is strong. "
        "Only reject on date alone if the gap exceeds ~30 days.\n"
        "   Exception: when the amount is an EXACT Phase A match (card-charged or same-currency total equal to line.amount), "
        "do NOT reject even on a large or implausible date gap — a wrong year/month is almost always a receipt OCR/parse error. "
        "Still pick that receipt, state 'date likely OCR error' in reason, and set a reduced confidence (~0.5-0.65) for review.\n"
        "The same receipt file may correctly apply to this line even if another expense line also uses it (split charges). "
        "Prefer null if nothing fits.\n\n"
        f"expense_line: {json.dumps(line_obj, ensure_ascii=False)}\n\n"
        f"receipts: {json.dumps(rec_objs, ensure_ascii=False)}\n"
    )


def _parse_single_line_match_response(
    raw_text: str,
    *,
    valid_files: set[str],
    line: dict[str, Any] | None = None,
    analyses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    cleaned = _normalize_json_text(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Match response was not valid JSON: {raw_text[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Match response JSON must be an object.")
    reason = str(parsed.get("reason", "") or "")[:500]
    br_raw = parsed.get("best_receipt")
    br = finalize_best_receipt_path(
        br_raw,
        reason,
        valid_files,
        line,
        analyses or [],
    )
    conf_f = _confidence_float(parsed.get("confidence"), default=0.0)

    br_raw_str = str(br_raw).strip() if br_raw is not None else ""
    if not br and br_raw_str:
        # Model cited a path but we could not map it to an analyzed source_file (e.g. Photos UUID
        # mismatch). Do not keep high confidence + optimistic prose with no attachable file.
        conf_f = min(conf_f, 0.35)
        prefix = (
            "No receipt file linked: model path did not match any analyzed import "
            "(often Apple Photos temp exports). Re-add from a stable folder or use Choose file. "
        )
        reason = (prefix + reason)[:500]

    translated = str(parsed.get("translated_merchant_name") or "").strip() or None

    return {
        "best_receipt": br or None,
        "confidence": conf_f,
        "reason": reason,
        "translated_merchant_name": translated,
    }


def match_one_expense_line_to_receipts(
    *,
    api_key: str,
    model: str,
    line: dict[str, Any],
    analyses: list[dict[str, Any]],
    http_verify_preferred: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Match a single expense line to receipt analyses (one API call).
    Returns { best_receipt, confidence, reason }.
    """
    log = on_status or (lambda _s: None)
    lid = str(line.get("line_id", "") or "").strip()
    if not lid:
        return {"best_receipt": None, "confidence": 0.0, "reason": "missing line_id"}
    valid_files = {str(a.get("source_file", "")).strip() for a in analyses if str(a.get("source_file", "")).strip()}
    if not valid_files:
        return {"best_receipt": None, "confidence": 0.0, "reason": "no receipt analyses"}
    client = build_openai_client(api_key, http_verify_preferred=http_verify_preferred)
    prompt = build_single_line_receipt_match_prompt(line, analyses)
    log(f"OpenAI: matching line {lid}…")
    try:
        response = client.responses.create(
            model=model,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        )
    except Exception as exc:
        hint = openai_tls_troubleshooting_hint(exc)
        raise RuntimeError(f"OpenAI match request failed: {exc}{hint}") from exc
    raw_text = (response.output_text or "").strip()
    result = _parse_single_line_match_response(
        raw_text,
        valid_files=valid_files,
        line=line,
        analyses=analyses,
    )
    by_path = {
        str(a.get("source_file", "")).strip(): a
        for a in analyses
        if str(a.get("source_file", "")).strip()
    }
    return _apply_match_quality_policy(line, by_path, result)


def match_receipts_to_expense_lines(
    *,
    api_key: str,
    model: str,
    lines: list[dict[str, Any]],
    analyses: list[dict[str, Any]],
    http_verify_preferred: str | None = None,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Returns line_id -> { best_receipt, confidence, reason }.
    best_receipt is filesystem path string or empty / null meaning no match.
    """
    log = on_status or (lambda _s: None)
    if not lines:
        return {}
    client = build_openai_client(api_key, http_verify_preferred=http_verify_preferred)
    prompt = build_receipt_match_prompt(lines, analyses)
    log("Sending batch receipt-to-line match request to OpenAI…")
    try:
        response = client.responses.create(
            model=model,
            input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        )
    except Exception as exc:
        hint = openai_tls_troubleshooting_hint(exc)
        raise RuntimeError(f"OpenAI match request failed: {exc}{hint}") from exc

    raw_text = (response.output_text or "").strip()
    cleaned = _normalize_json_text(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Match response was not valid JSON: {raw_text[:500]}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("Match response JSON must be an object keyed by line_id.")

    valid_files = {str(a.get("source_file", "")).strip() for a in analyses if str(a.get("source_file", "")).strip()}
    by_path = {
        str(a.get("source_file", "")).strip(): a
        for a in analyses
        if str(a.get("source_file", "")).strip()
    }
    line_ids = {str(L.get("line_id", "")).strip() for L in lines if str(L.get("line_id", "")).strip()}
    line_by_id = {str(L.get("line_id", "")).strip(): L for L in lines if str(L.get("line_id", "")).strip()}
    out: dict[str, dict[str, Any]] = {}

    for lid, val in parsed.items():
        if lid not in line_ids:
            continue
        if not isinstance(val, dict):
            continue
        reason = str(val.get("reason", "") or "")[:500]
        ln = line_by_id.get(lid)
        br_raw_batch = val.get("best_receipt")
        br = finalize_best_receipt_path(
            br_raw_batch,
            reason,
            valid_files,
            ln,
            analyses,
        )
        conf_f = _confidence_float(val.get("confidence"), default=0.0)
        br_raw_b_str = str(br_raw_batch).strip() if br_raw_batch is not None else ""
        if not br and br_raw_b_str:
            conf_f = min(conf_f, 0.35)
            prefix = (
                "No receipt file linked: model path did not match any analyzed import "
                "(often Apple Photos temp exports). Re-add from a stable folder or use Choose file. "
            )
            reason = (prefix + reason)[:500]
        translated = str(val.get("translated_merchant_name") or "").strip() or None
        one = {
            "best_receipt": br or None,
            "confidence": conf_f,
            "reason": reason,
            "translated_merchant_name": translated,
        }
        out[lid] = _apply_match_quality_policy(ln, by_path, one) if ln else one

    for lid in line_ids:
        if lid not in out:
            out[lid] = {"best_receipt": None, "confidence": 0.0, "reason": "not returned by model"}

    return out
