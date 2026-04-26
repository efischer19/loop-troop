# feat: The Coder Worker — Checklist-Driven Code Generation

## What do you want to build?

Implement the Coder worker that wakes up on issues labeled `loop: ready`, reads the first unchecked item from the Architect's checklist, generates a code patch, and submits a PR. The Coder operates on exactly one checklist item at a time — it never attempts to complete multiple items in a single cycle.

## Acceptance Criteria

- [ ] A `CoderWorker` class that triggers on issues labeled `loop: ready`.
- [ ] Parses the Architect's checklist comment to find the first unchecked (`- [ ]`) item.
- [ ] Hydrates context using: (1) the checklist item description, (2) Repomix context focused on the item's `files_touched`, (3) the full issue body for broader context. Context follows the Strict Context Hierarchy (Epic 2 Ticket 2).
- [ ] Calls the 35B model (or the model specified in the `TargetExecutionProfile`) via Instructor, producing a `CodePatch` schema.
- [ ] Applies the code patch to the target repository (file writes via the `WorkspaceManager`).
- [ ] Creates a new branch (`loop/issue-{number}-item-{index}`) and commits the changes.
- [ ] Delegates to the `InnerLoop` (Epic 4 Ticket 3) for the build/test cycle, which handles both standard and TDD modes based on `ChecklistItem.requires_test`.
- [ ] If tests pass: pushes the branch, opens a PR (via `PRManager`, Epic 4 Ticket 5), checks the checkbox on the original issue, and labels the PR `loop: needs-review`.
- [ ] If tests fail after max retries: marks the checklist item with `- [!]` and labels the issue `loop: needs-help`.
- [ ] Unit tests covering: checklist parsing, patch application, test-pass flow, test-fail retry, max-retry exhaustion.

## Implementation Notes (Optional)

The checklist parser should use regex to find `- [ ]` items in the issue comments. The `CodePatch.files_changed` should include both the file path and the complete new file content (not diffs) — simpler for the LLM to produce and for us to apply. For large files (>500 lines), consider using unified diffs instead to avoid exhausting the context window. Use `git` commands via subprocess for branch/commit/push operations (through `WorkspaceManager`).

The Coder receives the `TargetExecutionProfile` from the Dispatcher, which may specify a particular model (e.g., `qwen2.5-coder:32b`). Pass this to the `LLMClient` as `model_override`.
