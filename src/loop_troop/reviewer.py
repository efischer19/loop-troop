"""Tier 3 reviewer worker for pull request review and ADR enforcement."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from loop_troop.core.adr_loader import ADRLoader
from loop_troop.core.context_hydrator import ContextHydrator, estimate_token_count
from loop_troop.core.github_client import (
    GitHubCheckRun,
    GitHubIssue,
    GitHubIssueComment,
    GitHubLabel,
    GitHubPullRequest,
    GitHubPullRequestFile,
)
from loop_troop.core.llm_client import LLMClient
from loop_troop.core.schemas import ReviewComment, ReviewVerdict, ReviewVerdictType
from loop_troop.execution import WorkerTier

from .dispatcher import WorkflowLabel

REVIEW_PROMPT = (
    "You are the Tier 3 Loop Troop reviewer. Review only from the provided diff, issue, ADR, "
    "and Repomix context. Do not execute code, run tests, or assume facts not present in the "
    "context. Reject pull requests that make architectural changes without ADR coverage. "
    "Examine the test files in this diff. Flag any test that: (1) asserts True or 1 == 1, "
    "(2) has no meaningful assertions, (3) mocks the exact function it's testing, or "
    "(4) tests implementation details rather than behavior. These are signs of a model gaming "
    "the CI to get a green build."
)
ISSUE_REFERENCE_PATTERN = re.compile(r"#(?P<number>[0-9]+)")
CHECKLIST_PATTERN = re.compile(r"^\s*[-*]\s*\[(?P<done>[ xX])\]\s+(?P<text>.+)$")
MAX_DIFF_TOKENS = 4_000
BLOCKING_CHECK_RUN_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
}


class ReviewerGitHubClient(Protocol):
    async def get_pull_request(self, owner: str, repo: str, pull_number: int) -> GitHubPullRequest: ...

    async def get_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str: ...

    async def list_pull_request_files(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubPullRequestFile]: ...

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[GitHubCheckRun]: ...

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> GitHubIssue: ...

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]: ...

    async def create_pull_request_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        event: str,
        body: str,
        comments: list[dict[str, Any]] | None = None,
        commit_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]: ...


class StructuredLLMClient(Protocol):
    def complete_structured(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ReviewerOutcome:
    pr_number: int
    target_label: WorkflowLabel
    review_event: str
    review_body: str
    linked_issue_number: int | None = None
    ci_gate_blocked: bool = False


class ReviewerWorker:
    def __init__(
        self,
        *,
        github_client: ReviewerGitHubClient,
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

    async def handle_pull_request(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
        repo_path: str | Path,
    ) -> ReviewerOutcome:
        pull_request = await self._github_client.get_pull_request(owner, repo, pull_number)
        if not self._has_review_label(pull_request):
            raise ValueError(f"Pull request #{pull_number} does not have the Reviewer label.")

        head_sha = self._head_sha(pull_request)

        if self._is_bake_off(pull_request):
            return await self._submit_terminal_review(
                owner=owner,
                repo=repo,
                pull_request=pull_request,
                head_sha=head_sha,
                event="REQUEST_CHANGES",
                body=(
                    "## Reviewer Result\n"
                    "This `[BAKE-OFF]` pull request is reserved for human comparison and must remain a draft. "
                    "Loop Troop will not auto-approve it."
                ),
                target_label=WorkflowLabel.CHANGES_REQUESTED,
            )

        check_runs = await self._github_client.get_check_runs(owner, repo, head_sha)
        blocking_check_runs = self._blocking_check_runs(check_runs)
        if blocking_check_runs:
            return await self._submit_terminal_review(
                owner=owner,
                repo=repo,
                pull_request=pull_request,
                head_sha=head_sha,
                event="REQUEST_CHANGES",
                body=self._render_ci_gate_review(blocking_check_runs),
                target_label=WorkflowLabel.CHANGES_REQUESTED,
                ci_gate_blocked=True,
            )

        diff = await self._github_client.get_pull_request_diff(owner, repo, pull_number)
        changed_files = await self._github_client.list_pull_request_files(owner, repo, pull_number)
        linked_issue_number = self._linked_issue_number(pull_request)
        linked_issue, linked_issue_comments = await self._linked_issue_context(
            owner=owner,
            repo=repo,
            linked_issue_number=linked_issue_number,
        )
        issue_context = self._format_issue_context(
            pull_request=pull_request,
            diff=diff,
            changed_files=changed_files,
            linked_issue=linked_issue,
            linked_issue_comments=linked_issue_comments,
        )
        adr_context = self._adr_loader.build_context(repo_path)
        hydrated_context = self._context_hydrator.hydrate(
            repo_path=repo_path,
            issue_context=issue_context,
            adr_context=adr_context,
            focus_files=[item.filename for item in changed_files],
        )

        verdict = self._complete_with_validation_feedback(
            messages=[
                {"role": "system", "content": REVIEW_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Repository: {owner}/{repo}\n"
                        f"Pull request number: {pull_request.number}\n"
                        f"Pull request title: {pull_request.title}\n"
                        f"HEAD SHA: {head_sha}\n"
                        "Return a ReviewVerdict for this pull request.\n\n"
                        f"{hydrated_context}"
                    ),
                },
            ]
        )
        if verdict.pr_number != pull_request.number:
            raise ValueError(
                f"Review verdict referenced PR #{verdict.pr_number}, expected #{pull_request.number}."
            )

        review_event = "APPROVE" if verdict.verdict is ReviewVerdictType.APPROVE else "REQUEST_CHANGES"
        target_label = (
            WorkflowLabel.APPROVED if review_event == "APPROVE" else WorkflowLabel.CHANGES_REQUESTED
        )
        review_body = self._render_review_body(verdict)
        review_comments = self._review_comments_payload(verdict.comments)
        await self._github_client.create_pull_request_review(
            owner,
            repo,
            pull_request.number,
            event=review_event,
            body=review_body,
            comments=review_comments or None,
            commit_id=head_sha,
        )
        await self._github_client.replace_issue_labels(
            owner,
            repo,
            pull_request.number,
            labels=self._updated_labels(pull_request.labels, target_label),
        )
        return ReviewerOutcome(
            pr_number=pull_request.number,
            target_label=target_label,
            review_event=review_event,
            review_body=review_body,
            linked_issue_number=linked_issue_number,
        )

    def _complete_with_validation_feedback(self, *, messages: list[dict[str, str]]) -> ReviewVerdict:
        retry_messages = list(messages)
        for attempt in range(self._validation_retries):
            try:
                return self._llm_client.complete_structured(
                    tier=WorkerTier.T3,
                    response_model=ReviewVerdict,
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

    async def _linked_issue_context(
        self,
        *,
        owner: str,
        repo: str,
        linked_issue_number: int | None,
    ) -> tuple[GitHubIssue | None, list[GitHubIssueComment]]:
        if linked_issue_number is None:
            return None, []
        issue = await self._github_client.get_issue(owner, repo, linked_issue_number)
        comments = await self._github_client.list_issue_comments(owner, repo, linked_issue_number)
        return issue, comments

    async def _submit_terminal_review(
        self,
        *,
        owner: str,
        repo: str,
        pull_request: GitHubPullRequest,
        head_sha: str,
        event: str,
        body: str,
        target_label: WorkflowLabel,
        ci_gate_blocked: bool = False,
    ) -> ReviewerOutcome:
        await self._github_client.create_pull_request_review(
            owner,
            repo,
            pull_request.number,
            event=event,
            body=body,
            commit_id=head_sha,
        )
        await self._github_client.replace_issue_labels(
            owner,
            repo,
            pull_request.number,
            labels=self._updated_labels(pull_request.labels, target_label),
        )
        return ReviewerOutcome(
            pr_number=pull_request.number,
            target_label=target_label,
            review_event=event,
            review_body=body,
            ci_gate_blocked=ci_gate_blocked,
            linked_issue_number=self._linked_issue_number(pull_request),
        )

    @staticmethod
    def _format_issue_context(
        *,
        pull_request: GitHubPullRequest,
        diff: str,
        changed_files: list[GitHubPullRequestFile],
        linked_issue: GitHubIssue | None,
        linked_issue_comments: list[GitHubIssueComment],
    ) -> str:
        changed_file_lines = "\n".join(f"- `{item.filename}`" for item in changed_files) or "- (none)"
        checklist = ReviewerWorker._render_checklist(linked_issue.body if linked_issue else None)
        comments = ReviewerWorker._render_issue_comments(linked_issue_comments)
        issue_heading = (
            f"Issue #{linked_issue.number}: {linked_issue.title or ''}".rstrip() if linked_issue else "(none linked)"
        )
        return "\n".join(
            [
                f"Pull Request #{pull_request.number}: {pull_request.title}",
                "",
                "Pull Request Body:",
                (pull_request.body or "").strip() or "(empty)",
                "",
                "Changed Files:",
                changed_file_lines,
                "",
                "Pull Request Diff:",
                ReviewerWorker._truncate_diff(diff),
                "",
                "Linked Issue:",
                issue_heading,
                "",
                "Linked Issue Body:",
                ((linked_issue.body or "").strip() if linked_issue else "") or "(empty)",
                "",
                "Linked Issue Checklist:",
                checklist,
                "",
                "Linked Issue Comments:",
                comments,
            ]
        )

    @staticmethod
    def _render_checklist(issue_body: str | None) -> str:
        if not issue_body:
            return "- (none)"
        checklist_items = []
        for line in issue_body.splitlines():
            match = CHECKLIST_PATTERN.match(line)
            if not match:
                continue
            marker = "x" if match.group("done").lower() == "x" else " "
            checklist_items.append(f"- [{marker}] {match.group('text').strip()}")
        return "\n".join(checklist_items) or "- (none)"

    @staticmethod
    def _render_issue_comments(comments: list[GitHubIssueComment]) -> str:
        rendered_comments = "\n".join(
            f"- @{ReviewerWorker._comment_author(comment)}: {ReviewerWorker._comment_body(comment)}"
            for comment in comments
        )
        return rendered_comments or "- (none)"

    @staticmethod
    def _comment_author(comment: GitHubIssueComment) -> str:
        user = getattr(comment, "user", None)
        if isinstance(user, dict):
            return str(user.get("login", "unknown"))
        return getattr(user, "login", "unknown")

    @staticmethod
    def _comment_body(comment: GitHubIssueComment) -> str:
        return (comment.body or "").strip()

    @staticmethod
    def _truncate_diff(diff: str, *, max_tokens: int = MAX_DIFF_TOKENS) -> str:
        token_spans = list(re.finditer(r"\S+", diff))
        if len(token_spans) <= max_tokens:
            return diff
        marker = "\n[TRUNCATED]"
        marker_tokens = estimate_token_count(marker)
        available_tokens = max_tokens - marker_tokens
        if available_tokens <= 0:
            return marker.lstrip()
        cutoff = token_spans[available_tokens - 1].end()
        return f"{diff[:cutoff].rstrip()}{marker}"

    @staticmethod
    def _render_ci_gate_review(blocking_check_runs: list[GitHubCheckRun]) -> str:
        lines = [
            "## Reviewer Result",
            "Fix CI before requesting review. Loop Troop does not review pull requests with failing or pending checks.",
            "",
            "Blocking checks:",
        ]
        lines.extend(
            f"- `{check_run.name}` (status={check_run.status or 'unknown'}, conclusion={check_run.conclusion or 'pending'})"
            for check_run in blocking_check_runs
        )
        return "\n".join(lines)

    @staticmethod
    def _render_review_body(verdict: ReviewVerdict) -> str:
        lines = ["## Reviewer Result"]
        if verdict.adr_violations:
            lines.extend(["ADR violations:", *[f"- {item}" for item in verdict.adr_violations]])
        else:
            lines.append("ADR violations: none detected.")

        general_comments = [comment for comment in verdict.comments if comment.line is None]
        if general_comments:
            lines.extend(["", "Additional feedback:"])
            lines.extend(f"- `{comment.path}`: {comment.body}" for comment in general_comments)
        return "\n".join(lines)

    @staticmethod
    def _review_comments_payload(comments: list[ReviewComment]) -> list[dict[str, Any]]:
        payload = []
        for comment in comments:
            if comment.line is None:
                continue
            payload.append(
                {
                    "path": comment.path,
                    "body": comment.body,
                    "line": comment.line,
                    "side": "RIGHT",
                }
            )
        return payload

    @staticmethod
    def _blocking_check_runs(check_runs: list[GitHubCheckRun]) -> list[GitHubCheckRun]:
        blocking = []
        for check_run in check_runs:
            conclusion = check_run.conclusion.lower() if isinstance(check_run.conclusion, str) else None
            if conclusion is None or conclusion in BLOCKING_CHECK_RUN_CONCLUSIONS:
                blocking.append(check_run)
        return blocking

    @staticmethod
    def _linked_issue_number(pull_request: GitHubPullRequest) -> int | None:
        search_text = "\n".join(part for part in [pull_request.title, pull_request.body] if part)
        match = ISSUE_REFERENCE_PATTERN.search(search_text)
        if match is None:
            return None
        return int(match.group("number"))

    @staticmethod
    def _head_sha(pull_request: GitHubPullRequest) -> str:
        if pull_request.head is None:
            raise ValueError(f"Pull request #{pull_request.number} is missing head metadata.")
        return pull_request.head.sha

    @staticmethod
    def _has_review_label(pull_request: GitHubPullRequest) -> bool:
        return any(label.name == WorkflowLabel.NEEDS_REVIEW.value for label in pull_request.labels)

    @staticmethod
    def _is_bake_off(pull_request: GitHubPullRequest) -> bool:
        return (pull_request.title or "").startswith("[BAKE-OFF]")

    @staticmethod
    def _updated_labels(labels: list[GitHubLabel], target_label: WorkflowLabel) -> list[str]:
        updated_labels = [label.name for label in labels if label.name not in WorkflowLabel._value2member_map_]
        updated_labels.append(target_label.value)
        return updated_labels
