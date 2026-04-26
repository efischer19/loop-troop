"""Tier 1 dispatcher for label-waterfall routing."""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

import httpx
from pydantic import BaseModel, Field

from loop_troop.core.github_client import GitHubIssue, GitHubIssueComment
from loop_troop.core.llm_client import LLMClient
from loop_troop.execution import TargetExecutionProfile, WorkerTier
from loop_troop.shadow_log import LoggedEvent, ShadowLog

SleepFn = Callable[[float], Awaitable[None]]
DEPENDENCY_PATTERN = re.compile(r"\(Depends on:\s*(?P<deps>#[0-9]+(?:\s*,\s*#[0-9]+)*)\)")
ISSUE_NUMBER_PATTERN = re.compile(r"#(?P<number>[0-9]+)")


class WorkflowLabel(str, Enum):
    NEEDS_PLANNING = "loop: needs-planning"
    FEATURE = "loop: feature"
    READY = "loop: ready"
    NEEDS_REVIEW = "loop: needs-review"
    EPIC_TRACKING = "loop: epic-tracking"
    APPROVED = "loop: approved"
    CHANGES_REQUESTED = "loop: changes-requested"
    DONE = "loop: done"


class DispatchRoute(str, Enum):
    ARCHITECT_MICRO = "architect_micro"
    ARCHITECT_MACRO = "architect_macro"
    CODER = "coder"
    REVIEWER = "reviewer"


VALID_LABEL_TRANSITIONS: dict[WorkflowLabel | None, set[WorkflowLabel]] = {
    None: {
        WorkflowLabel.NEEDS_PLANNING,
        WorkflowLabel.FEATURE,
        WorkflowLabel.READY,
        WorkflowLabel.NEEDS_REVIEW,
    },
    WorkflowLabel.NEEDS_PLANNING: {WorkflowLabel.NEEDS_PLANNING, WorkflowLabel.READY},
    WorkflowLabel.FEATURE: {WorkflowLabel.FEATURE, WorkflowLabel.EPIC_TRACKING},
    WorkflowLabel.READY: {WorkflowLabel.READY, WorkflowLabel.NEEDS_REVIEW, WorkflowLabel.DONE},
    WorkflowLabel.NEEDS_REVIEW: {
        WorkflowLabel.NEEDS_REVIEW,
        WorkflowLabel.APPROVED,
        WorkflowLabel.CHANGES_REQUESTED,
    },
    WorkflowLabel.EPIC_TRACKING: {WorkflowLabel.EPIC_TRACKING, WorkflowLabel.DONE},
    WorkflowLabel.APPROVED: {WorkflowLabel.APPROVED, WorkflowLabel.DONE},
    WorkflowLabel.CHANGES_REQUESTED: {WorkflowLabel.CHANGES_REQUESTED, WorkflowLabel.READY},
    WorkflowLabel.DONE: {WorkflowLabel.DONE},
}
ROUTE_BY_LABEL = {
    WorkflowLabel.NEEDS_PLANNING: DispatchRoute.ARCHITECT_MICRO,
    WorkflowLabel.FEATURE: DispatchRoute.ARCHITECT_MACRO,
    WorkflowLabel.READY: DispatchRoute.CODER,
    WorkflowLabel.NEEDS_REVIEW: DispatchRoute.REVIEWER,
}
TIER_BY_ROUTE = {
    DispatchRoute.ARCHITECT_MICRO: WorkerTier.T3,
    DispatchRoute.ARCHITECT_MACRO: WorkerTier.T3,
    DispatchRoute.CODER: WorkerTier.T2,
    DispatchRoute.REVIEWER: WorkerTier.T3,
}


class DispatchLabelAction(BaseModel):
    from_label: WorkflowLabel
    to_label: WorkflowLabel


class DispatchDecision(BaseModel):
    target_profile: TargetExecutionProfile
    label_action: DispatchLabelAction
    reasoning: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    event_id: str
    status: str
    reason: str
    decision: DispatchDecision | None = None


class DispatchClassification(BaseModel):
    route: DispatchRoute
    model_name: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)


