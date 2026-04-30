"""Tier 2 coder worker for checklist-driven code generation."""

from __future__ import annotations

import inspect
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from loop_troop.core.adr_loader import ADRLoader
from loop_troop.core.context_hydrator import ContextHydrator
from loop_troop.core.github_client import GitHubIssue, GitHubIssueComment, GitHubLabel, GitHubPullRequest
from loop_troop.core.llm_client import LLMClient
from loop_troop.core.schemas import CodePatch, TargetExecutionProfile
from loop_troop.core.workspace_manager import WorkspaceManager
from loop_troop.execution import WorkerTier

from .dispatcher import WorkflowLabel

CODER_PROMPT = (
    "You are the Tier 2 Loop Troop coder. Complete exactly one checklist item. "
    "Return only a valid CodePatch. Use the provided branch name exactly. "
    "Keep the patch scoped to the selected checklist item and provide full file contents in files_changed."
)
ARCHITECT_PLAN_HEADING = "## Architect Plan"
CHECKLIST_ITEM_PATTERN = re.compile(r"^\s*[-*]\s*\[(?P<state>[ xX!])\]\s+(?P<text>.+)$")
FILES_PATTERN = re.compile(r"^\s*-\s*Files:\s*(?P<files>.+)$")
TESTS_PATTERN = re.compile(r"^\s*-\s*Tests required:\s*(?P<tests>.+)$")
CHECKLIST_STATE_PATTERN = re.compile(r"^(\s*[-*]\s*\[)[ xX!](\]\s+.+)$")


class CoderGitHubClient(Protocol):
    async def get_issue(self, owner: str, repo: str, issue_number: int) -> GitHubIssue: ...

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]: ...

    async def update_issue_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        *,
        body: str,
    ) -> GitHubIssueComment: ...

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]: ...

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
    ) -> GitHubPullRequest: ...


class StructuredLLMClient(Protocol):
    def complete_structured(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ParsedChecklistItem:
    comment_id: int
    comment_body: str
    item_index: int
    line_index: int
    description: str
    files_touched: tuple[str, ...]
    requires_test: bool
    test_instructions: str | None


@dataclass(frozen=True, slots=True)
class InnerLoopResult:
    success: bool
    mode: str
    failure_summary: str | None = None


@dataclass(frozen=True, slots=True)
class CoderOutcome:
    issue_number: int
    checklist_item_index: int
    branch_name: str
    target_label: WorkflowLabel
    pr_number: int | None = None
    attempts: int = 1


class InnerLoop:
    def __init__(
        self,
        *,
        runner: Any = subprocess.run,
    ) -> None:
        self._runner = runner

    async def run(
        self,
        *,
        repo_path: str | Path,
        checklist_item: ParsedChecklistItem,
        code_patch: CodePatch,
    ) -> InnerLoopResult:
        mode = "tdd" if checklist_item.requires_test else "standard"
        if not checklist_item.requires_test:
            return InnerLoopResult(success=True, mode=mode)

        completed = self._runner(
            shlex.split(code_patch.test_command),
            cwd=Path(repo_path),
            capture_output=True,
            text=True,
        )
        output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
        if completed.returncode == 0:
            return InnerLoopResult(success=True, mode=mode)
        return InnerLoopResult(success=False, mode=mode, failure_summary=output or "Build/test cycle failed.")


class PRManager:
    def __init__(self, *, github_client: CoderGitHubClient) -> None:
        self._github_client = github_client

    async def open_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        labels: list[str],
    ) -> GitHubPullRequest:
        pull_request = await self._github_client.create_pull_request(
            owner,
            repo,
            title=title,
            body=body,
            head=head,
            base=base,
        )
        await self._github_client.replace_issue_labels(owner, repo, pull_request.number, labels=labels)
        return pull_request


