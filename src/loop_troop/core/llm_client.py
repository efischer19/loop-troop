"""Shared Ollama-backed Instructor client factory."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import instructor
from openai import OpenAI
from pydantic import BaseModel

from loop_troop.core.metrics import LLMMetrics, MetricsCollector
from loop_troop.execution import WorkerTier

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_API_KEY = "ollama"
DEFAULT_MAX_RETRIES = 3
DEFAULT_MODEL_ENV_VARS = {
    WorkerTier.T1: "LOOP_TROOP_T1_MODEL",
    WorkerTier.T2: "LOOP_TROOP_T2_MODEL",
    WorkerTier.T3: "LOOP_TROOP_T3_MODEL",
}
_CREDENTIAL_PATTERNS = (
    ("ghp", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("gho", re.compile(r"\bgho_[A-Za-z0-9]{36}\b")),
    ("github_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
)
_LOGGER = logging.getLogger("loop_troop.llm_client")


@dataclass(frozen=True, slots=True)
class PreparedLLMClient:
    client: Any
    model_name: str


class PromptSanitizationError(ValueError):
    """Raised when a prompt appears to contain credentials."""


class _HealthCheckResponse(BaseModel):
    status: str


class LLMClient:
    def __init__(
        self,
        *,
        ollama_host: str | None = None,
        api_key: str | None = None,
        openai_factory: type[OpenAI] = OpenAI,
        instructor_factory: Any = instructor.from_openai,
        metrics_collector: MetricsCollector | None = None,
    ) -> None:
        self._ollama_host = (ollama_host or os.getenv("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
        self._api_key = api_key or os.getenv("OLLAMA_API_KEY") or DEFAULT_API_KEY
        self._openai_factory = openai_factory
        self._instructor_factory = instructor_factory
        self._metrics_collector = metrics_collector

    def create(
        self,
        *,
        tier: WorkerTier,
        model_override: str | None = None,
        mode: instructor.Mode = instructor.Mode.JSON,
    ) -> PreparedLLMClient:
        model_name = model_override or self._default_model_for_tier(tier)
        client = self._instructor_factory(
            self._openai_factory(api_key=self._api_key, base_url=f"{self._ollama_host}/v1"),
            mode=mode,
        )
        return PreparedLLMClient(client=client, model_name=model_name)

    def complete_structured(
        self,
        *,
        tier: WorkerTier,
        response_model: type[Any],
        messages: list[dict[str, Any]],
        model_override: str | None = None,
        mode: instructor.Mode = instructor.Mode.JSON,
        event_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self._validate_messages(messages)
        prepared = self.create(tier=tier, model_override=model_override, mode=mode)
        kwargs.setdefault("max_retries", DEFAULT_MAX_RETRIES)

        # Set up retry tracking when a MetricsCollector is configured
        get_stats = None
        if self._metrics_collector is not None:
            max_retries_int = (
                kwargs["max_retries"] if isinstance(kwargs["max_retries"], int) else DEFAULT_MAX_RETRIES
            )
            retrying, get_stats = self._metrics_collector.make_retry_tracker(max_retries_int)
            kwargs["max_retries"] = retrying

        call_id = MetricsCollector.new_call_id()
        started_at = time.perf_counter()
        response: Any = None
        error: Exception | None = None
        try:
            response = prepared.client.chat.completions.create(
                response_model=response_model,
                messages=messages,
                model=prepared.model_name,
                **kwargs,
            )
            return response
        except Exception as exc:
            error = exc
            raise
        finally:
            latency_ms = round((time.perf_counter() - started_at) * 1000, 3)
            usage = self._extract_usage(response)
            _LOGGER.info(
                "LLM call completed",
                extra={
                    "structured_data": {
                        "tier": tier.value,
                        "model": prepared.model_name,
                        "latency_ms": latency_ms,
                        "usage": usage,
                        "success": error is None,
                    }
                },
            )
            if self._metrics_collector is not None:
                retries, validation_errors = get_stats() if get_stats is not None else (0, [])
                prompt_tokens: int | None = None
                completion_tokens: int | None = None
                if usage:
                    prompt_tokens = usage.get("prompt_tokens")
                    completion_tokens = usage.get("completion_tokens")
                self._metrics_collector.record(
                    LLMMetrics(
                        call_id=call_id,
                        tier=tier.value,
                        model_name=prepared.model_name,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        # TTFT requires Ollama streaming mode (stream=True).  Non-streaming
                        # JSON-mode calls (the default for structured outputs) cannot measure
                        # time-to-first-token, so this is always null here.  A follow-up can
                        # add a streaming code path that records the monotonic elapsed time
                        # between request start and first chunk arrival.
                        ttft_ms=None,
                        total_latency_ms=latency_ms,
                        instructor_retries=retries,
                        validation_errors=validation_errors,
                        success=error is None,
                        event_id=event_id,
                    )
                )

    def health_check(
        self,
        *,
        tier: WorkerTier,
        model_override: str | None = None,
    ) -> bool:
        try:
            response = self.complete_structured(
                tier=tier,
                model_override=model_override,
                response_model=_HealthCheckResponse,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a health check endpoint for Loop Troop.",
                    },
                    {
                        "role": "user",
                        "content": 'Respond with a JSON object containing exactly one key "status" with value "ok".',
                    },
                ],
                temperature=0,
                max_tokens=32,
            )
        except Exception:
            return False
        return response.status.strip().lower() == "ok"

    @staticmethod
    def _default_model_for_tier(tier: WorkerTier) -> str:
        env_var = DEFAULT_MODEL_ENV_VARS[tier]
        model_name = os.getenv(env_var)
        if not model_name:
            raise ValueError(f"{env_var} must be set when no model_override is provided.")
        return model_name

    @staticmethod
    def _validate_messages(messages: list[dict[str, Any]]) -> None:
        prompt_text = "\n".join(LLMClient._collect_message_strings(messages))
        for pattern_name, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(prompt_text):
                raise PromptSanitizationError(
                    "Prompt rejected because it appears to contain a credential "
                    f"matching the {pattern_name} token format."
                )

    @staticmethod
    def _collect_message_strings(value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value,)
        if isinstance(value, dict):
            dict_strings: list[str] = []
            for nested_value in value.values():
                dict_strings.extend(LLMClient._collect_message_strings(nested_value))
            return tuple(dict_strings)
        if isinstance(value, list):
            list_strings: list[str] = []
            for item in value:
                list_strings.extend(LLMClient._collect_message_strings(item))
            return tuple(list_strings)
        return ()

    @staticmethod
    def _extract_usage(response: Any) -> dict[str, Any] | None:
        if response is None:
            return None
        usage_candidates = (
            getattr(response, "usage", None),
            getattr(getattr(response, "raw_response", None), "usage", None),
            getattr(getattr(response, "_raw_response", None), "usage", None),
        )
        for usage in usage_candidates:
            normalized = LLMClient._normalize_usage(usage)
            if normalized is not None:
                return normalized
        return None

    @staticmethod
    def _normalize_usage(usage: Any) -> dict[str, Any] | None:
        if usage is None:
            return None
        if isinstance(usage, dict):
            return usage
        model_dump = getattr(usage, "model_dump", None)
        if callable(model_dump):
            return model_dump()
        if hasattr(usage, "__dict__"):
            return {
                key: LLMClient._normalize_usage_value(value)
                for key, value in vars(usage).items()
                if not key.startswith("_")
                and not callable(value)
                and LLMClient._normalize_usage_value(value) is not None
            }
        return None

    @staticmethod
    def _normalize_usage_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            normalized = {
                key: LLMClient._normalize_usage_value(nested_value)
                for key, nested_value in value.items()
            }
            return {key: nested_value for key, nested_value in normalized.items() if nested_value is not None}
        if isinstance(value, list):
            return [nested_value for item in value if (nested_value := LLMClient._normalize_usage_value(item)) is not None]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return LLMClient._normalize_usage_value(model_dump())
        if hasattr(value, "__dict__"):
            nested = {
                key: LLMClient._normalize_usage_value(nested_value)
                for key, nested_value in vars(value).items()
                if not key.startswith("_") and not callable(nested_value)
            }
            return {key: nested_value for key, nested_value in nested.items() if nested_value is not None}
        return str(value)
