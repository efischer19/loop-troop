# feat: PR Creation & Checklist Checkbox Update

## What do you want to build?

Implement the GitHub integration layer that the Coder uses to create pull requests and update the Architect's checklist checkboxes on the original issue. This ensures the issue's checklist always reflects the current state of implementation progress.

## Acceptance Criteria

- [ ] A `PRManager` class with methods: `create_pr()`, `update_pr()`, `check_checkbox()`, `flag_checkbox()`.
- [ ] `create_pr()` creates a GitHub PR with: title referencing the issue and checklist item, body containing the checklist item description and a "Closes #N" reference, and label `loop: needs-review`.
- [ ] `check_checkbox()` edits the original issue comment to change `- [ ]` to `- [x]` for the completed item. Uses the GitHub API to update the comment body.
- [ ] `flag_checkbox()` edits the comment to change `- [ ]` to `- [!]` for items that failed after max retries.
- [ ] Handles the case where the issue comment has been modified by another agent or human since last read (ETag/conflict detection).
- [ ] When all checklist items are checked, labels the issue `loop: done`.
- [ ] Supports Ghost Run mode (Epic 5 Ticket 3): when a `bake_off` flag is set in the `DispatchDecision`, the `PRManager` appends the model name to the branch name (e.g., `loop/issue-42-qwen-test`) and opens the PR as a **Draft** with a `[BAKE-OFF]` title prefix so the Reviewer knows not to merge it.
- [ ] Unit tests covering: PR creation, checkbox update (check and flag), concurrent modification detection, all-items-complete detection, Ghost Run draft PR creation.

## Implementation Notes (Optional)

For checkbox updates, fetch the current comment body via `GET /repos/{owner}/{repo}/issues/comments/{comment_id}`, parse the Markdown, update the specific checkbox, and `PATCH` it back. Use the `If-Match` header with the comment's ETag to detect concurrent modifications — if it fails, re-fetch and retry. The "Closes #N" syntax in the PR body ensures GitHub auto-closes the issue when the PR is merged.
