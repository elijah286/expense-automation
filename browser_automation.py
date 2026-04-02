from __future__ import annotations

from collections.abc import Callable
import argparse
import base64
import json
import mimetypes
import re
import os
import subprocess
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

# Longer connect timeout helps on VPN/corporate networks; read cap matches OpenAI SDK default.
OPENAI_HTTP_TIMEOUT = httpx.Timeout(600.0, connect=30.0, pool=60.0)


def _openai_verify_raw(preferred: str | None = None) -> str:
    """Non-empty app Settings value wins; else OPENAI_HTTP_VERIFY env."""
    p = (preferred or "").strip()
    if p:
        return p
    return os.environ.get("OPENAI_HTTP_VERIFY", "").strip()


def _openai_httpx_client(verify_preferred: str | None = None) -> httpx.Client:
    """TLS for corporate SSL inspection: Settings field or OPENAI_HTTP_VERIFY."""
    verify: bool | str = True
    raw = _openai_verify_raw(verify_preferred)
    if raw.lower() in ("0", "false", "no", "off"):
        verify = False
        print(
            "[warn] OpenAI TLS verify disabled (insecure). "
            "Prefer a path to your organization's root CA .pem bundle."
        )
    elif raw:
        path = Path(raw).expanduser()
        if path.is_file():
            verify = str(path)
        else:
            print(f"[warn] OpenAI TLS CA path not found ({path}); using default CA bundle.")
    return httpx.Client(timeout=OPENAI_HTTP_TIMEOUT, verify=verify)


def build_openai_client(api_key: str, http_verify_preferred: str | None = None):
    """Shared OpenAI SDK client (timeouts + optional corporate TLS)."""
    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        http_client=_openai_httpx_client(verify_preferred=http_verify_preferred),
    )


def openai_tls_troubleshooting_hint(exc: BaseException) -> str:
    text = f"{exc!r}"
    c = exc.__cause__
    if c is not None:
        text += f" {c!r}"
    if "CERTIFICATE" in text.upper() or "SSL" in text.upper():
        return (
            " TLS verification failed (common behind corporate proxies). "
            "In Settings, set OpenAI TLS / CA to your org root .pem path (or ask IT). "
            "Alternatively set OPENAI_HTTP_VERIFY in .env."
        )
    return ""


def as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def click_with_selector(page: Page, selector: str, timeout_ms: int = 10000) -> bool:
    try:
        page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
        page.locator(selector).first.click(timeout=timeout_ms)
        print(f"[ok] Clicked element with selector: {selector}")
        return True
    except PlaywrightTimeoutError:
        print(f"[warn] Selector not found in time: {selector}")
        return False
    except Exception as exc:  # pragma: no cover - defensive logging
        print(f"[warn] Selector click failed: {exc}")
        return False


def click_with_coordinates(page: Page, x: int, y: int) -> None:
    page.mouse.move(x, y, steps=12)
    time.sleep(0.2)
    page.mouse.click(x, y)
    print(f"[ok] Clicked coordinates: ({x}, {y})")


def maybe_upload_receipt(page: Page, receipt_path: str | None) -> None:
    if not receipt_path:
        return

    path_obj = Path(receipt_path).expanduser()
    if not path_obj.exists():
        print(f"[warn] Receipt path does not exist: {path_obj}")
        return

    # Common upload locator candidates for legacy pages.
    candidates = [
        "input[type='file']",
        "[name*='file']",
        "[id*='file']",
        "[name*='upload']",
        "[id*='upload']",
    ]
    for selector in candidates:
        locator = page.locator(selector).first
        if locator.count() > 0:
            locator.set_input_files(str(path_obj))
            print(f"[ok] Uploaded receipt via selector: {selector}")
            return

    print("[warn] Could not locate a file input to upload receipt.")


def activate_photos_app() -> None:
    command = ["osascript", "-e", 'tell application "Photos" to activate']
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not activate Photos app.")


