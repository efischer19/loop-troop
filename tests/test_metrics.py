"""Unit tests for LLM metrics collection and persistence."""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import BaseModel

from loop_troop.core.llm_client import LLMClient
from loop_troop.core.metrics import LLMMetrics, MetricsCollector
from loop_troop.execution import WorkerTier
from loop_troop.shadow_log import ShadowLog


class DummyResponse(BaseModel):
    ok: bool
    usage: dict[str, int] | None = None


# ---------------------------------------------------------------------------
# LLMMetrics dataclass
# ---------------------------------------------------------------------------


def test_llm_metrics_fields_accessible() -> None:
    metrics = LLMMetrics(
        call_id="test-call-1",
        tier="t1",
        model_name="llama3.2:latest",
        prompt_tokens=10,
        completion_tokens=5,
        ttft_ms=None,
        total_latency_ms=123.4,
        instructor_retries=0,
        validation_errors=[],
        success=True,
        event_id="evt-42",
    )

    assert metrics.call_id == "test-call-1"
    assert metrics.tier == "t1"
    assert metrics.model_name == "llama3.2:latest"
    assert metrics.prompt_tokens == 10
    assert metrics.completion_tokens == 5
    assert metrics.ttft_ms is None
    assert metrics.total_latency_ms == 123.4
    assert metrics.instructor_retries == 0
    assert metrics.validation_errors == []
    assert metrics.success is True
    assert metrics.event_id == "evt-42"


def test_llm_metrics_event_id_optional() -> None:
    metrics = LLMMetrics(
        call_id="test-call-2",
        tier="t2",
        model_name="qwen:7b",
        prompt_tokens=None,
        completion_tokens=None,
        ttft_ms=None,
        total_latency_ms=50.0,
        instructor_retries=0,
        validation_errors=[],
        success=False,
    )
    assert metrics.event_id is None


# ---------------------------------------------------------------------------
# MetricsCollector
# ---------------------------------------------------------------------------


def test_metrics_collector_record_writes_to_shadow_log(tmp_path) -> None:
    with ShadowLog(tmp_path / "shadow.db") as shadow_log:
        collector = MetricsCollector(shadow_log=shadow_log)
        metrics = LLMMetrics(
            call_id="call-abc",
            tier="t1",
            model_name="llama3.2:latest",
            prompt_tokens=11,
            completion_tokens=7,
            ttft_ms=None,
            total_latency_ms=200.0,
            instructor_retries=0,
            validation_errors=[],
            success=True,
            event_id="evt-99",
        )

        collector.record(metrics)

        row = shadow_log._connection.execute(
            "SELECT * FROM llm_metrics WHERE call_id = ?", ("call-abc",)
        ).fetchone()

    assert row is not None
    assert row["call_id"] == "call-abc"
    assert row["event_id"] == "evt-99"
    assert row["tier"] == "t1"
    assert row["model_name"] == "llama3.2:latest"
    assert row["prompt_tokens"] == 11
    assert row["completion_tokens"] == 7
    assert row["ttft_ms"] is None
    assert row["total_latency_ms"] == 200.0
    assert row["instructor_retries"] == 0
    assert json.loads(row["validation_errors"]) == []
    assert row["success"] == 1


def test_metrics_collector_record_without_shadow_log_is_noop() -> None:
    collector = MetricsCollector(shadow_log=None)
    metrics = LLMMetrics(
        call_id="call-no-db",
        tier="t1",
        model_name="llama3.2:latest",
        prompt_tokens=None,
        completion_tokens=None,
        ttft_ms=None,
        total_latency_ms=10.0,
        instructor_retries=0,
        validation_errors=[],
        success=True,
    )
    # Should not raise
    collector.record(metrics)


def test_metrics_collector_make_retry_tracker_no_retries() -> None:
    retrying, get_stats = MetricsCollector.make_retry_tracker(3)

    def _always_succeeds():
        return "ok"

    retrying(_always_succeeds)
    retries, errors = get_stats()

    assert retries == 0
    assert errors == []


