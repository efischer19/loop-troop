"""Shared Ollama-backed Instructor client factory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import instructor
from openai import OpenAI

from loop_troop.execution import WorkerTier

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_API_KEY = "ollama"
DEFAULT_MODEL_ENV_VARS = {
    WorkerTier.T1: "LOOP_TROOP_T1_MODEL",
    WorkerTier.T2: "LOOP_TROOP_T2_MODEL",
    WorkerTier.T3: "LOOP_TROOP_T3_MODEL",
}


@dataclass(frozen=True, slots=True)
class PreparedLLMClient:
    client: Any
    model_name: str


class LLMClient:
    def __init__(
        self,
        *,
        ollama_host: str | None = None,
        api_key: str | None = None,
        openai_factory: type[OpenAI] = OpenAI,
        instructor_factory: Any = instructor.from_openai,
    ) -> None:
        self._ollama_host = (ollama_host or os.getenv("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).rstrip("/")
        self._api_key = api_key or os.getenv("OLLAMA_API_KEY") or DEFAULT_API_KEY
        self._openai_factory = openai_factory
        self._instructor_factory = instructor_factory

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
        messages: list[dict[str, str]],
        model_override: str | None = None,
        mode: instructor.Mode = instructor.Mode.JSON,
        **kwargs: Any,
    ) -> Any:
        prepared = self.create(tier=tier, model_override=model_override, mode=mode)
        return prepared.client.chat.completions.create(
            response_model=response_model,
            messages=messages,
            model=prepared.model_name,
            **kwargs,
        )

    @staticmethod
    def _default_model_for_tier(tier: WorkerTier) -> str:
        env_var = DEFAULT_MODEL_ENV_VARS[tier]
        model_name = os.getenv(env_var)
        if not model_name:
            raise ValueError(f"{env_var} must be set when no model_override is provided.")
        return model_name
