# feat: Pydantic Schemas for Event Routing, Worker Contracts & Epic Planning

## What do you want to build?

Define the canonical Pydantic v2 schemas that form the data contracts between the Sync Daemon, the Dispatcher, and all worker tiers. These schemas are the system's lingua franca — every LLM call uses Instructor to produce one of these models, and every inter-component message is validated against them.

This includes schemas for both micro-planning (single-issue checklists) and macro-planning (feature-to-sub-issue decomposition with dependency tracking), as well as support for Agentic TDD where the Architect can dictate testing requirements at the checklist level.

## Acceptance Criteria

- [ ] `TargetExecutionProfile` schema: `tier` (enum: T1/T2/T3), `model_name` (str, e.g., `qwen2.5-coder:32b`), `reasoning` (str). Used by the Dispatcher to specify the exact model for downstream workers.
- [ ] `DispatchDecision` schema: `event_id`, `event_type` (enum), `target_profile` (TargetExecutionProfile), `label_action` (add/remove label + label name), `reasoning` (str).
- [ ] `ChecklistItem` schema: `description` (str), `files_touched` (list[str], max 3), `logical_steps` (list[str], max 3), `architectural_decisions` (list — must be empty, validated), `requires_test` (bool), `test_instructions` (str, optional — for the Architect to specify what edge cases the test must cover).
- [ ] `ArchitectPlan` schema: `issue_number`, `checklist_items` (list of `ChecklistItem`), `adr_references` (list of referenced ADR filenames), `verification_strategy` (str — how the overall issue should be integration-tested).
- [ ] `SubIssue` schema: `title` (str), `description` (str), `depends_on` (list[int] — 1-based indices of other sub-issues in the same plan that must be completed first).
- [ ] `FeaturePlan` schema: `epic_issue_number` (int), `sub_issues` (list of `SubIssue`), `adr_references` (list of referenced ADR filenames). The final sub-issue should always be an integration/feature test.
- [ ] `CodePatch` schema: `issue_number`, `checklist_item_index`, `branch_name`, `files_changed` (list of `FileChange`), `test_command` (str), `commit_message` (str).
- [ ] `ReviewVerdict` schema: `pr_number`, `verdict` (enum: approve/request_changes/reject), `adr_violations` (list[str]), `comments` (list of `ReviewComment`).
- [ ] All schemas use Pydantic v2 `model_validator` or `field_validator` to enforce domain invariants (e.g., `ChecklistItem.files_touched` has max length 3, `ChecklistItem.architectural_decisions` must be empty).
- [ ] `FeaturePlan` validates that `depends_on` indices do not go out of bounds of the `sub_issues` list.
- [ ] Schemas are importable from a single `src/core/schemas.py` module.
- [ ] Unit tests validating: schema construction, validation error on Rule-of-3 violation, `FeaturePlan` and `SubIssue` serialization round-trip, out-of-bounds `depends_on` rejection, `requires_test` / `test_instructions` field behavior.

## Implementation Notes (Optional)

Use `Literal` types and `Enum` classes for constrained fields. The `ChecklistItem` validator enforcing "zero architectural decisions" should raise a clear `ValidationError` with a message like "Checklist items must not contain architectural decisions — use an ADR instead." This is the single most important schema in the system.

The `SubIssue.depends_on` uses 1-based indices (matching how humans read lists). The validator on `FeaturePlan` should ensure no index exceeds `len(sub_issues)` and no self-references exist. The final sub-issue in a `FeaturePlan` is conventionally the integration test — consider adding a validator hint but not a hard constraint, as the Architect should have flexibility.

The `requires_test` field on `ChecklistItem` enables Agentic TDD: when `True`, the Inner Loop (Epic 4) will split code generation into Red-Green phases. The `test_instructions` field gives the Architect control over what the test should verify, preventing the Coder from writing tautological tests.

The `TargetExecutionProfile` replaces the simple tier-routing approach. The Dispatcher fills in the specific `model_name` based on task context, and downstream workers pass it to the `LLMClient` as a `model_override`.
