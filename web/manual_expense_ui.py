"""Cash and mileage draft screens using the shared web shell."""

from __future__ import annotations

from datetime import date
from nicegui import ui

from portal_expense_types import get_expense_type_options
from web.manual_expenses import (
    ExpenseKind, ManualExpense, ManualExpenseStore, MILEAGE_RATE_SOURCE, mileage_estimate,
)
from web.service import ExpenseService


def manual_expense_screen(
    kind: ExpenseKind, report_id: str, svc: ExpenseService, store: ManualExpenseStore,
) -> None:
    title = "Cash" if kind == "cash" else "Mileage"
    groups = {g.id: g.name for g in svc.get_expense_report_groups()}
    report_options = {"": "Unassigned", **groups}
    selected_report = report_id if report_id in groups else ""

    def show_error(exc: Exception) -> None:
        ui.notify(str(exc), type="negative", timeout=10000)

    def edit(entry: ManualExpense | None = None) -> None:
        with ui.dialog() as dialog, ui.card().classes("w-full").style(
            "max-width:620px;border-radius:16px;padding:24px"
        ):
            ui.label(f"{'Edit' if entry else 'Add'} {title.lower()} transaction").classes(
                "text-xl font-semibold"
            )
            day = ui.input("Date", value=entry.date if entry else date.today().isoformat()).props(
                'outlined dense type=date'
            ).classes("w-full")
            report = ui.select(
                report_options, label="Expense report",
                value=entry.report_id if entry and entry.report_id in groups
                else (scope.value if scope.value in groups else ""),
            ).props("outlined dense").classes("w-full")
            if entry and entry.report_id and entry.report_id not in groups:
                ui.label("The original report is unavailable. Choose a report or save as unassigned.").classes(
                    "text-sm text-amber-700"
                )
                report.value = ""
            description = ui.textarea(
                "Description / business purpose", value=entry.description if entry else "",
            ).props("outlined autogrow").classes("w-full")
            merchant = category = currency = amount = distance = exchange = None
            if kind == "cash":
                merchant = ui.input("Merchant / payee", value=entry.merchant if entry else "").props(
                    "outlined dense"
                ).classes("w-full")
                options = get_expense_type_options()
                if entry and entry.expense_type not in options:
                    options = [*options, entry.expense_type]
                category = ui.select(
                    options, label="Expense type", value=entry.expense_type if entry else None,
                    with_input=True,
                ).props("outlined dense").classes("w-full")
                with ui.row().classes("w-full"):
                    amount = ui.input("Amount", value=entry.amount if entry else "").props(
                        'outlined dense inputmode=decimal'
                    ).classes("flex-1")
                    currency = ui.input("Currency", value=entry.currency if entry else "USD").props(
                        "outlined dense maxlength=3"
                    ).style("width:110px")
                exchange = ui.input(
                    "Exchange rate (USD per 1 receipt currency)",
                    value=entry.exchange_rate if entry else "",
                ).props("outlined dense inputmode=decimal").classes("w-full")
                ui.label(
                    "Required for non-USD cash expenses. Use your actual conversion rate; "
                    "the app never guesses a rate. Leave blank for USD."
                ).classes("text-sm text-slate-500")
            else:
                distance = ui.input("Miles", value=entry.miles if entry else "").props(
                    "outlined dense inputmode=decimal"
                ).classes("w-full")
                estimate = ui.label().classes("font-semibold")

                def update_estimate() -> None:
                    try:
                        rate, total = mileage_estimate(day.value or "", distance.value or "")
                        estimate.set_text(f"USD {rate} / mile x {distance.value} miles = USD {total}")
                    except ValueError as exc:
                        estimate.set_text(str(exc))

                day.on_value_change(update_estimate)
                distance.on_value_change(update_estimate)
                update_estimate()
                ui.label(
                    "US IRS business-rate estimate, selected by trip date (2024-2026). "
                    "Your employer / Oracle rate is not yet verified."
                ).classes("text-sm text-amber-700")
                ui.link("Mileage rate source", MILEAGE_RATE_SOURCE, new_tab=True)

            def save() -> None:
                try:
                    current_ids = {g.id for g in svc.get_expense_report_groups()}
                    if report.value and report.value not in current_ids:
                        raise ValueError("This report is no longer available. Reload the page.")
                    store.save(
                        kind=kind, transaction_date=day.value or "",
                        description=description.value or "",
                        report_id=report.value or "",
                        merchant=merchant.value or "" if merchant else "",
                        expense_type=category.value or "" if category else "",
                        amount=amount.value or "" if amount else "",
                        currency=currency.value or "" if currency else "USD",
                        miles=distance.value or "" if distance else "",
                        entry_id=entry.id if entry else "",
                        exchange_rate=exchange.value or "" if exchange else "",
                    )
                except (ValueError, OSError) as exc:
                    show_error(exc)
                    return
                dialog.close()
                listing.refresh()
                ui.notify("Draft saved locally; not submitted to Oracle.", type="positive")

            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                ui.button("Save draft", on_click=save).props("unelevated no-caps")
        dialog.open()

    def delete(entry: ManualExpense) -> None:
        with ui.dialog() as dialog, ui.card():
            ui.label("Delete this manual draft?").classes("text-lg font-semibold")
            ui.label(f"{entry.date}: {entry.description}")

            def confirm() -> None:
                try:
                    store.delete(entry.id)
                except (ValueError, OSError) as exc:
                    show_error(exc)
                    return
                dialog.close()
                listing.refresh()
                ui.notify("Draft deleted", type="positive")

            with ui.row().classes("justify-end w-full"):
                ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
                ui.button("Delete", on_click=confirm).props("color=negative no-caps")
        dialog.open()

    with ui.element("div").classes("page-container"):
        ui.label(f"{title} transactions").classes("section-title")
        ui.label(
            "Optional manual drafts. These are saved locally and can be assigned to a report. "
            "Choose whether to include cash and mileage when submitting that report. "
            "They are excluded by default."
        ).classes("section-subtitle")
        with ui.row().classes("items-center w-full justify-between mb-4"):
            scope = ui.select(
                {"__all__": "All reports", **report_options}, label="Filter by report",
                value=selected_report or "__all__", on_change=lambda: listing.refresh(),
            ).props("outlined dense").style("min-width:240px")
            ui.button(f"Add {title.lower()} transaction", icon="add", on_click=lambda: edit()).props(
                "unelevated no-caps"
            )

        @ui.refreshable
        def listing() -> None:
            try:
                entries = [
                    row for row in store.list() if row.kind == kind
                    and (scope.value == "__all__" or row.report_id == scope.value)
                ]
            except (ValueError, OSError) as exc:
                ui.label(str(exc)).classes("text-red-600")
                return
            if not entries:
                ui.label(f"No {title.lower()} transactions yet. This step is optional.").classes(
                    "text-slate-500 py-8"
                )
            for entry in sorted(entries, key=lambda row: row.date, reverse=True):
                with ui.card().classes("w-full mb-3"):
                    with ui.row().classes("items-start justify-between w-full"):
                        with ui.column().classes("gap-1").style("min-width:0;flex:1"):
                            ui.label(entry.description).classes("font-semibold break-words")
                            ui.label(f"{entry.date} | {entry.currency} {entry.amount}")
                            if kind == "cash":
                                ui.label(f"{entry.merchant} | {entry.expense_type}").classes("text-sm")
                            else:
                                ui.label(f"{entry.miles} miles x USD {entry.rate} / mile (IRS estimate)").classes(
                                    "text-sm"
                                )
                            ui.label(
                                f"Report: {groups.get(entry.report_id, 'Unavailable report' if entry.report_id else 'Unassigned')}"
                                " | Local draft - not submitted"
                            ).classes("text-xs text-slate-500")
                        with ui.row():
                            ui.button("Edit", icon="edit", on_click=lambda _, row=entry: edit(row)).props(
                                "flat no-caps"
                            )
                            ui.button(
                                "Delete", icon="delete_outline", on_click=lambda _, row=entry: delete(row),
                            ).props("flat color=negative no-caps")

        listing()


def report_manual_drafts(
    store: ManualExpenseStore, report_id: str,
) -> list[ManualExpense] | None:
    """Render a pending-only summary without changing credit-card readiness."""
    try:
        entries = [row for row in store.list() if row.report_id == report_id]
    except (ValueError, OSError) as exc:
        ui.label(f"Manual drafts unavailable: {exc}").classes("text-red-600 mb-4")
        return None
    if entries:
        with ui.card().classes("w-full mb-4"):
            ui.label(f"{len(entries)} manual draft(s) - optional Oracle entry").classes(
                "font-semibold text-amber-700"
            )
            ui.label(
                "Submitting below lets you opt into cash and/or mileage entry at Step 4. "
                "Both are excluded by default. Local drafts are retained; review the Oracle report "
                "and automation warnings before clicking Submit in Oracle."
            ).classes("text-sm")
            for entry in entries:
                ui.label(
                    f"{entry.kind.title()} | {entry.date} | {entry.description} | "
                    f"{entry.currency} {entry.amount}"
                ).classes("text-sm")
            with ui.row():
                ui.link("Review cash", f"/cash?report={report_id}")
                ui.link("Review mileage", f"/mileage?report={report_id}")
    return entries
