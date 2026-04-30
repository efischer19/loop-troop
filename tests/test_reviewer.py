import pytest

from loop_troop.core.github_client import (
    GitHubCheckRun,
    GitHubIssue,
    GitHubIssueComment,
    GitHubLabel,
    GitHubPullRequest,
    GitHubPullRequestFile,
)
from loop_troop.core.schemas import ReviewComment, ReviewVerdict, ReviewVerdictType
from loop_troop.dispatcher import WorkflowLabel
from loop_troop.reviewer import ReviewerWorker


class FakeGitHubClient:
    def __init__(
        self,
        pull_request: GitHubPullRequest,
        *,
        diff: str = "",
        files: list[GitHubPullRequestFile] | None = None,
        check_runs: list[GitHubCheckRun] | None = None,
        linked_issue: GitHubIssue | None = None,
        linked_issue_comments: list[GitHubIssueComment] | None = None,
    ) -> None:
        self.pull_request = pull_request
        self.diff = diff
        self.files = files or []
        self.check_runs = check_runs or []
        self.linked_issue = linked_issue
        self.linked_issue_comments = linked_issue_comments or []
        self.created_reviews: list[dict[str, object]] = []
        self.replaced_labels: list[list[str]] = []
        self.calls: list[str] = []

    async def get_pull_request(self, owner: str, repo: str, pull_number: int) -> GitHubPullRequest:
        assert (owner, repo, pull_number) == ("octo", "repo", self.pull_request.number)
        self.calls.append("get_pull_request")
        return self.pull_request

    async def get_pull_request_diff(self, owner: str, repo: str, pull_number: int) -> str:
        self.calls.append("get_pull_request_diff")
        return self.diff

    async def list_pull_request_files(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubPullRequestFile]:
        assert per_page == 100
        self.calls.append("list_pull_request_files")
        return self.files

    async def get_check_runs(self, owner: str, repo: str, ref: str) -> list[GitHubCheckRun]:
        assert ref == "abc123"
        self.calls.append("get_check_runs")
        return self.check_runs

    async def get_issue(self, owner: str, repo: str, issue_number: int) -> GitHubIssue:
        assert self.linked_issue is not None
        assert issue_number == self.linked_issue.number
        self.calls.append("get_issue")
        return self.linked_issue

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[GitHubIssueComment]:
        assert per_page == 100
        assert self.linked_issue is not None
        assert issue_number == self.linked_issue.number
        self.calls.append("list_issue_comments")
        return self.linked_issue_comments

    async def create_pull_request_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        *,
        event: str,
        body: str,
        comments: list[dict[str, object]] | None = None,
        commit_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append("create_pull_request_review")
        payload = {
            "pull_number": pull_number,
            "event": event,
            "body": body,
            "comments": comments or [],
            "commit_id": commit_id,
        }
        self.created_reviews.append(payload)
        return payload

    async def replace_issue_labels(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        *,
        labels: list[str],
    ) -> list[str]:
        self.calls.append("replace_issue_labels")
        self.replaced_labels.append(labels)
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
        self.calls: list[dict[str, object]] = []

    def hydrate(self, *, repo_path, issue_context: str, adr_context: str, focus_files=None, **_kwargs) -> str:
        self.calls.append(
            {
                "repo_path": str(repo_path),
                "issue_context": issue_context,
                "adr_context": adr_context,
                "focus_files": list(focus_files or []),
            }
        )
        return self.hydrated_context


def _pull_request(*, title: str = "feat: review #42", body: str = "Closes #42") -> GitHubPullRequest:
    return GitHubPullRequest(
        id=12,
        number=12,
        state="open",
        title=title,
        body=body,
        labels=[GitHubLabel(name=WorkflowLabel.NEEDS_REVIEW.value), GitHubLabel(name="backend")],
        head={"sha": "abc123", "ref": "feature/review"},
    )


def _linked_issue() -> GitHubIssue:
    return GitHubIssue(
        number=42,
        state="open",
        title="Original issue",
        body=(
            "## Acceptance Criteria\n"
            "- [ ] Add reviewer worker\n"
            "- [x] Preserve label transitions\n"
        ),
        labels=[GitHubLabel(name=WorkflowLabel.READY.value)],
    )


