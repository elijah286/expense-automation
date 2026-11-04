"""Read-only browsing of local submission snapshots."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from nicegui import ui

from persistence.report_history import ReportHistoryStore
from web.service import ReceiptDoc


_SEARCH_PROPS = (
    ':filter-method="(rows, terms) => rows.filter(row => '
    'row.search.toLowerCase().includes(terms.toLowerCase()))"'
)


def _columns(*names: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {"name": name, "label": label, "field": name, "align": "left", "sortable": True}
        for name, label in names
    ]


def _action(table: ui.table, label: str, event: str, callback: Callable) -> None:
    table.add_slot("body-cell-actions", f"""
        <q-td :props="props">
            <q-btn flat no-caps label="{label}"
                @click="$parent.$emit('{event}', props.row.id)" />
        </q-td>
    """)
    table.on(event, lambda e: callback(e.args))


def history_screen(
    store: ReportHistoryStore, submission_id: str,
    open_image: Callable[[ReceiptDoc], None],
) -> None:
    with ui.element("div").classes("page-container"):
        ui.label("History").classes("section-title")
        ui.label(
            "Read-only submitted reports and receipt copies, stored locally without an expiry."
        ).classes("section-subtitle")
        try:
            if submission_id:
                report = store.get(submission_id)
            else:
                reports = store.list()
        except (ValueError, OSError) as exc:
            ui.label(f"Could not load history: {exc}").classes("text-red-600")
            ui.link("Back to History", "/history")
            return
        if not submission_id:
            if not reports:
                ui.label("No submissions yet. Mark a report as submitted to save it here.").classes(
                    "text-slate-500 py-8"
                )
                return
            search = ui.input("Search submissions", placeholder="Report, date, expense or receipt").classes("w-full")
            rows = [{
                "id": report["id"], "name": report["report_name"],
                "submitted": report["submitted_at"],
                "expenses": len(report["expenses"]), "receipts": len(report["receipts"]),
                "search": json.dumps(report, ensure_ascii=False),
            } for report in reports]
            table = ui.table(
                columns=_columns(
                    ("name", "Report"), ("submitted", "Submitted"),
                    ("expenses", "Expenses"), ("receipts", "Receipts"), ("actions", ""),
                ), rows=rows, row_key="id", pagination=15,
            ).classes("w-full").props(_SEARCH_PROPS).bind_filter_from(search, "value")
            _action(table, "Open report", "open_report",
                    lambda rid: ui.navigate.to("/history?" + urlencode({"submission": rid})))
            return

        ui.link("Back to History", "/history")
        ui.label(report["report_name"]).classes("text-xl font-semibold mt-4")
        ui.label(f"Submitted: {report['submitted_at']}").classes("text-sm text-slate-500")
        ui.label("Archived snapshot - editing active expenses will not change this copy.").classes(
            "text-sm text-slate-500"
        )
        if report.get("recovered"):
            ui.label(
                "Recovered from the previous five-day restore window. Metadata reflects what "
                "was still available when recovered; unavailable receipts are flagged below."
            ).classes("text-sm text-amber-700")
        receipts = {r["id"]: r for r in report["receipts"]}

        def open_receipt(receipt_id: str) -> None:
            receipt = receipts[receipt_id]
            try:
                path = store.receipt_path(submission_id, receipt_id)
            except (ValueError, OSError) as exc:
                ui.notify(str(exc), type="negative")
                return
            url = "/api/image?" + urlencode({"path": str(path)})
            if path.suffix.lower() == ".pdf":
                with ui.dialog().props("maximized") as dialog, ui.card().classes("w-full h-full"):
                    with ui.row().classes("items-center justify-between w-full"):
                        ui.label(receipt["filename"]).classes("font-semibold")
                        ui.link("Open full PDF", url, new_tab=True)
                        ui.button("Close", on_click=dialog.close).props("flat no-caps")
                    ui.element("iframe").props(f'src="{url}" title="Archived receipt PDF"').classes(
                        "w-full grow"
                    ).style("min-height:70vh;border:0")
                dialog.open()
            else:
                analysis = receipt["analysis"]
                open_image(ReceiptDoc(
                    source_file=str(path), filename=receipt["filename"],
                    vendor=str(analysis.get("vendor", "")),
                    date=str(analysis.get("receipt_date", "")),
                    currency=str(analysis.get("currency", "")),
                    total=str(analysis.get("total_amount") or analysis.get("matched_amount") or ""),
                    rotation=int(analysis.get("display_rotation_quarter_turns", 0) or 0),
                    notes=str(analysis.get("notes", "")),
                ))

        def expense_receipts(expense: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                receipt for receipt in receipts.values()
                if expense["id"] in receipt["line_ids"]
                or receipt["source_file"] in expense.get("receipt_sources", [])
            ]

        def open_expense(expense_id: str) -> None:
            expense = next(e for e in report["expenses"] if e["id"] == expense_id)
            with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width:760px"):
                ui.label(expense.get("merchant") or expense.get("description") or "Expense").classes(
                    "text-lg font-semibold"
                )
                ui.label(f"{expense['date']} | {expense['currency']} {expense['amount']}")
                ui.label(str(expense.get("expense_type", "")))
                if expense.get("description"):
                    ui.label(expense["description"])
                if expense.get("miles"):
                    ui.label(f"{expense['miles']} miles | estimated rate: {expense.get('rate', '')}")
                if expense.get("oracle_amount"):
                    ui.label(f"Oracle reimbursement: USD {expense['oracle_amount']}")
                linked = expense_receipts(expense)
                if not linked:
                    ui.label("No receipt attached to this expense.").classes("text-slate-500")
                for receipt in linked:
                    ui.button(
                        f"Open receipt: {receipt['filename']}",
                        on_click=lambda _, rid=receipt["id"]: open_receipt(rid),
                    ).props("flat no-caps")
                with ui.expansion("Archived expense data").classes("w-full"):
                    ui.code(json.dumps(expense, indent=2, ensure_ascii=False), language="json").classes("w-full")
                ui.button("Close", on_click=dialog.close).props("flat no-caps")
            dialog.open()

        ui.label("Expenses").classes("text-lg font-semibold mt-6")
        search_expenses = ui.input(
            "Search expenses", placeholder="Merchant, date, amount, type, description or receipt"
        ).classes("w-full")
        expense_rows = [{
            **expense, "kind": expense["kind"].replace("_", " ").title(),
            "receipts": ", ".join(r["filename"] for r in expense_receipts(expense)),
            "search": json.dumps([expense, expense_receipts(expense)], ensure_ascii=False),
        } for expense in report["expenses"]]
        expenses_table = ui.table(
            columns=_columns(
                ("date", "Date"), ("kind", "Kind"), ("merchant", "Merchant"),
                ("description", "Description"), ("amount", "Amount"), ("currency", "Currency"),
                ("expense_type", "Expense type"), ("receipts", "Receipts"), ("actions", ""),
            ), rows=expense_rows, row_key="id", pagination=15,
        ).classes("w-full").props(_SEARCH_PROPS).bind_filter_from(search_expenses, "value")
        _action(expenses_table, "View expense", "open_expense", open_expense)

        ui.label("Receipts").classes("text-lg font-semibold mt-6")
        search_receipts = ui.input(
            "Search receipts", placeholder="Filename, vendor, date, amount or extracted receipt text"
        ).classes("w-full")
        receipt_rows = []
        for receipt in receipts.values():
            analysis = receipt["analysis"]
            receipt_rows.append({
                "id": receipt["id"], "filename": receipt["filename"],
                "vendor": analysis.get("vendor", ""), "date": analysis.get("receipt_date", ""),
                "amount": str(analysis.get("total_amount") or analysis.get("matched_amount") or ""),
                "currency": analysis.get("currency", ""),
                "status": "Unavailable at recovery" if receipt["missing"] else "Archived",
                "search": json.dumps(receipt, ensure_ascii=False),
            })
        receipt_table = ui.table(
            columns=_columns(
                ("filename", "Receipt"), ("vendor", "Vendor"), ("date", "Date"),
                ("amount", "Amount"), ("currency", "Currency"), ("status", "Status"), ("actions", ""),
            ), rows=receipt_rows, row_key="id", pagination=12,
        ).classes("w-full").props(_SEARCH_PROPS).bind_filter_from(search_receipts, "value")
        _action(receipt_table, "Open receipt", "open_receipt", open_receipt)
        if report.get("submission_result"):
            with ui.expansion("Automation results").classes("w-full mt-4"):
                ui.code(json.dumps(report["submission_result"], indent=2, ensure_ascii=False), language="json")