class GitHubDispatcherClient(Protocol):
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


class DispatcherClassifier(Protocol):
    def classify(
        self,
        *,
        event: LoggedEvent,
        issue: GitHubIssue,
        current_label: WorkflowLabel,
        expected_route: DispatchRoute,
    ) -> DispatchClassification: ...


class OllamaDispatcherClassifier:
    def __init__(self, *, llm_client: LLMClient | None = None) -> None:
        self._llm_client = llm_client or LLMClient()

    def classify(
        self,
        *,
        event: LoggedEvent,
        issue: GitHubIssue,
        current_label: WorkflowLabel,
        expected_route: DispatchRoute,
    ) -> DispatchClassification:
        return self._llm_client.complete_structured(
            tier=WorkerTier.T1,
            response_model=DispatchClassification,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are the Tier 1 Loop Troop dispatcher. "
                        "Classify the issue into the expected worker route and choose the best specific Ollama model."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Event payload: {event.payload}\n"
                        f"Issue number: {issue.number}\n"
                        f"Issue title: {issue.title or ''}\n"
                        f"Current loop label: {current_label.value}\n"
                        f"Expected route: {expected_route.value}\n"
                        "Return the expected route unless the event is clearly inconsistent with the issue state."
                    ),
                },
            ],
            temperature=0,
        )


