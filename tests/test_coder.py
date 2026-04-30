import subprocess
from pathlib import Path

import pytest

from loop_troop.coder import CoderWorker, InnerLoopResult
from loop_troop.core.github_client import GitHubIssue, GitHubIssueComment, GitHubLabel, GitHubPullRequest
from loop_troop.core.schemas import CodePatch, FileChange, TargetExecutionProfile
from loop_troop.core.workspace_manager import WorkspaceManager
from loop_troop.dispatcher import WorkflowLabel
from loop_troop.execution import WorkerTier


def _run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo_path, check=True, capture_output=True, text=True)


def _init_target_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text("# Fixture repo\n")
    (path / "Makefile").write_text("test:\n\t@echo test\n")
    (path / "Dockerfile").write_text("FROM scratch\n")
    (path / "docs" / "architecture").mkdir(parents=True)
    (path / "docs" / "architecture" / "ADR-0001.md").write_text("# ADR\n")
    (path / "src").mkdir()
    (path / "src" / "app.py").write_text("print('before')\n")

    _run_git(path, "init")
    _run_git(path, "config", "user.name", "LoopTroopTests")
    _run_git(path, "config", "user.email", "loop-troop@example.com")
    _run_git(path, "config", "receive.denyCurrentBranch", "updateInstead")
    _run_git(path, "add", ".")
    _run_git(path, "commit", "-m", "Initial fixture")
    return path


class FakeGitHubClient:
    def __init__(self, issue: GitHubIssue, comments: list[GitHubIssueComment]) -> None:
        self.issue = issue
        self.comments = comments
        self.updated_comments: list[tuple[int, str]] = []
        self.replaced_labels: list[list[str]] = []
        self.created_pull_requests: list[dict[str, str]] = []

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

    async def update_issue_comment(
        self,
        owner: str,
        repo: str,
        comment_id: int,
        *,
        body: str,
    ) -> GitHubIssueComment:
        self.updated_comments.append((comment_id, body))
        return GitHubIssueComment(id=comment_id, body=body)

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

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        head: str,
        base: str,
        body: str | None = None,
    ) -> GitHubPullRequest:
        payload = {"title": title, "head": head, "base": base, "body": body or ""}
        self.created_pull_requests.append(payload)
        return GitHubPullRequest(number=88, id=88, state="open", title=title, body=body, head={"sha": "abc", "ref": head})


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


class FakeInnerLoop:
    def __init__(self, results: list[InnerLoopResult]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, object]] = []

    async def run(self, *, repo_path, checklist_item, code_patch) -> InnerLoopResult:
        self.calls.append(
            {
                "repo_path": str(repo_path),
                "description": checklist_item.description,
                "requires_test": checklist_item.requires_test,
                "test_command": code_patch.test_command,
            }
        )
        return self._results.pop(0)


class FakePRManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def open_pull_request(self, **kwargs) -> GitHubPullRequest:
        self.calls.append(kwargs)
        return GitHubPullRequest(number=88, id=88, state="open", title=kwargs["title"], body=kwargs["body"])


def _create_workspace(tmp_path: Path) -> tuple[Path, Path, WorkspaceManager]:
    remote_repo = _init_target_repo(tmp_path / "remote-repo")
    manager = WorkspaceManager(workspace_base=tmp_path / "workspaces")
    workspace_repo = manager.clone_or_update(str(remote_repo))
    return remote_repo, workspace_repo, manager


def _architect_comment() -> GitHubIssueComment:
    return GitHubIssueComment(
        id=11,
        body=(
            "## Architect Plan\n"
            "- [x] Finish the schema work\n"
            "  - Files: `src/schema.py`\n"
            "  - Tests required: No\n"
            "- [ ] Update the app entrypoint\n"
            "  - Files: `src/app.py`, `tests/test_app.py`\n"
            "  - Steps:\n"
            "    1. Update the runtime behavior\n"
            "  - Tests required: Add focused app coverage.\n"
            "- [ ] Follow-up item\n"
            "  - Files: `README.md`\n"
            "  - Tests required: No\n"
        ),
    )


