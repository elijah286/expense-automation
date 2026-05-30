"""
Oracle expense report submission via Playwright CDP.

Extends TransactionScraper with the full submission workflow:
Step 1  General Information (template, purpose, approver)
Step 2  Select credit-card transactions belonging to this report
Step 3  Set expense types per line
Step 4–5  Click Next (no user action required)
Step 6  Attach receipt files
Final   Submit the report
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import Frame

from browser.scraper import TransactionScraper, _blob_shows_wizard_step

APP_DIR = Path.home() / ".expense-automator"


@dataclass
class SubmissionLine:
    """One transaction line to submit with its receipt and classification."""

    line_id: str
    merchant_name: str
    transaction_date: str
    amount: str
    currency: str
    expense_type: str
    receipt_path: str | None = None
    receipt_missing: bool = False


@dataclass
class SubmissionPayload:
    """Everything needed to submit a report through the Oracle wizard."""

    report_name: str
    approver: str
    lines: list[SubmissionLine] = field(default_factory=list)


class ReportSubmitter(TransactionScraper):
    """Drives the Oracle iExpenses wizard to create and submit an expense report."""

    # ------------------------------------------------------------------
    # Existing report detection (update-or-create flow)
    # ------------------------------------------------------------------

    _FIND_EXISTING_REPORT_JS = """
(reportName) => {
  const norm = (v) => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const wantName = norm(reportName);
  if (!wantName) return false;
  const isVisible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
  };
  const tables = Array.from(document.querySelectorAll('table'));
  for (const table of tables) {
    const headerRow = table.querySelector('thead tr') || table.querySelector('tr');
    if (!headerRow) continue;
    const headers = Array.from(headerRow.querySelectorAll('th, td')).map(c => norm(c.textContent || ''));
    const purposeIdx = headers.findIndex(h => h.includes('purpose'));
    const updateIdx = headers.findIndex(h => h.includes('update'));
    if (purposeIdx < 0) continue;
    const bodyRows = table.tBodies && table.tBodies.length
      ? Array.from(table.tBodies[0].querySelectorAll('tr'))
      : Array.from(table.querySelectorAll('tr')).filter(tr => tr.querySelector('td'));
    for (const tr of bodyRows) {
      const cells = Array.from(tr.querySelectorAll('td'));
      if (purposeIdx >= cells.length) continue;
      const purpose = norm(cells[purposeIdx].innerText || cells[purposeIdx].textContent || '');
      if (purpose !== wantName) continue;
      if (updateIdx >= 0 && updateIdx < cells.length) {
        const updateCell = cells[updateIdx];
        const clickables = updateCell.querySelectorAll('a, button, img, [role="button"], input[type="image"]');
        for (const el of clickables) {
          if (isVisible(el)) { el.click(); return true; }
        }
      }
      const allClickables = tr.querySelectorAll('img, a, button');
      for (const el of allClickables) {
        const blob = norm([el.getAttribute('alt') || '', el.getAttribute('title') || '',
          el.getAttribute('src') || '', el.getAttribute('class') || ''].join(' '));
        if ((blob.includes('update') || blob.includes('edit') || blob.includes('pencil'))
            && isVisible(el)) {
          el.click();
          return true;
        }
      }
    }
  }
  return false;
}
"""

    _UPDATE_TABLE_VISIBLE_JS = """
() => {
  const norm = (v) => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  for (const table of document.querySelectorAll('table')) {
    const hr = table.querySelector('thead tr') || table.querySelector('tr');
    if (!hr) continue;
    const ht = Array.from(hr.querySelectorAll('th, td')).map(c => norm(c.textContent || ''));
    if (ht.some(h => h.includes('purpose')) && ht.some(h => h.includes('update'))) return true;
  }
  return false;
}
"""

    def _find_and_click_existing_report(
        self, report_name: str, *, timeout_s: float = 15.0,
    ) -> bool:
        """Check for a saved/in-progress report with a matching Purpose.

        Waits up to *timeout_s* for the 'Update Expense Reports' table to
        render before scanning.  Oracle's iExpenses landing page loads the
        table asynchronously, so a single snapshot is unreliable.

        Returns True when an existing report was opened for editing.
        """
        if not self.browser_page:
            return False

        import time as _time
        deadline = _time.monotonic() + timeout_s

        table_seen = False
        while _time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                try:
                    if frame.evaluate(self._UPDATE_TABLE_VISIBLE_JS):
                        table_seen = True
                        if frame.evaluate(self._FIND_EXISTING_REPORT_JS, report_name):
                            return True
                except Exception:
                    continue
            if table_seen:
                break
            self.browser_page.wait_for_timeout(400)

        if not table_seen:
            self.set_status(
                "Update Expense Reports table did not appear — "
                "assuming no existing report."
            )
            return False

        remaining = deadline - _time.monotonic()
        if remaining > 1.0:
            self._wait_for_oracle_page_stable(
                timeout_s=min(remaining, 8.0), settle_ms=600,
            )
            for frame in self.browser_page.frames:
                try:
                    if frame.evaluate(self._FIND_EXISTING_REPORT_JS, report_name):
                        return True
                except Exception:
                    continue

        return False

    # ------------------------------------------------------------------
    # Step 2 — select / deselect credit-card transactions
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_step2_line_locator(line_id: str) -> tuple[int | None, int | None]:
        m = re.match(r"^p(\d+):r(\d+)$", str(line_id or "").strip())
        if not m:
            return (None, None)
        return (int(m.group(1)), int(m.group(2)))

    _SELECT_TRANSACTIONS_ON_PAGE_JS = """
(payload) => {
    const targets = Array.isArray(payload?.targets) ? payload.targets : [];
    const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
    const norm = (v) => clean(v).toLowerCase();
    const merchantKey = (v) => norm(v).replace(/[^a-z0-9]+/g, ' ').replace(/\\s+/g, ' ').trim();
    const amountNum = (v) => {
        const s = String(v || '').replace(/[^0-9.+-]/g, '');
        if (!s) return null;
        const n = Number(s);
        return Number.isFinite(n) ? n : null;
    };
    const monthMap = {
        jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3, apr: 4, april: 4,
        may: 5, jun: 6, june: 6, jul: 7, july: 7, aug: 8, august: 8, sep: 9, sept: 9,
        september: 9, oct: 10, october: 10, nov: 11, november: 11, dec: 12, december: 12,
    };
    const pad2 = (n) => String(n).padStart(2, '0');
    const dateKey = (v) => {
        const s = norm(v).replace(/,/g, ' ');
        if (!s) return '';
        let m = s.match(/^(\\d{1,2})[-/\\s]([a-z]{3,9})[-/\\s](\\d{2,4})$/i);
        if (m) {
            const d = Number(m[1]);
            const mo = monthMap[String(m[2]).toLowerCase()] || 0;
            let y = Number(m[3]);
            if (y < 100) y += 2000;
            if (mo > 0 && d > 0) return `${y}-${pad2(mo)}-${pad2(d)}`;
        }
        m = s.match(/^(\\d{4})[-/\\s](\\d{1,2})[-/\\s](\\d{1,2})$/);
        if (m) return `${Number(m[1])}-${pad2(Number(m[2]))}-${pad2(Number(m[3]))}`;
        return s.replace(/\\s+/g, ' ');
    };
    const isVisible = (el) => {
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
    };
    let best = null;
    let score = -1;
    for (const table of document.querySelectorAll('table')) {
        const hr = table.querySelector('thead tr') || table.querySelector('tr');
        if (!hr) continue;
        const ht = Array.from(hr.querySelectorAll('th, td')).map(c => norm(c.textContent || ''));
        const hasM = ht.some(t => t.includes('merchant') || t.includes('vendor'));
        let s = hasM ? 4 : 0;
        const di = ht.findIndex(t => /\\bdate\\b/.test(t) || t.includes('trans date') || t.includes('transaction date') || t.includes('post date'));
        const ai = ht.findIndex(t => /\\bamount\\b/.test(t) || t.includes('amt'));
        if (di >= 0) s += 2;
        if (ai >= 0) s += 3;
        if (table.querySelector('input[type="checkbox"]')) s += 1;
        if (s > score) { score = s; best = { table, mi: ht.findIndex(t => t.includes('merchant') || t.includes('vendor')), di, ai }; }
    }
    if (!best || best.mi < 0) return { selected: 0, matched: [] };
    const { table, mi, di, ai } = best;
    const bodyRows = table.tBodies && table.tBodies.length
        ? Array.from(table.tBodies[0].querySelectorAll('tr'))
        : Array.from(table.querySelectorAll('tr')).filter(tr => tr.querySelector('td'));
    const usedRows = new Set();
    let selected = 0;
    const matched = [];
    for (let ti = 0; ti < targets.length; ti++) {
        const target = targets[ti];
        const tgtMerchant = merchantKey(target?.merchant || '');
        if (!tgtMerchant) continue;
        const tgtDate = dateKey(target?.date || '');
        const tgtAmt = amountNum(target?.amount || '');
        const tgtRow = Number.isFinite(Number(target?.row_index)) ? Number(target.row_index) : null;
        const candidates = [];
        for (let i = 0; i < bodyRows.length; i++) {
            if (usedRows.has(i)) continue;
            const cells = Array.from(bodyRows[i].querySelectorAll('td'));
            if (mi >= cells.length) continue;
            const rowMerchant = merchantKey(cells[mi].innerText || cells[mi].textContent || '');
            if (!rowMerchant) continue;
            if (!rowMerchant.includes(tgtMerchant) && !tgtMerchant.includes(rowMerchant)) continue;
            if (tgtDate && di >= 0 && di < cells.length) {
                const rowDate = dateKey(cells[di].innerText || cells[di].textContent || '');
                if (rowDate && rowDate !== tgtDate) continue;
            }
            if (tgtAmt !== null && ai >= 0 && ai < cells.length) {
                const rowAmt = amountNum(cells[ai].innerText || cells[ai].textContent || '');
                if (rowAmt !== null && Math.abs(rowAmt - tgtAmt) > 0.01) continue;
            }
            const cb = bodyRows[i].querySelector('input[type="checkbox"]');
            if (!cb || !isVisible(cb)) continue;
            candidates.push({ idx: i, cb, hinted: (tgtRow !== null && i === tgtRow) });
        }
        if (candidates.length === 0) continue;
        const picked = candidates.find(c => c.hinted) || candidates[0];
        if (!picked.cb.checked) picked.cb.click();
        usedRows.add(picked.idx);
        selected++;
        matched.push(target?.target_id != null ? target.target_id : ti);
    }
    return { selected, matched };
}
"""

    _SELECT_TRANSACTIONS_BY_ROW_JS = """
