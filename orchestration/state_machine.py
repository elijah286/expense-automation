from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class WorkflowPhase(str, Enum):
    DOCUMENT_INGESTION = "DocumentIngestion"
    ORACLE_SCRAPING = "OracleScraping"
    MATCHING = "Matching"
    CLASSIFICATION = "Classification"
    USER_REVIEW = "UserReview"
    SUBMISSION = "Submission"
    RECOVERY = "Recovery"
    COMPLETED = "Completed"


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class WorkflowState:
    run_id: str
    current_phase: WorkflowPhase
    progress_percent: int = 0
    attention_required: bool = False
    attention_reason: str = ""
    next_action_label: str = ""
    checkpoint_id: str = ""
    updated_at: str = ""

    def with_update(self, **kwargs: Any) -> "WorkflowState":
        return replace(self, updated_at=_iso_now(), **kwargs)

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "current_phase": self.current_phase.value,
            "progress_percent": int(self.progress_percent),
            "attention_required": bool(self.attention_required),
            "attention_reason": self.attention_reason,
            "next_action_label": self.next_action_label,
            "checkpoint_id": self.checkpoint_id,
            "updated_at": self.updated_at or _iso_now(),
        }