def _ready_issue() -> GitHubIssue:
    return GitHubIssue(
        number=42,
        state="open",
        title="Implement the selected checklist item",
        body="Broader issue context for the coder worker.",
        labels=[GitHubLabel(name="backend"), GitHubLabel(name=WorkflowLabel.READY.value)],
    )


def _code_patch(*, content: str, commit_message: str = "feat: update app entrypoint") -> CodePatch:
    return CodePatch(
        issue_number=42,
        checklist_item_index=2,
        branch_name="loop/issue-42-item-2",
        files_changed=[FileChange(path="src/app.py", content=content)],
        test_command="python -m pytest tests/test_app.py",
        commit_message=commit_message,
    )


def test_coder_worker_parses_first_unchecked_architect_item() -> None:
    item = CoderWorker._first_unchecked_item([_architect_comment()])

    assert item.item_index == 2
    assert item.description == "Update the app entrypoint"
    assert item.files_touched == ("src/app.py", "tests/test_app.py")
    assert item.requires_test is True
    assert item.test_instructions == "Add focused app coverage."


def test_coder_worker_applies_code_patch(tmp_path: Path) -> None:
    _remote_repo, workspace_repo, manager = _create_workspace(tmp_path)
    worker = CoderWorker(
        github_client=FakeGitHubClient(_ready_issue(), [_architect_comment()]),
        llm_client=FakeStructuredLLMClient([]),
        workspace_manager=manager,
        inner_loop=FakeInnerLoop([]),
        pr_manager=FakePRManager(),
    )
    patch = _code_patch(content="print('after')\n")

    worker._apply_code_patch(repo_path=workspace_repo, code_patch=patch)

    assert (workspace_repo / "src" / "app.py").read_text() == "print('after')\n"


@pytest.mark.asyncio
async def test_coder_worker_creates_pr_and_checks_item_when_tests_pass(tmp_path: Path) -> None:
    remote_repo, workspace_repo, manager = _create_workspace(tmp_path)
    github_client = FakeGitHubClient(_ready_issue(), [_architect_comment()])
    hydrator = FakeContextHydrator("hydrated coder context")
    adr_loader = FakeADRLoader("accepted ADRs")
    llm_client = FakeStructuredLLMClient([_code_patch(content="print('pass')\n")])
    inner_loop = FakeInnerLoop([InnerLoopResult(success=True, mode="tdd")])
    pr_manager = FakePRManager()
    worker = CoderWorker(
        github_client=github_client,
        llm_client=llm_client,
        context_hydrator=hydrator,
        adr_loader=adr_loader,
        workspace_manager=manager,
        inner_loop=inner_loop,
        pr_manager=pr_manager,
    )
    profile = TargetExecutionProfile(
        tier=WorkerTier.T2,
        model_name="qwen2.5-coder:32b",
        reasoning="Use the stronger coder model.",
    )

    outcome = await worker.handle_issue(
        owner="octo",
        repo="repo",
        issue_number=42,
        repo_path=workspace_repo,
        target_execution_profile=profile,
    )

    assert outcome.target_label is WorkflowLabel.NEEDS_REVIEW
    assert outcome.pr_number == 88
    assert outcome.branch_name == "loop/issue-42-item-2"
    assert hydrator.calls[0]["repo_path"] == str(workspace_repo)
    assert hydrator.calls[0]["adr_context"] == "accepted ADRs"
    assert hydrator.calls[0]["focus_files"] == ["src/app.py", "tests/test_app.py"]
    assert "Broader issue context for the coder worker." in hydrator.calls[0]["issue_context"]
    assert "Update the app entrypoint" in hydrator.calls[0]["issue_context"]
    assert llm_client.calls[0]["model_override"] == "qwen2.5-coder:32b"
    assert inner_loop.calls[0]["requires_test"] is True
    assert (workspace_repo / "src" / "app.py").read_text() == "print('pass')\n"
    assert github_client.updated_comments[0][0] == 11
    assert "- [x] Update the app entrypoint" in github_client.updated_comments[0][1]
    assert "- [ ] Follow-up item" in github_client.updated_comments[0][1]
    assert pr_manager.calls[0]["labels"] == [WorkflowLabel.NEEDS_REVIEW.value]
    assert pr_manager.calls[0]["head"] == "loop/issue-42-item-2"
    assert "loop/issue-42-item-2" in _run_git(remote_repo, "branch", "--list").stdout


