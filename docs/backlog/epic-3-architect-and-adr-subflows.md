# Epic 3: The 70B Architect & ADR Subflows

> Building the planner prompt that enforces the "Rule of 3" checklist generation, ADR parsing, and the PR Reviewer logic.

This epic builds the Tier 3 (70B) workers: the Architect that decomposes features into implementation checklists, and the Reviewer that enforces architectural consistency via ADR checks.

---

### [Epic 3] Ticket 1: ADR Parser & Architecture Context Loader
**Priority:** High
**Description:**
Build the ADR (Architecture Decision Record) parser that reads and indexes all ADR documents from a target repository's `docs/architecture/` directory. This provides the Architect and Reviewer workers with the current architectural context, ensuring all planning and review decisions are grounded in the project's accepted ADRs.

**Acceptance Criteria:**
* [ ] An `ADRLoader` class that scans a target repo's `docs/architecture/` folder for Markdown ADR files.
* [ ] Parses each ADR into a structured `ADRDocument` model: `id`, `title`, `status` (Accepted/Superseded/Deprecated), `decision_summary`, `full_text`.
* [ ] Filters to only `Accepted` ADRs by default (with option to include all).
* [ ] Produces a combined context string of all active ADRs, suitable for LLM prompt injection.
* [ ] Enforces a configurable token budget for ADR context (default: 4,000 tokens), prioritizing by recency.
* [ ] The ADR directory path must resolve to within the target repository's workspace (not the Loop Troop source tree) — reuse workspace validation from Epic 2 Ticket 3.
* [ ] Unit tests with fixture ADR files covering: parsing, status filtering, token truncation, missing ADR directory (graceful handling).

**Implementation Notes (Tech Lead hints):**
ADR format follows the standard: `# ADR-NNNN: Title`, `## Status`, `## Decision`, etc. Use regex or a simple Markdown heading parser — no need for a full Markdown AST library. The `ADRDocument` model should be a Pydantic schema in `src/core/schemas.py`. Cache parsed ADRs per-commit-SHA alongside the Repomix cache.

---

### [Epic 3] Ticket 2: The Architect Planner — "Rule of 3" Checklist Generation
**Priority:** High
**Description:**
Implement the Architect worker that takes a GitHub issue labeled `loop: needs-planning` and produces a detailed implementation checklist. The checklist enforces the **"Rule of 3"**: every checklist item must touch a maximum of 3 files, require a maximum of 3 logical steps, and contain zero architectural decisions. This ensures each item is small enough for the 35B Coder to execute atomically.

**Acceptance Criteria:**
* [ ] An `ArchitectWorker` class that triggers on issues labeled `loop: needs-planning`.
* [ ] Hydrates context using: (1) the issue body and comments, (2) Repomix context of the target repo, (3) active ADR context from the `ADRLoader`.
* [ ] Calls the 70B model via Instructor, producing an `ArchitectPlan` (from Epic 2 Ticket 1) with `ChecklistItem` entries.
* [ ] Each `ChecklistItem` is validated against Rule of 3 constraints at the Pydantic schema level (max 3 `files_touched`, max 3 `logical_steps`, empty `architectural_decisions`).
* [ ] If validation fails (LLM produces items violating Rule of 3), Instructor's retry mechanism re-prompts the model with the specific validation error.
* [ ] On success, the Architect posts the checklist as a comment on the GitHub issue (Markdown checkbox format: `- [ ] Item description`).
* [ ] After posting, the Architect transitions the issue label from `loop: needs-planning` to `loop: ready`.
* [ ] If the Architect determines the issue requires architectural changes, it instead labels the issue `loop: needs-adr` and posts a comment explaining which ADR needs to be created or updated. It does NOT generate a checklist in this case.
* [ ] Unit tests with mocked LLM responses covering: valid plan generation, Rule of 3 violation retry, ADR-needed detection, label transitions.

