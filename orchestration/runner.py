from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .events import JsonlAutomationEventSink
from .state_machine import WorkflowPhase, WorkflowState


@dataclass
class PhaseHandlers:
    ingestion: Callable[[], Any]
    scraping: Callable[[], Any]
    matching: Callable[[], Any]
    classification: Callable[[], Any]
    review: Callable[[], Any]
    submission: Callable[[], Any]


def run_workflow(run_id: str, handlers: PhaseHandlers, event_sink: JsonlAutomationEventSink) -> WorkflowState:
    state = WorkflowState(run_id=run_id, current_phase=WorkflowPhase.DOCUMENT_INGESTION, progress_percent=0)
    ordered: list[tuple[WorkflowPhase, Callable[[], Any], int]] = [
        (WorkflowPhase.DOCUMENT_INGESTION, handlers.ingestion, 10),
        (WorkflowPhase.ORACLE_SCRAPING, handlers.scraping, 30),
        (WorkflowPhase.MATCHING, handlers.matching, 50),
        (WorkflowPhase.CLASSIFICATION, handlers.classification, 70),
        (WorkflowPhase.USER_REVIEW, handlers.review, 85),
        (WorkflowPhase.SUBMISSION, handlers.submission, 100),
    ]
    for phase, fn, progress in ordered:
        state = state.with_update(current_phase=phase, progress_percent=progress)
        event_sink.emit("phase.start", f"Starting {phase.value}.", phase=phase.value, run_id=run_id)
        fn()
        event_sink.emit("phase.complete", f"Completed {phase.value}.", phase=phase.value, run_id=run_id)
    return state.with_update(current_phase=WorkflowPhase.COMPLETED, progress_percent=100)