@pytest.mark.asyncio
async def test_coder_worker_retries_after_failed_inner_loop(tmp_path: Path) -> None:
    _remote_repo, workspace_repo, manager = _create_workspace(tmp_path)
    github_client = FakeGitHubClient(_ready_issue(), [_architect_comment()])
    llm_client = FakeStructuredLLMClient(
        [
            _code_patch(content="print('first attempt')\n", commit_message="feat: first attempt"),
            _code_patch(content="print('second attempt')\n", commit_message="feat: second attempt"),
        ]
    )
    inner_loop = FakeInnerLoop(
        [
            InnerLoopResult(success=False, mode="tdd", failure_summary="tests failed on the first attempt"),
            InnerLoopResult(success=True, mode="tdd"),
        ]
    )
    pr_manager = FakePRManager()
    worker = CoderWorker(
        github_client=github_client,
        llm_client=llm_client,
        context_hydrator=FakeContextHydrator("hydrated"),
        adr_loader=FakeADRLoader("adrs"),
        workspace_manager=manager,
        inner_loop=inner_loop,
        pr_manager=pr_manager,
        max_retries=2,
    )

    outcome = await worker.handle_issue(
        owner="octo",
        repo="repo",
        issue_number=42,
        repo_path=workspace_repo,
    )

    assert outcome.attempts == 2
    assert len(llm_client.calls) == 2
    assert "tests failed on the first attempt" in llm_client.calls[1]["messages"][-1]["content"]
    assert (workspace_repo / "src" / "app.py").read_text() == "print('second attempt')\n"
    assert "- [x] Update the app entrypoint" in github_client.updated_comments[0][1]
    assert pr_manager.calls


@pytest.mark.asyncio
async def test_coder_worker_marks_item_needs_help_after_max_retries(tmp_path: Path) -> None:
    remote_repo, workspace_repo, manager = _create_workspace(tmp_path)
    github_client = FakeGitHubClient(_ready_issue(), [_architect_comment()])
    llm_client = FakeStructuredLLMClient(
        [
            _code_patch(content="print('attempt one')\n", commit_message="feat: attempt one"),
            _code_patch(content="print('attempt two')\n", commit_message="feat: attempt two"),
        ]
    )
    worker = CoderWorker(
        github_client=github_client,
        llm_client=llm_client,
        context_hydrator=FakeContextHydrator("hydrated"),
        adr_loader=FakeADRLoader("adrs"),
        workspace_manager=manager,
        inner_loop=FakeInnerLoop(
            [
                InnerLoopResult(success=False, mode="tdd", failure_summary="first failure"),
                InnerLoopResult(success=False, mode="tdd", failure_summary="second failure"),
            ]
        ),
        pr_manager=FakePRManager(),
        max_retries=2,
    )

    outcome = await worker.handle_issue(
        owner="octo",
        repo="repo",
        issue_number=42,
        repo_path=workspace_repo,
    )

    assert outcome.target_label is WorkflowLabel.NEEDS_HELP
    assert outcome.pr_number is None
    assert "- [!] Update the app entrypoint" in github_client.updated_comments[0][1]
    assert github_client.replaced_labels == [["backend", WorkflowLabel.NEEDS_HELP.value]]
    assert _run_git(remote_repo, "branch", "--list", "loop/issue-42-item-2").stdout.strip() == ""