class CoderWorker:
    def __init__(
        self,
        *,
        github_client: CoderGitHubClient,
        llm_client: StructuredLLMClient | None = None,
        context_hydrator: ContextHydrator | None = None,
        adr_loader: ADRLoader | None = None,
        workspace_manager: WorkspaceManager | None = None,
        inner_loop: InnerLoop | None = None,
        pr_manager: PRManager | None = None,
        validation_retries: int = 3,
        max_retries: int = 3,
    ) -> None:
        self._github_client = github_client
        self._llm_client = llm_client or LLMClient()
        self._context_hydrator = context_hydrator or ContextHydrator()
        self._adr_loader = adr_loader or ADRLoader()
        self._workspace_manager = workspace_manager or WorkspaceManager()
        self._inner_loop = inner_loop or InnerLoop()
        self._pr_manager = pr_manager or PRManager(github_client=github_client)
        self._validation_retries = validation_retries
        self._max_retries = max_retries

    async def handle_issue(
        self,
        *,
        owner: str,
        repo: str,
        issue_number: int,
        repo_path: str | Path,
        target_execution_profile: TargetExecutionProfile | None = None,
    ) -> CoderOutcome:
        issue = await self._github_client.get_issue(owner, repo, issue_number)
        if self._workflow_label(issue) is not WorkflowLabel.READY:
            raise ValueError(f"Issue #{issue_number} does not have the Coder label.")

        comments = await self._github_client.list_issue_comments(owner, repo, issue_number)
        checklist_item = self._first_unchecked_item(comments)
        branch_name = f"loop/issue-{issue.number}-item-{checklist_item.item_index}"
        base_branch = self._workspace_manager.current_branch(repo_path)
        self._workspace_manager.create_branch(repo_path, branch_name)

        issue_context = self._format_issue_context(issue=issue, checklist_item=checklist_item)
        adr_context = self._adr_loader.build_context(repo_path)
        hydrated_context = self._context_hydrator.hydrate(
            repo_path=repo_path,
            issue_context=issue_context,
            adr_context=adr_context,
            focus_files=list(checklist_item.files_touched),
        )

        last_failure: str | None = None
        for attempt in range(1, self._max_retries + 1):
            code_patch = self._generate_code_patch(
                issue=issue,
                checklist_item=checklist_item,
                hydrated_context=hydrated_context,
                branch_name=branch_name,
                model_override=target_execution_profile.model_name if target_execution_profile else None,
                failure_feedback=last_failure,
            )
            self._validate_code_patch(
                code_patch=code_patch,
                issue_number=issue.number,
                checklist_item_index=checklist_item.item_index,
            )
            self._apply_code_patch(repo_path=repo_path, code_patch=code_patch)
            self._workspace_manager.commit_all(repo_path, code_patch.commit_message)

            inner_loop_result = await self._run_inner_loop(
                repo_path=repo_path,
                checklist_item=checklist_item,
                code_patch=code_patch,
            )
            if inner_loop_result.success:
                self._workspace_manager.push_branch(repo_path, branch_name)
                pull_request = await self._pr_manager.open_pull_request(
                    owner=owner,
                    repo=repo,
                    title=code_patch.commit_message,
                    body=self._render_pull_request_body(issue=issue, checklist_item=checklist_item),
                    head=branch_name,
                    base=base_branch,
                    labels=[WorkflowLabel.NEEDS_REVIEW.value],
                )
                await self._github_client.update_issue_comment(
                    owner,
                    repo,
                    checklist_item.comment_id,
                    body=self._set_checklist_state(checklist_item.comment_body, checklist_item.line_index, "x"),
                )
                return CoderOutcome(
                    issue_number=issue.number,
                    checklist_item_index=checklist_item.item_index,
                    branch_name=branch_name,
                    target_label=WorkflowLabel.NEEDS_REVIEW,
                    pr_number=pull_request.number,
                    attempts=attempt,
                )

            last_failure = inner_loop_result.failure_summary or "Build/test cycle failed."

        await self._github_client.update_issue_comment(
            owner,
            repo,
            checklist_item.comment_id,
            body=self._set_checklist_state(checklist_item.comment_body, checklist_item.line_index, "!"),
        )
        await self._github_client.replace_issue_labels(
            owner,
            repo,
            issue.number,
            labels=self._updated_labels(issue.labels, WorkflowLabel.NEEDS_HELP),
        )
        return CoderOutcome(
            issue_number=issue.number,
            checklist_item_index=checklist_item.item_index,
            branch_name=branch_name,
            target_label=WorkflowLabel.NEEDS_HELP,
            attempts=self._max_retries,
        )

    def _generate_code_patch(
        self,
        *,
        issue: GitHubIssue,
        checklist_item: ParsedChecklistItem,
        hydrated_context: str,
        branch_name: str,
        model_override: str | None,
        failure_feedback: str | None,
    ) -> CodePatch:
        messages = [
            {"role": "system", "content": CODER_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Repository issue: #{issue.number}\n"
                    f"Checklist item index: {checklist_item.item_index}\n"
                    f"Branch name: {branch_name}\n"
                    "Return a CodePatch for this single checklist item.\n\n"
                    f"{hydrated_context}"
                ),
            },
        ]
        if failure_feedback:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The previous implementation failed the build/test cycle. "
                        f"Address this failure and return a corrected CodePatch:\n{failure_feedback}"
                    ),
                }
            )
        return self._complete_with_validation_feedback(messages=messages, model_override=model_override)

    def _complete_with_validation_feedback(
        self,
        *,
        messages: list[dict[str, str]],
        model_override: str | None,
    ) -> CodePatch:
        retry_messages = list(messages)
        for attempt in range(self._validation_retries):
            try:
                return self._llm_client.complete_structured(
                    tier=WorkerTier.T2,
                    response_model=CodePatch,
                    messages=retry_messages,
                    model_override=model_override,
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

    async def _run_inner_loop(
        self,
        *,
        repo_path: str | Path,
        checklist_item: ParsedChecklistItem,
        code_patch: CodePatch,
    ) -> InnerLoopResult:
        result = self._inner_loop.run(
            repo_path=repo_path,
            checklist_item=checklist_item,
            code_patch=code_patch,
        )
        if inspect.isawaitable(result):
            resolved = await result
            if not isinstance(resolved, InnerLoopResult):
                raise TypeError("InnerLoop.run must return InnerLoopResult.")
            return resolved
        if not isinstance(result, InnerLoopResult):
            raise TypeError("InnerLoop.run must return InnerLoopResult.")
        return result

    def _apply_code_patch(self, *, repo_path: str | Path, code_patch: CodePatch) -> None:
        if not code_patch.files_changed:
            raise ValueError("CodePatch must include at least one file change.")
        for file_change in code_patch.files_changed:
            self._workspace_manager.write_file(repo_path, file_change.path, file_change.content)

    @staticmethod
    def _validate_code_patch(
        *,
        code_patch: CodePatch,
        issue_number: int,
        checklist_item_index: int,
    ) -> None:
        if code_patch.issue_number != issue_number:
            raise ValueError(
                f"Code patch referenced issue #{code_patch.issue_number}, expected #{issue_number}."
            )
        if code_patch.checklist_item_index != checklist_item_index:
            raise ValueError(
                "Code patch referenced checklist item "
                f"#{code_patch.checklist_item_index}, expected #{checklist_item_index}."
            )

    @staticmethod
    def _format_issue_context(*, issue: GitHubIssue, checklist_item: ParsedChecklistItem) -> str:
        lines = [
            f"Issue #{issue.number}: {issue.title or ''}".rstrip(),
            "",
            "Selected Checklist Item:",
            f"- {checklist_item.description}",
        ]
        if checklist_item.files_touched:
            lines.extend(["Files:", *[f"- `{path}`" for path in checklist_item.files_touched]])
        if checklist_item.requires_test and checklist_item.test_instructions:
            lines.extend(["Tests required:", checklist_item.test_instructions])
        else:
            lines.extend(["Tests required:", "No"])
        lines.extend(["", "Issue Body:", (issue.body or "").strip() or "(empty)"])
        return "\n".join(lines)

    @staticmethod
    def _render_pull_request_body(
        *,
        issue: GitHubIssue,
        checklist_item: ParsedChecklistItem,
    ) -> str:
        return "\n".join(
            [
                f"Closes #{issue.number}",
                "",
                f"Checklist item {checklist_item.item_index}: {checklist_item.description}",
            ]
        )

    @staticmethod
    def _first_unchecked_item(comments: list[GitHubIssueComment]) -> ParsedChecklistItem:
        for comment in reversed(comments):
            parsed = CoderWorker._parse_architect_checklist_comment(comment)
            if parsed is not None:
                return parsed
        raise ValueError("No unchecked Architect checklist item found.")

    @staticmethod
    def _parse_architect_checklist_comment(comment: GitHubIssueComment) -> ParsedChecklistItem | None:
        body = comment.body or ""
        if ARCHITECT_PLAN_HEADING not in body:
            return None

        lines = body.splitlines()
        item_index = 0
        items: list[dict[str, Any]] = []
        current_item: dict[str, Any] | None = None
        for line_index, line in enumerate(lines):
            checklist_match = CHECKLIST_ITEM_PATTERN.match(line)
            if checklist_match:
                item_index += 1
                current_item = {
                    "comment_id": comment.id,
                    "comment_body": body,
                    "item_index": item_index,
                    "line_index": line_index,
                    "description": checklist_match.group("text").strip(),
                    "state": checklist_match.group("state"),
                    "files_touched": [],
                    "requires_test": False,
                    "test_instructions": None,
                }
                items.append(current_item)
                continue
            if current_item is None:
                continue

            files_match = FILES_PATTERN.match(line)
            if files_match:
                current_item["files_touched"] = CoderWorker._parse_files(files_match.group("files"))
                continue

            tests_match = TESTS_PATTERN.match(line)
            if tests_match:
                test_value = tests_match.group("tests").strip()
                current_item["requires_test"] = test_value.lower() != "no"
                current_item["test_instructions"] = None if test_value.lower() == "no" else test_value

        for item in items:
            if item["state"] == " ":
                return ParsedChecklistItem(
                    comment_id=item["comment_id"],
                    comment_body=item["comment_body"],
                    item_index=item["item_index"],
                    line_index=item["line_index"],
                    description=item["description"],
                    files_touched=tuple(item["files_touched"]),
                    requires_test=bool(item["requires_test"]),
                    test_instructions=item["test_instructions"],
                )
        return None

    @staticmethod
    def _parse_files(raw_files: str) -> list[str]:
        matches = re.findall(r"`([^`]+)`", raw_files)
        if matches:
            return matches
        return [item.strip() for item in raw_files.split(",") if item.strip()]

    @staticmethod
    def _set_checklist_state(comment_body: str, line_index: int, state: str) -> str:
        lines = comment_body.splitlines()
        lines[line_index] = CHECKLIST_STATE_PATTERN.sub(rf"\1{state}\2", lines[line_index], count=1)
        return "\n".join(lines)

    @staticmethod
    def _workflow_label(issue: GitHubIssue) -> WorkflowLabel | None:
        for label in issue.labels:
            if label.name in WorkflowLabel._value2member_map_:
                return WorkflowLabel(label.name)
        return None

    @staticmethod
    def _updated_labels(labels: list[GitHubLabel], target_label: WorkflowLabel) -> list[str]:
        updated = [label.name for label in labels if label.name not in WorkflowLabel._value2member_map_]
        updated.append(target_label.value)
        return updated


__all__ = [
    "CoderOutcome",
    "CoderWorker",
    "InnerLoop",
    "InnerLoopResult",
    "PRManager",
    "ParsedChecklistItem",
]
