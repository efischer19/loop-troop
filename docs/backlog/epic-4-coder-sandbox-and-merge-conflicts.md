# Epic 4: The 35B Coder, Sandboxed Inner Loop & Merge Conflicts

> Building the Coder worker that reads the checklist, writes code, safely triggers `docker run ... make test` without exposing the host OS, and the dedicated Git conflict resolution subflow.

This epic builds the Tier 2 (35B) workers and the security-critical Docker sandbox execution layer. The Coder is the only worker that generates and executes code, and it does so exclusively inside network-isolated containers.

---

### [Epic 4] Ticket 1: Ephemeral Docker Sandbox — Network-Isolated Container Execution
**Priority:** High
**Description:**
Build the Docker sandbox execution layer that runs LLM-generated code inside ephemeral, network-isolated containers. This is the most security-critical component in the system. The sandbox must enforce the air-gap security model (ADR-0001): the LLM-generated code runs in a container with no network access, no Docker socket access, and no host credential access.

**Acceptance Criteria:**
* [ ] A `DockerSandbox` class that executes commands inside ephemeral Docker containers.
* [ ] Containers are launched with: `--network=none` (no network access), `--rm` (auto-cleanup), resource limits (`--memory`, `--cpus`), and a configurable timeout.
* [ ] The target repository directory is bind-mounted as the sole writable volume (`-v /path/to/target:/workspace`).
* [ ] **CRITICAL: The Docker socket (`/var/run/docker.sock`) is NEVER mounted into the container.** This must be enforced by code (explicit check before container launch) and validated by tests.
* [ ] **CRITICAL: No host environment variables are passed into the container.** The `--env` flag is not used; the container runs with a clean environment.
* [ ] Container image is built from the target repo's `Dockerfile` (assumed to contain all build dependencies).
* [ ] Returns structured execution results: `exit_code`, `stdout`, `stderr`, `duration_seconds`, `timed_out` (bool).
* [ ] Enforces a maximum execution timeout (default: 300 seconds), killing the container if exceeded.
* [ ] Unit tests covering: successful execution, timeout enforcement, exit code propagation, **explicit test that Docker socket mount is rejected**, **explicit test that env vars are not leaked**.

**Implementation Notes (Tech Lead hints):**
Use `subprocess.run(["docker", "run", ...])` from the native Control Plane — NOT the Docker Python SDK (which would require the socket). Build the `docker run` command string programmatically and include an assertion/check that scans the final command for `/var/run/docker.sock` before execution — defense in depth. Log the full `docker run` command (minus any secrets) to the shadow log for auditability. The resource limits should be configurable per-project via the target repo's template config.

---

### [Epic 4] Ticket 2: The Coder Worker — Checklist-Driven Code Generation
**Priority:** High
**Description:**
Implement the Coder worker that wakes up on issues labeled `loop: ready`, reads the first unchecked item from the Architect's checklist, generates a code patch, and submits a PR. The Coder operates on exactly one checklist item at a time — it never attempts to complete multiple items in a single cycle.

**Acceptance Criteria:**
* [ ] A `CoderWorker` class that triggers on issues labeled `loop: ready`.
* [ ] Parses the Architect's checklist comment to find the first unchecked (`- [ ]`) item.
* [ ] Hydrates context using: (1) the checklist item description, (2) Repomix context focused on the item's `files_touched`, (3) the full issue body for broader context.
* [ ] Calls the 35B model via Instructor, producing a `CodePatch` schema.
* [ ] Applies the code patch to the target repository (file writes via the `WorkspaceManager`).
* [ ] Creates a new branch (`loop/issue-{number}-item-{index}`) and commits the changes.
* [ ] Triggers the Docker sandbox (Ticket 1) to run `make test` (or the configured test command) against the patched code.
* [ ] If tests pass: pushes the branch, opens a PR, checks the checkbox on the original issue comment, and labels the PR `loop: needs-review`.
* [ ] If tests fail: feeds the test output back to the 35B model for a self-correction cycle (max 3 attempts). If still failing after retries, marks the checklist item with `- [!]` and labels the issue `loop: needs-help`.
* [ ] Unit tests covering: checklist parsing, patch application, test-pass flow, test-fail retry, max-retry exhaustion.

**Implementation Notes (Tech Lead hints):**
The checklist parser should use regex to find `- [ ]` items in the issue comments. The self-correction cycle is key: on test failure, inject the `stderr`/`stdout` from the Docker sandbox into the next LLM prompt along with the original code patch. The `CodePatch.files_changed` should include both the file path and the complete new file content (not diffs) — simpler for the LLM to produce and for us to apply. For large files (>500 lines), consider using unified diffs instead to avoid exhausting the context window. Use `git` commands via subprocess for branch/commit/push operations (through `WorkspaceManager`).

---

### [Epic 4] Ticket 3: Inner Loop — Build/Test Cycle in Docker Sandbox
**Priority:** High
**Description:**
Implement the inner loop that the Coder uses to iteratively build and test code inside the Docker sandbox. This loop ties together the code generation (Ticket 2) and sandbox execution (Ticket 1) into a tight feedback cycle: generate code → run tests → analyze failures → regenerate → repeat.

