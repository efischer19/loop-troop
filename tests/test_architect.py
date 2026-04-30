import pytest
from pydantic import ValidationError

from loop_troop.architect import ArchitectWorker
from loop_troop.core.github_client import GitHubIssue, GitHubIssueComment, GitHubLabel
from loop_troop.core.schemas import ArchitectPlan, ChecklistItem, FeaturePlan, SubIssue
from loop_troop.dispatcher import WorkflowLabel


class FakeGitHubClient:
    def __init__(self, issue: GitHubIssue, comments: list[GitHubIssueComment]) -> None:
        self.issue = issue
        self.comments = comments
        self.created_comments: list[tuple[int, str]] = []
        self.replaced_labels: list[list[str]] = []
        self.created_issues: list[tuple[str, str, list[str]]] = []
        self._next_issue_number = 200

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> GitHubIssue:
        assert (owner, repo, issue_number) == ("octo", "repo", self.issue.number)
        return self.issue

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]:
        assert per_page == 100
        return self.comments

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]:
        self.replaced_labels.append(labels)
        return labels

    async def create_issue_comment(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        body: str,
    ) -> GitHubIssueComment:
        self.created_comments.append((issue_number, body))
        return GitHubIssueComment(id=len(self.created_comments), body=body)

    async def create_issue(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> GitHubIssue:
        self._next_issue_number += 1
        self.created_issues.append((title, body, labels or []))
        return GitHubIssue(
            number=self._next_issue_number,
            state="open",
            title=title,
            body=body,
            labels=[GitHubLabel(name=label) for label in labels or []],
        )


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


class FakeADRLoader:
    def __init__(self, context: str) -> None:
        self.context = context
        self.repo_paths: list[str] = []

    def build_context(self, repo_path) -> str:
        self.repo_paths.append(str(repo_path))
        return self.context


class FakeContextHydrator:
    def __init__(self, hydrated_context: str) -> None:
        self.hydrated_context = hydrated_context
        self.calls: list[dict[str, str]] = []

    def hydrate(self, *, repo_path, issue_context: str, adr_context: str, **_kwargs) -> str:
        self.calls.append(
            {
                "repo_path": str(repo_path),
                "issue_context": issue_context,
                "adr_context": adr_context,
            }
        )
        return self.hydrated_context


def _create_rule_of_three_validation_error():
    with pytest.raises(ValidationError) as exc_info:
        ChecklistItem(
            description="bad item",
            files_touched=["a", "b", "c", "d"],
            requires_test=False,
        )
    return exc_info.value


@pytest.mark.asyncio
async def test_architect_worker_posts_valid_micro_plan_and_test_requirements() -> None:
    issue = GitHubIssue(
        number=42,
        state="open",
        title="Plan the worker",
        body="Implement the planning worker.",
        labels=[GitHubLabel(name=WorkflowLabel.NEEDS_PLANNING.value), GitHubLabel(name="backend")],
    )
    github_client = FakeGitHubClient(
        issue,
        [GitHubIssueComment(id=1, body="Please cover edge cases.", user={"login": "octocat"})],
    )
    hydrator = FakeContextHydrator("hydrated context")
    adr_loader = FakeADRLoader("accepted ADRs")
    llm_client = FakeStructuredLLMClient(
        [
            ArchitectPlan(
                issue_number=42,
                checklist_items=[
                    ChecklistItem(
                        description="Add the architect worker.",
                        files_touched=["src/loop_troop/architect.py"],
                        logical_steps=["Implement the orchestrator", "Export the class"],
                        requires_test=True,
                        test_instructions="Add unit coverage for micro-planning flow.",
                    ),
                    ChecklistItem(
                        description="Wire the new label transition.",
                        files_touched=["src/loop_troop/dispatcher.py"],
                        logical_steps=["Add the loop: needs-adr label"],
                        requires_test=False,
                    ),
                ],
                verification_strategy="Run focused architect tests and then full pytest.",
            )
        ]
    )
    worker = ArchitectWorker(
        github_client=github_client,
        llm_client=llm_client,
        context_hydrator=hydrator,
        adr_loader=adr_loader,
    )

    outcome = await worker.handle_issue(
        owner="octo",
        repo="repo",
        issue_number=42,
        repo_path="/tmp/target-repo",
    )

    assert outcome.mode == "micro"
    assert outcome.target_label is WorkflowLabel.READY
    assert hydrator.calls[0]["repo_path"] == "/tmp/target-repo"
    assert "Implement the planning worker." in hydrator.calls[0]["issue_context"]
    assert "Please cover edge cases." in hydrator.calls[0]["issue_context"]
    assert hydrator.calls[0]["adr_context"] == "accepted ADRs"
    assert "- [ ] Add the architect worker." in github_client.created_comments[0][1]
    assert "Tests required: Add unit coverage for micro-planning flow." in github_client.created_comments[0][1]
    assert "Tests required: No" in github_client.created_comments[0][1]
    assert github_client.replaced_labels == [["backend", WorkflowLabel.READY.value]]


@pytest.mark.asyncio
async def test_architect_worker_retries_micro_plan_with_validation_feedback() -> None:
    issue = GitHubIssue(
        number=7,
        state="open",
        title="Retry invalid plan",
        body="Break this down safely.",
        labels=[GitHubLabel(name=WorkflowLabel.NEEDS_PLANNING.value)],
    )
    llm_client = FakeStructuredLLMClient(
        [
            _create_rule_of_three_validation_error(),
            ArchitectPlan(
                issue_number=7,
                checklist_items=[
                    ChecklistItem(
                        description="Produce a compliant checklist item.",
                        files_touched=["src/loop_troop/architect.py"],
                        logical_steps=["Return a valid response"],
                        requires_test=True,
                        test_instructions="Validate the retry path.",
                    )
                ],
                verification_strategy="Run the architect tests.",
            ),
        ]
    )
    worker = ArchitectWorker(
        github_client=FakeGitHubClient(issue, []),
        llm_client=llm_client,
        context_hydrator=FakeContextHydrator("hydrated"),
        adr_loader=FakeADRLoader("adr"),
    )

    outcome = await worker.handle_issue(
        owner="octo",
        repo="repo",
        issue_number=7,
        repo_path="/tmp/target-repo",
    )

    assert outcome.target_label is WorkflowLabel.READY
    assert len(llm_client.calls) == 2
    retry_message = llm_client.calls[1]["messages"][-1]["content"]
    assert "failed schema validation" in retry_message
    assert "files_touched must contain at most 3 items" in retry_message


@pytest.mark.asyncio
async def test_architect_worker_routes_adr_required_issue_to_needs_adr() -> None:
    issue = GitHubIssue(
        number=99,
        state="open",
        title="Needs architecture",
        body="We may need a new persistence layer.",
        labels=[GitHubLabel(name=WorkflowLabel.NEEDS_PLANNING.value)],
    )
    github_client = FakeGitHubClient(issue, [])
    worker = ArchitectWorker(
        github_client=github_client,
        llm_client=FakeStructuredLLMClient(
            [
                ArchitectPlan(
                    issue_number=99,
                    checklist_items=[],
                    adr_references=["ADR-0005"],
                    requires_adr=True,
                    adr_instructions="Document the new persistence choice in ADR-0005 before implementation.",
                    verification_strategy="Re-run planning after the ADR lands.",
                )
            ]
        ),
        context_hydrator=FakeContextHydrator("hydrated"),
        adr_loader=FakeADRLoader("adr"),
    )

    outcome = await worker.handle_issue(
        owner="octo",
        repo="repo",
        issue_number=99,
        repo_path="/tmp/target-repo",
    )

    assert outcome.target_label is WorkflowLabel.NEEDS_ADR
    assert "ADR-0005" in github_client.created_comments[0][1]
    assert "requires architectural work before implementation" in github_client.created_comments[0][1]
    assert github_client.replaced_labels == [[WorkflowLabel.NEEDS_ADR.value]]


@pytest.mark.asyncio
async def test_architect_worker_creates_macro_plan_dag_and_recursive_feature_issue() -> None:
    issue = GitHubIssue(
        number=123,
        state="open",
        title="Big feature",
        body="Deliver the whole feature.",
        labels=[GitHubLabel(name=WorkflowLabel.FEATURE.value), GitHubLabel(name="product")],
    )
    github_client = FakeGitHubClient(issue, [GitHubIssueComment(id=1, body="Need a clean dependency chain.")])
    hydrator = FakeContextHydrator("unused")
    adr_loader = FakeADRLoader("unused")
    worker = ArchitectWorker(
        github_client=github_client,
        llm_client=FakeStructuredLLMClient(
            [
                FeaturePlan(
                    epic_issue_number=123,
                    sub_issues=[
                        SubIssue(title="Add shared schema", description="Define the canonical types."),
                        SubIssue(
                            title="Decompose the API slice",
                            description="Split the API slice into smaller feature issues.",
                            depends_on=[1],
                            is_feature=True,
                        ),
                        SubIssue(
                            title="Integration test",
                            description="Verify the end-to-end flow in CI.",
                            depends_on=[2],
                            is_integration_test=True,
                        ),
                    ],
                )
            ]
        ),
        context_hydrator=hydrator,
        adr_loader=adr_loader,
    )

    outcome = await worker.handle_issue(
        owner="octo",
        repo="repo",
        issue_number=123,
        repo_path="/tmp/unused-for-macro",
    )

    assert outcome.mode == "macro"
    assert outcome.target_label is WorkflowLabel.EPIC_TRACKING
    assert outcome.created_issue_numbers == (201, 202, 203)
    assert github_client.created_issues[0][2] == [WorkflowLabel.NEEDS_PLANNING.value]
    assert github_client.created_issues[1][2] == [WorkflowLabel.FEATURE.value]
    assert github_client.created_issues[2][2] == [WorkflowLabel.NEEDS_PLANNING.value]
    tracking_comment = github_client.created_comments[0][1]
    assert "- [ ] #201: Add shared schema (Depends on: none)" in tracking_comment
    assert "- [ ] #202: Decompose the API slice (Depends on: #201)" in tracking_comment
    assert "- [ ] #203: Integration test (Depends on: #202)" in tracking_comment
    assert github_client.replaced_labels == [["product", WorkflowLabel.EPIC_TRACKING.value]]
    assert hydrator.calls == []
    assert adr_loader.repo_paths == []