class Dispatcher:
    def __init__(
        self,
        *,
        shadow_log: ShadowLog,
        github_client: GitHubDispatcherClient,
        classifier: DispatcherClassifier,
        sleep: SleepFn = asyncio.sleep,
        inference_retries: int = 3,
        backoff_base_seconds: float = 1.0,
    ) -> None:
        self._shadow_log = shadow_log
        self._github_client = github_client
        self._classifier = classifier
        self._sleep = sleep
        self._inference_retries = inference_retries
        self._backoff_base_seconds = backoff_base_seconds

    async def dispatch_pending_events(self) -> list[DispatchOutcome]:
        outcomes: list[DispatchOutcome] = []
        for event in self._shadow_log.get_pending_events():
            outcomes.append(await self._dispatch_event(event))
        return outcomes

    async def _dispatch_event(self, event: LoggedEvent) -> DispatchOutcome:
        owner, repo = self._split_repo(event.repo)
        issue_number = self._issue_number_from_event(event)
        issue = await self._github_client.get_issue(owner, repo, issue_number)
        current_label = self._loop_label_from_issue(issue)
        if current_label is None:
            return DispatchOutcome(
                event_id=event.event_id,
                status="skipped",
                reason="Issue does not have a dispatchable loop label.",
            )

        expected_route = ROUTE_BY_LABEL.get(current_label)
        if expected_route is None:
            return DispatchOutcome(
                event_id=event.event_id,
                status="skipped",
                reason=f"Loop label {current_label.value} is not dispatchable.",
            )

        blocked_dependencies = await self._blocked_dependencies(owner, repo, issue.number)
        if blocked_dependencies:
            return DispatchOutcome(
                event_id=event.event_id,
                status="blocked",
                reason=f"Blocked by unresolved dependencies: {', '.join(f'#{item}' for item in blocked_dependencies)}",
            )

        classification = await self._classify_with_retries(
            event=event,
            issue=issue,
            current_label=current_label,
            expected_route=expected_route,
        )
        if classification is None:
            self._shadow_log.mark_failed(event.event_id)
            return DispatchOutcome(
                event_id=event.event_id,
                status="failed",
                reason=f"Ollama classification failed after {self._inference_retries} attempts.",
            )

        if classification.route != expected_route:
            raise ValueError(
                f"Classifier returned route {classification.route.value}, expected {expected_route.value}."
            )

        target_label = current_label
        self.validate_label_transition(current_label, target_label)
        decision = DispatchDecision(
            target_profile=TargetExecutionProfile(
                tier=TIER_BY_ROUTE[classification.route],
                model_name=classification.model_name,
                reasoning=classification.reasoning,
            ),
            label_action=DispatchLabelAction(from_label=current_label, to_label=target_label),
            reasoning=classification.reasoning,
        )
        await self._github_client.replace_issue_labels(
            owner,
            repo,
            issue.number,
            labels=self._updated_labels(issue, target_label),
        )
        self._shadow_log.mark_dispatched(event.event_id)
        return DispatchOutcome(
            event_id=event.event_id,
            status="dispatched",
            reason=f"Dispatched to {decision.target_profile.tier.value}.",
            decision=decision,
        )

    async def _classify_with_retries(
        self,
        *,
        event: LoggedEvent,
        issue: GitHubIssue,
        current_label: WorkflowLabel,
        expected_route: DispatchRoute,
    ) -> DispatchClassification | None:
        for attempt in range(self._inference_retries):
            try:
                result = self._classifier.classify(
                    event=event,
                    issue=issue,
                    current_label=current_label,
                    expected_route=expected_route,
                )
                if inspect.isawaitable(result):
                    result = await result
                return result
            except (httpx.HTTPError, TimeoutError):
                if attempt >= self._inference_retries - 1:
                    return None
                await self._sleep(self._backoff_base_seconds * (2**attempt))
        return None

    async def _blocked_dependencies(self, owner: str, repo: str, issue_number: int) -> list[int]:
        comments = await self._github_client.list_issue_comments(owner, repo, issue_number)
        dependencies = self.dependencies_for_issue(issue_number, comments)
        blocked: list[int] = []
        for dependency_issue in dependencies:
            if dependency_issue == issue_number:
                raise ValueError(f"Issue #{issue_number} cannot depend on itself.")
            dependency = await self._github_client.get_issue(owner, repo, dependency_issue)
            if dependency.state.lower() != "closed":
                blocked.append(dependency_issue)
        return blocked

    @classmethod
    def validate_label_transition(
        cls,
        from_label: WorkflowLabel | None,
        to_label: WorkflowLabel,
    ) -> None:
        allowed = VALID_LABEL_TRANSITIONS.get(from_label, set())
        if to_label not in allowed:
            source = "unlabeled" if from_label is None else from_label.value
            raise ValueError(f"Invalid label transition: {source} -> {to_label.value}")

    @classmethod
    def dependencies_for_issue(
        cls,
        issue_number: int,
        comments: list[GitHubIssueComment],
    ) -> list[int]:
        exact_matches: list[int] = []
        fallback_matches: list[int] = []
        for comment in comments:
            body = comment.body or ""
            for line in body.splitlines() or [body]:
                match = DEPENDENCY_PATTERN.search(line)
                if not match:
                    continue
                dependencies = [int(item.lstrip("#")) for item in ISSUE_NUMBER_PATTERN.findall(match.group("deps"))]
                if f"#{issue_number}" in line:
                    exact_matches.extend(dependencies)
                else:
                    fallback_matches.extend(dependencies)
        seen: set[int] = set()
        ordered = exact_matches or fallback_matches
        result: list[int] = []
        for dependency in ordered:
            if dependency not in seen:
                seen.add(dependency)
                result.append(dependency)
        return result

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        owner, name = repo.split("/", 1)
        return owner, name

    @staticmethod
    def _issue_number_from_event(event: LoggedEvent) -> int:
        issue = event.payload.get("issue")
        if isinstance(issue, dict) and issue.get("number") is not None:
            return int(issue["number"])
        if event.payload.get("number") is not None:
            return int(event.payload["number"])
        raise ValueError(f"Unable to determine issue number from event {event.event_id}.")

    @staticmethod
    def _loop_label_from_issue(issue: GitHubIssue) -> WorkflowLabel | None:
        for label in issue.labels:
            if label.name in WorkflowLabel._value2member_map_:
                return WorkflowLabel(label.name)
        return None

    @staticmethod
    def _updated_labels(issue: GitHubIssue, target_label: WorkflowLabel) -> list[str]:
        labels = [label.name for label in issue.labels if label.name not in WorkflowLabel._value2member_map_]
        labels.append(target_label.value)
        return labels