(payload) => {
    const targets = Array.isArray(payload?.targets) ? payload.targets : [];
    const clean = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
    const merchantKey = (v) => clean(v).toLowerCase().replace(/[^a-z0-9]+/g, ' ').replace(/\\s+/g, ' ').trim();
    const isVisible = (el) => {
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
    };
    let best = null;
    let bestRows = -1;
    for (const table of document.querySelectorAll('table')) {
        const hr = table.querySelector('thead tr') || table.querySelector('tr');
        if (!hr) continue;
        const headers = Array.from(hr.querySelectorAll('th, td')).map(c => String(c.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase());
        const mi = headers.findIndex(t => t.includes('merchant') || t.includes('vendor') || t.includes('payee') || t.includes('supplier'));
        if (mi < 0) continue;
        const rows = table.tBodies && table.tBodies.length
            ? Array.from(table.tBodies[0].querySelectorAll('tr'))
            : Array.from(table.querySelectorAll('tr')).filter(tr => tr.querySelector('td'));
        const hasCheckbox = !!table.querySelector('input[type="checkbox"]');
        if (!hasCheckbox) continue;
        if (rows.length > bestRows) {
            bestRows = rows.length;
            best = { table, rows, mi };
        }
    }
    if (!best) return { selected: 0, matched: [] };
    const used = new Set();
    let selected = 0;
    const matched = [];
    for (let ti = 0; ti < targets.length; ti++) {
        const target = targets[ti];
        const targetMerchant = merchantKey(target?.merchant || '');
        if (!targetMerchant) continue;
        const parsedRow = Number(target?.row_index);
        if (!Number.isFinite(parsedRow)) continue;
        const rowIndex = parsedRow;
        if (rowIndex < 0 || rowIndex >= best.rows.length) continue;
        if (used.has(rowIndex)) continue;
        const tr = best.rows[rowIndex];
        const cells = Array.from(tr.querySelectorAll('td'));
        const rowMerchant = (best.mi >= 0 && best.mi < cells.length)
            ? merchantKey(cells[best.mi]?.innerText || cells[best.mi]?.textContent || '')
            : '';
        if (!rowMerchant || (!rowMerchant.includes(targetMerchant) && !targetMerchant.includes(rowMerchant))) continue;
        const cb = tr.querySelector('input[type="checkbox"]');
        if (!cb || !isVisible(cb)) continue;
        if (!cb.checked) cb.click();
        used.add(rowIndex);
        selected++;
        matched.push(target?.target_id != null ? target.target_id : ti);
    }
    return { selected, matched };
}
"""

    _STEP2_TABLE_DIAGNOSTIC_JS = """
(payload) => {
    const sampleLimit = Number.isFinite(Number(payload?.sampleLimit)) ? Number(payload.sampleLimit) : 8;
    const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
    const norm = (v) => clean(v).toLowerCase();
    const merchantKey = (v) => norm(v).replace(/[^a-z0-9]+/g, ' ').replace(/\s+/g, ' ').trim();
    const isVisible = (el) => {
        if (!el) return false;
        const st = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
    };
    let best = null;
    let bestScore = -1;
    for (const table of document.querySelectorAll('table')) {
        const hr = table.querySelector('thead tr') || table.querySelector('tr');
        if (!hr) continue;
        const headers = Array.from(hr.querySelectorAll('th, td')).map(c => norm(c.textContent || ''));
        const mi = headers.findIndex(t => t.includes('merchant') || t.includes('vendor'));
        const di = headers.findIndex(t => /\\bdate\\b/.test(t) || t.includes('trans date') || t.includes('transaction date') || t.includes('post date'));
        const ai = headers.findIndex(t => /\\bamount\\b/.test(t) || t.includes('amt'));
        const hasCheckbox = !!table.querySelector('input[type="checkbox"]');
        let score = 0;
        if (mi >= 0) score += 4;
        if (di >= 0) score += 2;
        if (ai >= 0) score += 3;
        if (hasCheckbox) score += 1;
        if (score > bestScore) {
            bestScore = score;
            best = { table, headers, mi, di, ai, hasCheckbox };
        }
    }
    if (!best) {
        return {
            table_found: false,
            table_count: document.querySelectorAll('table').length,
            score: -1,
            header_texts: [],
            body_row_count: 0,
            visible_checkboxes: 0,
            sample_rows: [],
        };
    }
    const bodyRows = best.table.tBodies && best.table.tBodies.length
        ? Array.from(best.table.tBodies[0].querySelectorAll('tr'))
        : Array.from(best.table.querySelectorAll('tr')).filter(tr => tr.querySelector('td'));
    let visibleCheckboxes = 0;
    for (const tr of bodyRows) {
        const cb = tr.querySelector('input[type="checkbox"]');
        if (cb && isVisible(cb)) visibleCheckboxes += 1;
    }
    const sampleRows = [];
    for (let i = 0; i < bodyRows.length && sampleRows.length < sampleLimit; i++) {
        const tr = bodyRows[i];
        const cells = Array.from(tr.querySelectorAll('td'));
        const cb = tr.querySelector('input[type="checkbox"]');
        const merchant = (best.mi >= 0 && best.mi < cells.length)
            ? clean(cells[best.mi].innerText || cells[best.mi].textContent || '')
            : '';
        const date = (best.di >= 0 && best.di < cells.length)
            ? clean(cells[best.di].innerText || cells[best.di].textContent || '')
            : '';
        const amount = (best.ai >= 0 && best.ai < cells.length)
            ? clean(cells[best.ai].innerText || cells[best.ai].textContent || '')
            : '';
        sampleRows.push({
            row_index: i,
            merchant,
            date,
            amount,
            checkbox_present: !!cb,
            checkbox_visible: isVisible(cb),
            checked: !!(cb && cb.checked),
        });
    }
    return {
        table_found: true,
        table_count: document.querySelectorAll('table').length,
        score: bestScore,
        header_texts: best.headers,
        merchant_col: best.mi,
        date_col: best.di,
        amount_col: best.ai,
        has_checkbox_column: best.hasCheckbox,
        body_row_count: bodyRows.length,
        visible_checkboxes: visibleCheckboxes,
        sample_rows: sampleRows,
    };
}
"""

    def _collect_step2_failure_diagnostics(
        self,
        targets: list[dict[str, Any]],
        page_attempts: list[dict[str, Any]],
    ) -> str:
        """Build a compact, terminal-friendly diagnostic message for Step 2 failures."""
        targets_with_locator = [
            t for t in targets
            if isinstance(t.get("page_index"), int) and isinstance(t.get("row_index"), int)
        ]
        page_hist: dict[int, int] = {}
        for t in targets_with_locator:
            page_idx = int(t["page_index"])
            page_hist[page_idx] = page_hist.get(page_idx, 0) + 1

        target_sample_lines: list[str] = []
        for t in targets[:8]:
            target_sample_lines.append(
                f"p{t.get('page_index')} r{t.get('row_index')} | "
                f"{t.get('merchant', '')} | {t.get('date', '')} | {t.get('amount', '')}"
            )

        attempt_lines: list[str] = []
        for a in page_attempts[:12]:
            attempt_lines.append(
                f"page={a.get('page_index')} range={a.get('page_range', '?')} selected={a.get('selected', 0)}"
            )

        frame = self._step2_credit_card_frame
        snapshot: dict[str, Any] = {}
        if frame is None:
            picked = self._step2_pick_best_credit_snapshot()
            if picked:
                frame, _ = picked
        if frame is not None:
            try:
                snapshot = frame.evaluate(
                    self._STEP2_TABLE_DIAGNOSTIC_JS,
                    {"sampleLimit": 8},
                ) or {}
            except Exception as exc:
                snapshot = {"diagnostic_error": str(exc)}

        sample_rows = snapshot.get("sample_rows") or []
        sample_row_lines: list[str] = []
        for row in sample_rows[:8]:
            sample_row_lines.append(
                f"r{row.get('row_index')} | {row.get('merchant', '')} | "
                f"{row.get('date', '')} | {row.get('amount', '')} | "
                f"cb_present={row.get('checkbox_present')} cb_visible={row.get('checkbox_visible')} checked={row.get('checked')}"
            )

        header_texts = snapshot.get("header_texts") or []
        headers_joined = " | ".join(str(h) for h in header_texts[:12])

        lines_out = [
            "Step 2 diagnostics:",
            (
                f"targets_total={len(targets)} "
                f"targets_with_locator={len(targets_with_locator)} "
                f"targets_by_page={page_hist}"
            ),
            "target_sample=" + (" ; ".join(target_sample_lines) if target_sample_lines else "(none)"),
            "page_attempts=" + (" ; ".join(attempt_lines) if attempt_lines else "(none)"),
        ]
        pagination_issue = getattr(self, "_last_step2_pagination_issue", "")
        if pagination_issue:
            lines_out.append("pagination_issue=" + pagination_issue)

        if snapshot:
            lines_out.append(
                "table_snapshot="
                f"found={snapshot.get('table_found')} "
                f"tables={snapshot.get('table_count')} "
                f"score={snapshot.get('score')} "
                f"rows={snapshot.get('body_row_count')} "
                f"visible_checkboxes={snapshot.get('visible_checkboxes')} "
                f"merchant_col={snapshot.get('merchant_col')} "
                f"date_col={snapshot.get('date_col')} "
                f"amount_col={snapshot.get('amount_col')}"
            )
            lines_out.append("table_headers=" + (headers_joined if headers_joined else "(none)"))
            lines_out.append(
                "row_sample=" + (" ; ".join(sample_row_lines) if sample_row_lines else "(none)")
            )

        return "\n".join(lines_out)

    def _select_on_current_page(self, targets: list[dict]) -> tuple[int, list]:
        """Run the selection JS against the currently visible page.

        Returns (selected_count, list_of_matched_target_ids).
        """
        assert self.browser_page is not None
        picked = self._step2_pick_best_credit_snapshot()
        if not picked:
            return 0, []
        frame, _ = picked
        self._step2_credit_card_frame = frame

        primary_selected = 0
        primary_matched: list = []
        try:
            result = frame.evaluate(
                self._SELECT_TRANSACTIONS_ON_PAGE_JS,
                {"targets": targets},
            )
            if isinstance(result, dict):
                primary_selected = int(result.get("selected", 0))
                primary_matched = result.get("matched", [])
            elif isinstance(result, (int, float)) and result > 0:
                primary_selected = int(result)
        except Exception:
            pass

        fallback_selected = 0
        fallback_matched: list = []
        try:
            result = frame.evaluate(
                self._SELECT_TRANSACTIONS_BY_ROW_JS,
                {"targets": targets},
            )
            if isinstance(result, dict):
                fallback_selected = int(result.get("selected", 0))
                fallback_matched = result.get("matched", [])
            elif isinstance(result, (int, float)) and result > 0:
                fallback_selected = int(result)
        except Exception:
            pass

        if primary_selected >= fallback_selected:
            return primary_selected, primary_matched
        return fallback_selected, fallback_matched

    def _select_specific_transactions_step2(
        self, lines: list[SubmissionLine],
    ) -> int:
        """Select only the credit-card rows that match the report's lines.

        Pages through every page of the credit-card transactions table so
        that rows beyond the first visible page are also selected.
        Returns the total count of rows successfully selected.
        """
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")

        targets = []
        for i, ln in enumerate(lines):
            page_idx, row_idx = self._parse_step2_line_locator(ln.line_id)
            targets.append({
                "target_id": i,
                "merchant": ln.merchant_name.lower().strip(),
                "date": ln.transaction_date.strip(),
                "amount": ln.amount,
                "page_index": page_idx,
                "row_index": row_idx,
            })

        self.set_status("Step 2: navigating to first page of transactions…")
        self.expense_table_go_to_first_page_in_any_frame()
        self._wait_for_oracle_page_stable(settle_ms=600)

        total_selected = 0
        max_pages = 80
        page_attempts: list[dict[str, Any]] = []
        matched_ids: set = set()
        self._last_step2_pagination_issue = ""

        for page_idx in range(max_pages):
            remaining = [t for t in targets if t["target_id"] not in matched_ids]
            if not remaining:
                break

            page_range = self.get_step2_credit_table_page_range_in_any_frame()
            if page_range:
                start, end, total = page_range
                self.set_status(
                    f"Step 2: selecting transactions on page "
                    f"({start}–{end} of {total})…"
                )
            else:
                self.set_status(
                    f"Step 2: selecting transactions (page {page_idx + 1})…"
                )

            n, newly_matched = self._select_on_current_page(remaining)
            total_selected += n
            matched_ids.update(newly_matched)
            page_attempts.append({
                "page_index": page_idx,
                "page_range": (
                    f"{page_range[0]}-{page_range[1]}/{page_range[2]}"
                    if page_range else "unknown"
                ),
                "selected": n,
            })

            if len(matched_ids) >= len(targets):
                break

            clicked = self.click_expense_table_pagination_next_in_any_frame(
                preferred_frame=self._step2_credit_card_frame,
            )
            if not clicked:
                remaining_after = [t for t in targets if t["target_id"] not in matched_ids]
                if remaining_after:
                    self.set_status(
                        "Step 2: could not open next transaction page; saving and retrying…"
                    )
                    if self.click_text_in_any_frame("Save"):
                        self._wait_for_oracle_page_stable(settle_ms=700)
                    # Re-detect the credit card frame (Save may reload iframes)
                    refreshed = self._step2_pick_best_credit_snapshot()
                    if refreshed:
                        self._step2_credit_card_frame = refreshed[0]
                    clicked = self.click_expense_table_pagination_next_in_any_frame(
                        preferred_frame=self._step2_credit_card_frame,
                    )
                if not clicked:
                    if len(matched_ids) < len(targets):
                        self._last_step2_pagination_issue = (
                            f"stopped at page index {page_idx} with "
                            f"{len(matched_ids)}/{len(targets)} matched"
                        )
                    break
            self._wait_for_oracle_page_stable(settle_ms=600)

        self._last_step2_selection_targets = targets
        self._last_step2_page_attempts = page_attempts

        return total_selected

    _DESELECT_EXTRA_TRANSACTIONS_STEP2_JS = """
(payload) => {
    const targets = Array.isArray(payload?.targets) ? payload.targets : [];
    const currentPage = Number.isFinite(Number(payload?.currentPage)) ? Number(payload.currentPage) : null;
  const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();
        const amountNum = (v) => {
            const s = String(v || '').replace(/[^0-9.+-]/g, '');
            if (!s) return null;
            const n = Number(s);
            return Number.isFinite(n) ? n : null;
        };
    const monthMap = {
        jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3, apr: 4, april: 4,
        may: 5, jun: 6, june: 6, jul: 7, july: 7, aug: 8, august: 8, sep: 9, sept: 9,
        september: 9, oct: 10, october: 10, nov: 11, november: 11, dec: 12, december: 12,
    };
    const pad2 = (n) => String(n).padStart(2, '0');
    const dateKey = (v) => {
        const s = norm(v).replace(/,/g, ' ');
        if (!s) return '';
        let m = s.match(/^(\d{1,2})[-/\s]([a-z]{3,9})[-/\s](\d{2,4})$/i);
        if (m) {
            const d = Number(m[1]);
            const mo = monthMap[String(m[2]).toLowerCase()] || 0;
            let y = Number(m[3]);
            if (y < 100) y += 2000;
            if (mo > 0 && d > 0) return `${y}-${pad2(mo)}-${pad2(d)}`;
        }
        m = s.match(/^(\d{4})[-/\s](\d{1,2})[-/\s](\d{1,2})$/);
        if (m) return `${Number(m[1])}-${pad2(Number(m[2]))}-${pad2(Number(m[3]))}`;
        return s.replace(/\s+/g, ' ');
    };
  const isVisible = (el) => {
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
  };
  let best = null;
  let score = -1;
  for (const table of document.querySelectorAll('table')) {
    const hr = table.querySelector('thead tr') || table.querySelector('tr');
    if (!hr) continue;
    const ht = Array.from(hr.querySelectorAll('th, td')).map(c => norm(c.textContent || ''));
    const hasM = ht.some(t => t.includes('merchant') || t.includes('vendor'));
    let s = hasM ? 4 : 0;
        const di = ht.findIndex(t => /\bdate\b/.test(t) || t.includes('trans date') || t.includes('transaction date') || t.includes('post date'));
    const ai = ht.findIndex(t => /\\bamount\\b/.test(t) || t.includes('amt'));
        if (di >= 0) s += 2;
    if (ai >= 0) s += 3;
    if (table.querySelector('input[type="checkbox"]')) s += 1;
        if (s > score) { score = s; best = { table, mi: ht.findIndex(t => t.includes('merchant') || t.includes('vendor')), di, ai }; }
  }
  if (!best || best.mi < 0) return 0;
    const { table, mi, di, ai } = best;
  const bodyRows = table.tBodies && table.tBodies.length
    ? Array.from(table.tBodies[0].querySelectorAll('tr'))
    : Array.from(table.querySelectorAll('tr')).filter(tr => tr.querySelector('td'));
  let deselected = 0;
  for (let i = 0; i < bodyRows.length; i++) {
    const cells = Array.from(bodyRows[i].querySelectorAll('td'));
    if (mi >= cells.length) continue;
    const cb = bodyRows[i].querySelector('input[type="checkbox"]');
    if (!cb || !isVisible(cb) || !cb.checked) continue;
    const rowMerchant = norm(cells[mi].innerText || cells[mi].textContent || '');
    const rowDate = (di >= 0 && di < cells.length)
      ? dateKey(cells[di].innerText || cells[di].textContent || '')
      : '';
        const rowAmt = (ai >= 0 && ai < cells.length)
            ? amountNum(cells[ai].innerText || cells[ai].textContent || '')
            : null;
    let isTarget = false;
    for (const target of targets) {
      if (!rowMerchant.includes(target.merchant) && !target.merchant.includes(rowMerchant)) continue;
            const targetDate = dateKey(target?.date || '');
            if (targetDate && rowDate && targetDate !== rowDate) continue;
            if (target.amount && ai >= 0) {
                const wantAmt = amountNum(target.amount);
                if (wantAmt !== null && rowAmt !== null && Math.abs(rowAmt - wantAmt) > 0.01) continue;
            }
      isTarget = true;
      break;
    }
    if (!isTarget) {
      cb.click();
      deselected++;
    }
  }
  return deselected;
}
"""

    def _deselect_on_current_page(self, targets: list[dict], current_page: int) -> int:
        """Run the deselect JS against the currently visible page."""
        assert self.browser_page is not None
        picked = self._step2_pick_best_credit_snapshot()
        if not picked:
            return 0
        frame, _ = picked
        self._step2_credit_card_frame = frame
        try:
            result = frame.evaluate(
                self._DESELECT_EXTRA_TRANSACTIONS_STEP2_JS,
                {"targets": targets, "currentPage": current_page},
            )
            if isinstance(result, (int, float)) and result > 0:
                return int(result)
        except Exception:
            return 0
        return 0

    def _deselect_extra_transactions_step2(
        self, lines: list[SubmissionLine],
    ) -> int:
        """Uncheck credit-card rows that are selected but not in this report.

        Pages through every page of the credit-card transactions table so
        that rows beyond the first visible page are also unchecked.
        Returns the total number of rows deselected.
        """
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")

        targets = []
        for ln in lines:
            page_idx, row_idx = self._parse_step2_line_locator(ln.line_id)
            targets.append({
                "merchant": ln.merchant_name.lower().strip(),
                "date": ln.transaction_date.strip(),
                "amount": ln.amount,
                "page_index": page_idx,
                "row_index": row_idx,
            })

        self.set_status("Step 2: navigating to first page to deselect extras…")
        self.expense_table_go_to_first_page_in_any_frame()
        self._wait_for_oracle_page_stable(settle_ms=600)

        total_deselected = 0
        max_pages = 80

        for page_idx in range(max_pages):
            n = self._deselect_on_current_page(targets, page_idx)
            total_deselected += n

            clicked = self.click_expense_table_pagination_next_in_any_frame(
                preferred_frame=self._step2_credit_card_frame,
            )
            if not clicked:
                break
            self._wait_for_oracle_page_stable(settle_ms=600)

        return total_deselected

    # ------------------------------------------------------------------
    # Step 3 — expense type assignment
    # ------------------------------------------------------------------

    _EXTRACT_STEP3_ROWS_JS = """
() => {
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const normalize = (value) => clean(value).toLowerCase();
  const rowData = [];
  const tables = Array.from(document.querySelectorAll('table'));
  tables.forEach((table, tableIndex) => {
    const headerRow = table.querySelector('tr');
    if (!headerRow) return;
    const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
    const headerTexts = headerCells.map(cell => normalize(cell.textContent || ''));
    const merchantIdx = headerTexts.findIndex(txt => txt.includes('merchant'));
    const expenseIdx = headerTexts.findIndex(txt => txt.includes('expense type'));
    const justificationIdx = headerTexts.findIndex(txt => txt.includes('justification'));
    if (merchantIdx < 0 || expenseIdx < 0 || justificationIdx < 0) return;
    const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
    bodyRows.forEach((tr, rowIndex) => {
      try { tr.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
      const cells = Array.from(tr.querySelectorAll('td'));
      if (!cells.length) return;
      const expenseCell = cells[expenseIdx];
      const merchantCell = cells[merchantIdx];
      const justificationCell = cells[justificationIdx];
      if (!expenseCell || !merchantCell || !justificationCell) return;
      const select = expenseCell.querySelector('select');
      const justInput = justificationCell.querySelector('input, textarea');
      if (!select || !justInput) return;
      const merchant = clean(merchantCell.innerText || merchantCell.textContent || '');
      const options = Array.from(select.options)
        .map(opt => clean(opt.textContent || ''))
        .filter(txt => txt && !/^select/i.test(txt));
      if (!merchant || !options.length) return;
      rowData.push({
        row_key: `${tableIndex}:${rowIndex}`,
        merchant_name: merchant,
        options,
      });
    });
  });
  return rowData;
}
"""

    _APPLY_EXPENSE_TYPE_JS = """
([rowKey, selectedLabel]) => {
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const normalize = (value) => clean(value).toLowerCase();
  const pickOption = (select, label) => {
    const want = normalize(label);
    const opts = Array.from(select.options);
    let opt = opts.find(o => normalize(o.textContent || '') === want);
    if (opt) return opt;
    opt = opts.find(o => {
      const ot = normalize(o.textContent || '');
      return ot && !/^select/i.test(ot) && (ot.includes(want) || want.includes(ot));
    });
    return opt || null;
  };
  const tables = Array.from(document.querySelectorAll('table'));
  for (let tableIndex = 0; tableIndex < tables.length; tableIndex++) {
    const table = tables[tableIndex];
    const headerRow = table.querySelector('tr');
    if (!headerRow) continue;
    const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
    const headerTexts = headerCells.map(cell => normalize(cell.textContent || ''));
    const expenseIdx = headerTexts.findIndex(txt => txt.includes('expense type'));
    const justificationIdx = headerTexts.findIndex(txt => txt.includes('justification'));
    if (expenseIdx < 0 || justificationIdx < 0) continue;
    const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
    for (let rowIndex = 0; rowIndex < bodyRows.length; rowIndex++) {
      if (`${tableIndex}:${rowIndex}` !== String(rowKey)) continue;
      const tr = bodyRows[rowIndex];
      try { tr.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
      const cells = Array.from(tr.querySelectorAll('td'));
      const expenseCell = cells[expenseIdx];
      const justificationCell = cells[justificationIdx];
      if (!expenseCell || !justificationCell) return false;
      const select = expenseCell.querySelector('select');
      const justInput = justificationCell.querySelector('input, textarea');
      if (!select || !justInput) return false;
      const option = pickOption(select, selectedLabel);
      if (!option) return false;
      if (select.value === option.value) return true;
      const appliedLabel = clean(option.textContent || '') || selectedLabel;
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      select.dispatchEvent(new Event('input', { bubbles: true }));
      select.dispatchEvent(new Event('blur', { bubbles: true }));
      justInput.focus();
      justInput.value = appliedLabel;
      justInput.dispatchEvent(new Event('input', { bubbles: true }));
      justInput.dispatchEvent(new Event('change', { bubbles: true }));
      justInput.dispatchEvent(new Event('blur', { bubbles: true }));
      return true;
    }
  }
  return false;
}
"""

    def _nudge_step3_table(self, frame: Frame) -> None:
        """Scroll each row into view so Oracle/ADF lazily attaches <select> options."""
        try:
            frame.evaluate("""
() => {
  const normalize = (v) => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  for (const table of document.querySelectorAll('table')) {
    const hr = table.querySelector('tr');
    if (!hr) continue;
    const ht = Array.from(hr.querySelectorAll('th, td')).map(c => normalize(c.textContent || ''));
    if (!ht.some(t => t.includes('expense type'))) continue;
    for (const tr of Array.from(table.querySelectorAll('tr')).slice(1)) {
      try { tr.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
    }
  }
  return true;
}
""")
        except Exception:
            pass

    def _extract_step3_rows(self) -> tuple[Frame | None, list[dict]]:
        if not self.browser_page:
            return None, []
        for frame in self.browser_page.frames:
            try:
                rows = frame.evaluate(self._EXTRACT_STEP3_ROWS_JS)
                if rows:
                    return frame, rows
            except Exception:
                continue
        return None, []

    def _apply_expense_types_on_current_page(
        self,
        merchant_to_type: dict[str, str],
    ) -> int:
        """Set expense type dropdowns on the currently visible Step 3 page."""
        assert self.browser_page is not None

        frame, rows = self._extract_step3_rows()
        if not frame or not rows:
            return 0

        self._nudge_step3_table(frame)
        self.browser_page.wait_for_timeout(400)
        frame, rows = self._extract_step3_rows()
        if not frame or not rows:
            return 0

        applied = 0
        for row in rows:
            merchant = re.sub(r"\s+", " ", str(row.get("merchant_name", ""))).lower().strip()
            expense_type = merchant_to_type.get(merchant)
            if not expense_type:
                for mk, et in merchant_to_type.items():
                    if mk in merchant or merchant in mk:
                        expense_type = et
                        break
            if not expense_type:
                continue

            row_key = row.get("row_key", "")
            try:
                ok = frame.evaluate(self._APPLY_EXPENSE_TYPE_JS, [row_key, expense_type])
                if ok:
                    applied += 1
                    self.set_status(f"Step 3: set '{expense_type}' for {row.get('merchant_name', '')}")
                    self.browser_page.wait_for_timeout(250)
            except Exception:
                continue

        return applied

    def _step3_table_can_advance(self) -> bool:
        """Check if the Step 3 Business Expenses table has a clickable
        'Next N' pagination link — same check used on Step 2."""
        return self._credit_card_table_pagination_can_advance()

    def _apply_expense_types_step3(
        self,
        lines: list[SubmissionLine],
    ) -> int:
        """Set expense type dropdowns on Step 3, paging through all pages.

        After filling each page, clicks Save so Oracle persists the
        dropdown values before navigating to the next table page.

        Uses the same pagination strategy proven on Step 2: check for a
        visible 'Next N' link rather than trusting parsed page-range text
        (Oracle often has hidden full-range strings in other iframes that
        cause false last-page detection).
        """
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")

        merchant_to_type: dict[str, str] = {}
        for ln in lines:
            if ln.expense_type:
                key = re.sub(r"\s+", " ", ln.merchant_name).lower().strip()
                merchant_to_type[key] = ln.expense_type

        if not merchant_to_type:
            self.set_status("Step 3: no expense types to assign, skipping.")
            return 0

        frame, rows = self._extract_step3_rows()
        if not frame:
            self.set_status("Step 3: could not find Business Expenses table — nudging…")
            for fr in self.browser_page.frames:
                self._nudge_step3_table(fr)
            self.browser_page.wait_for_timeout(500)
            frame, rows = self._extract_step3_rows()

        if not frame or not rows:
            self.set_status("Step 3: Business Expenses table not found — skipping type assignment.")
            return 0

        total_applied = 0
        max_pages = 80

        for page_idx in range(max_pages):
            self.set_status(
                f"Step 3: setting expense types (page {page_idx + 1})…"
            )

            n = self._apply_expense_types_on_current_page(merchant_to_type)
            total_applied += n

            self.set_status(
                f"Step 3: set {n} type(s) on page {page_idx + 1} "
                f"({total_applied} total so far)."
            )

            if not self._step3_table_can_advance():
                self.set_status(
                    f"Step 3: no more pages (filled {total_applied} row(s) "
                    f"across {page_idx + 1} page(s))."
                )
                break

            self.set_status(
                f"Step 3: advancing to page {page_idx + 2}…"
            )
            clicked = self.click_expense_table_pagination_next_in_any_frame()
            if not clicked:
                self.set_status("Step 3: pagination click failed — stopping.")
                break
            self._wait_for_oracle_page_stable(settle_ms=600)

        return total_applied

    # ------------------------------------------------------------------
    # Step 3 — receipt missing checkbox
    # ------------------------------------------------------------------

    _STEP3_LINE_NUMBERS_JS = """
() => {
  const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();
  const tables = Array.from(document.querySelectorAll('table'));
  const results = [];
  for (let ti = 0; ti < tables.length; ti++) {
    const table = tables[ti];
    const hr = table.querySelector('tr');
    if (!hr) continue;
    const hc = Array.from(hr.querySelectorAll('th, td')).map(c => norm(c.textContent || ''));
    const mi = hc.findIndex(t => t.includes('merchant'));
    let li = hc.findIndex(t => {
      if (t === 'line' || t === 'line #' || t === 'ln') return true;
      if (t.includes('airline') || t.includes('deadline')) return false;
      return /^line\\b/.test(t);
    });
    const ai = hc.findIndex(t => t.includes('amount') && !t.includes('reimbursable'));
    if (mi < 0 || li < 0) continue;
    const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
    for (let ri = 0; ri < bodyRows.length; ri++) {
      const cells = Array.from(bodyRows[ri].querySelectorAll('td'));
      if (li >= cells.length || mi >= cells.length) continue;
      const lineNo = parseInt((cells[li].innerText || '').replace(/\\s/g, ''), 10);
      if (!lineNo || lineNo < 1) continue;
      const merchant = clean(cells[mi].innerText || cells[mi].textContent || '');
      const amount = (ai >= 0 && ai < cells.length) ? clean(cells[ai].innerText || '') : '';
      results.push({ lineNo, merchant, amount });
    }
    if (results.length) break;
  }
  return results;
}
"""

    _STEP3_CHECK_RECEIPT_MISSING_JS = """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const labels = Array.from(document.querySelectorAll('label, td, span'));
  for (const el of labels) {
    const txt = norm(el.textContent || '');
    if (!txt.includes('receipt missing') && !txt.includes('original receipt missing'))
      continue;
    let cb = el.querySelector('input[type="checkbox"]');
    if (!cb) {
      const forId = el.getAttribute('for');
      if (forId) cb = document.getElementById(forId);
    }
    if (!cb) {
      let sib = el;
      for (let i = 0; i < 5 && sib; i++) {
        sib = sib.nextElementSibling;
        if (sib) {
          cb = sib.querySelector('input[type="checkbox"]');
          if (cb) break;
        }
      }
    }
    if (!cb) {
      const parent = el.closest('tr') || el.closest('td') || el.parentElement;
      if (parent) cb = parent.querySelector('input[type="checkbox"]');
    }
    if (cb) {
      if (!cb.checked) {
        cb.focus();
        cb.click();
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
      return true;
    }
  }
  const checkboxes = Array.from(document.querySelectorAll('input[type="checkbox"]'));
  for (const cb of checkboxes) {
    const nearby = norm(
      (cb.closest('td') || cb.parentElement || {}).textContent || ''
    );
    if (nearby.includes('receipt missing')) {
      if (!cb.checked) {
        cb.focus();
        cb.click();
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
      return true;
    }
  }
  return false;
}
"""

    def _step3_scan_line_numbers(self) -> list[dict]:
        """Scan all pages of the Step 3 table and return a list of
        ``{lineNo, merchant, amount}`` dicts for every row."""
        if not self.browser_page:
            return []
        self._step3_go_to_first_table_page()
        self._wait_for_oracle_page_stable(settle_ms=500)

        all_lines: list[dict] = []
        for _ in range(80):
            for fr in self.browser_page.frames:
                try:
                    rows = fr.evaluate(self._STEP3_LINE_NUMBERS_JS)
                    if rows:
                        all_lines.extend(rows)
                        break
                except Exception:
                    continue
            if not self._step3_table_can_advance():
                break
            self.click_expense_table_pagination_next_in_any_frame()
            self._wait_for_oracle_page_stable(settle_ms=500)
        return all_lines

    def _step3_check_receipt_missing_on_detail(self) -> bool:
        """On the currently open detail form, tick 'Original Receipt Missing'."""
        if not self.browser_page:
            return False
        for fr in self.browser_page.frames:
            try:
                if fr.evaluate(self._STEP3_CHECK_RECEIPT_MISSING_JS):
                    return True
            except Exception:
                continue
        return False

    def _step3_mark_receipt_missing_lines(
        self, lines: list[SubmissionLine]
    ) -> int:
        """For every SubmissionLine with receipt_missing=True, navigate to
        its row in the Step 3 table, open Details, tick the 'Original
        Receipt Missing' checkbox, and Return."""
        if not self.browser_page:
            return 0

        missing_merchants: list[tuple[str, str]] = []
        for ln in lines:
            if ln.receipt_missing:
                m = re.sub(r"\s+", " ", ln.merchant_name).lower().strip()
                a = self._normalize_amount(ln.amount)
                missing_merchants.append((m, a))

        if not missing_merchants:
            return 0

        self.set_status(
            f"Step 3: {len(missing_merchants)} line(s) to mark receipt missing. "
            "Scanning table for line numbers…"
        )

        table_rows = self._step3_scan_line_numbers()
        if not table_rows:
            self.set_status("Step 3: could not read line numbers — skipping receipt missing.")
            return 0

        target_line_nos: list[int] = []
        used: list[bool] = [False] * len(missing_merchants)
        for tr in table_rows:
            tm = re.sub(r"\s+", " ", str(tr.get("merchant", ""))).lower().strip()
            ta = self._normalize_amount(str(tr.get("amount", "")))
            for i, (mm, ma) in enumerate(missing_merchants):
                if used[i]:
                    continue
                if mm == tm or self._fuzzy_merchant_match(mm, tm):
                    if ma and ta and ma == ta:
                        target_line_nos.append(int(tr["lineNo"]))
                        used[i] = True
                        break
                    elif not ma or not ta:
                        target_line_nos.append(int(tr["lineNo"]))
                        used[i] = True
                        break
            else:
                for i, (mm, _) in enumerate(missing_merchants):
                    if used[i]:
                        continue
                    if mm == tm or self._fuzzy_merchant_match(mm, tm):
                        target_line_nos.append(int(tr["lineNo"]))
                        used[i] = True
                        break

        if not target_line_nos:
            self.set_status("Step 3: no matching lines found for receipt missing.")
            return 0

        self.set_status(
            f"Step 3: marking receipt missing on line(s): {target_line_nos}"
        )

        def on_dialog(dialog):
            try:
                dialog.accept()
            except Exception:
                pass

        self.browser_page.on("dialog", on_dialog)
        marked = 0
        try:
            for line_no in target_line_nos:
                frame, row_key = self._step3_navigate_to_line_number(line_no)
                if not frame or not row_key:
                    self.set_status(
                        f"Step 3: could not find line {line_no} — skipping."
                    )
                    continue

                try:
                    clicked = frame.evaluate(
                        self._STEP3_CLICK_DETAILS_JS, row_key
                    )
                except Exception:
                    clicked = False
                if not clicked:
                    self.set_status(
                        f"Step 3: could not click Details for line {line_no}."
                    )
                    continue
                self._wait_for_oracle_page_stable(timeout_s=15.0)

                import time as _t
                deadline = _t.monotonic() + 15
                detail_ready = False
                while _t.monotonic() < deadline:
                    for fr in self.browser_page.frames:
                        try:
                            blob = fr.evaluate(
                                "() => (document.body && document.body.innerText) || ''"
                            )
                            if blob and "Return" in blob and "Receipt Missing" in blob:
                                detail_ready = True
                                break
                        except Exception:
                            continue
                    if detail_ready:
                        break
                    self.browser_page.wait_for_timeout(300)

                if not detail_ready:
                    self.set_status(
                        f"Step 3: detail form did not load for line {line_no}."
                    )
                    self._step3_return_to_table()
                    continue

                if self._step3_check_receipt_missing_on_detail():
                    marked += 1
                    self.set_status(
                        f"Step 3: checked receipt missing for line {line_no} "
                        f"({marked}/{len(target_line_nos)})"
                    )
                else:
                    self.set_status(
                        f"Step 3: could not find receipt missing checkbox for line {line_no}."
                    )

                self.browser_page.wait_for_timeout(500)
                self._step3_return_to_table()
        finally:
            try:
                self.browser_page.remove_listener("dialog", on_dialog)
            except Exception:
                pass

        return marked

    # ------------------------------------------------------------------
    # Step 3 — return from detail form to table
    # ------------------------------------------------------------------

    def _step3_return_to_table(self, timeout_s: float = 20.0) -> None:
        """Click Return on the detail form, handle any dialog, and wait
        until the Step 3 table is actually visible again before returning."""
        if not self.browser_page:
            return
        import time as _t

        self._step3_click_return()
        self.browser_page.wait_for_timeout(1000)
        self._step3_dismiss_dialog_after_return()
        self._wait_for_oracle_page_stable()

        deadline = _t.monotonic() + timeout_s
        while _t.monotonic() < deadline:
            body = self._step3_full_body_text().lower()
            if "business expenses" in body and "merchant" in body:
                on_detail = any(
                    kw in body
                    for kw in ("details for line", "start date", "daily rate")
                )
                if not on_detail:
                    return
            self.set_status("Step 3: waiting to return to table…")
            self._step3_click_return()
            self.browser_page.wait_for_timeout(1000)
            self._step3_dismiss_dialog_after_return()
            self._wait_for_oracle_page_stable()

    # ------------------------------------------------------------------
    # Step 3 — banner error detection and auto-fix
    # ------------------------------------------------------------------

    _STEP3_CURRENCY_ERROR_MARKERS = (
        "Exchange Rate will default to 1",
        "Receipt Currency is the same as the Reimbursement Currency",
    )

    def _step3_full_body_text(self) -> str:
        if not self.browser_page:
            return ""
        parts: list[str] = []
        for frame in self.browser_page.frames:
            try:
                parts.append(
                    frame.evaluate(
                        "() => (document.body && document.body.innerText) || ''"
                    ) or ""
                )
            except Exception:
                continue
        return "\n".join(parts)

    def _step3_banner_is_currency_error(self, chunk: str) -> bool:
        c = (chunk or "").lower()
        if not c.strip():
            return False
        if "expense type" in c and "please enter" in c:
            return False
        return any(m.lower() in c for m in self._STEP3_CURRENCY_ERROR_MARKERS)

    def _scrape_step3_banner_errors(self) -> list[tuple[int, str]]:
        """Parse ``Line N Error -`` segments from the Step 3 yellow banner.

        Returns list of ``(line_number, kind)`` where kind is one of:
        ``currency``, ``expense_justification``, or ``other``.
        """
        blob = self._step3_full_body_text()
        if not blob.strip():
            return []
        pattern = re.compile(r"Line\s+(\d+)\s+Error\s*-\s*", re.IGNORECASE)
        matches = list(pattern.finditer(blob))
        if not matches:
            return []
        out: list[tuple[int, str]] = []
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(blob)
            chunk = blob[start:end]
            try:
                line_no = int(m.group(1))
            except ValueError:
                continue
            cl = chunk.lower()
            if self._step3_banner_is_currency_error(chunk):
                kind = "currency"
            elif "expense type" in cl or "justification" in cl:
                kind = "expense_justification"
            else:
                kind = "other"
            out.append((line_no, kind))
        return out

    def _step3_has_banner_errors(self) -> bool:
        return bool(self._scrape_step3_banner_errors())

    _STEP3_ROW_KEY_FOR_LINE_JS = """
(lineNo) => {
  const want = Number(lineNo);
  if (!want || want < 1) return null;
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  function lineColumnIndex(headerTexts) {
    let idx = headerTexts.findIndex((txt) => {
      if (txt === 'line' || txt === 'line #' || txt === 'ln') return true;
      if (txt.includes('airline') || txt.includes('deadline')) return false;
      return /^line\\b/.test(txt);
    });
    return idx;
  }
  const tables = Array.from(document.querySelectorAll('table'));
  for (let tableIndex = 0; tableIndex < tables.length; tableIndex++) {
    const table = tables[tableIndex];
    const headerRow = table.querySelector('tr');
    if (!headerRow) continue;
    const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
    const headerTexts = headerCells.map(c => normalize(c.textContent || ''));
    const li = lineColumnIndex(headerTexts);
    if (li < 0) continue;
    if (!headerTexts.some(t => t.includes('expense type'))) continue;
    const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
    for (let rowIndex = 0; rowIndex < bodyRows.length; rowIndex++) {
      const cells = Array.from(bodyRows[rowIndex].querySelectorAll('td'));
      if (li >= cells.length) continue;
      const raw = (cells[li].innerText || cells[li].textContent || '').replace(/\\s/g, '');
      if (Number(raw) === want) return `${tableIndex}:${rowIndex}`;
    }
  }
  return null;
}
"""

    def _step3_go_to_first_table_page(self) -> None:
        """Navigate the Step 3 Business Expenses table back to page 1.

        Unlike ``expense_table_go_to_first_page_in_any_frame`` (which is
        Step 2-specific), this simply clicks 'Previous N' until it can't
        any more — works on any paginated Oracle table.
        """
        if not self.browser_page:
            return
        for _ in range(80):
            if not self.click_expense_table_pagination_previous_in_any_frame():
                break
            self._wait_for_oracle_page_stable(settle_ms=500)

    def _step3_navigate_to_line_number(
        self, line_no: int
    ) -> tuple[Frame | None, str | None]:
        """Page through the Step 3 table until the row for *line_no* is
        visible, and return ``(frame, row_key)``.

        Always rewinds to the first page before searching forward, so
        this works regardless of which page the table is currently on.
        """
        if not self.browser_page or line_no < 1:
            return None, None

        self._step3_go_to_first_table_page()
        self._wait_for_oracle_page_stable(settle_ms=500)

        max_pages = 60
        for _ in range(max_pages):
            frame, rows = self._extract_step3_rows()
            if not frame:
                return None, None
            try:
                rk = frame.evaluate(self._STEP3_ROW_KEY_FOR_LINE_JS, line_no)
            except Exception:
                rk = None
            if rk:
                return frame, str(rk)

            if not self._step3_table_can_advance():
                break

            if not self.click_expense_table_pagination_next_in_any_frame():
                break
            self._wait_for_oracle_page_stable(settle_ms=600)

        return None, None

    _STEP3_CLICK_DETAILS_JS = """
(rowKey) => {
  const parts = String(rowKey).split(':');
  const tableIndex = parseInt(parts[0], 10);
  const rowIndex = parseInt(parts[1], 10);
  if (Number.isNaN(tableIndex) || Number.isNaN(rowIndex)) return false;
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const isVisible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0 && r.height > 0;
  };
  const tables = Array.from(document.querySelectorAll('table'));
  const table = tables[tableIndex];
  if (!table) return false;
  const headerRow = table.querySelector('tr');
  if (!headerRow) return false;
  const headerCells = Array.from(headerRow.querySelectorAll('th, td'));
  const headerTexts = headerCells.map(cell => normalize(cell.textContent || ''));
  const detailsIdx = headerTexts.findIndex(
    txt => txt === 'details' || (txt.includes('detail') && !txt.includes('expense'))
  );
  if (detailsIdx < 0) return false;
  const bodyRows = Array.from(table.querySelectorAll('tr')).slice(1);
  if (rowIndex >= bodyRows.length) return false;
  const tr = bodyRows[rowIndex];
  try { tr.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch(e) {}
  const cell = tr.querySelectorAll('td')[detailsIdx];
  if (!cell) return false;
  const clickables = cell.querySelectorAll('a, button, img, [role="button"], input[type="image"]');
  for (const el of clickables) {
    if (isVisible(el)) { el.click(); return true; }
  }
  return false;
}
"""

    _STEP3_SHIFT_DATE_JS = r"""
() => {
  const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const monthMap = {
    jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11,
  };
  const monthNames = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
  ];
  function shiftOracleDateString(s) {
    const t = (s || '').trim();
    const m = t.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/);
    if (!m) return null;
    const mon = monthMap[m[2].toLowerCase()];
    if (mon === undefined) return null;
    const d = new Date(parseInt(m[3], 10), mon, parseInt(m[1], 10));
    if (isNaN(d.getTime())) return null;
    d.setDate(d.getDate() - 1);
    const day = String(d.getDate()).padStart(2, '0');
    return `${day}-${monthNames[d.getMonth()]}-${d.getFullYear()}`;
  }
  const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'));
  for (const inp of inputs) {
    const val = normalize(inp.value);
    const shifted = shiftOracleDateString(val);
    if (!shifted) continue;
    const isVis = (() => {
      try { const st = window.getComputedStyle(inp); const r = inp.getBoundingClientRect();
        return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 0; } catch(e) { return false; }
    })();
    if (!isVis) continue;
    inp.focus();
    inp.value = shifted;
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    inp.dispatchEvent(new Event('blur', { bubbles: true }));
    return true;
  }
  return false;
}
"""

    _STEP3_CLICK_YES_OK_JS = """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const visible = (el) => {
    if (!el || el.disabled) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 2 && r.height > 2;
  };
  const buttons = document.querySelectorAll(
    "button, a[href], input[type='button'], input[type='submit'], span[role='button']"
  );
  let okCandidate = null;
  for (const el of buttons) {
    const raw = norm(el.textContent || el.value || el.getAttribute('aria-label') || '');
    if (!raw) continue;
    if (raw === 'yes' && visible(el)) { el.click(); return true; }
    if (raw === 'ok' && visible(el)) okCandidate = el;
  }
  if (okCandidate) { okCandidate.click(); return true; }
  return false;
}
"""

    def _step3_click_return(self, timeout_s: float = 12.0) -> bool:
        """Click the 'Return' button on the detail form.

        Searches all frames for a visible, enabled button/link/input whose
        label is exactly 'Return'.  Falls back to pressing Enter if the
        button can't be located via roles (Oracle sometimes renders it as
        an <input> that Playwright role queries miss).
        """
        if not self.browser_page:
            return False
        import time as _t
        ret_re = re.compile(r"^\s*Return\s*$", re.IGNORECASE)
        deadline = _t.monotonic() + timeout_s
        while _t.monotonic() < deadline:
            for fr in self.browser_page.frames:
                try:
                    for role in ("button", "link"):
                        loc = fr.get_by_role(role, name=ret_re)
                        for i in range(loc.count()):
                            c = loc.nth(i)
                            try:
                                if c.is_visible() and c.is_enabled():
                                    c.click(timeout=5000)
                                    return True
                            except Exception:
                                continue
                except Exception:
                    continue
                try:
                    inputs = fr.locator("input[type='button'], input[type='submit']")
                    for i in range(inputs.count()):
                        c = inputs.nth(i)
                        val = (c.get_attribute("value") or "").strip()
                        if ret_re.match(val) and c.is_visible() and c.is_enabled():
                            c.click(timeout=5000)
                            return True
                except Exception:
                    continue
            self.browser_page.wait_for_timeout(250)
        try:
            self.browser_page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    def _step3_dismiss_dialog_after_return(self, timeout_s: float = 10.0) -> None:
        """After clicking Return on the detail form, Oracle may show an
        in-page confirmation dialog (Yes/OK).  Click it if it appears."""
        if not self.browser_page:
            return
        import time as _t
        deadline = _t.monotonic() + timeout_s
        while _t.monotonic() < deadline:
            for fr in self.browser_page.frames:
                try:
                    if fr.evaluate(self._STEP3_CLICK_YES_OK_JS):
                        self.browser_page.wait_for_timeout(350)
                        return
                except Exception:
                    continue
                try:
                    yes_re = re.compile(r"^\s*[Yy]es\s*$")
                    ok_re = re.compile(r"^\s*OK\s*$", re.IGNORECASE)
                    for role in ("button", "link"):
                        loc = fr.get_by_role(role, name=yes_re)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click(timeout=2000)
                            self.browser_page.wait_for_timeout(350)
                            return
                        loc = fr.get_by_role(role, name=ok_re)
                        if loc.count() > 0 and loc.first.is_visible():
                            loc.first.click(timeout=2000)
                            self.browser_page.wait_for_timeout(350)
                            return
                except Exception:
                    continue
            if not self._wizard_any_frame_on_step(3):
                return
            blob = self._step3_full_body_text().lower()
            if "business expenses" in blob and "return" not in blob:
                return
            self.browser_page.wait_for_timeout(300)

    def _step3_fix_currency_line_via_details(
        self, frame: Frame, row_key: str
    ) -> bool:
        """Open Details for a row, shift the date -1 day, click Return,
        and accept any confirmation dialog."""
        if not self.browser_page:
            return False

        def on_dialog(dialog):
            try:
                dialog.accept()
            except Exception:
                pass

        self.browser_page.on("dialog", on_dialog)
        try:
            return self._step3_fix_currency_line_via_details_inner(
                frame, row_key
            )
        finally:
            try:
                self.browser_page.remove_listener("dialog", on_dialog)
            except Exception:
                pass

    def _step3_fix_currency_line_via_details_inner(
        self, frame: Frame, row_key: str
    ) -> bool:
        assert self.browser_page is not None
        try:
            clicked = frame.evaluate(self._STEP3_CLICK_DETAILS_JS, row_key)
        except Exception:
            return False
        if not clicked:
            return False
        self._wait_for_oracle_page_stable(timeout_s=15.0)

        import time as _time
        deadline = _time.monotonic() + 15
        detail_ready = False
        while _time.monotonic() < deadline:
            for fr in self.browser_page.frames:
                try:
                    blob = fr.evaluate(
                        "() => (document.body && document.body.innerText) || ''"
                    )
                    if blob and "Return" in blob and "Start Date" in blob:
                        detail_ready = True
                        break
                except Exception:
                    continue
            if detail_ready:
                break
            self.browser_page.wait_for_timeout(300)

        if not detail_ready:
            self._step3_return_to_table()
            return False

        shifted = False
        for fr in self.browser_page.frames:
            try:
                if fr.evaluate(self._STEP3_SHIFT_DATE_JS):
                    shifted = True
                    break
            except Exception:
                continue

        self.browser_page.wait_for_timeout(500)
        self._step3_return_to_table()
        return shifted

    def _step3_fix_banner_errors(
        self, merchant_to_type: dict[str, str]
    ) -> int:
        """Parse Step 3 banner errors and fix them in order.

        For ``expense_justification`` errors: navigate to the line, set
        the expense type dropdown and justification field.
        For ``currency`` errors: open the line Details form, shift the
        date -1 day, and Return.

        Returns the number of errors successfully fixed.
        """
        if not self.browser_page:
            return 0

        fixed_total = 0
        fail_streak = 0
        max_rounds = 40

        for _ in range(max_rounds):
            errs = self._scrape_step3_banner_errors()
            if not errs:
                return fixed_total

            line_no, kind = errs[0]
            self.set_status(f"Step 3: fixing error on line {line_no} ({kind})…")

            frame, row_key = self._step3_navigate_to_line_number(line_no)
            if not frame or not row_key:
                self.set_status(
                    f"Step 3: could not find row for line {line_no} — skipping."
                )
                fail_streak += 1
                if fail_streak >= 4:
                    break
                self.click_save_button_wizard_in_any_frame(
                    body_must_contain=None, wizard_step=3
                )
                self._wait_for_oracle_page_stable()
                continue

            fixed = False
            if kind == "currency":
                fixed = self._step3_fix_currency_line_via_details(
                    frame, row_key
                )
            else:
                merchant = ""
                _, rows = self._extract_step3_rows()
                for r in (rows or []):
                    if str(r.get("row_key", "")).strip() == row_key:
                        merchant = re.sub(
                            r"\s+", " ",
                            str(r.get("merchant_name", ""))
                        ).lower().strip()
                        break

                expense_type = merchant_to_type.get(merchant, "")
                if not expense_type:
                    for mk, et in merchant_to_type.items():
                        if mk in merchant or merchant in mk:
                            expense_type = et
                            break

                if expense_type:
                    try:
                        ok = frame.evaluate(
                            self._APPLY_EXPENSE_TYPE_JS,
                            [row_key, expense_type],
                        )
                        fixed = bool(ok)
                        if fixed:
                            self.set_status(
                                f"Step 3: fixed '{expense_type}' for line {line_no}"
                            )
                    except Exception:
                        pass

            if fixed:
                fixed_total += 1
                fail_streak = 0
            else:
                fail_streak += 1
                if fail_streak >= 4:
                    self.set_status(
                        "Step 3: too many consecutive fix failures — stopping."
                    )
                    break

            self.click_save_button_wizard_in_any_frame(
                body_must_contain=None, wizard_step=3
            )
            self._wait_for_oracle_page_stable()

        return fixed_total

    def _step3_advance_with_error_recovery(
        self,
        lines: list[SubmissionLine],
    ) -> None:
        """Save, click Next on Step 3, and handle validation errors.

        If Oracle shows banner errors (missing expense type / justification
        or currency date mismatch), this method parses them, fixes the
        offending rows, saves, and retries Next — up to a reasonable limit.
        """
        assert self.browser_page is not None

        merchant_to_type: dict[str, str] = {}
        for ln in lines:
            if ln.expense_type:
                key = re.sub(r"\s+", " ", ln.merchant_name).lower().strip()
                merchant_to_type[key] = ln.expense_type

        max_rounds = 15
        for round_idx in range(max_rounds):
            self.set_status("Step 3: saving and advancing…")
            self.click_save_button_wizard_in_any_frame(
                body_must_contain=None, wizard_step=3
            )
            self._wait_for_oracle_page_stable()

            if self.wait_for_wizard_next_enabled_and_click(
                self._STEP_ADVANCE_TIMEOUT_MS, wizard_step=3,
            ):
                self._wait_for_oracle_page_stable()
                if not self._wizard_any_frame_on_step(3):
                    return
                self._wait_for_oracle_page_stable()

            if not self._wizard_any_frame_on_step(3):
                return

            errs = self._scrape_step3_banner_errors()
            if not errs:
                if round_idx == 0:
                    raise RuntimeError(
                        "Could not advance from Step 3 to Step 4."
                    )
                return

            kinds = set(k for _, k in errs)
            self.set_status(
                f"Step 3: {len(errs)} validation error(s) "
                f"({', '.join(sorted(kinds))}). Fixing…"
            )

            n_fixed = self._step3_fix_banner_errors(merchant_to_type)
            self.set_status(
                f"Step 3: fixed {n_fixed} error(s), retrying advance "
                f"(round {round_idx + 2}/{max_rounds})…"
            )
            if n_fixed == 0:
                raise RuntimeError(
                    f"Step 3: {len(errs)} validation error(s) remain but "
                    f"none could be auto-fixed. Check the browser."
                )

        raise RuntimeError(
            "Step 3: gave up after too many validation retries."
        )

    # ------------------------------------------------------------------
    # Step 6 — receipt attachments
    # ------------------------------------------------------------------

    _EXTRACT_STEP6_ROWS_JS = """
() => {
  const clean = (v) => (v || '').replace(/\\s+/g, ' ').trim();
  const norm = (v) => clean(v).toLowerCase();
  const tables = Array.from(document.querySelectorAll('table'));
  let best = null;
  let bestScore = -1;
  tables.forEach((table, tableIndex) => {
    const rows = Array.from(table.rows || []);
    if (rows.length < 2) return;
    for (let hIdx = 0; hIdx < Math.min(rows.length, 3); hIdx++) {
      const hr = rows[hIdx];
      const hc = Array.from(hr.cells || []).map(c => norm(c.textContent || ''));
      if (hc.length < 3) continue;
      const di = hc.findIndex(t => t === 'date' || (t.startsWith('date') && !t.includes('expense')));
      let rai = hc.findIndex(t =>
        (t.includes('receipt') && (t.includes('amount') || t.includes('amt'))) || t === 'receipt amount'
      );
      if (rai < 0) rai = hc.findIndex(t => t.includes('amount') && !t.includes('reimbursable'));
      const mi = hc.findIndex(t => t.includes('merchant'));
      const ai = hc.findIndex(t => t.includes('attachment'));
      const score = (di >= 0 ? 2 : 0) + (rai >= 0 ? 5 : 0) + (mi >= 0 ? 4 : 0) + (ai >= 0 ? 3 : 0);
      if (score > bestScore) {
        bestScore = score;
        best = { table, tableIndex, headerRowIndex: hIdx, di, rai, mi, ai };
      }
    }
  });
  if (!best || best.mi < 0 || best.di < 0) return [];
  const { table, tableIndex, headerRowIndex, di, rai, mi, ai } = best;
  const bodyRows = Array.from(table.rows || []).slice(headerRowIndex + 1);
  const out = [];
  bodyRows.forEach((tr, idx) => {
    const bodyRowIndex = headerRowIndex + 1 + idx;
    const cells = Array.from(tr.cells || []);
    if (cells.length <= mi) return;
    const date = di < cells.length ? clean(cells[di].innerText || cells[di].textContent) : '';
    const receiptAmt = (rai >= 0 && rai < cells.length) ? clean(cells[rai].innerText || cells[rai].textContent) : '';
    const merchant = clean(cells[mi].innerText || cells[mi].textContent);
    let hasExistingAttachment = false;
    if (ai >= 0 && ai < cells.length) {
      const ac = cells[ai];
      const acBlob = norm(ac.innerText || ac.textContent || '');
      if (acBlob && !acBlob.includes('+') && (acBlob.includes('.pdf') || acBlob.includes('.png') || acBlob.includes('.jpg') || acBlob.includes('.jpeg'))) {
        hasExistingAttachment = true;
      }
      if (!hasExistingAttachment) {
        const imgs = Array.from(ac.querySelectorAll('img'));
        for (const img of imgs) {
          const src = norm(img.getAttribute('src') || '');
          const alt = norm(img.getAttribute('alt') || '');
          if (src.includes('paperclip') || alt.includes('paperclip')) {
            hasExistingAttachment = true;
            break;
          }
        }
      }
    }
    if (norm(merchant).includes('merchant name')) return;
    if (!merchant) return;
    out.push({ tableIndex, bodyRowIndex, date, receiptAmount: receiptAmt, merchant, hasExistingAttachment });
  });
  return out;
}
"""

    _CLICK_ATTACH_PLUS_JS = """
([tableIndex, bodyRowIndex]) => {
  const norm = (v) => String(v || '').trim().toLowerCase();
  const isVisible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return st.visibility !== 'hidden' && st.display !== 'none' && r.width > 1 && r.height > 1;
  };
  const tables = Array.from(document.querySelectorAll('table'));
  if (tableIndex >= tables.length) return false;
  const table = tables[tableIndex];
  if (bodyRowIndex >= table.rows.length) return false;
  const tr = table.rows[bodyRowIndex];
  try { tr.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
  let attIdx = -1;
  for (let h = 0; h < Math.min(table.rows.length, 3); h++) {
    const hc = Array.from(table.rows[h].cells || []).map(c => norm(c.textContent || ''));
    const idx = hc.findIndex(t => t.includes('attachment'));
    if (idx >= 0) { attIdx = idx; break; }
  }
  if (attIdx >= 0 && attIdx < tr.cells.length) {
    const cell = tr.cells[attIdx];
    const clickables = cell.querySelectorAll('a, button, img, [role="button"], input[type="image"]');
    for (const el of clickables) {
      if (isVisible(el)) { el.click(); return true; }
    }
  }
  const imgs = tr.querySelectorAll('img');
  for (const img of imgs) {
    const blob = norm([img.getAttribute('alt') || '', img.getAttribute('title') || '',
      img.getAttribute('src') || ''].join(' '));
    if ((blob.includes('add') || blob.includes('attach') || blob.includes('plus') || blob.includes('new'))
        && isVisible(img)) { img.click(); return true; }
  }
  return false;
}
"""

    def _stage_attachment_file(self, line_id: str, source: Path) -> Path | None:
        staging_dir = APP_DIR / "staged-uploads"
        staging_dir.mkdir(parents=True, exist_ok=True)
        dest = staging_dir / f"{line_id}_{source.name}"
        try:
            shutil.copy2(str(source), str(dest))
            return dest if dest.is_file() else None
        except Exception:
            return None

    def _wait_for_add_attachment_modal(self, timeout_s: float = 8.0) -> bool:
        if not self.browser_page:
            return False
        import time
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate(
                        "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                    )
                    if blob and ("Add Attachment" in blob or "Choose File" in blob or "Browse" in blob):
                        return True
                except Exception:
                    continue
            self.browser_page.wait_for_timeout(200)
        return False

    _SELECT_CATEGORY_JS = """
(label) => {
  const norm = (v) => (v || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const want = norm(label);
  for (const sel of document.querySelectorAll('select')) {
    const id = (sel.id || sel.name || '').toLowerCase();
    const prev = sel.previousElementSibling || sel.closest('td')?.previousElementSibling;
    const ctx = norm(
      [id, prev ? prev.textContent : '', sel.getAttribute('title') || ''].join(' ')
    );
    if (!ctx.includes('categ')) continue;
    for (const opt of sel.options) {
      if (norm(opt.textContent) === want || norm(opt.value) === want) {
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
    for (const opt of sel.options) {
      if (norm(opt.textContent).includes(want) || want.includes(norm(opt.textContent))) {
        sel.value = opt.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
      }
    }
  }
  return false;
}
"""

    def _complete_attachment_upload(self, file_path: Path) -> bool:
        if not self.browser_page or not file_path.is_file():
            return False
        for frame in self.browser_page.frames:
            try:
                file_input = frame.locator('input[type="file"]')
                if file_input.count() > 0:
                    file_input.first.set_input_files(str(file_path))
                    self.browser_page.wait_for_timeout(500)

                    try:
                        frame.evaluate(self._SELECT_CATEGORY_JS, "Receipts")
                        self.browser_page.wait_for_timeout(300)
                    except Exception:
                        pass

                    for btn_text in ("Apply", "OK", "Add", "Upload", "Submit", "Save"):
                        btn = frame.get_by_role("button", name=re.compile(btn_text, re.IGNORECASE))
                        if btn.count() > 0:
                            try:
                                if btn.first.is_visible():
                                    btn.first.click(timeout=8000)
                                    self.browser_page.wait_for_timeout(800)
                                    return True
                            except Exception:
                                continue
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _fuzzy_merchant_match(key: str, merchant: str) -> bool:
        """Loose merchant comparison: substring or word-overlap."""
        if key in merchant or merchant in key:
            return True
        key_words = set(re.sub(r"[^a-z0-9 ]", "", key).split())
        merch_words = set(re.sub(r"[^a-z0-9 ]", "", merchant).split())
        if key_words and merch_words:
            overlap = key_words & merch_words
            if len(overlap) >= max(1, min(len(key_words), len(merch_words)) // 2):
                return True
        return False

    @staticmethod
    def _normalize_amount(raw: str) -> str:
        """Strip currency codes and whitespace, keep digits/dot/comma."""
        return re.sub(r"[^0-9.,]", "", raw).strip().rstrip(".")

    def _attach_receipts_step6(self, lines: list[SubmissionLine]) -> int:
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")

        @dataclass
        class _ReceiptEntry:
            merchant: str
            date: str
            amount: str
            path: Path
            used: bool = False

        receipt_pool: list[_ReceiptEntry] = []
        skipped_missing = 0
        skipped_no_path = 0
        skipped_not_found = 0
        for ln in lines:
            if ln.receipt_missing:
                skipped_missing += 1
                continue
            if not ln.receipt_path:
                skipped_no_path += 1
                continue
            p = Path(ln.receipt_path).expanduser()
            if not p.is_file():
                skipped_not_found += 1
                continue
            receipt_pool.append(_ReceiptEntry(
                merchant=ln.merchant_name.lower().strip(),
                date=ln.transaction_date.strip(),
                amount=self._normalize_amount(ln.amount),
                path=p,
            ))

        diag_parts = [f"{len(receipt_pool)} receipt(s) ready"]
        if skipped_no_path:
            diag_parts.append(f"{skipped_no_path} with no receipt path")
        if skipped_missing:
            diag_parts.append(f"{skipped_missing} marked missing")
        if skipped_not_found:
            diag_parts.append(f"{skipped_not_found} file(s) not found on disk")
        self.set_status(f"Step 6: {', '.join(diag_parts)}.")

        if not receipt_pool:
            return 0

        self.set_status("Step 6: waiting for expense table to render…")

        import time as _time
        _deadline = _time.monotonic() + 30
        step6_rows: list[dict] = []
        target_frame: Frame | None = None
        while _time.monotonic() < _deadline:
            for frame in self.browser_page.frames:
                try:
                    rows = frame.evaluate(self._EXTRACT_STEP6_ROWS_JS)
                    if rows and isinstance(rows, list) and len(rows) > 0:
                        step6_rows = rows
                        target_frame = frame
                        break
                except Exception:
                    continue
            if step6_rows:
                break
            self.browser_page.wait_for_timeout(500)

        if not step6_rows or not target_frame:
            self.set_status("Step 6: could not find expense lines table — skipping attachments.")
            return 0

        row_merchants = [r.get("merchant", "") for r in step6_rows]
        self.set_status(
            f"Step 6: found {len(step6_rows)} row(s). "
            f"Table merchants: {row_merchants[:5]}{'…' if len(row_merchants) > 5 else ''}"
        )

        def find_best_receipt(row_merchant: str, row_date: str, row_amount: str) -> _ReceiptEntry | None:
            norm_amt = self._normalize_amount(row_amount)
            for entry in receipt_pool:
                if entry.used:
                    continue
                if entry.merchant == row_merchant and entry.amount == norm_amt:
                    return entry
            for entry in receipt_pool:
                if entry.used:
                    continue
                if entry.merchant == row_merchant:
                    return entry
            for entry in receipt_pool:
                if entry.used:
                    continue
                if self._fuzzy_merchant_match(entry.merchant, row_merchant):
                    return entry
            return None

        attached = 0
        for row in step6_rows:
            if row.get("hasExistingAttachment"):
                continue
            merchant = row.get("merchant", "").lower().strip()
            row_date = row.get("date", "")
            row_amount = row.get("receiptAmount", "")

            entry = find_best_receipt(merchant, row_date, row_amount)
            if not entry:
                self.set_status(f"Step 6: no receipt match for '{merchant}'")
                continue

            staged = self._stage_attachment_file(f"s6_{attached}", entry.path)
            if not staged:
                self.set_status(f"Step 6: failed to stage file for '{merchant}'")
                continue

            try:
                self.set_status(f"Step 6: clicking attach for '{merchant}'…")
                clicked = target_frame.evaluate(
                    self._CLICK_ATTACH_PLUS_JS,
                    [row["tableIndex"], row["bodyRowIndex"]],
                )
                if not clicked:
                    self.set_status(f"Step 6: could not click attach icon for '{merchant}'")
                    continue
                self._wait_for_oracle_page_stable(timeout_s=12.0, settle_ms=600)

                if not self._wait_for_add_attachment_modal(timeout_s=15.0):
                    self.set_status(f"Step 6: attach modal did not appear for '{merchant}'")
                    continue

                if self._complete_attachment_upload(staged):
                    attached += 1
                    entry.used = True
                    self.set_status(
                        f"Step 6: attached receipt for '{merchant}' "
                        f"({attached}/{len(receipt_pool)})"
                    )
                    self._wait_for_oracle_page_stable(settle_ms=600)
                else:
                    self.set_status(f"Step 6: upload failed for '{merchant}'")
            except Exception as exc:
                self.set_status(f"Step 6: error attaching '{merchant}': {exc}")
                continue

        return attached

    # ------------------------------------------------------------------
    # Wizard step detection & waits
    # ------------------------------------------------------------------

    def _detect_wizard_step(self) -> int | None:
        """Detect which wizard step (1-6) the browser is currently on."""
        if not self.browser_page:
            return None
        for step in range(1, 7):
            if self._wizard_any_frame_on_step(step):
                return step
        return None

    def _wait_for_step3_business_expenses(self, timeout_ms: int = 120_000) -> None:
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        import time
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            if self._wizard_any_frame_on_step(3):
                self.browser_page.wait_for_timeout(500)
                return
            self.browser_page.wait_for_timeout(300)
        raise RuntimeError("Timeout waiting for Business Expenses (Step 3) to load.")

    def _wait_for_step6_attachments(self, timeout_ms: int = 120_000) -> None:
        if not self.browser_page:
            raise RuntimeError("Browser page not available.")
        import time
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            if self._wizard_any_frame_on_step(6):
                self.browser_page.wait_for_timeout(500)
                return
            for frame in self.browser_page.frames:
                try:
                    blob = frame.evaluate(
                        "() => (document.body && document.body.innerText) ? document.body.innerText : ''"
                    )
                    if blob and "Attachment" in blob and ("Receipt Amount" in blob or "Merchant" in blob):
                        self.browser_page.wait_for_timeout(500)
                        return
                except Exception:
                    continue
            self.browser_page.wait_for_timeout(300)
        raise RuntimeError("Timeout waiting for Attachments (Step 6) to load.")

    def _click_submit_button(self) -> bool:
        if not self.browser_page:
            return False
        submit_pat = re.compile(r"^\s*Submit\s*$", re.IGNORECASE)
        for frame in self.browser_page.frames:
            try:
                btn = frame.get_by_role("button", name=submit_pat)
                if btn.count() > 0 and btn.first.is_enabled():
                    btn.first.click(timeout=20000)
                    return True
            except Exception:
                continue
        return self.click_text_in_any_frame("Submit")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Wizard step execution (extracted for self-recovery)
    # ------------------------------------------------------------------

    def _execute_wizard_steps(
        self,
        start_from_step: int,
        purpose: str,
        payload: SubmissionPayload,
        summary: dict[str, Any],
    ) -> None:
        """Run wizard Steps 1–6 and click Submit.

        Starts from *start_from_step* (0 or 1 = Step 1, 2 = Step 2, etc.)
        and advances through the remaining steps.  Raises on failure so the
        caller can retry from the detected step.
        """
        assert self.browser_page is not None

        # --- Step 1: General Information ---
        if start_from_step <= 1:
            self.set_status("Waiting for General Information (Step 1)…")
            self.wait_for_step1_general_information_ready()
            self._wait_for_oracle_page_stable()

            self.set_status("Selecting Travel template…")
            if not self.select_travel_template_in_any_frame():
                raise RuntimeError(
                    "Could not find template dropdown or Travel option."
                )
            self._wait_for_oracle_page_stable(settle_ms=500)

            self.set_status(f"Setting purpose: {purpose}")
            if not self.fill_purpose_in_any_frame(purpose):
                raise RuntimeError("Could not locate Purpose field.")
            self.browser_page.wait_for_timeout(800)

            self.set_status(f"Setting approver: {payload.approver}")
            if not self.fill_approver_in_any_frame(payload.approver):
                raise RuntimeError("Could not locate Approver field.")
            self._wait_for_oracle_page_stable()

            self.set_status("Saving General Information…")
            if not self.click_save_button_wizard_in_any_frame():
                self._wait_for_oracle_page_stable(timeout_s=8.0)
                if not self.click_save_button_wizard_in_any_frame():
                    raise RuntimeError(
                        "Could not click Save on General Information."
                    )
            self._wait_for_oracle_page_stable()

            self.set_status("Clicking Next to Step 2…")
            if not self.wait_for_wizard_next_enabled_and_click(
                self._STEP_ADVANCE_TIMEOUT_MS, wizard_step=1,
            ):
                raise RuntimeError(
                    "Next button did not become enabled after Save."
                )

        # --- Step 2: Select credit card transactions ---
        if start_from_step <= 2:
            self.set_status("Waiting for Credit Card Transactions (Step 2)…")
            self.wait_for_step2_credit_card_transactions()
            self._wait_for_oracle_page_stable()

            self.set_status(
                f"Selecting {len(payload.lines)} report transactions (paging through all pages)…"
            )
            n_selected = self._select_specific_transactions_step2(
                payload.lines
            )
            summary["transactions_selected"] = n_selected
            if payload.lines and n_selected < len(payload.lines):
                targets = getattr(self, "_last_step2_selection_targets", [])
                page_attempts = getattr(self, "_last_step2_page_attempts", [])
                diagnostic = self._collect_step2_failure_diagnostics(
                    targets, page_attempts,
                )
                raise RuntimeError(
                    f"Step 2: selected {n_selected}/{len(payload.lines)} transactions; "
                    "expected all report lines to be selected before proceeding.\n"
                    f"{diagnostic}"
                )
            self.set_status(
                f"Step 2: selected {n_selected}/{len(payload.lines)} transaction(s) across all pages."
            )
            self.browser_page.wait_for_timeout(400)

            n_deselected = self._deselect_extra_transactions_step2(
                payload.lines
            )
            summary["transactions_deselected"] = n_deselected
            if n_deselected:
                self.set_status(
                    f"Step 2: deselected {n_deselected} removed transaction(s)."
                )
                self.browser_page.wait_for_timeout(400)

            self.set_status("Clicking Next to Step 3…")
            if not self.wait_for_wizard_next_enabled_and_click(
                self._STEP_ADVANCE_TIMEOUT_MS, wizard_step=2,
            ):
                raise RuntimeError(
                    "Could not advance from Step 2 to Step 3."
                )

        # --- Step 3: Expense types ---
        if start_from_step <= 3:
            self.set_status("Waiting for Business Expenses (Step 3)…")
            self._wait_for_step3_business_expenses()
            self._wait_for_oracle_page_stable()

            n_types = self._apply_expense_types_step3(payload.lines)
            summary["expense_types_set"] = n_types
            self.set_status(f"Step 3: assigned {n_types} expense type(s).")

            n_missing = self._step3_mark_receipt_missing_lines(payload.lines)
            summary["receipt_missing_marked"] = n_missing
            if n_missing:
                self.set_status(
                    f"Step 3: marked {n_missing} line(s) as receipt missing."
                )

            # Advance 3 → 4 with validation error recovery
            self._step3_advance_with_error_recovery(payload.lines)
            self._wait_for_oracle_page_stable()

        # --- Step 4 → 5 transition ---
        if start_from_step <= 4:
            self._wait_for_oracle_page_stable()
            self.set_status("Clicking Next through Step 5…")
            if not self.wait_for_wizard_next_enabled_and_click(
                self._STEP_ADVANCE_TIMEOUT_MS, wizard_step=4,
            ):
                raise RuntimeError(
                    "Could not advance from Step 4 to Step 5."
                )
            self._wait_for_oracle_page_stable()

        # --- Step 5 → 6 transition ---
        if start_from_step <= 5:
            self._wait_for_oracle_page_stable()
            self.set_status("Clicking Next past Step 5…")
            if not self.wait_for_wizard_next_enabled_and_click(
                self._STEP_ADVANCE_TIMEOUT_MS, wizard_step=5,
            ):
                raise RuntimeError(
                    "Could not advance from Step 5 to Step 6."
                )
            self._wait_for_oracle_page_stable()

        # --- Step 6: Attachments ---
        self.set_status("Waiting for Attachments (Step 6)…")
        self._wait_for_step6_attachments()
        self._wait_for_oracle_page_stable()

        n_attached = self._attach_receipts_step6(payload.lines)
        summary["receipts_attached"] = n_attached
        self.set_status(f"Step 6: attached {n_attached} receipt(s).")

        # --- Done — leave final submission to the user ---
        self.set_status(
            "Automation complete — review the report and click Submit "
            "in the browser when ready."
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    _MAX_RECOVERY_ATTEMPTS = 3
    _STEP_ADVANCE_TIMEOUT_MS = 45_000

    def submit(
        self,
        portal_url: str,
        payload: SubmissionPayload,
        *,
        resume_cdp_url: str | None = None,
        keep_browser_on_error: bool = True,
    ) -> dict[str, Any]:
        """Full submission flow: launch → manual login → wizard → submit → close.

        When *resume_cdp_url* is provided, attempts to reconnect to an
        existing Chromium instance and resume from the detected wizard step.
        If reconnection fails or the wizard position cannot be determined,
        the browser is closed and the flow restarts from scratch.

        The wizard steps include built-in self-recovery: if a step fails,
        the automation detects the current wizard position and retries up
        to ``_MAX_RECOVERY_ATTEMPTS`` times before giving up.

        Returns a summary dict with counts of actions taken.
        """
        succeeded = False
        summary: dict[str, Any] = {
            "is_update": False,
            "transactions_selected": 0,
            "transactions_deselected": 0,
            "expense_types_set": 0,
            "receipts_attached": 0,
            "submitted": False,
        }
        purpose = payload.report_name or "travel"
        start_from_step = 0

        try:
            # --- Reconnect or launch fresh ---
            if resume_cdp_url:
                self.set_status("Reconnecting to existing browser…")
                if self._reconnect_to_cdp(resume_cdp_url):
                    detected = self._detect_wizard_step()
                    if detected:
                        start_from_step = detected
                        summary["resumed_from_step"] = detected
                        self.set_status(
                            f"Reconnected — resuming from Step {detected}."
                        )
                    else:
                        self.set_status(
                            "Cannot determine wizard position — restarting."
                        )
                        self.close_browser()
                else:
                    self.set_status(
                        "Browser no longer available — starting fresh."
                    )

            if start_from_step == 0:
                self.set_status(f"Launching Chromium → {portal_url}")
                self.open_browser(portal_url)

                self.wait_for_manual_oracle_login()
                assert self.browser_page is not None
                self.browser_page.wait_for_timeout(500)

                # Navigate to iExpenses and detect existing report
                self.set_status("Expanding iExpenses in Navigator…")
                self._oracle_expand_nic_iexpenses_menu()
                self._wait_for_oracle_page_stable(settle_ms=600)

                self.set_status(f"Checking for existing report '{purpose}'…")
                is_update = self._find_and_click_existing_report(purpose)

                if is_update:
                    summary["is_update"] = True
                    self.set_status(
                        f"Found existing report '{purpose}' — opening for update."
                    )
                    self.browser_page.wait_for_timeout(2000)
                else:
                    summary["is_update"] = False
                    self.set_status(
                        "No existing report found — creating new report."
                    )
                    if not self._body_contains_text("Create Expense Report"):
                        self._oracle_expand_nic_iexpenses_menu()
                        self.browser_page.wait_for_timeout(600)
                    if not self.click_text_in_any_frame("Create Expense Report"):
                        raise RuntimeError(
                            "Could not click 'Create Expense Report'. "
                            "Ensure the Oracle portal is accessible."
                        )

            # --- Wizard steps with automatic self-recovery ---
            for attempt in range(self._MAX_RECOVERY_ATTEMPTS + 1):
                try:
                    self._execute_wizard_steps(
                        start_from_step, purpose, payload, summary,
                    )
                    break  # wizard completed successfully
                except Exception as exc:
                    if attempt >= self._MAX_RECOVERY_ATTEMPTS:
                        raise
                    if not self.browser_page:
                        raise
                    detected = self._detect_wizard_step()
                    if not detected:
                        raise
                    start_from_step = detected
                    self.set_status(
                        f"Step failed: {exc} — detected Step {detected}, "
                        f"recovering (attempt "
                        f"{attempt + 2}/{self._MAX_RECOVERY_ATTEMPTS + 1})…"
                    )
                    self.browser_page.wait_for_timeout(2000)

            succeeded = True
            return summary
        finally:
            if succeeded:
                self.set_status(
                    "Automation complete — Chromium remains open for review."
                )
            else:
                self.set_status(
                    "Submission did not complete — click Submit again to "
                    "resume, or close the browser to start over."
                )
            self._detach_playwright()
