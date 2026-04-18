# feat: Git Conflict Resolution Subflow

## What do you want to build?

Implement a dedicated subflow for resolving Git merge conflicts that arise when the Coder's branch has diverged from the target branch (typically `main`). This handles the case where another Coder worker (or a human) has pushed changes that conflict with the current PR.

## Acceptance Criteria

- [ ] A `ConflictResolver` class that detects and resolves merge conflicts in a target repository.
- [ ] Triggered when a PR is labeled `loop: merge-conflict` (detected by the Dispatcher or by a failed `git merge` during the Coder's push).
- [ ] Performs `git merge main` (or configured base branch) into the feature branch, detecting conflict markers.
- [ ] Hydrates context using: (1) the conflicting files (both versions), (2) the original checklist item, (3) Repomix context of the relevant files.
- [ ] Calls the 35B model via Instructor to produce a `ConflictResolution` schema: resolved file contents for each conflicting file.
- [ ] Applies the resolution, commits, and re-runs tests in the Docker sandbox (via the `InnerLoop`).
- [ ] If tests pass: pushes and removes the `loop: merge-conflict` label. If tests fail: escalates to `loop: needs-help`.
- [ ] All conflict resolution runs inside the workspace directory — git operations use `subprocess` with `cwd=`, never `os.chdir()`.
- [ ] Unit tests with fixture repos covering: simple conflict resolution, multi-file conflict, resolution that breaks tests (escalation).

## Implementation Notes (Optional)

The conflict resolution prompt should present both versions of each conflicting file clearly: "Version A (ours): [content]. Version B (theirs): [content]. The intended behavior is: [checklist item]. Produce the merged file content." Use `git diff --name-only --diff-filter=U` to find conflicting files. This subflow reuses the `InnerLoop` for the post-resolution test cycle.
