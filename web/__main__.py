"""Allow running with: python3 -m web"""

import shutil
from pathlib import Path

from dotenv import load_dotenv

_project_root = Path(__file__).resolve().parent.parent
_env_file = _project_root / ".env"
_env_example = _project_root / ".env.example"

if not _env_file.exists() and _env_example.exists():
    shutil.copy(_env_example, _env_file)

load_dotenv(_env_file)

from web.app import _kill_existing_on_port  # noqa: E402 — triggers page registration
from nicegui import ui  # noqa: E402

_kill_existing_on_port(8080)
ui.run(
    title="Expense Automator",
    port=8080,
    reload=False,
    show=True,
    favicon="💰",
)
