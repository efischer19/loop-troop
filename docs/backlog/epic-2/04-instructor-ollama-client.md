# feat: Instructor Client Configuration for Ollama with Model Override

## What do you want to build?

Set up the shared Instructor client configuration that all workers use to interact with local Ollama models. This includes model routing based on `TargetExecutionProfile` from the Dispatcher, retry policies, and structured output enforcement.

The `LLMClient` factory must accept a `model_override` parameter at runtime, falling back to the `.env` default only if the Dispatcher doesn't specify one. This enables the Dispatcher's `TargetExecutionProfile` to dynamically select the best model for each task.

## Acceptance Criteria

- [ ] An `LLMClient` factory that returns pre-configured Instructor clients for each cognitive tier.
- [ ] **The factory accepts a `model_override` parameter** (from `TargetExecutionProfile.model_name`). When provided, it overrides the default model for that tier. When absent, falls back to env var defaults (`LOOP_TROOP_T1_MODEL`, `LOOP_TROOP_T2_MODEL`, `LOOP_TROOP_T3_MODEL`).
- [ ] Ollama base URL is configurable (`OLLAMA_HOST`, default: `http://localhost:11434`).
- [ ] Each client is configured with Instructor retry logic: max 3 retries on validation failure, with the validation error fed back to the model for self-correction.
- [ ] Includes a health-check method that verifies the target model is loaded and responsive.
- [ ] Logs every LLM call's token usage and latency (for Epic 5 observability).
- [ ] Must never pass credentials (GitHub PAT, etc.) in LLM prompts — validated by a prompt-sanitization check that scans for known credential patterns (e.g., `ghp_`, `gho_`, `github_pat_`).
- [ ] Unit tests with mocked Ollama responses covering: successful structured output, model override behavior, retry on validation error, model health check, prompt sanitization rejection.

## Implementation Notes (Optional)

Use `instructor.from_openai()` with `openai.OpenAI(base_url="http://localhost:11434/v1")` — Ollama exposes an OpenAI-compatible API. The retry logic is Instructor's killer feature: when a Pydantic validation fails, Instructor automatically re-prompts the model with the error message. Log the `usage` field from the response for TTFT tracking. The prompt-sanitization check should scan for known credential patterns in the assembled prompt string.

The `model_override` flow: Dispatcher produces `TargetExecutionProfile(model_name="qwen2.5-coder:32b")` → Worker receives it → Worker calls `LLMClient.create(tier=T2, model_override="qwen2.5-coder:32b")` → Client uses the override instead of `LOOP_TROOP_T2_MODEL` env var.