**Implementation Notes (Tech Lead hints):**
The Architect prompt should explicitly state: "You are a technical lead. Break this issue into checklist items. Each item MUST touch ≤3 files, require ≤3 logical steps, and make ZERO architectural decisions. If the issue requires an architectural decision, stop and say so." The `ArchitectPlan` schema's validators will catch LLM non-compliance, and Instructor will retry. Include the active ADRs in the prompt so the model knows what decisions are already made.

---

### [Epic 3] Ticket 3: The PR Reviewer — Architecture Enforcement & ADR Validation
**Priority:** High
**Description:**
Implement the Reviewer worker that evaluates pull requests labeled `loop: needs-review`. The Reviewer's primary job is to enforce architectural consistency: it rejects any PR that introduces architectural changes without a corresponding ADR update. It also performs basic code quality checks.

**Acceptance Criteria:**
* [ ] A `ReviewerWorker` class that triggers on PRs labeled `loop: needs-review`.
* [ ] Hydrates context using: (1) the PR diff, (2) the linked issue and its checklist, (3) active ADR context, (4) Repomix context focused on changed files.
* [ ] Calls the 70B model via Instructor, producing a `ReviewVerdict` schema.
* [ ] The `ReviewVerdict` includes: `verdict` (approve/request_changes), `adr_violations` (list of detected architectural changes without ADR coverage), and `comments` (specific line-level feedback).
* [ ] If `adr_violations` is non-empty, the verdict MUST be `request_changes` (enforced at the schema level).
* [ ] Posts the review as a GitHub PR review (not a comment) using the GitHub API — with `APPROVE` or `REQUEST_CHANGES` status.
* [ ] On approval, transitions the PR label to `loop: approved`. On rejection, transitions to `loop: changes-requested`.
* [ ] The Reviewer must never execute code, run tests, or spawn Docker containers — it operates purely on diff and context analysis.
* [ ] Unit tests with mocked diffs and LLM responses covering: clean approval, ADR violation detection, label transitions.

**Implementation Notes (Tech Lead hints):**
Use GitHub's PR review API (`POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`) to post structured reviews. The ADR violation check should compare the PR diff against the ADR context: if the diff modifies interfaces, adds dependencies, or changes data models, the model should flag it as a potential architectural change. The prompt should include: "Reject this PR if it changes architecture without updating the ADRs. List the specific violations."

---

### [Epic 3] Ticket 4: Architect Subflow — Label Lifecycle Integration
**Priority:** Medium
**Description:**
Wire the Architect and Reviewer workers into the Sync Daemon's main loop, establishing the full label lifecycle for Tier 3 operations. This ticket connects the workers built in Tickets 1-3 to the event-processing pipeline from Epic 1.

**Acceptance Criteria:**
* [ ] The Sync Daemon dispatches `loop: needs-planning` events to the `ArchitectWorker`.
* [ ] The Sync Daemon dispatches `loop: needs-review` events to the `ReviewerWorker`.
* [ ] The Dispatcher (Tier 1) correctly identifies PR-opened and PR-updated events and applies the `loop: needs-review` label.
* [ ] End-to-end label lifecycle is documented: `loop: needs-planning` → (Architect) → `loop: ready` → (Coder, Epic 4) → `loop: needs-review` → (Reviewer) → `loop: approved` or `loop: changes-requested`.
* [ ] Failed Architect/Reviewer runs mark the event as `failed` in the shadow log with error details, without crashing the daemon.
* [ ] Integration test simulating the full lifecycle from issue creation through planning and review using mocked GitHub/Ollama endpoints.

**Implementation Notes (Tech Lead hints):**
This is a wiring ticket — minimal new logic, mostly connecting existing components. The key challenge is ensuring the daemon doesn't re-process events that are already in-flight. Use the shadow log's `dispatched` state to prevent duplicate processing. Consider adding a `processing_started_at` timestamp to detect stale dispatches (e.g., a worker crashed mid-execution).
