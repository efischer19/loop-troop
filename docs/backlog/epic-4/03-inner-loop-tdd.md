# feat: Inner Loop — Build/Test Cycle with Red-Green TDD Pipeline & 8B Error Extraction

## What do you want to build?

Implement the inner loop that the Coder uses to iteratively build and test code inside the Docker sandbox. This loop ties together code generation and sandbox execution into a tight feedback cycle. When the Architect specifies `requires_test: true` on a checklist item, the Inner Loop implements a strict **Red-Green Agentic TDD pipeline** — the LLM cannot write the test and the implementation in the same step, preventing tautological (false-positive) tests.

Additionally, when the Docker sandbox produces a raw error dump, an **8B model subflow** extracts the relevant error lines before passing them to the 35B model. Smaller models have larger effective context windows, making them ideal for this extraction task.

## Acceptance Criteria

- [ ] An `InnerLoop` class that orchestrates the generate-test-fix cycle.
- [ ] Accepts a `ChecklistItem`, a `CoderWorker` reference, and a `DockerSandbox` reference.
- [ ] Checks `ChecklistItem.requires_test` to determine the execution mode:
  - **Standard mode** (`requires_test: false`): Generate code → run `make test` in sandbox → analyze results. Up to `max_iterations` (default: 3) retries.
  - **TDD mode** (`requires_test: true`): Two-phase Red-Green pipeline (see below).
- [ ] **TDD Phase 1 (Red):** Generate the test file using `ChecklistItem.test_instructions` → Run Docker Sandbox → **Assert the exit code is non-zero** (test must fail against the current code). If the test passes (tautological test), throw an error back to the LLM: "This test passes without implementation — write a stricter test that validates the actual behavior described in the test instructions."
- [ ] **TDD Phase 2 (Green):** Generate the implementation code → Run Docker Sandbox → **Assert the exit code is zero** (test must pass). If it fails, enter the standard retry loop (up to `max_iterations`).
- [ ] **8B Error Extraction Subflow:** When the Docker sandbox returns a failing test output, pass the raw `stderr`/`stdout` to an 8B model to extract the relevant error lines, stack trace, and failure summary. The extracted summary (not the raw dump) is what gets injected into the 35B model's fix prompt. This saves context window budget on the larger model.
- [ ] On each failed iteration, constructs a "fix prompt" containing: the original checklist item, the current code, the **extracted error summary** (from the 8B subflow), and the specific failure analysis.
- [ ] Tracks iteration metrics: `attempts`, `first_attempt_passed` (bool), `final_status` (pass/fail), `total_sandbox_time_seconds`, `tdd_mode` (bool), `tautological_test_rejections` (int).
- [ ] Returns a structured `InnerLoopResult` with the final code state and metrics.
- [ ] All sandbox executions obey ADR-0001: no network, no socket mount, no host env vars.
- [ ] Unit tests covering: first-attempt pass, iterative fix, max-iteration exhaustion, sandbox timeout during inner loop, **TDD Red phase — tautological test rejection** (Phase 1 accidentally passes), **TDD Green phase — standard retry**, 8B error extraction subflow.

## Implementation Notes (Optional)

The TDD flow is the most important quality gate in the system. The Red phase catches a common LLM failure mode: writing tests that assert `True` or mock the function being tested, producing a green CI build that tests nothing. By requiring Phase 1 to fail, we guarantee the test actually exercises unwritten code.

The fix prompt is critical. Structure it as: "Your previous code failed. Here is the error summary: [8B-extracted summary]. Here is your code: [files]. Fix the code to pass the tests. Only modify the files listed in the checklist item." Truncate the raw test output to ~500 tokens for the 8B extraction prompt, and use the 8B's extracted summary (~200 tokens) in the 35B fix prompt.

The 8B error extraction subflow: Call the 8B model via Instructor with a simple schema: `ErrorSummary(relevant_lines: list[str], error_type: str, root_cause: str, suggested_fix_area: str)`. This is a fast, cheap call that dramatically improves the quality of the 35B's fix attempts by removing noise from raw Docker output.

The `InnerLoopResult` metrics feed into Epic 5's observability system. Track tautological test rejections separately — a high rate may indicate the Architect's `test_instructions` are too vague.
