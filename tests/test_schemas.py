import pytest
from pydantic import ValidationError

from loop_troop.core.schemas import (
    ADRDocument,
    ADRStatus,
    ArchitectPlan,
    ChecklistItem,
    CodePatch,
    DispatchDecision,
    DispatchLabelAction,
    EventType,
    FeaturePlan,
    FileChange,
    LabelActionType,
    ReviewComment,
    ReviewVerdict,
    ReviewVerdictType,
    SubIssue,
    TargetExecutionProfile,
    WorkerTier,
)


def test_schema_construction_uses_canonical_models() -> None:
    adr_document = ADRDocument(
        id="ADR-0001",
        title="Use ADRs",
        status=ADRStatus.ACCEPTED,
        decision_summary="We will record significant architectural decisions in ADRs.",
        full_text="# ADR-0001: Use ADRs\n\n## Decision\n\nWe will record significant architectural decisions in ADRs.",
    )
    profile = TargetExecutionProfile(
        tier=WorkerTier.T2,
        model_name="qwen2.5-coder:32b",
        reasoning="Implementation-heavy task.",
    )
    checklist_item = ChecklistItem(
        description="Add the new worker contract.",
        files_touched=["src/loop_troop/core/schemas.py"],
        logical_steps=["Define the schema", "Export it", "Wire imports"],
        requires_test=True,
        test_instructions="Cover valid input, invalid enum values, and serialization.",
    )
    architect_plan = ArchitectPlan(
        issue_number=42,
        checklist_items=[checklist_item],
        adr_references=["ADR-0002-recursive-macro-planning.md"],
        verification_strategy="Run the schema unit tests and a full pytest pass.",
    )
    dispatch_decision = DispatchDecision(
        event_id="evt-42",
        event_type=EventType.LABELED,
        target_profile=profile,
        label_action=DispatchLabelAction(
            action=LabelActionType.ADD,
            label_name="loop: ready",
        ),
        reasoning="The issue already has a validated checklist.",
    )
    code_patch = CodePatch(
        issue_number=42,
        checklist_item_index=1,
        branch_name="loop/issue-42-item-1",
        files_changed=[FileChange(path="src/loop_troop/core/schemas.py", content="...")],
        test_command="python -m pytest tests/test_schemas.py",
        commit_message="feat: add canonical worker schemas",
    )
    review_verdict = ReviewVerdict(
        pr_number=7,
        verdict=ReviewVerdictType.APPROVE,
        comments=[ReviewComment(path="tests/test_schemas.py", line=1, body="Looks good.")],
    )

    assert adr_document.status is ADRStatus.ACCEPTED
    assert dispatch_decision.target_profile == profile
    assert architect_plan.checklist_items == [checklist_item]
    assert code_patch.files_changed[0].path == "src/loop_troop/core/schemas.py"
    assert review_verdict.verdict is ReviewVerdictType.APPROVE


@pytest.mark.parametrize("field_name", ["files_touched", "logical_steps"])
def test_checklist_item_rejects_rule_of_three_violations(field_name: str) -> None:
    with pytest.raises(ValidationError, match=f"{field_name} must contain at most 3 items"):
        ChecklistItem(
            description="Breaks the rule of three.",
            files_touched=["a.py", "b.py", "c.py", "d.py"] if field_name == "files_touched" else [],
            logical_steps=["one", "two", "three", "four"] if field_name == "logical_steps" else [],
            requires_test=False,
        )


def test_checklist_item_rejects_architectural_decisions() -> None:
    with pytest.raises(
        ValidationError,
        match="Checklist items must not contain architectural decisions — use an ADR instead.",
    ):
        ChecklistItem(
            description="Sneaks in an ADR-worthy decision.",
            architectural_decisions=["Switch the database backend."],
            requires_test=False,
        )


def test_feature_plan_and_sub_issue_round_trip() -> None:
    plan = FeaturePlan(
        epic_issue_number=99,
        sub_issues=[
            SubIssue(title="Add schema models", description="Define the contracts."),
            SubIssue(
                title="Integration test",
                description="Verify the end-to-end feature flow.",
                depends_on=[1],
                is_integration_test=True,
            ),
        ],
        adr_references=["ADR-0002-recursive-macro-planning.md"],
    )

    round_tripped = FeaturePlan.model_validate_json(plan.model_dump_json())

    assert round_tripped == plan
    assert round_tripped.sub_issues[1].depends_on == [1]


def test_feature_plan_rejects_out_of_bounds_and_self_dependencies() -> None:
    with pytest.raises(ValidationError, match="depends on #3"):
        FeaturePlan(
            epic_issue_number=100,
            sub_issues=[
                SubIssue(title="One", description="First sub-issue."),
                SubIssue(
                    title="Two",
                    description="Second sub-issue.",
                    depends_on=[3],
                    is_integration_test=True,
                ),
            ],
        )

    with pytest.raises(ValidationError, match="cannot depend on itself"):
        FeaturePlan(
            epic_issue_number=101,
            sub_issues=[
                SubIssue(
                    title="One",
                    description="First sub-issue.",
                    depends_on=[1],
                    is_integration_test=True,
                ),
            ],
        )

    with pytest.raises(ValidationError, match="final sub-issue must be marked as the integration or feature test"):
        FeaturePlan(
            epic_issue_number=102,
            sub_issues=[
                SubIssue(title="One", description="First sub-issue."),
                SubIssue(title="Two", description="Second sub-issue.", depends_on=[1]),
            ],
        )


def test_checklist_item_requires_test_and_test_instructions_contract() -> None:
    item = ChecklistItem(
        description="Add behavior tests.",
        requires_test=True,
        test_instructions="Cover the empty payload edge case.",
    )

    assert item.test_instructions == "Cover the empty payload edge case."

    with pytest.raises(ValidationError, match="must include test_instructions"):
        ChecklistItem(description="Missing instructions.", requires_test=True)

    with pytest.raises(ValidationError, match="must not include test_instructions"):
        ChecklistItem(
            description="Instructions without a test.",
            requires_test=False,
            test_instructions="This should not be present.",
        )


def test_architect_plan_uses_explicit_adr_resolution_path() -> None:
    plan = ArchitectPlan(
        issue_number=55,
        checklist_items=[],
        adr_references=["ADR-0009"],
        requires_adr=True,
        adr_instructions="Create ADR-0009 before implementation.",
        verification_strategy="Re-run planning after the ADR merges.",
    )

    assert plan.requires_adr is True
    assert plan.adr_instructions == "Create ADR-0009 before implementation."

    with pytest.raises(ValidationError, match="must not include checklist_items"):
        ArchitectPlan(
            issue_number=55,
            checklist_items=[
                ChecklistItem(
                    description="Should not coexist with ADR flow.",
                    requires_test=False,
                )
            ],
            requires_adr=True,
            adr_instructions="Create ADR-0009 before implementation.",
            verification_strategy="Retry later.",
        )

    with pytest.raises(ValidationError, match="must include at least one checklist item"):
        ArchitectPlan(
            issue_number=56,
            checklist_items=[],
            verification_strategy="Retry later.",
        )


def test_review_verdict_requires_request_changes_for_adr_violations() -> None:
    with pytest.raises(ValidationError, match="must request changes"):
        ReviewVerdict(
            pr_number=11,
            verdict=ReviewVerdictType.REJECT,
            adr_violations=["Modified architecture without ADR update."],
        )
