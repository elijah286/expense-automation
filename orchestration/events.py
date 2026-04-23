from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AutomationEvent:
    kind: str
    message: str
    phase: str | None = None
    run_id: str | None = None
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "phase": self.phase,
            "run_id": self.run_id,
            "timestamp_ms": int(self.timestamp_ms),
            "data": dict(self.data or {}),
        }


class JsonlAutomationEventSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def emit(
        self,
        kind: str,
        message: str,
        *,
        phase: str | None = None,
        run_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        event = AutomationEvent(
            kind=kind,
            message=message,
            phase=phase,
            run_id=run_id,
            data=dict(data or {}),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