@pytest.mark.asyncio
async def test_reviewer_worker_requests_changes_when_ci_is_not_green() -> None:
    github_client = FakeGitHubClient(
        _pull_request(),
        check_runs=[GitHubCheckRun(id=1, name="pytest", status="completed", conclusion="failure")],
    )
    llm_client = FakeStructuredLLMClient([])
    worker = ReviewerWorker(
        github_client=github_client,
        llm_client=llm_client,
        context_hydrator=FakeContextHydrator("unused"),
        adr_loader=FakeADRLoader("unused"),
    )

    outcome = await worker.handle_pull_request(
        owner="octo",
        repo="repo",
        pull_number=12,
        repo_path="/tmp/target-repo",
    )

    assert outcome.ci_gate_blocked is True
    assert outcome.target_label is WorkflowLabel.CHANGES_REQUESTED
    assert outcome.review_event == "REQUEST_CHANGES"
    assert "Fix CI before requesting review" in github_client.created_reviews[0]["body"]
    assert github_client.replaced_labels == [["backend", WorkflowLabel.CHANGES_REQUESTED.value]]
    assert llm_client.calls == []
    assert github_client.calls == [
        "get_pull_request",
        "get_check_runs",
        "create_pull_request_review",
        "replace_issue_labels",
    ]


@pytest.mark.asyncio
async def test_reviewer_worker_approves_clean_pull_request() -> None:
    github_client = FakeGitHubClient(
        _pull_request(),
        diff="diff --git a/src/loop_troop/reviewer.py b/src/loop_troop/reviewer.py",
        files=[
            GitHubPullRequestFile(filename="src/loop_troop/reviewer.py"),
            GitHubPullRequestFile(filename="tests/test_reviewer.py"),
        ],
        check_runs=[GitHubCheckRun(id=1, name="pytest", status="completed", conclusion="success")],
        linked_issue=_linked_issue(),
        linked_issue_comments=[GitHubIssueComment(id=3, body="Please keep the review strict.", user={"login": "octocat"})],
    )
    hydrator = FakeContextHydrator("hydrated review context")
    adr_loader = FakeADRLoader("accepted ADRs")
    llm_client = FakeStructuredLLMClient(
        [
            ReviewVerdict(
                pr_number=12,
                verdict=ReviewVerdictType.APPROVE,
                comments=[ReviewComment(path="src/loop_troop/reviewer.py", line=18, body="Looks consistent.")],
            )
        ]
    )
    worker = ReviewerWorker(
        github_client=github_client,
        llm_client=llm_client,
        context_hydrator=hydrator,
        adr_loader=adr_loader,
    )

    outcome = await worker.handle_pull_request(
        owner="octo",
        repo="repo",
        pull_number=12,
        repo_path="/tmp/target-repo",
    )

    assert outcome.target_label is WorkflowLabel.APPROVED
    assert outcome.review_event == "APPROVE"
    assert outcome.linked_issue_number == 42
    assert hydrator.calls[0]["repo_path"] == "/tmp/target-repo"
    assert hydrator.calls[0]["adr_context"] == "accepted ADRs"
    assert hydrator.calls[0]["focus_files"] == [
        "src/loop_troop/reviewer.py",
        "tests/test_reviewer.py",
    ]
    issue_context = hydrator.calls[0]["issue_context"]
    assert "Pull Request Diff:" in issue_context
    assert "- [ ] Add reviewer worker" in issue_context
    assert "@octocat: Please keep the review strict." in issue_context
    assert github_client.created_reviews[0]["comments"] == [
        {
            "path": "src/loop_troop/reviewer.py",
            "body": "Looks consistent.",
            "line": 18,
            "side": "RIGHT",
        }
    ]
    assert github_client.created_reviews[0]["event"] == "APPROVE"
    assert github_client.replaced_labels == [["backend", WorkflowLabel.APPROVED.value]]


