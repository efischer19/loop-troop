"""Canonical Pydantic schemas shared across Loop Troop workers."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkerTier(str, Enum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class EventType(str, Enum):
    GITHUB_EVENT = "github_event"
    ISSUE_EVENT = "issue_event"
    ISSUE_COMMENT = "issue_comment"
    PULL_REQUEST = "pull_request"
    LABELED = "labeled"
    OPENED = "opened"
    EDITED = "edited"
    REOPENED = "reopened"
    CLOSED = "closed"


class LabelActionType(str, Enum):
    ADD = "add"
    REMOVE = "remove"


class ReviewVerdictType(str, Enum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"
    REJECT = "reject"


class TargetExecutionProfile(BaseModel):
    tier: WorkerTier
    model_name: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)


class DispatchLabelAction(BaseModel):
    action: LabelActionType
    label_name: str = Field(min_length=1)


class DispatchDecision(BaseModel):
    event_id: str = Field(min_length=1)
    event_type: EventType
    target_profile: TargetExecutionProfile
    label_action: DispatchLabelAction
    reasoning: str = Field(min_length=1)


class ChecklistItem(BaseModel):
    description: str = Field(min_length=1)
    files_touched: list[str] = Field(default_factory=list)
    logical_steps: list[str] = Field(default_factory=list)
    architectural_decisions: list[str] = Field(default_factory=list)
    requires_test: bool
    test_instructions: str | None = None

    @field_validator("files_touched", "logical_steps")
    @classmethod
    def validate_rule_of_three(cls, value: list[str], info) -> list[str]:
        if len(value) > 3:
            raise ValueError(f"{info.field_name} must contain at most 3 items.")
        return value

    @field_validator("architectural_decisions")
    @classmethod
    def validate_no_architecture_changes(cls, value: list[str]) -> list[str]:
        if value:
            raise ValueError(
                "Checklist items must not contain architectural decisions — use an ADR instead."
            )
        return value

    @model_validator(mode="after")
    def validate_test_contract(self) -> ChecklistItem:
        if self.requires_test and not self.test_instructions:
            raise ValueError("Checklist items that require tests must include test_instructions.")
        if not self.requires_test and self.test_instructions:
            raise ValueError("Checklist items without tests must not include test_instructions.")
        return self


class ArchitectPlan(BaseModel):
    issue_number: int = Field(ge=1)
    checklist_items: list[ChecklistItem] = Field(default_factory=list)
    adr_references: list[str] = Field(default_factory=list)
    verification_strategy: str = Field(min_length=1)


class SubIssue(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    depends_on: list[int] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def validate_positive_dependencies(cls, value: list[int]) -> list[int]:
        if any(item < 1 for item in value):
            raise ValueError("depends_on must use 1-based indices.")
        return value


class FeaturePlan(BaseModel):
    epic_issue_number: int = Field(ge=1)
    sub_issues: list[SubIssue] = Field(default_factory=list)
    adr_references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dependencies(self) -> FeaturePlan:
        sub_issue_count = len(self.sub_issues)
        for index, sub_issue in enumerate(self.sub_issues, start=1):
            for dependency in sub_issue.depends_on:
                if dependency > sub_issue_count:
                    raise ValueError(
                        f"Sub-issue {index} depends on #{dependency}, but only {sub_issue_count} sub-issues exist."
                    )
                if dependency == index:
                    raise ValueError(f"Sub-issue {index} cannot depend on itself.")
        return self


class FileChange(BaseModel):
    path: str = Field(min_length=1)
    content: str


class CodePatch(BaseModel):
    issue_number: int = Field(ge=1)
    checklist_item_index: int = Field(ge=1)
    branch_name: str = Field(min_length=1)
    files_changed: list[FileChange] = Field(default_factory=list)
    test_command: str = Field(min_length=1)
    commit_message: str = Field(min_length=1)


class ReviewComment(BaseModel):
    path: str = Field(min_length=1)
    body: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)


class ReviewVerdict(BaseModel):
    pr_number: int = Field(ge=1)
    verdict: ReviewVerdictType
    adr_violations: list[str] = Field(default_factory=list)
    comments: list[ReviewComment] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_adr_violations(self) -> ReviewVerdict:
        if self.adr_violations and self.verdict is not ReviewVerdictType.REQUEST_CHANGES:
            raise ValueError("Review verdicts with ADR violations must request changes.")
        return self


__all__ = [
    "ArchitectPlan",
    "ChecklistItem",
    "CodePatch",
    "DispatchDecision",
    "DispatchLabelAction",
    "EventType",
    "FeaturePlan",
    "FileChange",
    "LabelActionType",
    "ReviewComment",
    "ReviewVerdict",
    "ReviewVerdictType",
    "SubIssue",
    "TargetExecutionProfile",
    "WorkerTier",
]
