# feat: Observability — TTFT & Instructor Retry Rate Tracking

## What do you want to build?

Instrument all LLM calls across the system to track key performance metrics: Time-To-First-Token (TTFT), total inference latency, token usage (prompt + completion), and Instructor retry rates. These metrics are stored in the SQLite shadow log alongside the events that triggered them, enabling post-hoc analysis and model comparison.

## Acceptance Criteria

- [ ] An `LLMMetrics` dataclass/Pydantic model: `call_id`, `tier`, `model_name`, `prompt_tokens`, `completion_tokens`, `ttft_ms` (nullable — depends on Ollama streaming support), `total_latency_ms`, `instructor_retries`, `validation_errors` (list[str]), `success` (bool).
- [ ] A `MetricsCollector` that wraps Instructor calls and automatically captures the above metrics.
- [ ] Metrics are written to a dedicated `llm_metrics` table in the SQLite shadow log.
- [ ] Schema migration extends the existing shadow log DB (from Epic 1 Ticket 2) without breaking existing tables.
- [ ] `MetricsCollector` is integrated into the `LLMClient` factory (Epic 2 Ticket 4) so all LLM calls are automatically instrumented — workers don't need to opt in.
- [ ] Metrics include the `event_id` from the shadow log, linking each LLM call back to the GitHub event that triggered it.
- [ ] TTFT is measured by timing the first chunk from Ollama's streaming API (if available), otherwise recorded as `null`.
- [ ] Unit tests covering: metric capture on success, metric capture on retry, metric persistence to SQLite.

## Implementation Notes (Optional)

Wrap the Instructor client's `create()` method with a timing decorator. For TTFT, use Ollama's streaming mode (`stream=True`) and measure `time.monotonic()` between request start and first chunk arrival. Instructor retry count can be tracked by hooking into `instructor`'s retry callback. Store metrics in a separate table to avoid polluting the event log, but use the same SQLite database for simplicity.