def export_receipts_from_photos(
    photos_album: str | None,
    use_photos_selection: bool,
    photos_limit: int,
    export_dir: Path,
) -> list[Path]:
    export_dir.mkdir(parents=True, exist_ok=True)
    session_dir = export_dir / f"run-{int(time.time())}"
    session_dir.mkdir(parents=True, exist_ok=True)

    album_name = photos_album or ""
    mode = "selection" if use_photos_selection else "album"
    applescript = r'''
on run argv
    set exportPath to POSIX file (item 1 of argv)
    set albumName to item 2 of argv
    set modeName to item 3 of argv
    set limitCount to (item 4 of argv) as integer

    tell application "Photos"
        if modeName is "selection" then
            set sourceItems to selection
        else
            set sourceItems to media items of album albumName
        end if

        if (count of sourceItems) is 0 then
            return "NO_ITEMS"
        end if

        if limitCount > 0 and (count of sourceItems) > limitCount then
            set sourceItems to items 1 thru limitCount of sourceItems
        end if

        export sourceItems to exportPath with using originals
    end tell

    return "OK"
end run
'''
    command = [
        "osascript",
        "-e",
        applescript,
        str(session_dir),
        album_name,
        mode,
        str(photos_limit),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript export failed.")

    if "NO_ITEMS" in result.stdout:
        print("[warn] Photos source returned no items.")
        return []

    exported = [
        p
        for p in session_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".heic", ".pdf", ".tiff"}
    ]
    exported.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    print(f"[info] Exported {len(exported)} file(s) from Photos into: {session_dir}")
    return exported


def resolve_receipt_paths(
    receipt_path: str | None,
    photos_album: str | None,
    use_photos_selection: bool,
    interactive_photos_selection: bool,
    photos_limit: int,
    photos_export_dir: str,
) -> list[str]:
    if receipt_path:
        path_obj = Path(receipt_path).expanduser()
        if not path_obj.exists():
            print(f"[warn] Receipt path does not exist: {path_obj}")
            return []
        return [str(path_obj)]

    if interactive_photos_selection and not photos_album and not use_photos_selection:
        print("[info] Opening Photos so you can choose receipt images.")
        activate_photos_app()
        input(
            "Select receipts in Photos, then return here and press Enter to continue export..."
        )
        use_photos_selection = True

    if not photos_album and not use_photos_selection:
        return []

    if photos_album and use_photos_selection:
        print("[warn] Both album and selection were provided; using selection.")
        photos_album = None

    exported_files = export_receipts_from_photos(
        photos_album=photos_album,
        use_photos_selection=use_photos_selection,
        photos_limit=photos_limit,
        export_dir=Path(photos_export_dir).expanduser(),
    )
    if exported_files:
        print(f"[ok] Prepared {len(exported_files)} receipt file(s) for analysis/upload.")
    return [str(path) for path in exported_files]


