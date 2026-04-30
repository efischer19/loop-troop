"""LLM call metrics collection and persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import tenacity

if TYPE_CHECKING:
    from loop_troop.shadow_log import ShadowLog


@dataclass(frozen=True, slots=True)
class LLMMetrics:
    """Performance metrics captured for a single LLM call."""

    call_id: str
    tier: str
    model_name: str
    prompt_tokens: int | None
    completion_tokens: int | None
    ttft_ms: float | None
    total_latency_ms: float
    instructor_retries: int
    validation_errors: list[str]
    success: bool
    event_id: str | None = None


class MetricsCollector:
    """Captures LLM performance metrics and persists them to the shadow log."""

    def __init__(self, shadow_log: ShadowLog | None = None) -> None:
        self._shadow_log = shadow_log

    def record(self, metrics: LLMMetrics) -> None:
        """Persist metrics to the shadow log."""
        if self._shadow_log is not None:
            self._shadow_log.write_llm_metrics(metrics)

    @staticmethod
    def make_retry_tracker(
        max_retries: int,
    ) -> tuple[tenacity.Retrying, Callable[[], tuple[int, list[str]]]]:
        """Create a tenacity Retrying object that tracks retry count and validation errors.

        Returns a tuple of (retrying, get_stats) where get_stats() returns
        (instructor_retries, validation_errors).
        """
        total_calls_box: list[int] = [0]
        validation_errors: list[str] = []

        def _before_attempt(retry_state: tenacity.RetryCallState) -> None:
            total_calls_box[0] = retry_state.attempt_number

        def _after_retry(retry_state: tenacity.RetryCallState) -> None:
            if retry_state.outcome is not None and retry_state.outcome.failed:
                exc = retry_state.outcome.exception()
                if exc is not None:
                    validation_errors.append(str(exc))

        retrying = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(max_retries),
            before=_before_attempt,
            after=_after_retry,
            reraise=True,
        )

        def get_stats() -> tuple[int, list[str]]:
            return max(0, total_calls_box[0] - 1), list(validation_errors)

        return retrying, get_stats

    @staticmethod
    def new_call_id() -> str:
        """Generate a unique identifier for an LLM call."""
        return str(uuid.uuid4())
