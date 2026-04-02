"""
Local cache for scraped expense portal lines and LLM receipt-to-line matches.
Separate from llm_query_pending.json so unknown query kinds never block VPN replay.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MIN_ACCEPTED_MATCH_CONFIDENCE = 0.40
_PHOTOS_EXPORT_UUID_RE = re.compile(
    r"uuid=([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})"
)

EXPENSE_LINES_FILENAME = "expense_lines_cache.json"
RECEIPT_MATCH_FILENAME = "receipt_line_match.json"
APPROVED_MATCH_FILENAME = "receipt_match_approved.json"
ANALYSES_SNAPSHOT_FILENAME = "receipt_analyses_snapshot.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def expense_lines_cache_path(app_dir: Path) -> Path:
    return app_dir / EXPENSE_LINES_FILENAME


def receipt_line_match_path(app_dir: Path) -> Path:
    return app_dir / RECEIPT_MATCH_FILENAME


def approved_match_path(app_dir: Path) -> Path:
    return app_dir / APPROVED_MATCH_FILENAME


def receipt_analyses_snapshot_path(app_dir: Path) -> Path:
    return app_dir / ANALYSES_SNAPSHOT_FILENAME


def prune_receipt_sidecars_after_step2_scrape(
    app_dir: Path,
    new_lines: list[dict[str, Any]],
) -> list[Path]:
    """After rewriting expense_lines_cache.json, drop match/approve entries for line_ids that no longer exist.

    Receipt LLM parses (`receipt_analyses_snapshot.json`) are keyed by source file, not portal line_id — they are
    not removed here so document analysis and line→receipt matching survive Step 2 re-runs and app restarts.
    """
    valid_ids = {
        str(line.get("line_id", "") or "").strip()
        for line in new_lines
        if isinstance(line, dict) and str(line.get("line_id", "") or "").strip()
    }
    touched: list[Path] = []

    matches = load_receipt_line_matches(app_dir)
    stale_m = set(matches.keys()) - valid_ids
    if stale_m:
        for lid in stale_m:
            matches.pop(lid, None)
        touched.append(save_receipt_line_matches(app_dir, matches))

    approved = load_approved_matches(app_dir)
    stale_a = set(approved.keys()) - valid_ids
    if stale_a:
        for lid in stale_a:
            approved.pop(lid, None)
        touched.append(save_approved_matches(app_dir, approved))

    return touched


def save_expense_lines_cache(
    app_dir: Path,
    lines: list[dict[str, Any]],
    *,
    source: str = "step2_credit_card",
) -> Path:
    path = expense_lines_cache_path(app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "updated_at": _iso_now(),
        "source": source,
        "lines": lines,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_expense_lines_cache(app_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = expense_lines_cache_path(app_dir)
    if not path.exists():
        return [], {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], {}
    if not isinstance(raw, dict):
        return [], {}
    lines = raw.get("lines")
    if not isinstance(lines, list):
        return [], raw
    return [x for x in lines if isinstance(x, dict)], raw


def remove_expense_lines_by_ids(app_dir: Path, line_ids: set[str]) -> tuple[int, int]:
    """
    Remove scraped lines with those line_ids from expense_lines_cache.json.
    Drops matching keys from receipt_line_match.json and receipt_match_approved.json.
    Does not modify receipt analyses, snapshots, or the Documents file list.
    Returns (removed_count, remaining_line_count).
    """
    ids = {str(x).strip() for x in line_ids if str(x).strip()}
    if not ids:
        lines, _ = load_expense_lines_cache(app_dir)
        return 0, len(lines)

    lines, meta = load_expense_lines_cache(app_dir)
    if not lines:
        return 0, 0

    kept: list[dict[str, Any]] = []
    removed = 0
    for row in lines:
        lid = str(row.get("line_id", "") or "").strip()
        if lid and lid in ids:
            removed += 1
            continue
        kept.append(row)

    if removed == 0:
        return 0, len(lines)

    source = str(meta.get("source", "") or "step2_credit_card")
    save_expense_lines_cache(app_dir, kept, source=source)

    matches = load_receipt_line_matches(app_dir)
    for lid in ids:
        matches.pop(lid, None)
    save_receipt_line_matches(app_dir, matches)

    approved = load_approved_matches(app_dir)
    for lid in ids:
        approved.pop(lid, None)
    save_approved_matches(app_dir, approved)

    return removed, len(kept)


def save_receipt_line_matches(
    app_dir: Path,
    matches: dict[str, dict[str, Any]],
) -> Path:
    path = receipt_line_match_path(app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": SCHEMA_VERSION,
        "updated_at": _iso_now(),
        "matches": matches,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_receipt_line_matches(app_dir: Path) -> dict[str, dict[str, Any]]:
    path = receipt_line_match_path(app_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    m = raw.get("matches")
    if not isinstance(m, dict):
        return {}
    out: dict[str, dict[str, Any]] = {
        str(k): dict(v) if isinstance(v, dict) else {} for k, v in m.items()
    }
    analyses = load_analyses_snapshot(app_dir)
    valid_files = {
        str(a.get("source_file", "")).strip()
        for a in analyses
        if str(a.get("source_file", "") or "").strip()
    }
    if not valid_files:
        return out
    from receipt_matching import normalize_best_receipt_path

    changed = False
    for blk in out.values():
        if not isinstance(blk, dict):
            continue
        conf_raw = blk.get("confidence")
        try:
            conf_f = float(conf_raw) if conf_raw is not None and str(conf_raw).strip() != "" else 0.0
        except (TypeError, ValueError):
            conf_f = 0.0
        cur_best = str(blk.get("best_receipt") or "").strip()
        # Hard safety: do not keep a linked document when confidence is no-match/low.
        if cur_best and conf_f < MIN_ACCEPTED_MATCH_CONFIDENCE:
            blk["best_receipt"] = None
            changed = True
        if not str(blk.get("reason") or "").strip() and not str(blk.get("best_receipt") or "").strip():
            blk["reason"] = "Receipt missing."
            changed = True
        raw_br = blk.get("best_receipt")
        cur = str(raw_br or "").strip()
        fixed = normalize_best_receipt_path(raw_br, valid_files)
        if fixed and fixed != cur:
            blk["best_receipt"] = fixed
            changed = True
    if changed:
        save_receipt_line_matches(app_dir, out)
    return out


def validate_lines_cache_for_match(app_dir: Path) -> tuple[bool, str]:
    lines, _ = load_expense_lines_cache(app_dir)
    if not lines:
        return False, f"No scraped lines in {expense_lines_cache_path(app_dir)}. Run VPN flow through Step 2 first."
    return True, ""


def validate_matches_for_attach(app_dir: Path) -> tuple[bool, str]:
    ok, err = validate_lines_cache_for_match(app_dir)
    if not ok:
        return False, err
    m = load_receipt_line_matches(app_dir)
    if not m:
        return (
            False,
            f"No receipt matches in {receipt_line_match_path(app_dir)}. "
            "Run receipt matching (VPN off) first.",
        )
    return True, ""


def save_approved_matches(app_dir: Path, approved: dict[str, dict[str, Any]]) -> Path:
    path = approved_match_path(app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": SCHEMA_VERSION,
        "updated_at": _iso_now(),
        "approved": approved,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_approved_matches(app_dir: Path) -> dict[str, dict[str, Any]]:
    path = approved_match_path(app_dir)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    a = raw.get("approved")
    if not isinstance(a, dict):
        return {}
    return {str(k): dict(v) if isinstance(v, dict) else {} for k, v in a.items()}


def validate_approved_for_attach(app_dir: Path) -> tuple[bool, str]:
    ok, err = validate_lines_cache_for_match(app_dir)
    if not ok:
        return False, err
    a = load_approved_matches(app_dir)
    if not a:
        return (
            False,
            f"No approved matches in {approved_match_path(app_dir)}. "
            "Use Create report on the Expense report tab (after lines have receipt files).",
        )
    analyses = load_analyses_snapshot(app_dir)
    existing_analysis_paths: list[Path] = []
    analysis_by_basename: dict[str, list[Path]] = {}
    for analysis in analyses:
        p = Path(str((analysis or {}).get("source_file", "") or "").strip()).expanduser()
        if not p.is_file():
            continue
        existing_analysis_paths.append(p)
        k = p.name.lower()
        arr = analysis_by_basename.setdefault(k, [])
        ps = str(p)
        if all(str(x) != ps for x in arr):
            arr.append(p)

    def _uuid_token(path_str: str) -> str:
        m = _PHOTOS_EXPORT_UUID_RE.search(path_str or "")
        return m.group(1).lower() if m else ""

    def _attachable(path_str: str) -> bool:
        p = Path(path_str).expanduser()
        if p.is_file():
            return True
        if not p.name:
            return False
        hits = analysis_by_basename.get(p.name.lower(), [])
        if len(hits) == 1:
            return True
        u = _uuid_token(path_str)
        if u and hits:
            return any(u in str(h).lower() for h in hits)
        if u and existing_analysis_paths:
            return any(u in str(h).lower() for h in existing_analysis_paths)
        return False

    with_file = 0
    for _lid, block in a.items():
        sf = str((block or {}).get("source_file", "") or "").strip()
        if sf and _attachable(sf):
            with_file += 1
    if with_file == 0:
        return (
            False,
            "Approved rows exist but no attachable file paths were found. "
            "Fix paths in the review dialog or re-import receipts from a stable folder.",
        )
    return True, ""


def save_analyses_snapshot(app_dir: Path, analyses: list[dict[str, Any]]) -> Path:
    path = receipt_analyses_snapshot_path(app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": SCHEMA_VERSION,
        "updated_at": _iso_now(),
        "analyses": analyses,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_analyses_snapshot(app_dir: Path) -> list[dict[str, Any]]:
    path = receipt_analyses_snapshot_path(app_dir)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    a = raw.get("analyses")
    if not isinstance(a, list):
        return []
    return [x for x in a if isinstance(x, dict)]


def _normalize_vendor_key_local(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def load_vendor_expense_types_flat(app_dir: Path) -> dict[str, str]:
    """Same keys as the Expense report vendor cache file (normalized merchant -> expense type)."""
    path = app_dir / "vendor_expense_types.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("vendors") if isinstance(raw.get("vendors"), dict) else raw
    out: dict[str, str] = {}
    for k, v in inner.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        key = _normalize_vendor_key_local(k)
        val = v.strip()
        if key and val:
            out[key] = val
    return out


def persist_expense_line_derived_fields(
    app_dir: Path,
    matches: dict[str, dict[str, Any]] | None = None,
    vendor_by_key: dict[str, str] | None = None,
) -> Path | None:
    """Copy receipt-match + expense-type results onto each scraped line in expense_lines_cache.json.

    Sidecar files (receipt_line_match.json, vendor_expense_types.json) remain authoritative for
    automation, but embedded *cached_* keys let the Expense report grid repopulate after relaunch
    even when in-memory state was not saved, and give a single cache file to inspect/back up.
    """
    lines, meta = load_expense_lines_cache(app_dir)
    if not lines:
        return None
    if matches is None:
        matches = load_receipt_line_matches(app_dir)
    if vendor_by_key is None:
        vendor_by_key = load_vendor_expense_types_flat(app_dir)
    at = _iso_now()
    for row in lines:
        if not isinstance(row, dict):
            continue
        lid = str(row.get("line_id", "") or "").strip()
        if not lid:
            continue
        blk = matches.get(lid) or {}
        if not isinstance(blk, dict):
            blk = {}
        vk = _normalize_vendor_key_local(str(row.get("merchant_name", "") or ""))
        et = (vendor_by_key.get(vk, "") or "").strip() if vk else ""
        row["cached_analysis_at"] = at
        row["cached_expense_type"] = et
        row["cached_best_receipt"] = str(blk.get("best_receipt") or "").strip()
        conf = blk.get("confidence", "")
        row["cached_match_confidence"] = conf if conf is not None else ""
        row["cached_match_reason"] = str(blk.get("reason") or "").strip()[:500]
    source = str(meta.get("source") or "step2_credit_card")
    return save_expense_lines_cache(app_dir, lines, source=source)
