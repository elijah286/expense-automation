"""Allow running with: python3 -m web"""

from web.app import _kill_existing_on_port  # noqa: F401 — triggers page registration
from nicegui import ui

_kill_existing_on_port(8080)
ui.run(
    title="Expense Automator",
    port=8080,
    reload=False,
    show=True,
    favicon="💰",
)
