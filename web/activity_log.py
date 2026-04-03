"""Thread-safe activity log for real-time UI terminal updates."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LogEntry:
    timestamp: float
    category: str
    message: str
    task_name: str = ""


class ActivityLog:
    """Global singleton — background threads write, UI timers read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[LogEntry] = []
        self._active_task: str = ""
        self._progress: float = 0.0
        self._progress_label: str = ""
        self._processing_items: set[str] = set()

    def emit(self, category: str, message: str, task_name: str = "") -> None:
        with self._lock:
            self._entries.append(LogEntry(
                timestamp=time.time(),
                category=category,
                message=message,
                task_name=task_name or self._active_task,
            ))

    def set_active_task(self, name: str) -> None:
        with self._lock:
            self._active_task = name
            self._progress = 0.0
            self._progress_label = ""

    def clear_active_task(self) -> None:
        with self._lock:
            self._active_task = ""
            self._progress = 0.0
            self._progress_label = ""
            self._processing_items.clear()

    def set_progress(self, progress: float, label: str = "") -> None:
        with self._lock:
            self._progress = min(max(progress, 0.0), 1.0)
            self._progress_label = label

    def mark_item_processing(self, item_id: str) -> None:
        with self._lock:
            self._processing_items.add(item_id)

    def mark_item_done(self, item_id: str) -> None:
        with self._lock:
            self._processing_items.discard(item_id)

    def get_entries_since(self, after_index: int = 0) -> tuple[list[LogEntry], int]:
        with self._lock:
            return list(self._entries[after_index:]), len(self._entries)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_task": self._active_task,
                "progress": self._progress,
                "progress_label": self._progress_label,
                "total_entries": len(self._entries),
                "processing_items": set(self._processing_items),
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


activity_log = ActivityLog()
