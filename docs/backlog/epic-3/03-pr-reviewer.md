# feat: The PR Reviewer — Architecture, CI & Tautological Test Enforcement

## What do you want to build?

Implement the Reviewer worker that evaluates pull requests labeled `loop: needs-review`. The Reviewer enforces architectural consistency (rejecting PRs that change architecture without ADR updates), verifies CI status before reviewing, and checks for tautological tests that provide false confidence.

## Acceptance Criteria

- [ ] A `ReviewerWorker` class that triggers on PRs labeled `loop: needs-review`.
- [ ] Hydrates context using: (1) the PR diff, (2) the linked issue and its checklist, (3) active ADR context, (4) Repomix context focused on changed files. Context follows the Strict Context Hierarchy.
- [ ] **CI Status Gate:** Before reviewing, the Reviewer fetches `check_runs` (CI status) for the PR's HEAD commit via the GitHub API.
  - [ ] If check runs are failing or pending, the Reviewer immediately posts `REQUEST_CHANGES` with a message to fix CI first — it does not review failing PRs.
  - [ ] If check runs passed, the Reviewer proceeds with the full review.
- [ ] Calls the 70B model via Instructor, producing a `ReviewVerdict` schema.
- [ ] The `ReviewVerdict` includes: `verdict` (approve/request_changes), `adr_violations` (list of detected architectural changes without ADR coverage), and `comments` (specific line-level feedback).
- [ ] If `adr_violations` is non-empty, the verdict MUST be `request_changes` (enforced at the schema level).
- [ ] **Tautological Test Detection:** The Reviewer evaluates the diff of test files to check if the 35B Coder wrote fake/tautological tests (e.g., tests that always pass, assert nothing meaningful, or test implementation details rather than behavior). Flags these in the review comments.
- [ ] Posts the review as a GitHub PR review (not a comment) using the GitHub API — with `APPROVE` or `REQUEST_CHANGES` status.
- [ ] On approval, transitions the PR label to `loop: approved`. On rejection, transitions to `loop: changes-requested`.
- [ ] The Reviewer must never execute code, run tests, or spawn Docker containers — it operates purely on diff, context, and CI log analysis.
- [ ] Unit tests with mocked diffs, CI statuses, and LLM responses covering: clean approval, ADR violation detection, CI-failing rejection, tautological test flagging, label transitions.

## Implementation Notes (Optional)

Use GitHub's PR review API (`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`) to post structured reviews. The ADR violation check should compare the PR diff against the ADR context: if the diff modifies interfaces, adds dependencies, or changes data models, the model should flag it as a potential architectural change.

For CI status, use `GET /repos/{owner}/{repo}/commits/{ref}/check-runs`. The Reviewer should look at the `conclusion` field of each check run. If any are `failure` or `null` (still running), exit early.

For tautological test detection, the prompt should include: "Examine the test files in this diff. Flag any test that: (1) asserts `True` or `1 == 1`, (2) has no meaningful assertions, (3) mocks the exact function it's testing, or (4) tests implementation details rather than behavior. These are signs of a model gaming the CI to get a green build."

The Reviewer should never merge PRs with a `[BAKE-OFF]` prefix in the title — these are Ghost Run evaluation PRs (Epic 5) and should be left as drafts for human review.
