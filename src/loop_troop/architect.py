"""Tier 3 architect worker for issue planning and feature decomposition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from loop_troop.core.adr_loader import ADRLoader
from loop_troop.core.context_hydrator import ContextHydrator
from loop_troop.core.github_client import GitHubIssue, GitHubIssueComment
from loop_troop.core.llm_client import LLMClient
from loop_troop.core.schemas import ArchitectPlan, FeaturePlan
from loop_troop.execution import WorkerTier

from .dispatcher import WorkflowLabel

MICRO_PROMPT = (
    "You are a technical lead. Break this issue into checklist items. Each item MUST touch ≤3 files, "
    "require ≤3 logical steps, and make ZERO architectural decisions. If the issue requires an "
    "architectural decision, stop and set requires_adr to true with specific adr_instructions. "
    "For each item, determine if it requires a test — set requires_test: true for business logic, "
    "API routing, and data transformations; set requires_test: false for trivial config or setup changes."
)
MACRO_PROMPT = (
    "Decompose this feature into discrete sub-issues with explicit dependencies. The final sub-issue MUST "
    "be an integration or feature test. Each sub-issue should be independently implementable once its "
    "dependencies are resolved. Mark recursive decomposition candidates with is_feature=true."
)


class ArchitectGitHubClient(Protocol):
    async def get_issue(self, owner: str, repo: str, issue_number: int) -> GitHubIssue: ...

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]: ...

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]: ...

    async def create_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        body: str,
    ) -> GitHubIssueComment: ...

    async def create_issue(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> GitHubIssue: ...


class StructuredLLMClient(Protocol):
    def complete_structured(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ArchitectOutcome:
    mode: str
    issue_number: int
    target_label: WorkflowLabel
    comment_body: str
    created_issue_numbers: tuple[int, ...] = ()


class ArchitectWorker:
    def __init__(
        self,
        *,
        github_client: ArchitectGitHubClient,
        llm_client: StructuredLLMClient | None = None,
        context_hydrator: ContextHydrator | None = None,
        adr_loader: ADRLoader | None = None,
        validation_retries: int = 3,
    ) -> None:
        self._github_client = github_client
        self._llm_client = llm_client or LLMClient()
        self._context_hydrator = context_hydrator or ContextHydrator()
        self._adr_loader = adr_loader or ADRLoader()
        self._validation_retries = validation_retries

    async def handle_issue(
        self,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        repo_path: str | Path,
    ) -> ArchitectOutcome:
        issue = await self._github_client.get_issue(owner, repo, issue_number)
        comments = await self._github_client.list_issue_comments(owner, repo, issue_number)
        label = self._workflow_label(issue)
        if label is WorkflowLabel.NEEDS_PLANNING:
            return await self._run_micro_plan(
                owner=owner,
                repo=repo,
                issue=issue,
                comments=comments,
                repo_path=repo_path,
            )
        if label is WorkflowLabel.FEATURE:
            return await self._run_macro_plan(
                owner=owner,
                repo=repo,
                issue=issue,
                comments=comments,
            )
        raise ValueError(f"Issue #{issue_number} does not have an Architect label.")

    async def _run_micro_plan(
        self,
        *,
        owner: str,
        repo: str,
        issue: GitHubIssue,
        comments: list[GitHubIssueComment],
        repo_path: str | Path,
    ) -> ArchitectOutcome:
        issue_context = self._format_issue_context(issue, comments)
        adr_context = self._adr_loader.build_context(repo_path)
        hydrated_context = self._context_hydrator.hydrate(
            repo_path=repo_path,
            issue_context=issue_context,
            adr_context=adr_context,
        )
        plan = self._complete_with_validation_feedback(
            response_model=ArchitectPlan,
            messages=[
                {"role": "system", "content": MICRO_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Repository: {owner}/{repo}\n"
                        f"Issue number: {issue.number}\n"
                        "Use the following strict-context input to produce an ArchitectPlan.\n\n"
                        f"{hydrated_context}"
                    ),
                },
            ],
        )
        target_label = WorkflowLabel.NEEDS_ADR if plan.requires_adr else WorkflowLabel.READY
        comment_body = self._render_adr_comment(plan) if plan.requires_adr else self._render_checklist_comment(plan)
        await self._github_client.create_issue_comment(owner, repo, issue.number, body=comment_body)
        await self._github_client.replace_issue_labels(
            owner,
            repo,
            issue.number,
            labels=self._updated_labels(issue, target_label),
        )
        return ArchitectOutcome(
            mode="micro",
            issue_number=issue.number,
            target_label=target_label,
            comment_body=comment_body,
        )

    async def _run_macro_plan(
        self,
        *,
        owner: str,
        repo: str,
        issue: GitHubIssue,
        comments: list[GitHubIssueComment],
    ) -> ArchitectOutcome:
        issue_context = self._format_issue_context(issue, comments)
        plan = self._complete_with_validation_feedback(
            response_model=FeaturePlan,
            messages=[
                {"role": "system", "content": MACRO_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Repository: {owner}/{repo}\n"
                        f"Epic issue number: {issue.number}\n"
                        "Create a FeaturePlan for this feature issue.\n\n"
                        f"{issue_context}"
                    ),
                },
            ],
        )

        created_issues: list[GitHubIssue] = []
        for sub_issue in plan.sub_issues:
            created_issues.append(
                await self._github_client.create_issue(
                    owner,
                    repo,
                    title=sub_issue.title,
                    body=self._render_sub_issue_body(issue.number, sub_issue.description, sub_issue.depends_on),
                    labels=[
                        WorkflowLabel.FEATURE.value
                        if sub_issue.is_feature
                        else WorkflowLabel.NEEDS_PLANNING.value
                    ],
                )
            )

        comment_body = self._render_tracking_comment(plan, created_issues)
        await self._github_client.create_issue_comment(owner, repo, issue.number, body=comment_body)
        await self._github_client.replace_issue_labels(
            owner,
            repo,
            issue.number,
            labels=self._updated_labels(issue, WorkflowLabel.EPIC_TRACKING),
        )
        return ArchitectOutcome(
            mode="macro",
            issue_number=issue.number,
            target_label=WorkflowLabel.EPIC_TRACKING,
            comment_body=comment_body,
            created_issue_numbers=tuple(created_issue.number for created_issue in created_issues),
        )

    def _complete_with_validation_feedback(
        self,
        *,
        response_model: type[ArchitectPlan] | type[FeaturePlan],
        messages: list[dict[str, str]],
    ) -> ArchitectPlan | FeaturePlan:
        retry_messages = list(messages)
        for attempt in range(self._validation_retries):
            try:
                return self._llm_client.complete_structured(
                    tier=WorkerTier.T3,
                    response_model=response_model,
                    messages=retry_messages,
                    temperature=0,
                )
            except ValidationError as exc:
                if attempt >= self._validation_retries - 1:
                    raise
                retry_messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": (
                            "Your previous response failed schema validation. "
                            f"Fix these errors and return a corrected JSON object only:\n{exc}"
                        ),
                    },
                ]
        raise RuntimeError("Validation retry loop exhausted unexpectedly.")

    @staticmethod
    def _format_issue_context(issue: GitHubIssue, comments: list[GitHubIssueComment]) -> str:
        rendered_comments = "\n".join(
            f"- @{ArchitectWorker._comment_author(comment)}: {(comment.body or '').strip()}"
            for comment in comments
        ) or "- (none)"
        return "\n".join(
            [
                f"Issue #{issue.number}: {issue.title or ''}".rstrip(),
                "",
                "Body:",
                (issue.body or "").strip() or "(empty)",
                "",
                "Comments:",
                rendered_comments,
            ]
        )

    @staticmethod
    def _comment_author(comment: GitHubIssueComment) -> str:
        user = getattr(comment, "user", None)
        if isinstance(user, dict):
            return str(user.get("login", "unknown"))
        return getattr(user, "login", "unknown")

    @staticmethod
    def _render_checklist_comment(plan: ArchitectPlan) -> str:
        lines = ["## Architect Plan"]
        for item in plan.checklist_items:
            lines.append(f"- [ ] {item.description}")
            if item.files_touched:
                files = ", ".join(f"`{path}`" for path in item.files_touched)
                lines.append(f"  - Files: {files}")
            if item.logical_steps:
                lines.append("  - Steps:")
                lines.extend(f"    1. {step}" for step in item.logical_steps)
            if item.requires_test:
                lines.append(f"  - Tests required: {item.test_instructions}")
            else:
                lines.append("  - Tests required: No")
        lines.extend(["", f"Verification strategy: {plan.verification_strategy}"])
        return "\n".join(lines)

    @staticmethod
    def _render_adr_comment(plan: ArchitectPlan) -> str:
        references = ", ".join(plan.adr_references) if plan.adr_references else "a new ADR"
        return "\n".join(
            [
                "## Architect Planning Paused",
                f"This issue requires architectural work before implementation. Please create or update {references}.",
                "",
                plan.adr_instructions,
                "",
                f"Verification strategy: {plan.verification_strategy}",
            ]
        )

    @staticmethod
    def _render_sub_issue_body(
        parent_issue_number: int,
        description: str,
        dependency_indices: list[int],
    ) -> str:
        dependency_text = ", ".join(f"sub-issue {dependency}" for dependency in dependency_indices) or "none"
        return "\n".join(
            [
                description,
                "",
                f"Parent feature: #{parent_issue_number}",
                f"Planned dependencies: {dependency_text}",
            ]
        )

    @staticmethod
    def _render_tracking_comment(plan: FeaturePlan, created_issues: list[GitHubIssue]) -> str:
        lines = ["## Feature Breakdown"]
        for index, sub_issue in enumerate(plan.sub_issues):
            issue_number = created_issues[index].number
            dependency_numbers = [
                f"#{ArchitectWorker._created_issue_number_for_dependency(created_issues, item)}"
                for item in sub_issue.depends_on
            ]
            dependency_text = ", ".join(dependency_numbers) or "none"
            lines.append(f"- [ ] #{issue_number}: {sub_issue.title} (Depends on: {dependency_text})")
        return "\n".join(lines)

    @staticmethod
    def _created_issue_number_for_dependency(created_issues: list[GitHubIssue], dependency_index: int) -> int:
        if dependency_index < 1 or dependency_index > len(created_issues):
            raise ValueError(f"Dependency index {dependency_index} is out of bounds for the created issue list.")
        return created_issues[dependency_index - 1].number

    @staticmethod
    def _workflow_label(issue: GitHubIssue) -> WorkflowLabel | None:
        for label in issue.labels:
            if label.name in WorkflowLabel._value2member_map_:
                return WorkflowLabel(label.name)
        return None

    @staticmethod
    def _updated_labels(issue: GitHubIssue, target_label: WorkflowLabel) -> list[str]:
        labels = [label.name for label in issue.labels if label.name not in WorkflowLabel._value2member_map_]
        labels.append(target_label.value)
        return labels