def test_metrics_collector_make_retry_tracker_counts_retries() -> None:
    retrying, get_stats = MetricsCollector.make_retry_tracker(3)

    attempt_counter = [0]

    def _fails_twice():
        attempt_counter[0] += 1
        if attempt_counter[0] < 3:
            raise ValueError(f"Attempt {attempt_counter[0]} failed")
        return "ok"

    retrying(_fails_twice)
    retries, errors = get_stats()

    assert retries == 2
    assert len(errors) == 2
    assert "Attempt 1 failed" in errors[0]
    assert "Attempt 2 failed" in errors[1]


def test_metrics_collector_make_retry_tracker_all_fail() -> None:
    retrying, get_stats = MetricsCollector.make_retry_tracker(2)

    def _always_fails():
        raise RuntimeError("always fails")

    import tenacity

    with pytest.raises(RuntimeError, match="always fails"):
        retrying(_always_fails)

    retries, errors = get_stats()
    # 2 total attempts with stop_after_attempt(2) → 1 retry (2nd call was the retry)
    assert retries == 1
    assert len(errors) == 2


# ---------------------------------------------------------------------------
# LLMClient integration
# ---------------------------------------------------------------------------


def test_llm_client_captures_metrics_on_success(tmp_path) -> None:
    with ShadowLog(tmp_path / "shadow.db") as shadow_log:
        collector = MetricsCollector(shadow_log=shadow_log)

        class FakeCompletions:
            def create(self, **kwargs):
                return DummyResponse(
                    ok=True,
                    usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                )

        class FakeChat:
            completions = FakeCompletions()

        class FakeInstructorClient:
            chat = FakeChat()

        llm_client = LLMClient(
            openai_factory=lambda **_: object(),
            instructor_factory=lambda *_args, **_kwargs: FakeInstructorClient(),
            metrics_collector=collector,
        )
        llm_client.complete_structured(
            tier=WorkerTier.T1,
            response_model=DummyResponse,
            messages=[{"role": "user", "content": "hello"}],
            model_override="test-model",
            event_id="evt-123",
        )

        rows = shadow_log._connection.execute("SELECT * FROM llm_metrics").fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row["tier"] == "T1"
    assert row["model_name"] == "test-model"
    assert row["prompt_tokens"] == 10
    assert row["completion_tokens"] == 4
    assert row["success"] == 1
    assert row["instructor_retries"] == 0
    assert row["event_id"] == "evt-123"
    assert row["ttft_ms"] is None


def test_llm_client_captures_metrics_on_failure(tmp_path) -> None:
    with ShadowLog(tmp_path / "shadow.db") as shadow_log:
        collector = MetricsCollector(shadow_log=shadow_log)

        class FakeCompletions:
            def create(self, **kwargs):
                raise RuntimeError("model unavailable")

        class FakeChat:
            completions = FakeCompletions()

        class FakeInstructorClient:
            chat = FakeChat()

        llm_client = LLMClient(
            openai_factory=lambda **_: object(),
            instructor_factory=lambda *_args, **_kwargs: FakeInstructorClient(),
            metrics_collector=collector,
        )

        with pytest.raises(RuntimeError, match="model unavailable"):
            llm_client.complete_structured(
                tier=WorkerTier.T2,
                response_model=DummyResponse,
                messages=[{"role": "user", "content": "fail"}],
                model_override="fail-model",
                event_id="evt-456",
            )

        rows = shadow_log._connection.execute("SELECT * FROM llm_metrics").fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row["tier"] == "T2"
    assert row["model_name"] == "fail-model"
    assert row["success"] == 0
    assert row["event_id"] == "evt-456"


