# feat: CLI Subflow — The Ghost Run Command

## What do you want to build?

Build a CLI command that enables developers to replay a specific issue against a different model configuration without going through the full GitHub polling pipeline. This is the primary tool for comparing model performance on real tasks — a developer can run the same issue through multiple models and compare the resulting PRs side by side.

**Command:** `loop-troop replay --issue 42 --model qwen2.5-coder:32b`

## Acceptance Criteria

- [ ] A `loop-troop replay` CLI subcommand that accepts `--issue` (GitHub issue number) and `--model` (Ollama model name) parameters.
- [ ] **Bypass logic:** The command bypasses the GitHub polling client. It manually injects a synthetic `loop: ready` event directly into the local SQLite `raw_events` table.
- [ ] The synthetic event includes a special `ghost_run` flag in the `DispatchDecision` payload that triggers modified behavior in downstream workers.
- [ ] **Branch naming:** The `WorkspaceManager` appends the model name to the Git branch (e.g., `loop/issue-42-qwen-test`) to avoid collisions with the primary implementation branch.
- [ ] **Draft PR:** The `PRManager` opens the PR as a **Draft** with a `[BAKE-OFF]` prefix in the title (e.g., `[BAKE-OFF] feat: Issue 42 — qwen2.5-coder:32b`).
- [ ] The 70B Reviewer knows not to merge or approve `[BAKE-OFF]` PRs — they are left as drafts for human comparison.
- [ ] The Ghost Run uses the same `InnerLoop`, `DockerSandbox`, and `WorkspaceManager` as the standard flow — no special code paths for execution, only for event injection and PR metadata.
- [ ] All Ghost Run metrics are captured in the `llm_metrics` table (Ticket 1) with the model name, enabling direct comparison queries.
- [ ] Unit tests covering: synthetic event injection, branch name construction, draft PR creation with `[BAKE-OFF]` prefix, metrics capture with correct model name.

## Implementation Notes (Optional)

The Ghost Run is architecturally simple: it's just a CLI that writes a row to SQLite and sets a flag. The standard daemon picks it up on the next tick and processes it normally. The only differences are: (1) the branch name includes the model, (2) the PR is a draft, and (3) the Reviewer skips it.

For the CLI, use `click` or `argparse`. The `--model` parameter should validate against Ollama's `GET /api/tags` endpoint to ensure the model is actually available before injecting the event. Consider adding a `--dry-run` flag that shows what would be injected without writing to SQLite.
