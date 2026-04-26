import httpx
import pytest
from pydantic import BaseModel

from loop_troop.core.github_client import GitHubIssue, GitHubIssueComment, GitHubLabel
from loop_troop.core.llm_client import LLMClient
from loop_troop.dispatcher import (
    DispatchClassification,
    DispatchRoute,
    Dispatcher,
    OllamaDispatcherClassifier,
    WorkflowLabel,
)
from loop_troop.execution import TargetExecutionProfile, WorkerTier
from loop_troop.shadow_log import ShadowLog


class FakeGitHubClient:
    def __init__(
        self,
        *,
        issues: dict[int, GitHubIssue],
        comments: dict[int, list[GitHubIssueComment]] | None = None,
    ) -> None:
        self.issues = issues
        self.comments = comments or {}
        self.replaced_labels: list[tuple[str, str, int, list[str]]] = []

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> GitHubIssue:
        return self.issues[issue_number]

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]:
        return self.comments.get(issue_number, [])

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]:
        self.replaced_labels.append((owner, repo, issue_number, labels))
        return labels


class FakeStructuredLLMClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.mark.asyncio
async def test_dispatcher_routes_ready_issue_and_selects_specific_model(tmp_path) -> None:
    shadow_log = ShadowLog(tmp_path / "shadow.db")
    try:
        shadow_log.log_event({"id": 1, "event": "labeled", "issue": {"number": 12}}, repo="octo/repo")
        github_client = FakeGitHubClient(
            issues={
                12: GitHubIssue(
                    number=12,
                    state="open",
                    title="Implement dispatcher",
                    labels=[GitHubLabel(name="bug"), GitHubLabel(name=WorkflowLabel.READY.value)],
                )
            }
        )
        classifier = OllamaDispatcherClassifier(
            llm_client=FakeStructuredLLMClient(
                [
                    DispatchClassification(
                        route=DispatchRoute.CODER,
                        model_name="qwen2.5-coder:32b",
                        reasoning="Coder task with multi-file implementation work.",
                    )
                ]
            )
        )
        dispatcher = Dispatcher(
            shadow_log=shadow_log,
            github_client=github_client,
            classifier=classifier,
        )

        outcomes = await dispatcher.dispatch_pending_events()

        assert len(outcomes) == 1
        assert outcomes[0].status == "dispatched"
        assert outcomes[0].decision is not None
        assert outcomes[0].decision.target_profile == TargetExecutionProfile(
            tier=WorkerTier.T2,
            model_name="qwen2.5-coder:32b",
            reasoning="Coder task with multi-file implementation work.",
        )
        assert github_client.replaced_labels == [("octo", "repo", 12, ["bug", "loop: ready"])]
        assert shadow_log.get_pending_events() == []
    finally:
        shadow_log.close()


def test_dispatcher_rejects_invalid_label_transition() -> None:
    with pytest.raises(ValueError, match="Invalid label transition"):
        Dispatcher.validate_label_transition(WorkflowLabel.NEEDS_PLANNING, WorkflowLabel.DONE)


@pytest.mark.asyncio
async def test_dispatcher_marks_failed_after_ollama_timeouts(tmp_path) -> None:
    shadow_log = ShadowLog(tmp_path / "shadow.db")
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    try:
        shadow_log.log_event({"id": 2, "event": "labeled", "issue": {"number": 99}}, repo="octo/repo")
        github_client = FakeGitHubClient(
            issues={
                99: GitHubIssue(
                    number=99,
                    state="open",
                    title="Timeout case",
                    labels=[GitHubLabel(name=WorkflowLabel.READY.value)],
                )
            }
        )
        classifier = OllamaDispatcherClassifier(
            llm_client=FakeStructuredLLMClient(
                [
                    httpx.TimeoutException("timed out"),
                    httpx.TimeoutException("timed out"),
                    httpx.TimeoutException("timed out"),
                ]
            )
        )
        dispatcher = Dispatcher(
            shadow_log=shadow_log,
            github_client=github_client,
            classifier=classifier,
            sleep=fake_sleep,
            backoff_base_seconds=0.25,
        )

        outcomes = await dispatcher.dispatch_pending_events()

        assert len(outcomes) == 1
        assert outcomes[0].status == "failed"
        assert outcomes[0].reason == "Ollama classification failed after 3 attempts."
        assert slept == [0.25, 0.5]
        assert shadow_log.get_pending_events() == []
        status = shadow_log._connection.execute(
            "SELECT status FROM event_state WHERE event_id = ?",
            ("2",),
        ).fetchone()[0]
        assert status == "failed"
    finally:
        shadow_log.close()


@pytest.mark.asyncio
async def test_dispatcher_blocks_when_dependencies_are_not_resolved(tmp_path) -> None:
    shadow_log = ShadowLog(tmp_path / "shadow.db")
    try:
        shadow_log.log_event({"id": 3, "event": "labeled", "issue": {"number": 12}}, repo="octo/repo")
        github_client = FakeGitHubClient(
            issues={
                10: GitHubIssue(number=10, state="closed", labels=[]),
                11: GitHubIssue(number=11, state="open", labels=[]),
                12: GitHubIssue(
                    number=12,
                    state="open",
                    title="Blocked issue",
                    labels=[GitHubLabel(name=WorkflowLabel.READY.value)],
                ),
            },
            comments={
                12: [
                    GitHubIssueComment(
                        id=1,
                        body="- [ ] #12: Blocked issue (Depends on: #10, #11)",
                    )
                ]
            },
        )
        classifier = OllamaDispatcherClassifier(
            llm_client=FakeStructuredLLMClient(
                [
                    DispatchClassification(
                        route=DispatchRoute.CODER,
                        model_name="qwen2.5-coder:32b",
                        reasoning="Would be coder work if unblocked.",
                    )
                ]
            )
        )
        dispatcher = Dispatcher(
            shadow_log=shadow_log,
            github_client=github_client,
            classifier=classifier,
        )

        outcomes = await dispatcher.dispatch_pending_events()

        assert len(outcomes) == 1
        assert outcomes[0].status == "blocked"
        assert "#11" in outcomes[0].reason
        assert github_client.replaced_labels == []
        assert [event.event_id for event in shadow_log.get_pending_events()] == ["3"]
    finally:
        shadow_log.close()


def test_target_execution_profile_model_override_flows_to_llm_client() -> None:
    captured: dict[str, object] = {}

    class DummyResponse(BaseModel):
        ok: bool

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return DummyResponse(ok=True)

    class FakeChat:
        completions = FakeCompletions()

    class FakeInstructorClient:
        chat = FakeChat()

    llm_client = LLMClient(
        ollama_host="http://ollama.test",
        openai_factory=lambda **_: object(),
        instructor_factory=lambda *_args, **_kwargs: FakeInstructorClient(),
    )
    profile = TargetExecutionProfile(
        tier=WorkerTier.T2,
        model_name="qwen2.5-coder:32b",
        reasoning="Use the larger coder model for implementation-heavy work.",
    )

    response = llm_client.complete_structured(
        tier=profile.tier,
        model_override=profile.model_name,
        response_model=DummyResponse,
        messages=[{"role": "user", "content": "hello"}],
    )

    assert response == DummyResponse(ok=True)
    assert captured["model"] == "qwen2.5-coder:32b"
