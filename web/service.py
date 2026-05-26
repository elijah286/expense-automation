"""
Data-access layer for the web UI.

Reads/writes the same JSON caches under ~/.expense-automator that the
Tk app and automation modules use, so both UIs stay in sync.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid as _uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from expense_lines_cache import (
    delete_report_with_data,
    get_report_submission_status,
    load_analyses_snapshot,
    load_approved_matches,
    load_expense_lines_cache,
    load_expense_report_groups,
    load_pending_deletions,
    load_receipt_line_matches,
    load_submitted_receipts,
    load_vendor_expense_types_flat,
    purge_expired_deletions,
    remove_expense_lines_by_ids,
    restore_pending_deletion,
    save_approved_matches,
    save_expense_report_groups,
    save_receipt_line_matches,
    save_analyses_snapshot,
    soft_delete_report,
)
import keychain_credentials
from classification.service import classify_transactions

APP_DIR = Path.home() / ".expense-automator"


@dataclass
class DashboardStats:
    total_transactions: int = 0
    total_receipts: int = 0
    matched_high: int = 0
    matched_medium: int = 0
    unmatched: int = 0
    classified: int = 0
    unclassified: int = 0
    approved: int = 0
    ready_to_submit: bool = False


@dataclass
class TransactionRow:
    line_id: str = ""
    merchant: str = ""
    date: str = ""
    amount: str = ""
    currency: str = ""
    match_status: str = "unmatched"  # high | medium | low | unmatched
    match_confidence: float = 0.0
    match_reason: str = ""
    translated_merchant_name: str = ""
    matched_receipt: str = ""
    expense_type: str = ""
    expense_source: str = ""
    approved: bool = False
    report_id: str = ""
    report_name: str = ""


@dataclass
class ReceiptDoc:
    source_file: str = ""
    filename: str = ""
    vendor: str = ""
    date: str = ""
    currency: str = ""
    total: str = ""
    confidence: float = 0.0
    notes: str = ""
    rotation: int = 0
    is_image: bool = True
    used: bool = False
    used_by_line_ids: list[str] = field(default_factory=list)
    matched: bool = False
    matched_line_ids: list[str] = field(default_factory=list)
    analyzed: bool = True
    date_added: str = ""
    raw_analysis: dict = field(default_factory=dict)


@dataclass
class MatchReviewItem:
    transaction: TransactionRow
    receipt: ReceiptDoc | None = None
    alternatives: list[ReceiptDoc] = field(default_factory=list)


@dataclass
class ExpenseReportGroup:
    id: str = ""
    name: str = ""
    created_at: str = ""
    line_ids: list[str] = field(default_factory=list)


@dataclass
class AttentionItem:
    line_id: str = ""
    merchant: str = ""
    date: str = ""
    amount: str = ""
    currency: str = ""
    issue: str = ""


@dataclass
class ReportReadiness:
    ready: bool = False
    total_lines: int = 0
    matched: int = 0
    approved: int = 0
    classified: int = 0
    with_receipt: int = 0
    receipt_missing_marked: int = 0
    needs_fix: int = 0
    needs_fix_line_ids: list[str] = field(default_factory=list)
    attention_items: list[AttentionItem] = field(default_factory=list)
    submission_status: str = "Not submitted"


def _confidence_level(c: float) -> str:
    if c >= 0.85:
        return "high"
    if c >= 0.60:
        return "medium"
    return "low"


def _float_safe(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


class ExpenseService:
    def __init__(self, app_dir: Path | None = None):
        self.app_dir = app_dir or APP_DIR

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_transactions(self) -> list[TransactionRow]:
        lines, _ = load_expense_lines_cache(self.app_dir)
        matches = load_receipt_line_matches(self.app_dir)
        approved = load_approved_matches(self.app_dir)
        vendor_types = load_vendor_expense_types_flat(self.app_dir)
        grouped_map = self.get_grouped_line_ids()
        report_groups = {g.id: g.name for g in self.get_expense_report_groups()}

        rows: list[TransactionRow] = []
        for line in lines:
            lid = str(line.get("line_id", "")).strip()
            if not lid:
                continue
            m = matches.get(lid, {})
            conf = _float_safe(m.get("confidence"))
            best = str(m.get("best_receipt") or "").strip()
            status = _confidence_level(conf) if best else "unmatched"
            if not best and conf < 0.40:
                status = "unmatched"

            merchant = str(line.get("merchant_name", "")).strip()
            merchant_key = re.sub(r"\s+", " ", merchant).lower()
            etype = vendor_types.get(merchant_key, "")

            rid = grouped_map.get(lid, "")
            rname = report_groups.get(rid, "") if rid else ""

            rows.append(TransactionRow(
                line_id=lid,
                merchant=merchant,
                date=str(line.get("transaction_date", "")).strip(),
                amount=str(line.get("amount", "")).strip(),
                currency=str(line.get("currency", "USD")).strip(),
                match_status=status,
                match_confidence=conf,
                match_reason=str(m.get("reason", "")).strip(),
                translated_merchant_name=str(m.get("translated_merchant_name") or "").strip(),
                matched_receipt=best,
                expense_type=etype,
                expense_source="user" if etype else "",
                approved=lid in approved,
                report_id=rid,
                report_name=rname,
            ))
        return rows

    def _get_valid_line_ids(self) -> set[str]:
        """Return the set of line_ids that currently exist in the transaction cache."""
        lines, _ = load_expense_lines_cache(self.app_dir)
        return {
            str(row.get("line_id", "")).strip()
            for row in lines
            if str(row.get("line_id", "")).strip()
        }

    def _get_used_receipt_map(self) -> dict[str, list[str]]:
        """Return {receipt_source_file: [line_id, ...]} for receipts attached to an Oracle report."""
        submitted = load_submitted_receipts(self.app_dir)
        valid_ids = self._get_valid_line_ids()
        usage: dict[str, list[str]] = {}
        for sf, block in submitted.items():
            lid = str((block or {}).get("line_id", "") or "").strip()
            if sf and lid and lid in valid_ids:
                usage.setdefault(sf, []).append(lid)
        return usage

    @staticmethod
    def _file_date_added(path_str: str) -> str:
        try:
            ts = Path(path_str).stat().st_mtime
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return ""

    def _get_matched_receipt_map(self) -> dict[str, list[str]]:
        """Return {receipt_source_file: [line_id, ...]} for receipts matched to expense lines.

        Only includes line_ids that still exist in the transaction cache so
        that deleting a transaction immediately un-matches its receipt.
        """
        matches = load_receipt_line_matches(self.app_dir)
        valid_ids = self._get_valid_line_ids()
        match_map: dict[str, list[str]] = {}
        for lid, block in matches.items():
            if lid not in valid_ids:
                continue
            best = str((block or {}).get("best_receipt") or "").strip()
            if best:
                match_map.setdefault(best, []).append(lid)
        return match_map

    def _get_pending_deletion_files(self) -> set[str]:
        """Return the set of receipt file paths scheduled for deferred deletion."""
        pending = load_pending_deletions(self.app_dir)
        files: set[str] = set()
        for entry in pending.values():
            if not isinstance(entry, dict):
                continue
            for sf in entry.get("receipt_files", []):
                sf_str = str(sf).strip()
                if sf_str:
                    files.add(sf_str)
        return files

    def get_receipts(self) -> list[ReceiptDoc]:
        analyses = load_analyses_snapshot(self.app_dir)
        usage_map = self._get_used_receipt_map()
        match_map = self._get_matched_receipt_map()
        pending_files = self._get_pending_deletion_files()
        analyzed_files: set[str] = set()
        docs: list[ReceiptDoc] = []
        for a in analyses:
            sf = str(a.get("source_file", "")).strip()
            if not sf or sf in pending_files:
                continue
            analyzed_files.add(sf)
            ext = Path(sf).suffix.lower()
            line_ids = usage_map.get(sf, [])
            m_line_ids = match_map.get(sf, [])
            docs.append(ReceiptDoc(
                source_file=sf,
                filename=Path(sf).name,
                vendor=str(a.get("vendor", "")).strip(),
                date=str(a.get("receipt_date", "")).strip(),
                currency=str(a.get("currency", "")).strip(),
                total=str(a.get("total_amount") or a.get("matched_amount") or "").strip(),
                confidence=_float_safe(a.get("confidence")),
                notes=str(a.get("notes", "")).strip(),
                rotation=int(a.get("display_rotation_quarter_turns", 0) or 0),
                is_image=ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tiff"),
                used=bool(line_ids),
                used_by_line_ids=line_ids,
                matched=bool(m_line_ids),
                matched_line_ids=m_line_ids,
                analyzed=True,
                date_added=self._file_date_added(sf),
                raw_analysis={k: v for k, v in a.items() if k != "source_file"},
            ))

        state_path = self.app_dir / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
            for p in state.get("receipt_paths", []):
                p_str = str(p).strip()
                if p_str and p_str not in analyzed_files and p_str not in pending_files and Path(p_str).is_file():
                    ext = Path(p_str).suffix.lower()
                    docs.append(ReceiptDoc(
                        source_file=p_str,
                        filename=Path(p_str).name,
                        is_image=ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tiff"),
                        analyzed=False,
                        date_added=self._file_date_added(p_str),
                    ))

        return docs

    def get_receipt_by_path(self, path: str) -> ReceiptDoc | None:
        for r in self.get_receipts():
            if r.source_file == path:
                return r
        return None

    def get_dashboard_stats(self) -> DashboardStats:
        txns = self.get_transactions()
        receipts = self.get_receipts()
        approved = load_approved_matches(self.app_dir)
        high = sum(1 for t in txns if t.match_status == "high")
        med = sum(1 for t in txns if t.match_status == "medium")
        unmatched = sum(1 for t in txns if t.match_status in ("unmatched", "low"))
        classified = sum(1 for t in txns if t.expense_type)
        return DashboardStats(
            total_transactions=len(txns),
            total_receipts=len(receipts),
            matched_high=high,
            matched_medium=med,
            unmatched=unmatched,
            classified=classified,
            unclassified=len(txns) - classified,
            approved=len(approved),
            ready_to_submit=(unmatched == 0 and len(txns) > 0),
        )

    def get_match_review_queue(self) -> list[MatchReviewItem]:
        """Items sorted: unmatched first, then low confidence, then medium, then high."""
        txns = self.get_transactions()
        receipts_by_path = {r.source_file: r for r in self.get_receipts()}

        items: list[MatchReviewItem] = []
        for t in txns:
            receipt = receipts_by_path.get(t.matched_receipt) if t.matched_receipt else None
            items.append(MatchReviewItem(transaction=t, receipt=receipt))

        order = {"unmatched": 0, "low": 1, "medium": 2, "high": 3}
        items.sort(key=lambda x: (order.get(x.transaction.match_status, 4), -x.transaction.match_confidence))
        return items

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def approve_match(self, line_id: str) -> None:
        matches = load_receipt_line_matches(self.app_dir)
        m = matches.get(line_id, {})
        best = str(m.get("best_receipt") or "").strip()
        if not best:
            return
        approved = load_approved_matches(self.app_dir)
        approved[line_id] = {"source_file": best, "approved": True}
        save_approved_matches(self.app_dir, approved)

    def reject_match(self, line_id: str) -> None:
        matches = load_receipt_line_matches(self.app_dir)
        if line_id in matches:
            matches[line_id] = {
                "best_receipt": None,
                "confidence": 0.0,
                "reason": "Rejected by user.",
            }
            save_receipt_line_matches(self.app_dir, matches)
        approved = load_approved_matches(self.app_dir)
        approved.pop(line_id, None)
        save_approved_matches(self.app_dir, approved)

    def mark_receipt_missing(self, line_id: str) -> None:
        """Flag a transaction as 'receipt missing' so it can proceed without a receipt file."""
        matches = load_receipt_line_matches(self.app_dir)
        matches[line_id] = {
            "best_receipt": None,
            "confidence": 0.0,
            "reason": "Marked as receipt missing by user.",
        }
        save_receipt_line_matches(self.app_dir, matches)

    def unmark_receipt_missing(self, line_id: str) -> None:
        """Remove the 'receipt missing' flag so the item requires a receipt again."""
        matches = load_receipt_line_matches(self.app_dir)
        entry = matches.get(line_id, {})
        reason = str(entry.get("reason", "")).lower()
        if "receipt missing" in reason:
            matches.pop(line_id, None)
            save_receipt_line_matches(self.app_dir, matches)

    def set_manual_match(self, line_id: str, receipt_path: str) -> None:
        matches = load_receipt_line_matches(self.app_dir)
        matches[line_id] = {
            "best_receipt": receipt_path,
            "confidence": 0.99,
            "reason": "Manually matched by user.",
        }
        save_receipt_line_matches(self.app_dir, matches)
        approved = load_approved_matches(self.app_dir)
        approved[line_id] = {"source_file": receipt_path, "approved": True}
        save_approved_matches(self.app_dir, approved)

    def approve_all_high_confidence(self) -> int:
        matches = load_receipt_line_matches(self.app_dir)
        approved = load_approved_matches(self.app_dir)
        count = 0
        for lid, m in matches.items():
            conf = _float_safe(m.get("confidence"))
            best = str(m.get("best_receipt") or "").strip()
            if best and conf >= 0.85 and lid not in approved:
                approved[lid] = {"source_file": best, "approved": True}
                count += 1
        if count:
            save_approved_matches(self.app_dir, approved)
        return count

    def set_classification(self, line_id: str, expense_type: str) -> None:
        vendor_path = self.app_dir / "vendor_expense_types.json"
        txns = self.get_transactions()
        merchant = ""
        for t in txns:
            if t.line_id == line_id:
                merchant = t.merchant
                break
        if not merchant:
            return
        merchant_key = re.sub(r"\s+", " ", merchant).lower()

        data: dict[str, Any] = {}
        if vendor_path.exists():
            try:
                data = json.loads(vendor_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        vendors = data.get("vendors", data)
        if not isinstance(vendors, dict):
            vendors = {}
        vendors[merchant_key] = expense_type
        vendor_path.write_text(json.dumps({"vendors": vendors}, indent=2, ensure_ascii=False), encoding="utf-8")

    def set_vendor_classification(self, merchant_key: str, expense_type: str) -> None:
        vendor_path = self.app_dir / "vendor_expense_types.json"
        data: dict[str, Any] = {}
        if vendor_path.exists():
            try:
                data = json.loads(vendor_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        vendors = data.get("vendors", data)
        if not isinstance(vendors, dict):
            vendors = {}
        vendors[merchant_key] = expense_type
        vendor_path.write_text(json.dumps({"vendors": vendors}, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_vendor_classifications(self) -> list[dict[str, str]]:
        """Unique merchant -> expense type mappings from vendor cache + transactions."""
        vendor_types = load_vendor_expense_types_flat(self.app_dir)
        lines, _ = load_expense_lines_cache(self.app_dir)

        seen: dict[str, str] = {}
        for vk, et in vendor_types.items():
            seen[vk] = et

        for line in lines:
            m = str(line.get("merchant_name", "")).strip()
            mk = re.sub(r"\s+", " ", m).lower()
            if mk and mk not in seen:
                seen[mk] = ""

        rows: list[dict[str, str]] = []
        for mk in sorted(seen):
            rows.append({"merchant_key": mk, "expense_type": seen[mk]})
        return rows

    # ------------------------------------------------------------------
    # Expense report groups
    # ------------------------------------------------------------------

    def get_expense_report_groups(self) -> list[ExpenseReportGroup]:
        raw = load_expense_report_groups(self.app_dir)
        groups: list[ExpenseReportGroup] = []
        for rid, data in raw.items():
            line_ids = data.get("line_ids", [])
            if not isinstance(line_ids, list):
                line_ids = []
            groups.append(ExpenseReportGroup(
                id=rid,
                name=str(data.get("name", "")).strip() or "Untitled",
                created_at=str(data.get("created_at", "")).strip(),
                line_ids=[str(x) for x in line_ids],
            ))
        groups.sort(key=lambda g: g.created_at or "")
        return groups

    def create_expense_report_group(self, name: str) -> ExpenseReportGroup:
        raw = load_expense_report_groups(self.app_dir)
        rid = str(_uuid.uuid4())
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw[rid] = {
            "id": rid,
            "name": name.strip(),
            "created_at": now,
            "line_ids": [],
        }
        save_expense_report_groups(self.app_dir, raw)
        return ExpenseReportGroup(id=rid, name=name.strip(), created_at=now, line_ids=[])

    def rename_expense_report_group(self, report_id: str, name: str) -> None:
        raw = load_expense_report_groups(self.app_dir)
        if report_id in raw:
            raw[report_id]["name"] = name.strip()
            save_expense_report_groups(self.app_dir, raw)

    def delete_expense_report_group(self, report_id: str) -> None:
        raw = load_expense_report_groups(self.app_dir)
        raw.pop(report_id, None)
        save_expense_report_groups(self.app_dir, raw)

    def assign_transactions_to_report(
        self, line_ids: list[str], report_id: str | None
    ) -> None:
        """Move line_ids into report_id. Pass None to move back to Ungrouped."""
        raw = load_expense_report_groups(self.app_dir)
        ids_set = set(line_ids)

        # Remove these line_ids from ALL existing groups first
        for rid, data in raw.items():
            existing = data.get("line_ids", [])
            if not isinstance(existing, list):
                existing = []
            data["line_ids"] = [x for x in existing if str(x) not in ids_set]

        # Add to target group (unless ungrouped)
        if report_id and report_id in raw:
            current = raw[report_id].get("line_ids", [])
            if not isinstance(current, list):
                current = []
            existing_set = set(current)
            for lid in line_ids:
                if lid not in existing_set:
                    current.append(lid)
            raw[report_id]["line_ids"] = current

        save_expense_report_groups(self.app_dir, raw)

    def delete_transactions(self, line_ids: list[str]) -> int:
        """Delete transactions by line_id. Also removes from matches, approvals, and report groups."""
        ids = set(line_ids)
        if not ids:
            return 0
        removed, _ = remove_expense_lines_by_ids(self.app_dir, ids)
        # Also remove from report groups
        raw = load_expense_report_groups(self.app_dir)
        changed = False
        for rid, data in raw.items():
            existing = data.get("line_ids", [])
            if not isinstance(existing, list):
                existing = []
            filtered = [x for x in existing if str(x) not in ids]
            if len(filtered) != len(existing):
                data["line_ids"] = filtered
                changed = True
        if changed:
            save_expense_report_groups(self.app_dir, raw)
        return removed

    def get_grouped_line_ids(self) -> dict[str, str]:
        """Return {line_id: report_id} for all assigned transactions."""
        raw = load_expense_report_groups(self.app_dir)
        mapping: dict[str, str] = {}
        for rid, data in raw.items():
            for lid in data.get("line_ids", []):
                mapping[str(lid)] = rid
        return mapping

    def get_report_readiness(self, report_id: str) -> ReportReadiness:
        """Check whether a report is ready to submit.

        A line is OK if it has a receipt file on disk OR its match reason
        indicates it was explicitly marked 'receipt missing'.
        """
        groups = load_expense_report_groups(self.app_dir)
        group = groups.get(report_id)
        if not group:
            return ReportReadiness()

        line_ids = group.get("line_ids", [])
        lid_set = {str(x).strip() for x in line_ids if str(x).strip()}
        if not lid_set:
            return ReportReadiness(
                submission_status=get_report_submission_status(self.app_dir, report_id),
            )

        lines, _ = load_expense_lines_cache(self.app_dir)
        lines_by_id = {str(l.get("line_id", "")).strip(): l for l in lines if isinstance(l, dict)}
        matches = load_receipt_line_matches(self.app_dir)
        approved = load_approved_matches(self.app_dir)
        vendor_types = load_vendor_expense_types_flat(self.app_dir)

        with_receipt = 0
        receipt_missing_marked = 0
        matched_count = 0
        approved_count = 0
        classified_count = 0
        needs_fix: list[str] = []
        attention: list[AttentionItem] = []

        for lid in sorted(lid_set):
            m = matches.get(lid, {})
            best = str(m.get("best_receipt") or "").strip()
            reason = str(m.get("reason") or "").strip().lower()
            conf = _float_safe(m.get("confidence"))
            is_receipt_missing = "receipt missing" in reason

            line = lines_by_id.get(lid, {})
            merchant = str(line.get("merchant_name", "")).strip()
            merchant_key = re.sub(r"\s+", " ", merchant).lower()
            etype = vendor_types.get(merchant_key, "")

            if best:
                matched_count += 1
            if lid in approved:
                approved_count += 1
            if etype:
                classified_count += 1

            if best and Path(best).expanduser().is_file():
                with_receipt += 1
            elif is_receipt_missing:
                receipt_missing_marked += 1
            else:
                needs_fix.append(lid)
                if not best:
                    issue = "No receipt matched"
                elif conf < 0.60:
                    issue = f"Low confidence ({int(conf * 100)}%)"
                else:
                    issue = "Receipt file missing from disk"
                attention.append(AttentionItem(
                    line_id=lid,
                    merchant=merchant,
                    date=str(line.get("transaction_date", "")).strip(),
                    amount=str(line.get("amount", "")).strip(),
                    currency=str(line.get("currency", "USD")).strip(),
                    issue=issue,
                ))

        return ReportReadiness(
            ready=len(needs_fix) == 0 and len(lid_set) > 0,
            total_lines=len(lid_set),
            matched=matched_count,
            approved=approved_count,
            classified=classified_count,
            with_receipt=with_receipt,
            receipt_missing_marked=receipt_missing_marked,
            needs_fix=len(needs_fix),
            needs_fix_line_ids=needs_fix,
            attention_items=attention,
            submission_status=get_report_submission_status(self.app_dir, report_id),
        )

    def delete_report_completely(self, report_id: str) -> dict[str, Any]:
        """Delete a report, its transactions, and associated receipt documents.

        Merchant classifications (vendor_expense_types.json) are preserved.
        """
        return delete_report_with_data(self.app_dir, report_id)

    def mark_report_submitted(self, report_id: str) -> dict[str, Any]:
        """Soft-delete a report: hide from views, schedule permanent deletion in 5 days."""
        return soft_delete_report(self.app_dir, report_id, countdown_days=5)

    def get_pending_deletions(self) -> list[dict[str, Any]]:
        """Return all reports pending permanent deletion, sorted by deletion date."""
        raw = load_pending_deletions(self.app_dir)
        items = list(raw.values())
        items.sort(key=lambda x: x.get("delete_after", ""))
        return items

    def restore_report(self, report_id: str) -> dict[str, Any]:
        """Restore a soft-deleted report back to active views."""
        return restore_pending_deletion(self.app_dir, report_id)

    def purge_expired(self) -> list[dict[str, Any]]:
        """Permanently delete reports whose countdown has expired."""
        return purge_expired_deletions(self.app_dir)

    def prepare_report_for_submission(self, report_id: str) -> dict[str, Any]:
        """Approve all matched lines in this report so they are ready for Oracle automation.

        Returns summary of what was approved.  Lines marked receipt-missing are
        left without an approval entry (Oracle will get 'Original Receipt Missing').
        """
        groups = load_expense_report_groups(self.app_dir)
        group = groups.get(report_id)
        if not group:
            return {"error": "Report not found"}

        line_ids = group.get("line_ids", [])
        lid_set = {str(x).strip() for x in line_ids if str(x).strip()}
        if not lid_set:
            return {"error": "Report has no transactions"}

        matches = load_receipt_line_matches(self.app_dir)
        approved = load_approved_matches(self.app_dir)
        count = 0

        for lid in lid_set:
            m = matches.get(lid, {})
            best = str(m.get("best_receipt") or "").strip()
            if best and Path(best).expanduser().is_file():
                approved[lid] = {"source_file": best, "approved": True}
                count += 1

        save_approved_matches(self.app_dir, approved)
        return {"approved": count, "total": len(lid_set)}

    def run_matching_pipeline(self, on_status=None) -> dict[str, Any]:
        """Run deterministic + LLM matching. Returns summary dict."""
        from matching.pipeline import match_transactions_to_receipts

        log = on_status or (lambda s: None)
        lines, _ = load_expense_lines_cache(self.app_dir)
        analyses = load_analyses_snapshot(self.app_dir)
        if not lines:
            return {"error": "No transactions loaded"}
        if not analyses:
            return {"error": "No receipts analyzed"}

        existing_matches = load_receipt_line_matches(self.app_dir)
        deterministic = match_transactions_to_receipts(lines, analyses)

        updated = 0
        for row in deterministic:
            tid = str(row.get("transaction_id", "")).strip()
            rid = str(row.get("receipt_id") or "").strip()
            conf = float(row.get("confidence", 0))
            if not tid or not rid or conf < 0.60:
                continue
            if tid in existing_matches:
                old_conf = _float_safe(existing_matches[tid].get("confidence"))
                if old_conf >= conf:
                    continue
            existing_matches[tid] = {
                "best_receipt": rid,
                "confidence": conf,
                "reason": str(row.get("reasoning", "Deterministic match")),
            }
            updated += 1

        save_receipt_line_matches(self.app_dir, existing_matches)
        log(f"Updated {updated} matches")
        return {"updated": updated, "total": len(lines)}

    def run_full_matching_pipeline(self, on_status=None) -> dict[str, Any]:
        """Full matching pipeline: deterministic pass then LLM for remaining items."""
        from web.activity_log import activity_log
        from matching.pipeline import match_transactions_to_receipts

        lines, _ = load_expense_lines_cache(self.app_dir)
        analyses = load_analyses_snapshot(self.app_dir)

        if not lines:
            activity_log.emit("error", "No transactions loaded")
            return {"error": "No transactions loaded"}
        if not analyses:
            activity_log.emit("error", "No receipts analyzed")
            return {"error": "No receipts analyzed"}

        activity_log.emit(
            "step", f"Loaded {len(lines)} transactions, {len(analyses)} receipts"
        )

        # -- Phase 1: deterministic --
        activity_log.emit(
            "match", "Phase 1 \u2014 Deterministic matching (amount \u00b7 date \u00b7 merchant)"
        )
        existing_matches = load_receipt_line_matches(self.app_dir)
        deterministic = match_transactions_to_receipts(lines, analyses)

        det_updated = 0
        for row in deterministic:
            tid = str(row.get("transaction_id", "")).strip()
            rid = str(row.get("receipt_id") or "").strip()
            try:
                conf = float(row.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            if not tid or not rid or conf < 0.60:
                continue
            if tid in existing_matches:
                old_conf = _float_safe(existing_matches[tid].get("confidence"))
                if old_conf >= conf:
                    continue
            receipt_name = Path(rid).name if rid else "?"
            activity_log.emit(
                "cache",
                f"{tid[:12]}\u2026 \u2192 {receipt_name} ({int(conf * 100)}%)",
            )
            existing_matches[tid] = {
                "best_receipt": rid,
                "confidence": conf,
                "reason": str(row.get("reasoning", "Deterministic match")),
            }
            det_updated += 1

        save_receipt_line_matches(self.app_dir, existing_matches)
        activity_log.emit("match", f"Deterministic complete: {det_updated} matched")

        # -- Phase 2: LLM for remaining --
        api_key = self._get_openai_key() or os.environ.get("OPENAI_API_KEY", "")
        raw_settings = self._load_settings_raw()
        model = (
            raw_settings.get("openai_model")
            or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        )

        if not api_key:
            activity_log.emit(
                "info", "No API key configured \u2014 skipping LLM matching"
            )
            return {
                "deterministic_updated": det_updated,
                "llm_updated": 0,
                "total": len(lines),
            }

        matches = load_receipt_line_matches(self.app_dir)
        unmatched: list[dict] = []
        for line in lines:
            lid = str(line.get("line_id", "")).strip()
            if not lid:
                continue
            m = matches.get(lid, {})
            best = str(m.get("best_receipt") or "").strip()
            conf = _float_safe(m.get("confidence"))
            if not best or conf < 0.60:
                unmatched.append(line)

        if not unmatched:
            activity_log.emit("success", "All transactions already matched")
            return {
                "deterministic_updated": det_updated,
                "llm_updated": 0,
                "total": len(lines),
            }

        activity_log.emit(
            "step", f"Phase 2 \u2014 LLM matching ({len(unmatched)} items)"
        )

        from receipt_matching import match_one_expense_line_to_receipts

        llm_updated = 0
        consecutive_connection_errors = 0
        _CONNECTION_ERROR_LIMIT = 3
        for i, line in enumerate(unmatched):
            lid = str(line.get("line_id", "")).strip()
            merchant = str(line.get("merchant_name", "")).strip() or "unknown"
            amount = str(line.get("amount", "")).strip()

            activity_log.mark_item_processing(lid)
            activity_log.set_progress(
                i / len(unmatched), f"{i + 1}/{len(unmatched)}"
            )
            activity_log.emit("llm", f"Sending to LLM: {merchant} ({amount})")

            try:
                result = match_one_expense_line_to_receipts(
                    api_key=api_key,
                    model=model,
                    line=line,
                    analyses=analyses,
                    on_status=lambda msg: activity_log.emit("llm", msg),
                )
                consecutive_connection_errors = 0
                if result.get("best_receipt"):
                    rname = Path(str(result["best_receipt"])).name
                    rconf = float(result.get("confidence", 0))
                    activity_log.emit(
                        "llm",
                        f"\u2190 {merchant} \u2192 {rname} ({int(rconf * 100)}%)",
                    )
                    matches[lid] = result
                    llm_updated += 1
                else:
                    activity_log.emit("info", f"No match found for {merchant}")
            except Exception as exc:
                exc_text = f"{exc!r}"
                is_connection_error = any(
                    kw in exc_text.upper()
                    for kw in ("CONNECTION", "SSL", "CERTIFICATE", "TLS", "TIMEOUT", "NETWORK")
                )
                if is_connection_error:
                    consecutive_connection_errors += 1
                else:
                    consecutive_connection_errors = 0

                activity_log.emit("error", f"LLM error for {merchant}: {exc}")

                if consecutive_connection_errors >= _CONNECTION_ERROR_LIMIT:
                    activity_log.mark_item_done(lid)
                    activity_log.emit(
                        "error",
                        f"Stopping — {_CONNECTION_ERROR_LIMIT} consecutive connection "
                        "failures. Some corporate VPNs block OpenAI API calls. "
                        "Disconnect from VPN and try again, or verify your API key "
                        "is valid in Settings.",
                    )
                    raise RuntimeError(
                        f"OpenAI API unreachable ({_CONNECTION_ERROR_LIMIT} consecutive failures). "
                        "Your VPN may be blocking the connection. Disconnect from VPN "
                        "and try again, or check your API key in Settings."
                    ) from exc
            finally:
                activity_log.mark_item_done(lid)

        save_receipt_line_matches(self.app_dir, matches)
        activity_log.set_progress(1.0, "Complete")
        activity_log.emit(
            "success",
            f"Pipeline complete \u2014 deterministic: {det_updated}, LLM: {llm_updated}",
        )
        return {
            "deterministic_updated": det_updated,
            "llm_updated": llm_updated,
            "total": len(lines),
        }

    def rescan_lines_for_match(self, line_ids: list[str], on_status=None) -> dict[str, Any]:
        """Re-run deterministic + LLM matching for specific line IDs only."""
        from web.activity_log import activity_log
        from matching.pipeline import match_transactions_to_receipts
        from receipt_matching import match_one_expense_line_to_receipts

        lid_set = {str(x).strip() for x in line_ids if str(x).strip()}
        if not lid_set:
            return {"error": "No line IDs provided"}

        lines, _ = load_expense_lines_cache(self.app_dir)
        analyses = load_analyses_snapshot(self.app_dir)
        if not lines or not analyses:
            return {"error": "No transactions or receipts loaded"}

        target_lines = [l for l in lines if str(l.get("line_id", "")).strip() in lid_set]
        if not target_lines:
            return {"error": "None of the specified lines found"}

        matches = load_receipt_line_matches(self.app_dir)
        for lid in lid_set:
            matches.pop(lid, None)

        # Exclude receipts already matched to lines NOT in this rescan set
        # to prevent a rescanned line from "stealing" another line's receipt.
        already_taken: set[str] = set()
        for other_lid, block in matches.items():
            if other_lid not in lid_set:
                taken = str((block or {}).get("best_receipt") or "").strip()
                if taken:
                    already_taken.add(taken)
        available_analyses = [
            a for a in analyses
            if str(a.get("source_file", "")).strip() not in already_taken
        ]

        activity_log.emit("step", f"Rescanning {len(target_lines)} line(s)")

        det_results = match_transactions_to_receipts(target_lines, available_analyses)
        det_updated = 0
        for row in det_results:
            tid = str(row.get("transaction_id", "")).strip()
            rid = str(row.get("receipt_id") or "").strip()
            try:
                conf = float(row.get("confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            if not tid or not rid or conf < 0.60 or tid not in lid_set:
                continue
            matches[tid] = {
                "best_receipt": rid,
                "confidence": conf,
                "reason": str(row.get("reasoning", "Deterministic match")),
            }
            det_updated += 1

        api_key = self._get_openai_key() or os.environ.get("OPENAI_API_KEY", "")
        raw_settings = self._load_settings_raw()
        model = raw_settings.get("openai_model") or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

        llm_updated = 0
        if api_key:
            still_unmatched = [
                l for l in target_lines
                if str(l.get("line_id", "")).strip() not in matches
                or not str(matches.get(str(l.get("line_id", "")).strip(), {}).get("best_receipt") or "").strip()
            ]
            consecutive_connection_errors = 0
            for i, line in enumerate(still_unmatched):
                lid = str(line.get("line_id", "")).strip()
                merchant = str(line.get("merchant_name", "")).strip() or "unknown"
                activity_log.mark_item_processing(lid)
                activity_log.emit("llm", f"Rescanning {i+1}/{len(still_unmatched)}: {merchant}")
                try:
                    result = match_one_expense_line_to_receipts(
                        api_key=api_key, model=model, line=line, analyses=available_analyses,
                        on_status=lambda msg: activity_log.emit("llm", msg),
                    )
                    consecutive_connection_errors = 0
                    if result.get("best_receipt"):
                        matches[lid] = result
                        llm_updated += 1
                except Exception as exc:
                    exc_text = f"{exc!r}"
                    is_conn = any(
                        kw in exc_text.upper()
                        for kw in ("CONNECTION", "SSL", "CERTIFICATE", "TLS", "TIMEOUT", "NETWORK")
                    )
                    if is_conn:
                        consecutive_connection_errors += 1
                    else:
                        consecutive_connection_errors = 0
                    activity_log.emit("error", f"LLM error for {merchant}: {exc}")
                    if consecutive_connection_errors >= 3:
                        activity_log.mark_item_done(lid)
                        raise RuntimeError(
                            "OpenAI API unreachable (3 consecutive failures). "
                            "Your VPN may be blocking the connection. Disconnect "
                            "from VPN and try again, or check your API key in Settings."
                        ) from exc
                finally:
                    activity_log.mark_item_done(lid)

        save_receipt_line_matches(self.app_dir, matches)
        activity_log.emit("success", f"Rescan complete \u2014 det: {det_updated}, LLM: {llm_updated}")
        return {"deterministic_updated": det_updated, "llm_updated": llm_updated}

    def run_llm_matching(self, on_status=None) -> dict[str, Any]:
        """Run LLM matching for unmatched items. Requires OPENAI_API_KEY."""
        from receipt_matching import match_one_expense_line_to_receipts

        log = on_status or (lambda s: None)
        api_key = os.environ.get("OPENAI_API_KEY", "")
        model = os.environ.get("OPENAI_MODEL", "gpt-4o")
        if not api_key:
            return {"error": "OPENAI_API_KEY not set"}

        lines, _ = load_expense_lines_cache(self.app_dir)
        analyses = load_analyses_snapshot(self.app_dir)
        matches = load_receipt_line_matches(self.app_dir)

        unmatched = [
            l for l in lines
            if str(l.get("line_id", "")).strip()
            and (
                str(l.get("line_id", "")).strip() not in matches
                or not str(matches.get(str(l.get("line_id", "")).strip(), {}).get("best_receipt") or "").strip()
            )
        ]

        if not unmatched:
            return {"updated": 0, "message": "All lines already matched"}

        updated = 0
        consecutive_connection_errors = 0
        for i, line in enumerate(unmatched):
            lid = str(line.get("line_id", "")).strip()
            log(f"Matching {i+1}/{len(unmatched)}: {line.get('merchant_name', 'unknown')}")
            try:
                result = match_one_expense_line_to_receipts(
                    api_key=api_key,
                    model=model,
                    line=line,
                    analyses=analyses,
                    on_status=log,
                )
                consecutive_connection_errors = 0
                if result.get("best_receipt"):
                    matches[lid] = result
                    updated += 1
            except Exception as exc:
                exc_text = f"{exc!r}"
                is_conn = any(
                    kw in exc_text.upper()
                    for kw in ("CONNECTION", "SSL", "CERTIFICATE", "TLS", "TIMEOUT", "NETWORK")
                )
                if is_conn:
                    consecutive_connection_errors += 1
                else:
                    consecutive_connection_errors = 0
                log(f"Error matching {lid}: {exc}")
                if consecutive_connection_errors >= 3:
                    raise RuntimeError(
                        "OpenAI API unreachable (3 consecutive failures). "
                        "Your VPN may be blocking the connection. Disconnect "
                        "from VPN and try again, or check your API key in Settings."
                    ) from exc

        save_receipt_line_matches(self.app_dir, matches)
        return {"updated": updated, "attempted": len(unmatched)}

    def analyze_receipts(self, on_status=None) -> dict[str, Any]:
        """Run LLM vision analysis on un-analyzed receipt files."""
        from browser_automation import analyze_receipts_with_llm

        log = on_status or (lambda s: None)
        api_key = self._get_openai_key() or os.environ.get("OPENAI_API_KEY", "")
        raw = self._load_settings_raw()
        model = raw.get("openai_model") or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        if not api_key:
            return {"error": "OPENAI_API_KEY not set"}

        state_path = self.app_dir / "state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}

        receipt_paths = state.get("receipt_paths", [])
        existing = load_analyses_snapshot(self.app_dir)
        existing_files = {str(a.get("source_file", "")).strip() for a in existing}

        new_paths = [p for p in receipt_paths if p not in existing_files and Path(p).is_file()]
        if not new_paths:
            return {"analyzed": 0, "message": "All receipts already analyzed"}

        from web.activity_log import activity_log

        activity_log.emit("step", f"Analyzing {len(new_paths)} receipts with {model}")
        try:
            new_analyses = analyze_receipts_with_llm(
                new_paths, model, api_key, on_status=log
            )
            all_analyses = existing + new_analyses
            save_analyses_snapshot(self.app_dir, all_analyses)
            activity_log.emit("success", f"Analyzed {len(new_analyses)} receipts")
            return {"analyzed": len(new_analyses)}
        except Exception as exc:
            activity_log.emit("error", f"Receipt analysis failed: {exc}")
            return {"error": str(exc)}

    def import_receipt_files(self, file_paths: list[str]) -> int:
        """Copy receipt files into the managed import directory."""
        import_dir = self.app_dir / "receipt-imports"
        import_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        state_path = self.app_dir / "state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        receipt_paths: list[str] = state.get("receipt_paths", [])

        for fp in file_paths:
            src = Path(fp)
            if not src.is_file():
                continue
            dest = import_dir / src.name
            if dest.exists():
                stem = src.stem
                suffix = src.suffix
                i = 1
                while dest.exists():
                    dest = import_dir / f"{stem}_{i}{suffix}"
                    i += 1
            shutil.copy2(str(src), str(dest))
            dest_str = str(dest)
            if dest_str not in receipt_paths:
                receipt_paths.append(dest_str)
            count += 1

        state["receipt_paths"] = receipt_paths
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        return count

    def import_uploaded_content(self, filename: str, content: bytes) -> str | None:
        """Write uploaded bytes into receipt-imports and register in state.json.

        Returns the destination path on success, or None on failure.
        """
        import_dir = self.app_dir / "receipt-imports"
        import_dir.mkdir(parents=True, exist_ok=True)

        dest = import_dir / filename
        if dest.exists():
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            i = 1
            while dest.exists():
                dest = import_dir / f"{stem}_{i}{suffix}"
                i += 1

        dest.write_bytes(content)
        dest_str = str(dest)

        state_path = self.app_dir / "state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        receipt_paths: list[str] = state.get("receipt_paths", [])
        if dest_str not in receipt_paths:
            receipt_paths.append(dest_str)
        state["receipt_paths"] = receipt_paths
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        return dest_str

    def remove_used_receipts(self) -> int:
        """Remove receipts that have been attached to a submitted Oracle expense report.

        Removes them from receipt_analyses_snapshot.json and state.json
        receipt_paths so they no longer appear on the Documents page.
        The actual files are kept on disk (the report already references them).
        """
        usage_map = self._get_used_receipt_map()
        if not usage_map:
            return 0

        used_paths = set(usage_map.keys())

        analyses = load_analyses_snapshot(self.app_dir)
        kept = [a for a in analyses if str(a.get("source_file", "")).strip() not in used_paths]
        removed = len(analyses) - len(kept)
        if removed:
            save_analyses_snapshot(self.app_dir, kept)

        state_path = self.app_dir / "state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        rp = state.get("receipt_paths", [])
        if isinstance(rp, list):
            state["receipt_paths"] = [p for p in rp if p not in used_paths]
            state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

        return removed

    def rescan_receipts(self, source_files: list[str], on_status=None) -> dict[str, Any]:
        """Force re-analyze specific receipt files with the LLM."""
        from browser_automation import analyze_receipts_with_llm

        log = on_status or (lambda s: None)
        api_key = self._get_openai_key() or os.environ.get("OPENAI_API_KEY", "")
        raw = self._load_settings_raw()
        model = raw.get("openai_model") or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
        if not api_key:
            return {"error": "OPENAI_API_KEY not set"}

        valid = [p for p in source_files if Path(p).is_file()]
        if not valid:
            return {"rescanned": 0, "message": "No valid files to rescan"}

        log(f"Rescanning {len(valid)} receipt(s)...")
        try:
            new_analyses = analyze_receipts_with_llm(
                valid, model, api_key, on_status=log
            )
        except Exception as exc:
            return {"error": str(exc)}

        rescan_set = set(valid)
        existing = load_analyses_snapshot(self.app_dir)
        kept = [a for a in existing if str(a.get("source_file", "")).strip() not in rescan_set]
        all_analyses = kept + new_analyses
        save_analyses_snapshot(self.app_dir, all_analyses)
        return {"rescanned": len(new_analyses)}

    def update_receipt_fields(self, source_file: str, updates: dict[str, Any]) -> bool:
        """Update specific fields on a receipt's analysis entry and persist."""
        analyses = load_analyses_snapshot(self.app_dir)
        for a in analyses:
            if str(a.get("source_file", "")).strip() == source_file:
                for key, val in updates.items():
                    a[key] = val
                save_analyses_snapshot(self.app_dir, analyses)
                return True
        return False

    def remove_receipts(self, source_files: list[str]) -> int:
        """Remove specified receipts from the documents view.

        Files are kept on disk but removed from the analyses snapshot
        and state.json receipt_paths so they no longer appear on the
        Documents page.
        """
        if not source_files:
            return 0
        paths_to_remove = set(source_files)

        analyses = load_analyses_snapshot(self.app_dir)
        kept = [a for a in analyses if str(a.get("source_file", "")).strip() not in paths_to_remove]
        if len(kept) < len(analyses):
            save_analyses_snapshot(self.app_dir, kept)

        state_path = self.app_dir / "state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        rp = state.get("receipt_paths", [])
        if isinstance(rp, list):
            state["receipt_paths"] = [p for p in rp if p not in paths_to_remove]
        # Also clean up receipt-report assignments
        assignments = state.get("receipt_report_assignments", {})
        if isinstance(assignments, dict):
            state["receipt_report_assignments"] = {
                k: v for k, v in assignments.items() if k not in paths_to_remove
            }
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

        return len(paths_to_remove)

    # ------------------------------------------------------------------
    # Receipt ↔ report direct assignments
    # ------------------------------------------------------------------

    def get_receipt_report_assignments(self) -> dict[str, str]:
        """Return {source_file: report_id} for receipts directly assigned to a report."""
        state_path = self.app_dir / "state.json"
        if not state_path.exists():
            return {}
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        raw = state.get("receipt_report_assignments", {})
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def assign_receipts_to_report(self, source_files: list[str], report_id: str) -> None:
        """Directly assign receipt files to a report so they appear under that report's filter."""
        if not source_files or not report_id:
            return
        state_path = self.app_dir / "state.json"
        state: dict[str, Any] = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        assignments: dict[str, str] = state.get("receipt_report_assignments", {})
        if not isinstance(assignments, dict):
            assignments = {}
        for sf in source_files:
            assignments[sf] = report_id
        state["receipt_report_assignments"] = assignments
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # Transaction scraping (Playwright / Oracle)
    # ------------------------------------------------------------------

    def run_scrape(self, *, on_status: Callable[[str], None] | None = None) -> dict[str, int]:
        """Launch Chromium, scrape Oracle credit-card transactions, persist to cache.

        Returns dict with keys: scraped, before, after, new.
        """
        from browser.scraper import TransactionScraper

        raw = self._load_settings_raw()
        portal_url = (raw.get("legacy_url") or "").strip()
        approver = (raw.get("approver") or "").strip()

        if not portal_url:
            raise RuntimeError(
                "Portal URL not configured. Set it in Settings before scraping."
            )
        if not approver:
            raise RuntimeError(
                "Oracle approver not configured. Set the approver display name in Settings."
            )

        before, _ = load_expense_lines_cache(self.app_dir)
        before_count = len(before)

        scraper = TransactionScraper(on_status=on_status, nav_menu_label=raw.get("nav_menu_label", ""))
        rows = scraper.scrape(portal_url, approver)
        scraped = len(rows)

        after, _ = load_expense_lines_cache(self.app_dir)
        after_count = len(after)
        new_count = max(0, after_count - before_count)

        return {"scraped": scraped, "before": before_count, "after": after_count, "new": new_count}

    # ------------------------------------------------------------------
    # Submission state (persists CDP URL for resume-on-failure)
    # ------------------------------------------------------------------

    def _submission_state_path(self) -> Path:
        return self.app_dir / "submission_state.json"

    def _load_submission_state(self) -> dict[str, Any] | None:
        p = self._submission_state_path()
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save_submission_state(self, state: dict[str, Any]) -> None:
        p = self._submission_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _clear_submission_state(self) -> None:
        p = self._submission_state_path()
        p.unlink(missing_ok=True)

    def get_pending_submission(self, report_id: str) -> dict[str, Any] | None:
        """Return the incomplete submission state for *report_id*, or None."""
        state = self._load_submission_state()
        if (
            state
            and state.get("report_id") == report_id
            and not state.get("completed")
        ):
            return state
        return None

    # ------------------------------------------------------------------
    # Submission automation
    # ------------------------------------------------------------------

    def run_submission(
        self,
        report_id: str,
        *,
        on_status: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Launch Chromium, navigate the Oracle wizard, and submit the report.

        If a previous submission attempt for the same report left a browser
        window open, this method will attempt to reconnect and resume the
        wizard from where it stopped.  If reconnection fails or the wizard
        state cannot be determined, the browser is closed and the flow
        restarts from scratch.
        """
        from browser.submitter import ReportSubmitter, SubmissionLine, SubmissionPayload

        raw = self._load_settings_raw()
        portal_url = (raw.get("legacy_url") or "").strip()
        approver = (raw.get("approver") or "").strip()

        if not portal_url:
            raise RuntimeError("Portal URL not configured. Set it in Settings before submitting.")
        if not approver:
            raise RuntimeError(
                "Oracle approver not configured. Set the approver display name in Settings."
            )

        groups = load_expense_report_groups(self.app_dir)
        group = groups.get(report_id)
        if not group:
            raise RuntimeError("Report not found.")

        report_name = str(group.get("name", "")).strip() or "Expense Report"
        line_ids = {str(x).strip() for x in group.get("line_ids", []) if str(x).strip()}
        if not line_ids:
            raise RuntimeError("Report has no transactions to submit.")

        lines_cache, _ = load_expense_lines_cache(self.app_dir)
        lines_by_id = {
            str(l.get("line_id", "")).strip(): l
            for l in lines_cache if isinstance(l, dict)
        }
        matches = load_receipt_line_matches(self.app_dir)
        approved = load_approved_matches(self.app_dir)
        vendor_types = load_vendor_expense_types_flat(self.app_dir)

        submission_lines: list[SubmissionLine] = []
        for lid in sorted(line_ids):
            line = lines_by_id.get(lid, {})
            merchant = str(line.get("merchant_name", "")).strip()
            if not merchant:
                continue

            merchant_key = re.sub(r"\s+", " ", merchant).lower()
            expense_type = vendor_types.get(merchant_key, "")

            m = matches.get(lid, {})
            reason = str(m.get("reason") or "").strip().lower()
            is_missing = "receipt missing" in reason

            receipt_path: str | None = None
            if lid in approved:
                receipt_path = str(approved[lid].get("source_file", "") or "").strip() or None

            submission_lines.append(SubmissionLine(
                line_id=lid,
                merchant_name=merchant,
                transaction_date=str(line.get("transaction_date", "") or "").strip(),
                amount=str(line.get("amount", "") or "").strip(),
                currency=str(line.get("currency", "") or "").strip(),
                expense_type=expense_type,
                receipt_path=receipt_path,
                receipt_missing=is_missing,
            ))

        payload = SubmissionPayload(
            report_name=report_name,
            approver=approver,
            lines=submission_lines,
        )

        pending = self.get_pending_submission(report_id)
        resume_cdp_url = pending.get("cdp_url") if pending else None

        submitter = ReportSubmitter(on_status=on_status, nav_menu_label=raw.get("nav_menu_label", ""))
        try:
            result = submitter.submit(
                portal_url,
                payload,
                resume_cdp_url=resume_cdp_url,
            )
            self._save_submission_state({
                "report_id": report_id,
                "cdp_url": submitter._cdp_http_url,
                "completed": True,
            })
            return result
        except Exception as exc:
            cdp_url = getattr(submitter, "_cdp_http_url", None)
            if cdp_url:
                self._save_submission_state({
                    "report_id": report_id,
                    "cdp_url": cdp_url,
                    "completed": False,
                    "error": str(exc),
                })
            raise

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _settings_path(self) -> Path:
        return self.app_dir / "settings.json"

    def get_settings(self) -> dict[str, str]:
        """Return user-facing settings (secrets are masked)."""
        raw = self._load_settings_raw()
        openai_key = self._get_openai_key()
        return {
            "oracle_url": raw.get("legacy_url", ""),
            "approver": raw.get("approver", ""),
            "openai_key_set": bool(openai_key),
            "openai_key_hint": f"...{openai_key[-4:]}" if len(openai_key) >= 4 else "",
            "openai_model": raw.get("openai_model", "gpt-4.1-mini"),
            "nav_menu_label": raw.get("nav_menu_label", ""),
        }

    def credentials_ready(self) -> bool:
        """True when all required secrets/credentials have been entered."""
        s = self.get_settings()
        return bool(
            s["oracle_url"]
            and str(s.get("approver", "")).strip()
            and s["openai_key_set"]
        )

    def missing_credentials(self) -> list[str]:
        """Return human-readable list of credentials still needed."""
        s = self.get_settings()
        missing: list[str] = []
        if not s["openai_key_set"]:
            missing.append("OpenAI API Key")
        if not s["oracle_url"]:
            missing.append("Oracle Portal URL")
        if not str(s.get("approver", "")).strip():
            missing.append("Oracle approver display name")
        return missing

    def save_settings(
        self,
        oracle_url: str | None = None,
        approver: str | None = None,
        openai_key: str | None = None,
        openai_model: str | None = None,
        nav_menu_label: str | None = None,
    ) -> list[str]:
        """Persist settings. Returns list of warnings (empty = all good)."""
        warnings: list[str] = []
        raw = self._load_settings_raw()

        if oracle_url is not None:
            raw["legacy_url"] = oracle_url.strip()
        if approver is not None:
            raw["approver"] = approver.strip()
        if openai_model is not None:
            raw["openai_model"] = openai_model.strip()
        if nav_menu_label is not None:
            raw["nav_menu_label"] = nav_menu_label.strip()
        raw.pop("expense_username", None)

        self._settings_path().parent.mkdir(parents=True, exist_ok=True)
        self._settings_path().write_text(
            json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if openai_key is not None and openai_key.strip():
            w = self._set_openai_key(openai_key.strip())
            if w:
                warnings.append(w)

        os.environ["OPENAI_API_KEY"] = openai_key.strip() if openai_key and openai_key.strip() else self._get_openai_key()
        if openai_model and openai_model.strip():
            os.environ["OPENAI_MODEL"] = openai_model.strip()

        return warnings

    def _load_settings_raw(self) -> dict[str, Any]:
        p = self._settings_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _get_openai_key(self) -> str:
        try:
            val = keychain_credentials.get_keychain_openai_key()
            if val:
                return val.strip()
        except Exception:
            pass
        raw = self._load_settings_raw()
        if raw.get("openai_api_key"):
            return str(raw["openai_api_key"]).strip()
        return os.environ.get("OPENAI_API_KEY", "") or ""

    def _set_openai_key(self, key: str) -> str | None:
        stripped = key.strip()
        err = keychain_credentials.set_keychain_openai_key(key)
        if err is None:
            return None
        raw = self._load_settings_raw()
        raw["openai_api_key"] = key
        self._settings_path().write_text(
            json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return f"Saved to settings file (keychain unavailable: {err})"

    # ------------------------------------------------------------------
    # Factory reset
    # ------------------------------------------------------------------

    def reset_all_data(self) -> None:
        """Delete all user data, caches, settings, and keychain entries.

        Wipes ``~/.expense-automator/`` back to an empty state and clears
        keychain credentials so the app behaves as a fresh install.
        """
        # Clear keychain secrets
        try:
            keychain_credentials.delete_keychain_openai_key()
        except Exception:
            pass

        # Remove every file and subdirectory inside app_dir
        if self.app_dir.is_dir():
            for child in list(self.app_dir.iterdir()):
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except Exception:
                    pass