**Acceptance Criteria:**
* [ ] An `InnerLoop` class that orchestrates the generate-test-fix cycle.
* [ ] Accepts a `ChecklistItem`, a `CoderWorker` reference, and a `DockerSandbox` reference.
* [ ] Executes up to `max_iterations` (default: 3) of: (1) generate/regenerate code, (2) write files, (3) run `make test` in sandbox, (4) analyze results.
* [ ] On each failed iteration, constructs a "fix prompt" containing: the original checklist item, the current code, the test output (stdout + stderr, truncated to fit context window), and the specific error messages.
* [ ] Tracks iteration metrics: `attempts`, `first_attempt_passed` (bool), `final_status` (pass/fail), `total_sandbox_time_seconds`.
* [ ] Returns a structured `InnerLoopResult` with the final code state and metrics.
* [ ] All sandbox executions obey ADR-0001: no network, no socket mount, no host env vars.
* [ ] Unit tests covering: first-attempt pass, iterative fix, max-iteration exhaustion, sandbox timeout during inner loop.

**Implementation Notes (Tech Lead hints):**
The fix prompt is the most important prompt in the system. It should be structured as: "Your previous code failed. Here is the test output: [stderr]. Here is your code: [files]. Fix the code to pass the tests. Only modify the files listed in the checklist item." Truncate test output to ~2,000 tokens to leave room for code context. The `InnerLoopResult` metrics feed into Epic 5's observability system.

---

### [Epic 4] Ticket 4: Git Conflict Resolution Subflow
**Priority:** Medium
**Description:**
Implement a dedicated subflow for resolving Git merge conflicts that arise when the Coder's branch has diverged from the target branch (typically `main`). This handles the case where another Coder worker (or a human) has pushed changes that conflict with the current PR.

**Acceptance Criteria:**
* [ ] A `ConflictResolver` class that detects and resolves merge conflicts in a target repository.
* [ ] Triggered when a PR is labeled `loop: merge-conflict` (detected by the Dispatcher or by a failed `git merge` during the Coder's push).
* [ ] Performs `git merge main` (or configured base branch) into the feature branch, detecting conflict markers.
* [ ] Hydrates context using: (1) the conflicting files (both versions), (2) the original checklist item, (3) Repomix context of the relevant files.
* [ ] Calls the 35B model via Instructor to produce a `ConflictResolution` schema: resolved file contents for each conflicting file.
* [ ] Applies the resolution, commits, and re-runs tests in the Docker sandbox (via the `InnerLoop`).
* [ ] If tests pass: pushes and removes the `loop: merge-conflict` label. If tests fail: escalates to `loop: needs-help`.
* [ ] All conflict resolution runs inside the workspace directory — git operations use `subprocess` with `cwd=`, never `os.chdir()`.
* [ ] Unit tests with fixture repos covering: simple conflict resolution, multi-file conflict, resolution that breaks tests (escalation).

**Implementation Notes (Tech Lead hints):**
The conflict resolution prompt should present both versions of each conflicting file clearly: "Version A (ours): [content]. Version B (theirs): [content]. The intended behavior is: [checklist item]. Produce the merged file content." Use `git diff --name-only --diff-filter=U` to find conflicting files. This subflow reuses the `InnerLoop` for the post-resolution test cycle.

---

### [Epic 4] Ticket 5: PR Creation & Checklist Checkbox Update
**Priority:** Medium
**Description:**
Implement the GitHub integration layer that the Coder uses to create pull requests and update the Architect's checklist checkboxes on the original issue. This ensures the issue's checklist always reflects the current state of implementation progress.

**Acceptance Criteria:**
* [ ] A `PRManager` class with methods: `create_pr()`, `update_pr()`, `check_checkbox()`, `flag_checkbox()`.
* [ ] `create_pr()` creates a GitHub PR with: title referencing the issue and checklist item, body containing the checklist item description and a "Closes #N" reference, and label `loop: needs-review`.
* [ ] `check_checkbox()` edits the original issue comment to change `- [ ]` to `- [x]` for the completed item. Uses the GitHub API to update the comment body.
* [ ] `flag_checkbox()` edits the comment to change `- [ ]` to `- [!]` for items that failed after max retries.
* [ ] Handles the case where the issue comment has been modified by another agent or human since last read (ETag/conflict detection).
* [ ] When all checklist items are checked, labels the issue `loop: done`.
* [ ] Unit tests covering: PR creation, checkbox update (check and flag), concurrent modification detection, all-items-complete detection.

**Implementation Notes (Tech Lead hints):**
For checkbox updates, fetch the current comment body via `GET /repos/{owner}/{repo}/issues/comments/{comment_id}`, parse the Markdown, update the specific checkbox, and `PATCH` it back. Use the `If-Match` header with the comment's ETag to detect concurrent modifications — if it fails, re-fetch and retry. The "Closes #N" syntax in the PR body ensures GitHub auto-closes the issue when the PR is merged.