@pytest.mark.asyncio
async def test_reviewer_worker_requests_changes_for_adr_violations() -> None:
    github_client = FakeGitHubClient(
        _pull_request(),
        diff="diff --git a/src/loop_troop/core/github_client.py b/src/loop_troop/core/github_client.py",
        files=[GitHubPullRequestFile(filename="src/loop_troop/core/github_client.py")],
        check_runs=[GitHubCheckRun(id=1, name="pytest", status="completed", conclusion="success")],
        linked_issue=_linked_issue(),
    )
    llm_client = FakeStructuredLLMClient(
        [
            ReviewVerdict(
                pr_number=12,
                verdict=ReviewVerdictType.REQUEST_CHANGES,
                adr_violations=["Adds a new integration boundary without updating ADR coverage."],
            )
        ]
    )
    worker = ReviewerWorker(
        github_client=github_client,
        llm_client=llm_client,
        context_hydrator=FakeContextHydrator("hydrated"),
        adr_loader=FakeADRLoader("accepted ADRs"),
    )

    outcome = await worker.handle_pull_request(
        owner="octo",
        repo="repo",
        pull_number=12,
        repo_path="/tmp/target-repo",
    )

    assert outcome.target_label is WorkflowLabel.CHANGES_REQUESTED
    assert github_client.created_reviews[0]["event"] == "REQUEST_CHANGES"
    assert "Adds a new integration boundary" in github_client.created_reviews[0]["body"]
    assert github_client.replaced_labels == [["backend", WorkflowLabel.CHANGES_REQUESTED.value]]


@pytest.mark.asyncio
async def test_reviewer_worker_flags_tautological_tests_in_review_comments() -> None:
    github_client = FakeGitHubClient(
        _pull_request(),
        diff="diff --git a/tests/test_example.py b/tests/test_example.py",
        files=[GitHubPullRequestFile(filename="tests/test_example.py")],
        check_runs=[GitHubCheckRun(id=1, name="pytest", status="completed", conclusion="success")],
        linked_issue=_linked_issue(),
    )
    llm_client = FakeStructuredLLMClient(
        [
            ReviewVerdict(
                pr_number=12,
                verdict=ReviewVerdictType.REQUEST_CHANGES,
                comments=[
                    ReviewComment(
                        path="tests/test_example.py",
                        line=8,
                        body="This test asserts `True` and does not validate behavior.",
                    )
                ],
            )
        ]
    )
    worker = ReviewerWorker(
        github_client=github_client,
        llm_client=llm_client,
        context_hydrator=FakeContextHydrator("hydrated"),
        adr_loader=FakeADRLoader("accepted ADRs"),
    )

    await worker.handle_pull_request(
        owner="octo",
        repo="repo",
        pull_number=12,
        repo_path="/tmp/target-repo",
    )

    prompt = llm_client.calls[0]["messages"][0]["content"]
    assert "asserts True or 1 == 1" in prompt
    assert github_client.created_reviews[0]["comments"] == [
        {
            "path": "tests/test_example.py",
            "body": "This test asserts `True` and does not validate behavior.",
            "line": 8,
            "side": "RIGHT",
        }
    ]


@pytest.mark.asyncio
async def test_reviewer_worker_rejects_bake_off_pull_requests_for_human_review() -> None:
    github_client = FakeGitHubClient(
        _pull_request(title="[BAKE-OFF] feat: compare models"),
        check_runs=[GitHubCheckRun(id=1, name="pytest", status="completed", conclusion="success")],
    )
    worker = ReviewerWorker(
        github_client=github_client,
        llm_client=FakeStructuredLLMClient([]),
        context_hydrator=FakeContextHydrator("unused"),
        adr_loader=FakeADRLoader("unused"),
    )

    outcome = await worker.handle_pull_request(
        owner="octo",
        repo="repo",
        pull_number=12,
        repo_path="/tmp/target-repo",
    )

    assert outcome.target_label is WorkflowLabel.CHANGES_REQUESTED
    assert "must remain a draft" in github_client.created_reviews[0]["body"]
    assert github_client.calls == [
        "get_pull_request",
        "create_pull_request_review",
        "replace_issue_labels",
    ]
