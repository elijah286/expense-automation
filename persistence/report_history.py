"""Write-once submission snapshots with independent copies of receipt files."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from persistence.atomic_json import atomic_write_json


class ReportHistoryStore:
    def __init__(self, app_dir: Path):
        self.root = app_dir / "submission_history"

    def get(self, submission_id: str) -> dict[str, Any]:
        try:
            if str(uuid.UUID(submission_id)) != submission_id:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Submission not found.") from exc
        path = self.root / submission_id / "report.json"
        if not path.is_file():
            raise ValueError("Submission not found.")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("id") != submission_id:
            raise ValueError(f"Invalid history snapshot: {submission_id}")
        return data

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        reports = [
            self.get(path.parent.name)
            for path in self.root.glob("*/report.json")
            if not path.parent.name.startswith(".")
        ]
        return sorted(reports, key=lambda report: report["submitted_at"], reverse=True)

    def create(self, snapshot: dict[str, Any], *, allow_missing: bool = False) -> str:
        submission_id = str(uuid.uuid4())
        self.root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=self.root))
        try:
            # Detach nested objects so callers cannot mutate the saved snapshot.
            data = json.loads(json.dumps(snapshot))
            data["id"] = submission_id
            data["version"] = 1
            receipts_dir = staging / "receipts"
            receipts_dir.mkdir()
            for index, receipt in enumerate(data["receipts"]):
                source = Path(receipt["source_file"]).expanduser()
                receipt["id"] = str(index)
                receipt["filename"] = source.name
                receipt["archive_file"] = ""
                if not source.is_file():
                    if not allow_missing:
                        raise FileNotFoundError(
                            f"Cannot preserve receipt '{source.name}': file not found. "
                            "Restore the file or correct its match before marking submitted."
                        )
                    receipt["missing"] = True
                    continue
                relative = Path("receipts") / f"{index}{source.suffix.lower()}"
                shutil.copyfile(source, staging / relative)
                receipt["archive_file"] = relative.as_posix()
                receipt["missing"] = False
            atomic_write_json(staging / "report.json", data)
            staging.rename(self.root / submission_id)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return submission_id

    def receipt_path(self, submission_id: str, receipt_id: str) -> Path:
        report = self.get(submission_id)
        receipt = next((r for r in report["receipts"] if r["id"] == receipt_id), None)
        if not receipt or not receipt["archive_file"]:
            raise FileNotFoundError("This receipt was unavailable when the report was archived.")
        folder = (self.root / submission_id).resolve()
        path = (folder / receipt["archive_file"]).resolve()
        if not path.is_relative_to(folder / "receipts") or not path.is_file():
            raise FileNotFoundError("The archived receipt is missing or invalid.")
        return path
