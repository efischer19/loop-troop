# feat: The Architect Planner — Rule of 3, Macro-Planning & Agentic TDD

## What do you want to build?

Implement the Architect worker that handles both micro-planning and macro-planning:

- **Micro-planning** (`loop: needs-planning`): Takes a GitHub issue and produces a detailed implementation checklist enforcing the "Rule of 3" — every checklist item touches max 3 files, requires max 3 logical steps, and contains zero architectural decisions. The Architect also dictates testing requirements via `requires_test` and `test_instructions` fields.
- **Macro-planning** (`loop: feature`): Takes a feature-level issue and decomposes it into discrete sub-issues with explicit dependency tracking, creating them via the GitHub API and posting a Markdown DAG on the parent issue. Supports unlimited recursive decomposition per ADR-0002.

## Acceptance Criteria

- [ ] An `ArchitectWorker` class that triggers on issues labeled `loop: needs-planning` (micro) AND `loop: feature` (macro).
- [ ] **Micro-planning (loop: needs-planning):**
  - [ ] Hydrates context using: (1) the issue body and comments, (2) Repomix context of the target repo, (3) active ADR context from the `ADRLoader`. Context follows the Strict Context Hierarchy (Epic 2 Ticket 2).
  - [ ] Calls the 70B model via Instructor, producing an `ArchitectPlan` with `ChecklistItem` entries.
  - [ ] Each `ChecklistItem` is validated against Rule of 3 constraints at the Pydantic schema level.
  - [ ] For each checklist item, the Architect evaluates whether it requires a test. Items involving business logic, API routing, or data transformation must set `requires_test: true` with specific `test_instructions`. Trivial config or setup items set `requires_test: false`.
  - [ ] If validation fails (LLM produces items violating Rule of 3), Instructor's retry mechanism re-prompts the model with the specific validation error.
  - [ ] On success, posts the checklist as a comment on the GitHub issue (Markdown checkbox format) and transitions the label from `loop: needs-planning` to `loop: ready`.
  - [ ] If the issue requires architectural changes, labels it `loop: needs-adr` instead and posts a comment explaining which ADR needs to be created or updated.
- [ ] **Macro-planning (loop: feature):**
  - [ ] Calls the 70B model via Instructor, producing a `FeaturePlan` with `SubIssue` entries.
  - [ ] For each `SubIssue` in the `FeaturePlan`, creates a new GitHub issue via the API, applying the `loop: needs-planning` label. Sub-issues that are themselves too large may receive `loop: feature` for further recursive decomposition.
  - [ ] The final sub-issue must always be an integration/feature test that runs via standard CI, verifying the feature works end-to-end.
  - [ ] Posts a master tracking comment (Markdown DAG) on the original feature issue: `- [ ] #{issue_id}: {title} (Depends on: #{dependency_ids})`.
  - [ ] After posting the DAG, transitions the label from `loop: feature` to `loop: epic-tracking`.
  - [ ] There are no inherent restrictions on nesting depth — a sub-issue can itself be a `loop: feature` per ADR-0002.
- [ ] Unit tests with mocked LLM responses covering: valid micro-plan, Rule of 3 violation retry, ADR-needed detection, valid macro-plan with DAG, `requires_test` field population, recursive decomposition scenario.

## Implementation Notes (Optional)

The micro-planning prompt should explicitly state: "You are a technical lead. Break this issue into checklist items. Each item MUST touch ≤3 files, require ≤3 logical steps, and make ZERO architectural decisions. If the issue requires an architectural decision, stop and say so. For each item, determine if it requires a test — set `requires_test: true` for business logic, API routing, and data transformations; `requires_test: false` for trivial config changes."

The macro-planning prompt should state: "Decompose this feature into discrete sub-issues with explicit dependencies. The final sub-issue MUST be an integration or feature test. Each sub-issue should be independently implementable once its dependencies are resolved."

For the DAG comment, the Dispatcher (Epic 1 Ticket 3) uses a fast regex to parse `(Depends on: #X, #Y)` strings. Keep the format consistent and parseable. When all sub-issues are closed, the parent tracking issue can be auto-closed or labeled `loop: done`.