def normalize_json_text(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json\n", "", 1).strip()
    return cleaned


def _normalize_receipt_date_fields(parsed: dict) -> None:
    """Keep receipt_date and transaction_date aligned (purchase date on the receipt, not print time)."""
    rd = parsed.get("receipt_date")
    td = parsed.get("transaction_date")
    rd_s = str(rd).strip() if rd is not None else ""
    td_s = str(td).strip() if td is not None else ""
    if rd_s and not td_s:
        parsed["transaction_date"] = rd_s
    elif td_s and not rd_s:
        parsed["receipt_date"] = td_s
    for key in ("receipt_date", "transaction_date"):
        v = parsed.get(key)
        if v is not None and not isinstance(v, str):
            parsed[key] = str(v).strip()


def normalize_currency_code(val: object | None) -> str:
    """Best-effort ISO 4217-style code from LLM or scraper (uppercase, trimmed)."""
    if val is None:
        return ""
    s = str(val).strip().upper().replace("'", "")
    if not s:
        return ""
    if re.fullmatch(r"[A-Z]{3}", s):
        return s
    for tok in re.split(r"[^A-Z]+", s):
        if re.fullmatch(r"[A-Z]{3}", tok):
            return tok
    s_compact = "".join(ch for ch in s if ch.isalnum())
    if len(s_compact) == 3 and s_compact.isalpha():
        return s_compact
    return ""


def _normalize_receipt_currency_root(parsed: dict) -> None:
    parsed["currency"] = normalize_currency_code(parsed.get("currency"))


def _normalize_receipt_card_charge(parsed: dict) -> None:
    """
    DCC / dual-currency slips: amount/Currency actually charged to the card when printed separately
    from the folio total (e.g. hotel Tax Summary: 'Transaction Amount (in shoppers currency) USD 192.76').
    """
    cur = normalize_currency_code(parsed.get("card_charged_currency"))
    amt_raw = parsed.get("card_charged_amount")
    famt: float | None
    try:
        famt = float(amt_raw) if amt_raw is not None and str(amt_raw).strip() != "" else None
    except (TypeError, ValueError):
        famt = None
    raw_list = parsed.get("card_charges")
    if isinstance(raw_list, list) and (famt is None or not cur):
        for it in raw_list[:3]:
            if not isinstance(it, dict):
                continue
            try:
                a = float(it.get("amount")) if it.get("amount") is not None else None
            except (TypeError, ValueError):
                a = None
            if a is None:
                continue
            c = normalize_currency_code(it.get("currency"))
            if c:
                famt = a
                cur = c
                break
    if famt is not None and cur:
        parsed["card_charged_amount"] = famt
        parsed["card_charged_currency"] = cur
    else:
        parsed.pop("card_charged_amount", None)
        parsed.pop("card_charged_currency", None)
    parsed.pop("card_charges", None)


def _normalize_receipt_line_items(parsed: dict) -> None:
    """Vision JSON: optional line_items for split card charges (e.g. fare + tip on one receipt)."""
    raw = parsed.get("line_items")
    if raw is None:
        parsed["line_items"] = []
        return
    if not isinstance(raw, list):
        parsed["line_items"] = []
        return
    cleaned: list[dict] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        desc = str(it.get("description", "") or it.get("label", "") or "").strip()
        amt = it.get("amount")
        try:
            famt = float(amt) if amt is not None else None
        except (TypeError, ValueError):
            famt = None
        if famt is None:
            continue
        item: dict = {"amount": famt, "description": desc[:200]}
        kind = it.get("kind")
        if kind is not None and str(kind).strip():
            item["kind"] = str(kind).strip()[:80]
        ic = normalize_currency_code(it.get("currency"))
        if ic:
            item["currency"] = ic
        eu_raw = it.get("estimated_usd")
        try:
            eu = float(eu_raw) if eu_raw is not None and str(eu_raw).strip() != "" else None
        except (TypeError, ValueError):
            eu = None
        if eu is not None:
            item["estimated_usd"] = round(eu, 2)
        cleaned.append(item)
    parsed["line_items"] = cleaned
    root = normalize_currency_code(parsed.get("currency"))
    for item in cleaned:
        if not item.get("currency") and root:
            item["currency"] = root


def _normalize_receipt_estimated_usd(parsed: dict) -> None:
    """
    Model-estimated USD equivalent for foreign-currency totals (transaction-date FX).
    Used when DCC/card USD is missing but the corporate feed is USD.
    """
    note = parsed.get("estimated_usd_fx_note")
    if note is not None and str(note).strip():
        parsed["estimated_usd_fx_note"] = str(note).strip()[:240]
    else:
        parsed.pop("estimated_usd_fx_note", None)

    root = normalize_currency_code(parsed.get("currency"))
    raw = parsed.get("estimated_usd_total")
    est: float | None
    try:
        est = float(raw) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        est = None

    if root == "USD" and est is None:
        ma = parsed.get("matched_amount")
        try:
            est = float(ma) if ma is not None and str(ma).strip() != "" else None
        except (TypeError, ValueError):
            est = None

    if est is None and normalize_currency_code(parsed.get("card_charged_currency")) == "USD":
        cc = parsed.get("card_charged_amount")
        try:
            est = float(cc) if cc is not None and str(cc).strip() != "" else None
        except (TypeError, ValueError):
            pass

    if est is not None:
        parsed["estimated_usd_total"] = round(est, 2)
    else:
        parsed.pop("estimated_usd_total", None)


def _float_field(val: object) -> float | None:
    try:
        if val is None:
            return None
        s = str(val).strip().replace(",", "")
        if not s:
            return None
        return float(s)
    except (TypeError, ValueError):
        return None


def native_receipt_total_numeric(analysis: dict) -> tuple[float | None, str]:
    """
    Single payable total in document currency and that currency code.
    Prefer matched_amount / total_amount, else sum line_items (same currency as receipt).
    Shared with receipt_matching._native_total_from_analysis for consistent matching vs UI.
    """
    doc_cur = normalize_currency_code(analysis.get("currency"))
    for key in ("matched_amount", "total_amount"):
        v = _float_field(analysis.get(key))
        if v is not None and v > 0:
            return v, doc_cur
    raw_li = analysis.get("line_items")
    if isinstance(raw_li, list) and raw_li:
        s = 0.0
        n = 0
        for it in raw_li:
            if not isinstance(it, dict):
                continue
            ic = normalize_currency_code(it.get("currency")) or doc_cur
            if doc_cur and ic and ic != doc_cur:
                continue
            av = _float_field(it.get("amount"))
            if av is not None and av > 0:
                s += av
                n += 1
        if n and s > 0:
            return s, doc_cur
    return None, doc_cur


def receipt_local_amount_display(analysis: dict) -> str:
    """Receipt-native total plus ISO currency (EUR, CHF, …). Empty when receipt currency is USD."""
    cur = normalize_currency_code(analysis.get("currency"))
    if cur == "USD":
        return ""

    cc_amt = analysis.get("card_charged_amount")
    cc_cur = normalize_currency_code(analysis.get("card_charged_currency"))
    local_card_suffix = ""
    if cc_amt is not None and cc_cur and cc_cur != "USD":
        try:
            local_card_suffix = f" · card {float(cc_amt):.2f} {cc_cur}"
        except (TypeError, ValueError):
            local_card_suffix = f" · card {cc_amt} {cc_cur}".strip()

    tot, _ = native_receipt_total_numeric(analysis)
    if tot is not None:
        base = f"{tot:.2f} {cur}"
        return f"{base}{local_card_suffix}" if local_card_suffix else base

    items = analysis.get("line_items")
    if isinstance(items, list) and items:
        parts: list[str] = []
        for it in items[:8]:
            if not isinstance(it, dict):
                continue
            a = it.get("amount")
            if a is None:
                continue
            try:
                parts.append(f"{float(a):.2f}")
            except (TypeError, ValueError):
                parts.append(str(a).strip())
        if len(items) > 8:
            parts.append("…")
        if parts:
            joined = " + ".join(parts)
            base = f"{joined} {cur}".strip() if cur else joined
            return f"{base}{local_card_suffix}" if local_card_suffix else base

    m = analysis.get("matched_amount")
    if m is not None and str(m).strip():
        ms = str(m).strip()
        base = f"{ms} {cur}".strip() if cur else ms
        return f"{base}{local_card_suffix}" if local_card_suffix else base
    t = analysis.get("total_amount")
    if t is not None and str(t).strip():
        ts = str(t).strip()
        base = f"{ts} {cur}".strip() if cur else ts
        return f"{base}{local_card_suffix}" if local_card_suffix else base

    if local_card_suffix:
        return local_card_suffix.lstrip(" ·")
    return ""


def receipt_usd_amount_display(analysis: dict) -> str:
    """USD line: native-USD totals, or card-USD / ~estimate for foreign receipts."""
    cur = normalize_currency_code(analysis.get("currency"))
    est_usd = analysis.get("estimated_usd_total")
    cc_amt = analysis.get("card_charged_amount")
    cc_cur = normalize_currency_code(analysis.get("card_charged_currency"))

    if cur == "USD":
        val: float | None = None
        if est_usd is not None and str(est_usd).strip() != "":
            try:
                val = float(est_usd)
            except (TypeError, ValueError):
                pass
        if val is None:
            for key in ("matched_amount", "total_amount"):
                raw = analysis.get(key)
                if raw is not None and str(raw).strip() != "":
                    try:
                        val = float(raw)
                        break
                    except (TypeError, ValueError):
                        pass
        if val is None and cc_cur == "USD" and cc_amt is not None:
            try:
                val = float(cc_amt)
            except (TypeError, ValueError):
                pass
        if val is not None:
            return f"{val:.2f}"
        return ""

    cc_f = _float_field(cc_amt) if cc_cur == "USD" and cc_amt is not None else None
    est_f = _float_field(est_usd) if est_usd is not None and str(est_usd).strip() != "" else None

    parts: list[str] = []
    if cc_f is not None:
        if est_f is not None and abs(cc_f - est_f) <= 0.02:
            parts.append(f"{cc_f:.2f}")
        else:
            parts.append(f"card {cc_f:.2f}")
            if est_f is not None:
                parts.append(f"~{est_f:.2f}")
    elif est_f is not None:
        parts.append(f"~{est_f:.2f}")
    return " · ".join(parts)


def receipt_amount_display(analysis: dict) -> str:
    """Single-line summary: local · USD (backward compatible with older logs)."""
    loc = receipt_local_amount_display(analysis)
    usd = receipt_usd_amount_display(analysis)
    if loc and usd:
        return f"{loc} · {usd}"
    return loc or usd


def analyze_receipts_with_llm(
    receipt_paths: list[str],
    model: str,
    api_key: str | None,
    on_status: Callable[[str], None] | None = None,
    http_verify_preferred: str | None = None,
) -> list[dict]:
    if not receipt_paths:
        print("[warn] No receipt files available for LLM inspection.")
        return []
    if not api_key:
        print("[warn] OPENAI_API_KEY is missing; skipping LLM receipt inspection.")
        if on_status:
            on_status("LLM receipt parse skipped (no API key).")
        return []

    client = build_openai_client(api_key, http_verify_preferred=http_verify_preferred)
    analyses: list[dict] = []
    for path_str in receipt_paths:
        path_obj = Path(path_str)
        if on_status:
            on_status(f"LLM: waiting for model to read receipt {path_obj.name}…")
        mime_type = mimetypes.guess_type(path_obj.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path_obj.read_bytes()).decode("utf-8")
        prompt = (
            "Inspect this receipt image and return strict JSON with keys: "
            "vendor, receipt_date, currency, subtotal, tax, total_amount, "
            "matched_amount, line_items, card_charged_amount, card_charged_currency, "
            "estimated_usd_total, estimated_usd_fx_note, confidence, notes, display_rotation_quarter_turns. "
            "receipt_date must be the purchase/transaction date printed on the receipt "
            "(not 'printed on' or server metadata). Prefer ISO 8601 YYYY-MM-DD when the year is clear; "
            "otherwise use the exact date string as shown. "
            "Use numeric values for money fields when possible. "
            "matched_amount must be the overall payable total shown on the receipt (if a single total is shown). "
            "currency must be the ISO 4217 code for the currency of those amounts (EUR, USD, GBP, CHF, JPY, …) "
            "when identifiable from symbols or text on the receipt; use \"\" only if truly unknown. "
            "All amounts (subtotal, tax, total_amount, matched_amount, line_items amounts) are in that currency "
            "unless a line_items entry includes its own \"currency\" for a rare mixed-currency slip. "
            "card_charged_amount and card_charged_currency: when the slip shows the card was charged in a different "
            "currency than the main folio (Dynamic Currency Conversion / DCC, currency choice on Visa/Mastercard, "
            "labels like 'transaction amount in shopper's currency', 'billed in USD', 'amount charged to your card', "
            "hotel tax summary with a local total and a separate explicit total in the customer's/charged currency), "
            "set card_charged_amount to that numeric amount and card_charged_currency to ISO 4217 (e.g. USD). "
            "This must reflect the amount that would appear on the card statement in that currency—not the local "
            "folio total. If the document only shows one currency for the charge, use null for both fields. "
            "estimated_usd_total: when receipt currency is not USD (or totals are clearly in a foreign currency), set this "
            "to your best estimate in US dollars of matched_amount (the same payable total) using the approximate "
            "market/spot exchange rate for receipt_date (the transaction day)—not a guess from thin air; use typical "
            "rates for that calendar day. If the slip already states an explicit USD/DCC total for the card, you may "
            "set estimated_usd_total to that same numeric value for consistency. If the document is entirely USD, "
            "set estimated_usd_total to the numeric USD total (same as matched_amount) or null. "
            "estimated_usd_fx_note: optional one short line, e.g. assumed rate or source hint (e.g. 'GBP/USD ~1.10 on date'). "
            "line_items must be an array (use [] if not applicable). Each element is an object with: "
            "\"amount\" (number, required), \"description\" (short string, e.g. Trip, Fare, Tip, Service fee), "
            "optional \"currency\" (ISO code) only if this row is not in the receipt’s main currency, "
            "optional \"estimated_usd\" (number): approximate USD for that line at receipt_date when that line is not USD. "
            "Populate line_items when the receipt itemizes amounts that the bank may post as separate card "
            "transactions for the same trip or order (common for rideshare fare vs tip, or split hotel charges). "
            "When in doubt for rideshares, include separate line_items for trip and tip if both amounts appear. "
            "Individual line_items amounts may sum to matched_amount or total_amount but do not fabricate splits. "
            "display_rotation_quarter_turns must be an integer 0, 1, 2, or 3 meaning how many "
            "90-degree CLOCKWISE rotations are needed after any correct embedded camera orientation "
            "so the receipt reads upright in a normal viewer (total at bottom, text horizontal). "
            "Use 0 if already upright."
        )
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{encoded}",
                        },
                    ],
                }
            ],
        )
        raw_text = response.output_text or "{}"
        json_text = normalize_json_text(raw_text)
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            parsed = {"raw_response": raw_text}
        if isinstance(parsed, dict):
            _normalize_receipt_date_fields(parsed)
            _normalize_receipt_currency_root(parsed)
            _normalize_receipt_line_items(parsed)
            _normalize_receipt_card_charge(parsed)
            _normalize_receipt_estimated_usd(parsed)
        parsed["source_file"] = str(path_obj)
        analyses.append(parsed)
        print(f"[ok] LLM reviewed: {path_obj.name}")
        if on_status:
            vendor = str(parsed.get("vendor", "")).strip() or "(unknown vendor)"
            on_status(f"LLM: finished {path_obj.name} — vendor {vendor}")

    return analyses


