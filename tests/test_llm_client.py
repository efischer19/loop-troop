import logging

import pytest
from pydantic import BaseModel

from loop_troop.core.llm_client import LLMClient, PromptSanitizationError
from loop_troop.execution import WorkerTier


class DummyResponse(BaseModel):
    ok: bool
    usage: dict[str, int] | None = None


class DummyHealthResponse(BaseModel):
    status: str


def test_complete_structured_returns_response_and_logs_usage(caplog, monkeypatch) -> None:
    captured: dict[str, object] = {}
    openai_calls: list[dict[str, str]] = []

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return DummyResponse(
                ok=True,
                usage={"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
            )

    class FakeChat:
        completions = FakeCompletions()

    class FakeInstructorClient:
        chat = FakeChat()

    monkeypatch.setenv("LOOP_TROOP_T1_MODEL", "llama3.2:latest")
    caplog.set_level(logging.INFO, logger="loop_troop.llm_client")
    llm_client = LLMClient(
        ollama_host="http://ollama.test",
        openai_factory=lambda **kwargs: openai_calls.append(kwargs) or object(),
        instructor_factory=lambda *_args, **_kwargs: FakeInstructorClient(),
    )

    response = llm_client.complete_structured(
        tier=WorkerTier.T1,
        response_model=DummyResponse,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response == DummyResponse(
        ok=True,
        usage={"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    )
    assert openai_calls == [{"api_key": "ollama", "base_url": "http://ollama.test/v1"}]
    assert captured["model"] == "llama3.2:latest"
    assert captured["max_retries"] == 3
    assert caplog.records[-1].structured_data["usage"]["total_tokens"] == 16
    assert caplog.records[-1].structured_data["success"] is True
    assert caplog.records[-1].structured_data["latency_ms"] >= 0


def test_create_prefers_model_override_over_tier_default(monkeypatch) -> None:
    monkeypatch.setenv("LOOP_TROOP_T2_MODEL", "mistral:default")
    llm_client = LLMClient(
        openai_factory=lambda **_: object(),
        instructor_factory=lambda *_args, **_kwargs: object(),
    )

    default_client = llm_client.create(tier=WorkerTier.T2)
    override_client = llm_client.create(
        tier=WorkerTier.T2,
        model_override="qwen2.5-coder:32b",
    )

    assert default_client.model_name == "mistral:default"
    assert override_client.model_name == "qwen2.5-coder:32b"


def test_complete_structured_configures_instructor_validation_retries(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return DummyResponse(ok=True)

    class FakeChat:
        completions = FakeCompletions()

    class FakeInstructorClient:
        chat = FakeChat()

    monkeypatch.setenv("LOOP_TROOP_T3_MODEL", "deepseek-r1:14b")
    llm_client = LLMClient(
        openai_factory=lambda **_: object(),
        instructor_factory=lambda *_args, **_kwargs: FakeInstructorClient(),
    )

    llm_client.complete_structured(
        tier=WorkerTier.T3,
        response_model=DummyResponse,
        messages=[{"role": "user", "content": "retry me"}],
    )

    assert captured["max_retries"] == 3


def test_health_check_returns_true_when_model_responds(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return DummyHealthResponse(status="ok")

    class FakeChat:
        completions = FakeCompletions()

    class FakeInstructorClient:
        chat = FakeChat()

    monkeypatch.setenv("LOOP_TROOP_T2_MODEL", "phi4-mini")
    llm_client = LLMClient(
        openai_factory=lambda **_: object(),
        instructor_factory=lambda *_args, **_kwargs: FakeInstructorClient(),
    )

    assert llm_client.health_check(
        tier=WorkerTier.T2,
        model_override="qwen2.5-coder:32b",
    )
    assert captured["model"] == "qwen2.5-coder:32b"
    assert captured["max_tokens"] == 32


def test_complete_structured_rejects_prompts_with_github_credentials(monkeypatch) -> None:
    called = False

    class FakeCompletions:
        def create(self, **_kwargs):
            nonlocal called
            called = True
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

    with pytest.raises(PromptSanitizationError, match="matching the ghp token format"):
        llm_client.complete_structured(
            tier=WorkerTier.T1,
            response_model=DummyResponse,
            messages=[
                {
                    "role": "user",
                    "content": "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
                }
            ],
        )

    assert called is False
