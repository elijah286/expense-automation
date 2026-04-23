from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff_s: float = 0.4
    max_backoff_s: float = 4.0
    jitter_s: float = 0.15

    def backoff_for_attempt(self, attempt: int) -> float:
        exp = max(0, attempt - 1)
        base = min(self.max_backoff_s, self.initial_backoff_s * (2**exp))
        jitter = random.uniform(0.0, self.jitter_s) if self.jitter_s > 0 else 0.0
        return base + jitter


def is_transient_error(exc: BaseException) -> bool:
    name = exc.__class__.__name__.lower()
    text = f"{exc}".lower()
    return any(
        marker in name or marker in text
        for marker in (
            "timeout",
            "targetclosed",
            "connection",
            "detached",
            "context closed",
            "net::",
            "econnreset",
            "temporarily unavailable",
            "closed",
        )
    )


def execute_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    transient_predicate: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    p = policy or RetryPolicy()
    pred = transient_predicate or is_transient_error
    last_exc: BaseException | None = None
    for attempt in range(1, max(1, p.max_attempts) + 1):
        try:
            return fn()
        except Exception as exc:  # pragma: no cover - defensive flow wrapper
            last_exc = exc
            if attempt >= p.max_attempts or not pred(exc):
                raise
            if on_retry:
                on_retry(attempt, exc)
            time.sleep(p.backoff_for_attempt(attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("execute_with_retry reached an unexpected terminal state")
