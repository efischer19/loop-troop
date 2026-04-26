# feat: Repomix Context Hydration Pipeline with Strict Context Hierarchy

## What do you want to build?

Build the context-assembly pipeline that uses Repomix to generate a token-optimized representation of the target repository. This context is fed to every LLM call to ground the model in the current state of the codebase. The pipeline must produce a context string that fits within the model's context window, prioritizing files relevant to the current task.

Critically, the pipeline must enforce a **Strict Context Hierarchy** to prevent truncation of essential context. If the Issue Body or the Architect's Checklist gets truncated, the 35B model will start hallucinating steps that don't exist or missing critical constraints.

## Acceptance Criteria

- [ ] A `ContextHydrator` class that wraps the Repomix CLI (`npx repomix`).
- [ ] Accepts a target repository path and an optional file-focus list (from the `ChecklistItem.files_touched` field) to prioritize relevant files.
- [ ] Enforces a **Strict Context Hierarchy** — when assembling the full prompt context, tokens are allocated in this exact priority order:
  1. **GitHub Issue / Checklist** — Must fit completely, or throw a hard error. Never truncated.
  2. **ADR Context** — Must fit completely, or throw a hard error. Never truncated.
  3. **Repomix Codebase Context** — Can be truncated with a `[TRUNCATED]` marker. This is the only layer that absorbs budget pressure.
- [ ] Produces a single string output suitable for LLM prompt injection.
- [ ] Enforces a configurable max-token budget (default: 16,000 tokens). The budget is split: issue/checklist and ADR context consume what they need, and the remainder goes to Repomix.
- [ ] Raises `ContextBudgetExceededError` if issue/checklist + ADR context alone exceeds the total budget (i.e., there's no room left even for truncated codebase context).
- [ ] Caches Repomix output per-commit-SHA to avoid redundant regeneration.
- [ ] The target repository path must be validated to ensure it is NOT inside the Loop Troop source tree (enforcing Control Plane / Data Plane separation per ADR-0001).
- [ ] Raises `WorkspaceViolationError` if a hydration request targets a path inside the Loop Troop installation directory.
- [ ] Unit tests with a fixture repository covering: full hydration, focused hydration, token truncation (only codebase layer), context hierarchy enforcement (issue too large → error), cache hit/miss, workspace violation.

## Implementation Notes (Optional)

Repomix is a Node.js tool — invoke it via `subprocess.run(["npx", "repomix", ...])` from the native Control Plane. Parse its stdout/file output. The cache key should be `(repo_path, commit_sha, focus_files_hash)`. Store the cache in the SQLite shadow log or a simple file-based cache under `~/.loop-troop/cache/`. The workspace violation check is a critical security boundary — use `pathlib.Path.resolve()` to prevent symlink traversal attacks.

The Strict Context Hierarchy is essential because the 8B and 35B models have different context window sizes, but in all cases the "instructions" (issue, checklist, ADRs) must be preserved verbatim. Only the "reference material" (codebase) can be compressed. The `ContextHydrator` should accept pre-computed token counts for issue and ADR content so it can calculate the remaining budget for Repomix before invoking it.