def test_llm_client_captures_retry_count(tmp_path) -> None:
    """Verify MetricsCollector tracks retries when instructor retries via tenacity."""
    import tenacity

    with ShadowLog(tmp_path / "shadow.db") as shadow_log:
        collector = MetricsCollector(shadow_log=shadow_log)

        attempt_counter = [0]

        class FakeCompletions:
            def create(self, **kwargs):
                # Simulate instructor's tenacity-based retry behavior:
                # instructor extracts max_retries and uses it as a Retrying object.
                max_retries = kwargs.get("max_retries", 3)
                if isinstance(max_retries, tenacity.BaseRetrying):
                    return max_retries(self._attempt)
                return self._attempt()

            def _attempt(self):
                attempt_counter[0] += 1
                if attempt_counter[0] < 3:
                    raise ValueError(f"validation error attempt {attempt_counter[0]}")
                return DummyResponse(ok=True)

        class FakeChat:
            completions = FakeCompletions()

        class FakeInstructorClient:
            chat = FakeChat()

        llm_client = LLMClient(
            openai_factory=lambda **_: object(),
            instructor_factory=lambda *_args, **_kwargs: FakeInstructorClient(),
            metrics_collector=collector,
        )
        llm_client.complete_structured(
            tier=WorkerTier.T3,
            response_model=DummyResponse,
            messages=[{"role": "user", "content": "retry me"}],
            model_override="retry-model",
        )

        rows = shadow_log._connection.execute("SELECT * FROM llm_metrics").fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row["instructor_retries"] == 2
    assert row["success"] == 1
    errors = json.loads(row["validation_errors"])
    assert len(errors) == 2


def test_llm_client_without_metrics_collector_behaves_as_before(monkeypatch) -> None:
    """Ensure LLMClient without metrics_collector works unchanged."""
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return DummyResponse(ok=True)

    class FakeChat:
        completions = FakeCompletions()

    class FakeInstructorClient:
        chat = FakeChat()

    monkeypatch.setenv("LOOP_TROOP_T1_MODEL", "llama3.2:latest")
    llm_client = LLMClient(
        openai_factory=lambda **_: object(),
        instructor_factory=lambda *_args, **_kwargs: FakeInstructorClient(),
    )

    response = llm_client.complete_structured(
        tier=WorkerTier.T1,
        response_model=DummyResponse,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response == DummyResponse(ok=True)
    assert captured["max_retries"] == 3


def test_shadow_log_write_llm_metrics_persists_to_db(tmp_path) -> None:
    with ShadowLog(tmp_path / "shadow.db") as shadow_log:
        metrics = LLMMetrics(
            call_id="persist-test",
            tier="t2",
            model_name="qwen:7b",
            prompt_tokens=20,
            completion_tokens=8,
            ttft_ms=95.5,
            total_latency_ms=300.0,
            instructor_retries=1,
            validation_errors=["schema mismatch"],
            success=True,
            event_id=None,
        )
        shadow_log.write_llm_metrics(metrics)

        row = shadow_log._connection.execute(
            "SELECT * FROM llm_metrics WHERE call_id = ?", ("persist-test",)
        ).fetchone()

    assert row is not None
    assert row["tier"] == "t2"
    assert row["model_name"] == "qwen:7b"
    assert row["prompt_tokens"] == 20
    assert row["completion_tokens"] == 8
    assert row["ttft_ms"] == 95.5
    assert row["total_latency_ms"] == 300.0
    assert row["instructor_retries"] == 1
    assert json.loads(row["validation_errors"]) == ["schema mismatch"]
    assert row["success"] == 1
    assert row["event_id"] is None


def test_shadow_log_write_llm_metrics_deduplicates_by_call_id(tmp_path) -> None:
    with ShadowLog(tmp_path / "shadow.db") as shadow_log:
        metrics = LLMMetrics(
            call_id="dup-call",
            tier="t1",
            model_name="llama3.2",
            prompt_tokens=5,
            completion_tokens=3,
            ttft_ms=None,
            total_latency_ms=100.0,
            instructor_retries=0,
            validation_errors=[],
            success=True,
        )
        shadow_log.write_llm_metrics(metrics)
        shadow_log.write_llm_metrics(metrics)  # second write should be ignored

        count = shadow_log._connection.execute(
            "SELECT COUNT(*) FROM llm_metrics WHERE call_id = ?", ("dup-call",)
        ).fetchone()[0]

    assert count == 1


def test_llm_metrics_new_call_id_generates_unique_ids() -> None:
    ids = {MetricsCollector.new_call_id() for _ in range(50)}
    assert len(ids) == 50