def write_analysis_report(analyses: list[dict], output_dir: Path) -> Path | None:
    if not analyses:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"receipt-analysis-{int(time.time())}.json"
    report_path.write_text(json.dumps(analyses, indent=2), encoding="utf-8")
    print(f"[info] Analysis report written: {report_path}")
    return report_path


def run(
    url: str,
    plus_selector: str | None,
    headless: bool,
    x: int | None,
    y: int | None,
    receipt_path: str | None,
    photos_album: str | None,
    use_photos_selection: bool,
    interactive_login: bool,
    interactive_photos_selection: bool,
    photos_limit: int,
    photos_export_dir: str,
    llm_review: bool,
    openai_model: str,
    openai_api_key: str | None,
) -> None:
    resolved_receipt_paths = resolve_receipt_paths(
        receipt_path=receipt_path,
        photos_album=photos_album,
        use_photos_selection=use_photos_selection,
        interactive_photos_selection=interactive_photos_selection,
        photos_limit=photos_limit,
        photos_export_dir=photos_export_dir,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=100)
        context = browser.new_context()
        page = context.new_page()

        print(f"[info] Opening: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
        if interactive_login:
            input("Log into Oracle in the opened browser, then press Enter to continue...")

        clicked = False
        if plus_selector:
            clicked = click_with_selector(page, plus_selector)

        if not clicked and x is not None and y is not None:
            print("[info] Falling back to coordinate click.")
            click_with_coordinates(page, x, y)
            clicked = True

        if not clicked:
            print("[warn] No click performed. Provide a valid selector or x/y coordinates.")

        if resolved_receipt_paths:
            maybe_upload_receipt(page, resolved_receipt_paths[0])

        analyses: list[dict] = []
        if llm_review:
            analyses = analyze_receipts_with_llm(
                receipt_paths=resolved_receipt_paths,
                model=openai_model,
                api_key=openai_api_key,
            )
            write_analysis_report(analyses, Path(photos_export_dir).expanduser())
            if analyses:
                print("[info] LLM receipt amount matching preview:")
                for item in analyses:
                    source = Path(item.get("source_file", "unknown")).name
                    matched = item.get("matched_amount", "n/a")
                    confidence = item.get("confidence", "n/a")
                    print(f"  - {source}: matched_amount={matched}, confidence={confidence}")

        print("[info] Leaving browser open for manual review. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[info] Shutting down.")
        finally:
            context.close()
            browser.close()


def parse_args() -> argparse.Namespace:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Automate legacy browser data entry in a visible browser window."
    )
    parser.add_argument("--url", default=os.getenv("LEGACY_URL"), help="Target application URL.")
    parser.add_argument(
        "--plus-selector",
        default=os.getenv("PLUS_SELECTOR"),
        help="CSS selector for the green plus button.",
    )
    parser.add_argument("--x", type=int, default=None, help="Fallback click X coordinate.")
    parser.add_argument("--y", type=int, default=None, help="Fallback click Y coordinate.")
    parser.add_argument(
        "--receipt-image-path",
        default=os.getenv("RECEIPT_IMAGE_PATH"),
        help="Optional path to a receipt image for upload.",
    )
    parser.add_argument(
        "--photos-album",
        default=os.getenv("PHOTOS_ALBUM"),
        help="Apple Photos album name to export receipts from.",
    )
    parser.add_argument(
        "--use-photos-selection",
        action="store_true",
        default=as_bool(os.getenv("USE_PHOTOS_SELECTION"), default=False),
        help="Use currently selected items in Apple Photos as receipt source.",
    )
    parser.add_argument(
        "--interactive-login",
        action="store_true",
        default=as_bool(os.getenv("INTERACTIVE_LOGIN"), default=True),
        help="Pause after page load and wait for manual login confirmation.",
    )
    parser.add_argument(
        "--interactive-photos-selection",
        action="store_true",
        default=as_bool(os.getenv("INTERACTIVE_PHOTOS_SELECTION"), default=True),
        help="Prompt you to select receipts in Photos before export.",
    )
    parser.add_argument(
        "--photos-limit",
        type=int,
        default=int(os.getenv("PHOTOS_LIMIT", "5")),
        help="Number of images to export from Photos (default: 5).",
    )
    parser.add_argument(
        "--photos-export-dir",
        default=os.getenv("PHOTOS_EXPORT_DIR", "./photos-exports"),
        help="Directory where Photos exports are staged.",
    )
    parser.add_argument(
        "--llm-review",
        action="store_true",
        default=as_bool(os.getenv("LLM_REVIEW"), default=True),
        help="Analyze receipt images with an LLM and match receipt amounts.",
    )
    parser.add_argument(
        "--openai-model",
        default=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        help="Vision-capable OpenAI model for receipt inspection.",
    )
    parser.add_argument(
        "--openai-api-key",
        default=os.getenv("OPENAI_API_KEY"),
        help="OpenAI API key; defaults to OPENAI_API_KEY env var.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=as_bool(os.getenv("HEADLESS"), default=False),
        help="Run browser without a visible window (default: visible).",
    )
    args = parser.parse_args()
    if not args.url:
        parser.error("Missing URL. Set --url or LEGACY_URL in .env.")
    return args


if __name__ == "__main__":
    cli_args = parse_args()
    run(
        url=cli_args.url,
        plus_selector=cli_args.plus_selector,
        headless=cli_args.headless,
        x=cli_args.x,
        y=cli_args.y,
        receipt_path=cli_args.receipt_image_path,
        photos_album=cli_args.photos_album,
        use_photos_selection=cli_args.use_photos_selection,
        interactive_login=cli_args.interactive_login,
        interactive_photos_selection=cli_args.interactive_photos_selection,
        photos_limit=cli_args.photos_limit,
        photos_export_dir=cli_args.photos_export_dir,
        llm_review=cli_args.llm_review,
        openai_model=cli_args.openai_model,
        openai_api_key=cli_args.openai_api_key,
    )
