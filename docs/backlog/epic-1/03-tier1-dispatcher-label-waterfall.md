# feat: Tier 1 (8B) Dispatcher — Label Waterfall Router with TargetExecutionProfile

## What do you want to build?

Implement the Tier 1 dispatcher that reads pending events from the shadow log and routes them to the appropriate worker tier using a "Label Waterfall" pattern. The dispatcher uses an 8B model (via Ollama) to classify events and decide on label transitions. Labels on GitHub issues drive the state machine: `loop: needs-planning` → Architect (Tier 3), `loop: feature` → Architect macro-planning (Tier 3), `loop: ready` → Coder (Tier 2), `loop: needs-review` → Reviewer (Tier 3).

Critically, the dispatcher does not just route to a "tier" — it outputs a `TargetExecutionProfile` that includes the **specific model name** based on the task context, allowing downstream workers to use the optimal model for the job rather than a static default.

## Acceptance Criteria

- [ ] A `Dispatcher` class that reads `pending` events from the `ShadowLog`.
- [ ] Uses Instructor + Pydantic to call the 8B model (via Ollama) for event classification, producing a structured `DispatchDecision` containing a `TargetExecutionProfile` (target tier, **specific model name**, label action, reasoning).
- [ ] The `TargetExecutionProfile` specifies the exact Ollama model name (e.g., `qwen2.5-coder:32b`) rather than just a tier enum. The profile is passed to downstream workers so the `LLMClient` uses it as a `model_override`.
- [ ] Applies label changes to GitHub issues via the `GitHubClient`.
- [ ] Follows a strict label state machine: only valid transitions are allowed (e.g., cannot jump from `loop: needs-planning` directly to `loop: done`).
- [ ] Supports `loop: feature` events — routes them to the Architect for macro-planning (FeaturePlan generation per ADR-0002).
- [ ] Parses `(Depends on: #X)` strings from parent issue DAG comments to enforce dependency ordering — a sub-issue is only dispatched when all its dependencies are resolved.
- [ ] Marks events as `dispatched` in the shadow log after successful label application.
- [ ] Handles Ollama inference failures gracefully (retry with backoff, mark event as `failed` after 3 attempts).
- [ ] The dispatcher MUST NOT execute any code or spawn Docker containers — it only reads, classifies, and labels.
- [ ] Unit tests with mocked Ollama responses covering: valid routing with model selection, invalid label transitions (rejected), Ollama timeout handling, dependency blocking, `TargetExecutionProfile` propagation.

## Implementation Notes (Optional)

The label waterfall is the core state machine. Define the valid transitions as a simple dict/enum, not in the LLM prompt. The LLM's job is to *classify the event type and select the best model*, not to decide the state machine transitions — those are deterministic. Use `instructor.from_openai()` pointed at the Ollama-compatible endpoint.

The `TargetExecutionProfile` should include: `tier` (T1/T2/T3), `model_name` (str, e.g., `qwen2.5-coder:32b`), `reasoning` (str). The `LLMClient` factory (Epic 2 Ticket 4) must accept this profile's `model_name` as a `model_override` parameter, falling back to the `.env` default only if the Dispatcher doesn't specify one.

For dependency parsing, use a fast regex to extract `(Depends on: #X, #Y)` from DAG comments. The Dispatcher should check the status of referenced issues via the GitHub API before dispatching.
